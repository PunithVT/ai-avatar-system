import base64
import json
import logging
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import WebSocket

from app.services.animator import avatar_animator
from app.services.llm import llm_service
from app.services.storage import storage_service
from app.services.stt import stt_service
from app.services.tts import tts_service

logger = logging.getLogger(__name__)
TMPDIR = Path(tempfile.gettempdir())


class ConnectionManager:
    """Manage WebSocket connections and the real-time avatar pipeline."""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.session_data: Dict[str, dict] = {}

    # ── connection lifecycle ──────────────────────────────────────────────────

    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        self.session_data[session_id] = {
            "messages": [],
            "avatar_id": None,
            "avatar_image_key": None,
            "avatar_image_local": None,
            "voice_wav": None,
            "user_id": None,
            "connected_at": datetime.now(timezone.utc),
        }
        await self._load_session_data(session_id)
        logger.info(f"WebSocket connected: {session_id}")

    async def _load_session_data(self, session_id: str):
        try:
            from app.database import AsyncSessionLocal
            from app.models import Avatar
            from app.models import Session as SessionModel
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(SessionModel).where(SessionModel.id == session_id)
                )
                session = result.scalar_one_or_none()
                if not session:
                    return

                self.session_data[session_id]["avatar_id"] = session.avatar_id
                self.session_data[session_id]["user_id"] = session.user_id

                result = await db.execute(
                    select(Avatar).where(Avatar.id == session.avatar_id)
                )
                avatar = result.scalar_one_or_none()
                if avatar:
                    self.session_data[session_id]["avatar_image_key"] = avatar.s3_key
                    local = await self._resolve_local_image(avatar)
                    self.session_data[session_id]["avatar_image_local"] = local
                    if avatar.voice_id:
                        wav = self._get_voice_wav_path(avatar.voice_id)
                        if wav:
                            self.session_data[session_id]["voice_wav"] = wav
                            logger.info(f"Auto-loaded voice {avatar.voice_id} for session {session_id}")
                    logger.info(f"Loaded avatar {avatar.id} for session {session_id}")

        except Exception as e:
            logger.error(f"Failed to load session data for {session_id}: {e}")

    def _get_voice_wav_path(self, voice_id: str) -> Optional[str]:
        """Return the WAV filesystem path for a voice profile, or None if not found."""
        voice_index = Path("voice_profiles") / "index.json"
        if not voice_index.exists():
            return None
        try:
            for entry in json.loads(voice_index.read_text()):
                if entry["id"] == voice_id:
                    return entry.get("wav_path")
        except Exception as e:
            logger.warning(f"Could not read voice index: {e}")
        return None

    async def _resolve_local_image(self, avatar) -> str:
        """Return a local FS path to the avatar image, downloading from S3 if needed."""
        cache_path = TMPDIR / "avatars" / f"{avatar.id}.jpg"
        if cache_path.exists():
            return str(cache_path)

        # Local storage: use get_local_path directly
        try:
            local = storage_service.get_local_path(avatar.s3_key)
            if Path(local).exists():
                return local
        except (NotImplementedError, AttributeError):
            pass

        # S3 fallback: download and cache locally for the animator
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = await storage_service.download_file(avatar.s3_key)
        cache_path.write_bytes(data)
        return str(cache_path)

    async def disconnect(self, session_id: str):
        self.active_connections.pop(session_id, None)
        self.session_data.pop(session_id, None)
        logger.info(f"WebSocket disconnected: {session_id}")

    async def send_message(self, session_id: str, message: dict):
        ws = self.active_connections.get(session_id)
        if ws:
            try:
                await ws.send_json(message)
            except Exception as e:
                logger.error(f"Send failed [{session_id}]: {e}")
                await self.disconnect(session_id)

    # ── handlers ──────────────────────────────────────────────────────────────

    async def handle_audio_input(self, session_id: str, audio_data: str):
        try:
            await self.send_message(session_id, {
                "type": "status", "message": "Transcribing audio…", "stage": "transcription"
            })

            tmp_audio = TMPDIR / f"{session_id}_input.webm"
            tmp_audio.write_bytes(base64.b64decode(audio_data))

            text = await stt_service.transcribe(str(tmp_audio))
            tmp_audio.unlink(missing_ok=True)

            if not text:
                await self.send_message(session_id, {"type": "error", "message": "Could not transcribe audio"})
                return

            await self.send_message(session_id, {"type": "transcription", "text": text})
            await self.handle_text_input(session_id, text)

        except Exception as e:
            logger.error(f"Audio error [{session_id}]: {e}")
            await self.send_message(session_id, {"type": "error", "message": f"Audio processing failed: {e}"})

    async def handle_text_input(self, session_id: str, text: str):
        try:
            data = self.session_data.get(session_id, {})
            messages = data.get("messages", [])
            messages.append({"role": "user", "content": text})

            await self.send_message(session_id, {"type": "status", "message": "Thinking…", "stage": "llm"})

            response = await llm_service.generate_response(messages)
            messages.append({"role": "assistant", "content": response})
            data["messages"] = messages

            await self.send_message(session_id, {
                "type": "message", "role": "assistant", "content": response
            })
            await self._stream_avatar_video_chunks(session_id, response)

        except Exception as e:
            logger.error(f"Text error [{session_id}]: {e}")
            await self.send_message(session_id, {"type": "error", "message": f"Processing failed: {e}"})

    # ── video generation ──────────────────────────────────────────────────────

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        """Split text into sentences, keeping each chunk meaningful."""
        # Split on sentence-ending punctuation followed by whitespace
        raw = re.split(r'(?<=[.!?])\s+', text.strip())
        sentences: List[str] = []
        buf = ""
        for s in raw:
            s = s.strip()
            if not s:
                continue
            buf = (buf + " " + s).strip() if buf else s
            # Flush buffer when it's long enough to be worth a chunk
            if len(buf) >= 30 or s.endswith(('.', '!', '?')):
                sentences.append(buf)
                buf = ""
        if buf:
            sentences.append(buf)
        return sentences if sentences else [text.strip()]

    async def _stream_avatar_video_chunks(self, session_id: str, text: str):
        """
        Sentence-based streaming: process each sentence independently and
        send video_chunk messages as they complete so the frontend can play
        them back-to-back without waiting for the full response.
        """
        data = self.session_data.get(session_id, {})
        avatar_image = data.get("avatar_image_local")
        speaker_wav: Optional[str] = data.get("voice_wav")

        if not avatar_image:
            logger.warning(f"No avatar image for session {session_id}")
            return

        sentences = self._split_sentences(text)
        total = len(sentences)
        logger.info(f"Streaming {total} chunk(s) for session {session_id}")

        await self.send_message(session_id, {
            "type": "video_chunk_start",
            "total_chunks": total,
        })

        sent_any = False
        for i, sentence in enumerate(sentences):
            try:
                await self.send_message(session_id, {
                    "type": "status",
                    "message": f"Animating part {i + 1} of {total}…",
                    "stage": "animation",
                })

                job_id = uuid.uuid4().hex[:12]
                tmp_audio = TMPDIR / f"{session_id}_{job_id}_audio.wav"
                tmp_video = TMPDIR / f"{session_id}_{job_id}_video.mp4"

                await tts_service.synthesize(
                    text=sentence,
                    output_path=str(tmp_audio),
                    speaker_wav=speaker_wav,
                    language="en",
                )

                await avatar_animator.animate(
                    avatar_image_path=avatar_image,
                    audio_path=str(tmp_audio),
                    output_path=str(tmp_video),
                )

                ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                video_key = f"videos/{session_id}/{ts}_c{i}.mp4"
                video_url = await storage_service.upload_file(
                    tmp_video.read_bytes(), video_key, content_type="video/mp4"
                )

                tmp_audio.unlink(missing_ok=True)
                tmp_video.unlink(missing_ok=True)

                await self.send_message(session_id, {
                    "type": "video_chunk",
                    "chunk_index": i,
                    "total_chunks": total,
                    "video_url": video_url,
                    "text": sentence,
                })
                logger.info(f"Chunk {i + 1}/{total} ready [{session_id}]")
                sent_any = True

            except Exception as e:
                logger.error(f"Chunk {i} failed [{session_id}]: {e}")
                tmp_audio.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
                tmp_video.unlink(missing_ok=True)  # type: ignore[possibly-undefined]

        await self.send_message(session_id, {
            "type": "video_chunk_end",
            "sent_chunks": sum(1 for _ in sentences) if sent_any else 0,
        })

        if not sent_any:
            await self.send_message(session_id, {
                "type": "error", "message": "Avatar animation failed for all sentences."
            })

    # ── helpers ───────────────────────────────────────────────────────────────

    async def set_avatar(self, session_id: str, avatar_id: str):
        if session_id in self.session_data:
            self.session_data[session_id]["avatar_id"] = avatar_id

    async def set_voice(self, session_id: str, voice_wav_path: str):
        """Attach a cloned-voice WAV file to the session for TTS synthesis."""
        if session_id in self.session_data:
            self.session_data[session_id]["voice_wav"] = voice_wav_path
            logger.info(f"Voice set [{session_id}]: {voice_wav_path}")


websocket_manager = ConnectionManager()
