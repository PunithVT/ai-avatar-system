import base64
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

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
                    logger.info(f"Loaded avatar {avatar.id} for session {session_id}")

        except Exception as e:
            logger.error(f"Failed to load session data for {session_id}: {e}")

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
            await self._generate_avatar_video(session_id, response)

        except Exception as e:
            logger.error(f"Text error [{session_id}]: {e}")
            await self.send_message(session_id, {"type": "error", "message": f"Processing failed: {e}"})

    # ── video generation ──────────────────────────────────────────────────────

    async def _generate_avatar_video(self, session_id: str, text: str):
        try:
            data = self.session_data.get(session_id, {})
            avatar_image = data.get("avatar_image_local")

            if not avatar_image:
                logger.warning(f"No avatar image for session {session_id}")
                return

            # TTS — uses cloned voice WAV if one is set
            await self.send_message(session_id, {
                "type": "status", "message": "Synthesising speech…", "stage": "tts"
            })
            tmp_audio = TMPDIR / f"{session_id}_tts.wav"
            speaker_wav: Optional[str] = data.get("voice_wav")

            await tts_service.synthesize(
                text=text,
                output_path=str(tmp_audio),
                speaker_wav=speaker_wav,   # None → default voice, path → cloned voice
                language="en",
            )

            # Animation
            await self.send_message(session_id, {
                "type": "status", "message": "Animating avatar…", "stage": "animation"
            })
            tmp_video = TMPDIR / f"{session_id}_video.mp4"
            await avatar_animator.animate(
                avatar_image_path=avatar_image,
                audio_path=str(tmp_audio),
                output_path=str(tmp_video),
            )

            # Upload / serve
            ts = int(datetime.now(timezone.utc).timestamp())
            video_key = f"videos/{session_id}/{ts}.mp4"
            video_url = await storage_service.upload_file(
                tmp_video.read_bytes(), video_key, content_type="video/mp4"
            )

            tmp_audio.unlink(missing_ok=True)
            tmp_video.unlink(missing_ok=True)

            await self.send_message(session_id, {
                "type": "video", "video_url": video_url, "text": text
            })
            logger.info(f"Video ready [{session_id}]: {video_url}")

        except Exception as e:
            logger.error(f"Video generation error [{session_id}]: {e}")
            await self.send_message(session_id, {
                "type": "error", "message": f"Video generation failed: {e}"
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
