import asyncio
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

# Minimum sentence length (chars) to bother animating
_MIN_SENTENCE_LEN = 8


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
            "language": "en",
            "system_prompt": None,
            "user_id": None,
            "connected_at": datetime.now(timezone.utc),
        }
        await self._load_session_data(session_id)
        logger.info(f"WebSocket connected: {session_id}")

    async def _load_session_data(self, session_id: str):
        try:
            from app.database import AsyncSessionLocal
            from app.models import Session as SessionModel
            from sqlalchemy import select
            from sqlalchemy.orm import joinedload

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(SessionModel)
                    .options(joinedload(SessionModel.avatar))
                    .where(SessionModel.id == session_id)
                )
                session = result.scalar_one_or_none()
                if not session:
                    return

                self.session_data[session_id]["avatar_id"] = session.avatar_id
                self.session_data[session_id]["user_id"] = session.user_id

                avatar = session.avatar
                if avatar:
                    self.session_data[session_id]["avatar_image_key"] = avatar.s3_key
                    local = await self._resolve_local_image(avatar)
                    self.session_data[session_id]["avatar_image_local"] = local

                    # Load per-avatar system prompt from metadata
                    meta = avatar.avatar_metadata or {}
                    if isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except Exception:
                            meta = {}
                    sp = meta.get("system_prompt")
                    if sp:
                        self.session_data[session_id]["system_prompt"] = sp
                        logger.info(f"Loaded system prompt for avatar {avatar.id}")

                    if avatar.voice_id:
                        wav = await self._get_voice_wav_path(avatar.voice_id)
                        if wav:
                            self.session_data[session_id]["voice_wav"] = wav
                            logger.info(f"Auto-loaded voice {avatar.voice_id} for session {session_id}")
                    logger.info(f"Loaded avatar {avatar.id} for session {session_id}")

        except Exception as e:
            logger.error(f"Failed to load session data for {session_id}: {e}")

    async def _get_voice_wav_path(self, voice_id: str) -> Optional[str]:
        """Return the WAV filesystem path for a voice profile, or None if not found."""
        voice_index = Path("voice_profiles") / "index.json"
        if not voice_index.exists():
            return None
        try:
            text = await asyncio.to_thread(voice_index.read_text)
            for entry in json.loads(text):
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
        tmp_audio = TMPDIR / f"{session_id}_input.webm"
        try:
            await self.send_message(session_id, {
                "type": "status", "message": "Transcribing audio…", "stage": "transcription"
            })

            try:
                raw = base64.b64decode(audio_data)
            except Exception:
                await self.send_message(session_id, {"type": "error", "message": "Invalid audio data"})
                return

            tmp_audio.write_bytes(raw)
            text = await stt_service.transcribe(str(tmp_audio))

            if not text:
                await self.send_message(session_id, {"type": "error", "message": "Could not transcribe audio"})
                return

            await self.send_message(session_id, {"type": "transcription", "text": text})
            await self.handle_text_input(session_id, text)

        except Exception as e:
            logger.error(f"Audio error [{session_id}]: {e}")
            await self.send_message(session_id, {"type": "error", "message": f"Audio processing failed: {e}"})
        finally:
            tmp_audio.unlink(missing_ok=True)

    async def handle_text_input(self, session_id: str, text: str):
        """
        Streaming pipeline:
          1. Stream LLM tokens → send `token` events for live UI display
          2. Detect sentence boundaries during streaming → enqueue sentences
          3. Consumer coroutine picks up each sentence and runs TTS+animation
             in parallel with ongoing LLM generation (first chunk starts before
             the LLM finishes the full response)
        """
        try:
            data = self.session_data.get(session_id, {})
            messages = data.get("messages", [])
            messages.append({"role": "user", "content": text})

            # Context window trimming: keep first + last 60 messages to control cost
            if len(messages) > 61:
                messages = messages[:1] + messages[-60:]  # type: ignore[index]

            system_prompt = data.get("system_prompt")

            await self.send_message(session_id, {"type": "status", "message": "Thinking…", "stage": "llm"})

            # Bounded queue prevents the LLM producer from racing too far ahead
            sentence_queue: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=4)

            results = await asyncio.gather(
                self._llm_producer(session_id, messages, system_prompt, sentence_queue),
                self._animate_from_queue(session_id, sentence_queue),
                return_exceptions=True,
            )

            # Check for errors
            for r in results:
                if isinstance(r, Exception):
                    raise r

            response_text = results[0] if isinstance(results[0], str) else ""
            if response_text:
                messages.append({"role": "assistant", "content": response_text})
                data["messages"] = messages

        except Exception as e:
            logger.error(f"Text error [{session_id}]: {e}")
            await self.send_message(session_id, {"type": "error", "message": f"Processing failed: {e}"})

    # ── streaming pipeline ────────────────────────────────────────────────────

    _SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')

    async def _llm_producer(
        self,
        session_id: str,
        messages: List[dict],
        system_prompt: Optional[str],
        queue: "asyncio.Queue[Optional[str]]",
    ) -> str:
        """
        Stream LLM tokens, emit `token` events to frontend,
        detect sentence boundaries and push complete sentences into the queue.
        Returns the complete response text.
        """
        buf = ""
        full_text = ""

        try:
            async for token in llm_service.stream_response(messages, system_prompt):
                if session_id not in self.active_connections:
                    break  # client disconnected

                full_text += token
                buf += token

                # Send live token to frontend
                await self.send_message(session_id, {"type": "token", "token": token})

                # Split on sentence boundaries; keep the incomplete tail in buf
                parts = self._SENTENCE_RE.split(buf)
                if len(parts) > 1:
                    for sentence in parts[:-1]:
                        sentence = sentence.strip()
                        if len(sentence) >= _MIN_SENTENCE_LEN:
                            await queue.put(sentence)
                    buf = parts[-1]

            # Flush remaining buffer
            remainder = buf.strip()
            if len(remainder) >= _MIN_SENTENCE_LEN:
                await queue.put(remainder)

        except Exception as e:
            logger.error(f"LLM producer error [{session_id}]: {e}")
            raise
        finally:
            # Always signal end so the consumer doesn't hang
            await queue.put(None)

        # Send complete assembled message
        await self.send_message(session_id, {
            "type": "message", "role": "assistant", "content": full_text
        })
        return full_text

    async def _animate_from_queue(
        self,
        session_id: str,
        queue: "asyncio.Queue[Optional[str]]",
    ) -> None:
        """
        Consume sentences from the queue and run TTS + animation for each,
        streaming video_chunk events to the frontend as they complete.
        """
        data = self.session_data.get(session_id, {})
        avatar_image = data.get("avatar_image_local")
        speaker_wav: Optional[str] = data.get("voice_wav")
        language: str = data.get("language", "en")

        # If no avatar image, drain queue silently
        if not avatar_image:
            logger.warning(f"No avatar image for session {session_id}")
            while True:
                item = await queue.get()
                if item is None:
                    break
            return

        chunk_index: int = 0
        sent_any = False

        await self.send_message(session_id, {
            "type": "video_chunk_start",
            "total_chunks": -1,  # streaming mode — total unknown up front
        })

        while True:
            sentence = await queue.get()
            if sentence is None:
                break

            if session_id not in self.active_connections:
                break  # client disconnected mid-stream

            job_id = uuid.uuid4().hex[:12]  # type: ignore[index]
            tmp_audio = TMPDIR / f"{session_id}_{job_id}_audio.wav"
            tmp_video = TMPDIR / f"{session_id}_{job_id}_video.mp4"

            try:
                await self.send_message(session_id, {
                    "type": "status",
                    "message": "Animating…",
                    "stage": "animation",
                })

                await tts_service.synthesize(
                    text=sentence,
                    output_path=str(tmp_audio),
                    speaker_wav=speaker_wav,
                    language=language,
                )

                await avatar_animator.animate(
                    avatar_image_path=avatar_image,
                    audio_path=str(tmp_audio),
                    output_path=str(tmp_video),
                )

                ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                video_key = f"videos/{session_id}/{ts}_c{chunk_index}.mp4"
                video_url = await storage_service.upload_file(
                    tmp_video.read_bytes(), video_key, content_type="video/mp4"
                )

                await self.send_message(session_id, {
                    "type": "video_chunk",
                    "chunk_index": chunk_index,
                    "total_chunks": -1,
                    "video_url": video_url,
                    "text": sentence,
                })
                chunk_index = chunk_index + 1  # type: ignore[operator]
                sent_any = True
                logger.info(f"Chunk {chunk_index} ready [{session_id}]")

            except Exception as e:
                logger.error(f"Chunk {chunk_index} failed [{session_id}]: {e}")

            finally:
                tmp_audio.unlink(missing_ok=True)
                tmp_video.unlink(missing_ok=True)

        await self.send_message(session_id, {
            "type": "video_chunk_end",
            "sent_chunks": chunk_index,
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

    async def set_language(self, session_id: str, language: str):
        """Set TTS language for the session."""
        if session_id in self.session_data:
            self.session_data[session_id]["language"] = language
            logger.info(f"Language set [{session_id}]: {language}")


websocket_manager = ConnectionManager()
