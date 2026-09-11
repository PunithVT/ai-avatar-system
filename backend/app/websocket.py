import asyncio
import base64
import json
import logging
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import WebSocket

from app.config import settings
from app.services.animator import avatar_animator
from app.services.llm import llm_service
from app.services.storage import storage_service
from app.services.stt import stt_service
from app.services.tts import tts_service
from app.telemetry import span

logger = logging.getLogger(__name__)
TMPDIR = Path(tempfile.gettempdir())

# Owner-only file/dir modes — keep another user on a shared host from
# eavesdropping on raw audio inputs or in-flight video chunks.
_OWNER_ONLY_FILE = stat.S_IRUSR | stat.S_IWUSR  # 0o600
_OWNER_ONLY_DIR = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR  # 0o700


def _private_session_dir(session_id: str) -> Path:
    """
    Return (creating if needed) a per-session subdirectory of TMPDIR with
    mode 0o700. Anything written inside is invisible to other UNIX users —
    cheaper than chmod'ing each tmp file after creation.
    """
    d = TMPDIR / f"avatar-session-{session_id}"
    d.mkdir(mode=_OWNER_ONLY_DIR, exist_ok=True)
    # If the dir already existed with a looser mode, tighten it now.
    try:
        os.chmod(str(d), _OWNER_ONLY_DIR)
    except OSError:
        pass
    return d


def _write_private_bytes(path: Path, data: bytes) -> None:
    """Atomically write bytes to `path` with file mode 0o600 (owner-only)."""
    # os.open lets us set the mode at create time so there's no readable window
    # between create and chmod. O_CREAT|O_TRUNC|O_WRONLY is the standard "open
    # for writing, truncate, create if missing" combination.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_ONLY_FILE)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(str(path), _OWNER_ONLY_FILE)
    except OSError:
        pass


# ── chunking thresholds (first-frame latency vs prosody trade-off) ──────────
# The opening fragment ships at the first CLAUSE boundary (comma/semicolon/
# colon/dash) once it's long enough, so audio+video start as early as
# possible. Every chunk after that uses SENTENCE boundaries — fewer TTS
# calls and smoother prosody for the bulk of the reply.
_MIN_SENTENCE_LEN = 8
_MIN_FIRST_CHUNK_LEN = 10  # ship the opening clause fast (~2 words)
# Force-flush a run-on with no usable punctuation so we never stall waiting
# for a boundary that may never come.
_MAX_CHUNK_CHARS = 200

# Boundary regexes. Lookbehind keeps the punctuation attached to the chunk
# (better TTS prosody than trailing a bare clause).
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_RE = re.compile(r"(?<=[.!?,;:—])\s+")


def _drain_chunks(buf: str, sep_re: "re.Pattern[str]", min_len: int, max_len: int):
    """
    Pull speakable chunks out of an in-progress LLM buffer without ever
    dropping text. Returns (chunks_ready_to_speak, remaining_buffer).

    A chunk is emitted at a punctuation boundary only once the text up to
    that boundary is at least `min_len` chars — short leading fragments
    (e.g. "Hi,") stay in the buffer and merge forward instead of being
    spoken as their own tiny clip. A run-on longer than `max_len` with no
    usable boundary is force-flushed at the last space.
    """
    chunks: list[str] = []

    # Emit complete punctuation-bounded segments that meet the length bar.
    while True:
        emitted = False
        for m in sep_re.finditer(buf):
            head = buf[: m.end()].strip()
            if len(head) >= min_len:
                chunks.append(head)
                buf = buf[m.end() :]
                emitted = True
                break
        if not emitted:
            break

    # Backstop: no punctuation but the buffer is getting long — cut at a space.
    while len(buf) >= max_len:
        cut = buf.rfind(" ", 0, max_len)
        if cut <= 0:
            break
        chunks.append(buf[:cut].strip())
        buf = buf[cut:].lstrip()

    return chunks, buf


# Per-message input cap. Long inputs waste LLM tokens and create DoS surface.
MAX_TEXT_INPUT_LEN = 4000

# Conversation memory cap — keep the most recent N user/assistant pairs.
# System prompt is stored separately so it survives trimming.
MAX_CONTEXT_MESSAGES = 60

# Soft TTL for an idle (disconnected/abandoned) session in seconds.
STALE_SESSION_TTL_SECS = 60 * 60 * 2  # 2 hours


class ConnectionManager:
    """Manage WebSocket connections and the real-time avatar pipeline."""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.session_data: Dict[str, dict] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        # Serializes connect/disconnect/cleanup-snapshot so the stale-session
        # reaper can't race a fresh connection for the same session id.
        self._mutation_lock = asyncio.Lock()
        # Per-session handle to the currently-running turn task, used for
        # barge-in: when a fresh user input arrives we cancel the in-flight
        # task instead of queueing.
        self._active_turns: Dict[str, asyncio.Task] = {}
        # Per-session send lock. The turn task streams chunks while the WS
        # receive loop may also send (pong/error) — without this, two
        # coroutines could interleave mid-frame and corrupt the connection.
        self._send_locks: Dict[str, asyncio.Lock] = {}

    # ── connection lifecycle ──────────────────────────────────────────────────

    async def at_capacity(self) -> bool:
        """
        True when WS_MAX_CONNECTIONS live connections are already open.

        Each connection pins session state, a send lock, a private temp dir
        and potentially a GPU inference pipeline, so the cap is what keeps a
        connection flood from exhausting the box. Reconnecting to a session
        that is already open does not count against it — that replaces an
        existing entry rather than adding one.
        """
        return len(self.active_connections) >= settings.WS_MAX_CONNECTIONS

    async def connect(self, session_id: str, websocket: WebSocket, user_id: Optional[str] = None):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        self._send_locks[session_id] = asyncio.Lock()
        self.session_data[session_id] = {
            "messages": [],
            "avatar_id": None,
            "avatar_image_key": None,
            "avatar_image_local": None,
            "voice_wav": None,
            "language": "en",
            "system_prompt": None,
            "user_id": user_id,
            "connected_at": datetime.now(timezone.utc),
            "last_activity": datetime.now(timezone.utc),
        }
        await self._load_session_data(session_id)
        logger.info(f"WebSocket connected: {session_id} (user={user_id})")

    async def _load_session_data(self, session_id: str):
        try:
            from sqlalchemy import select
            from sqlalchemy.orm import joinedload

            from app.database import AsyncSessionLocal
            from app.models import Message
            from app.models import Session as SessionModel

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
                # Trust DB owner over caller-supplied claim
                if session.user_id:
                    self.session_data[session_id]["user_id"] = session.user_id

                # Rehydrate the LLM context window from persisted messages so
                # a reconnect (refresh, network blip, etc.) resumes the same
                # conversation instead of starting fresh. We pull the most
                # recent MAX_CONTEXT_MESSAGES rows to bound memory. Order by
                # (created_at, id) so ties (when several rows share a
                # sub-millisecond timestamp on bulk insert) are still stable
                # — message IDs are monotonic UUIDs assigned in insertion order
                # per session, so they make a reliable secondary key.
                hist_result = await db.execute(
                    select(Message.role, Message.content)
                    .where(Message.session_id == session_id)
                    .where(Message.role.in_(("user", "assistant")))
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(MAX_CONTEXT_MESSAGES)
                )
                # Reverse to chronological order for the LLM
                hist_rows = list(hist_result.all())[::-1]
                if hist_rows:
                    self.session_data[session_id]["messages"] = [
                        {"role": row.role, "content": row.content} for row in hist_rows
                    ]
                    logger.info(f"Rehydrated {len(hist_rows)} message(s) for session {session_id}")

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
                            logger.info(
                                f"Auto-loaded voice {avatar.voice_id} for session {session_id}"
                            )
                    logger.info(f"Loaded avatar {avatar.id} for session {session_id}")

        except Exception as e:
            logger.error(f"Failed to load session data for {session_id}: {e}")

    async def _get_voice_wav_path(self, voice_id: str) -> Optional[str]:
        """Return the WAV filesystem path for a voice profile, or None if not found."""
        voice_index = Path("voice_profiles") / "index.json"
        if not voice_index.exists():
            return None
        try:
            raw = await asyncio.to_thread(voice_index.read_text)
            for entry in json.loads(raw):
                if entry["id"] == voice_id:
                    return entry.get("wav_path")
        except Exception as e:
            logger.warning(f"Could not read voice index: {e}")
        return None

    async def _get_voice_entry(self, voice_id: str) -> Optional[dict]:
        """Return the full voice-index entry (including `user_id`) for ownership checks."""
        voice_index = Path("voice_profiles") / "index.json"
        if not voice_index.exists():
            return None
        try:
            raw = await asyncio.to_thread(voice_index.read_text)
            for entry in json.loads(raw):
                if entry["id"] == voice_id:
                    return entry
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
        # Cancel any in-flight LLM/TTS/animation task for this session so it
        # doesn't keep churning after the client is gone (wasted tokens + GPU).
        task = self._active_turns.pop(session_id, None)
        if task and not task.done():
            task.cancel()

        ws = self.active_connections.pop(session_id, None)
        self.session_data.pop(session_id, None)
        self._send_locks.pop(session_id, None)

        # Actually close the socket. Disconnect is also reached when the
        # stale-session reaper or a REST "end session" tears a session down —
        # without this, the client's connection stayed open with all its
        # server-side state already gone (a zombie that ignored every input).
        if ws is not None:
            close = getattr(ws, "close", None)
            if close is not None:
                try:
                    await close(code=1000)
                except Exception:
                    pass  # already closed by the peer
        # Best-effort wipe of the per-session temp dir. We use shutil.rmtree
        # via to_thread because rmtree on a large dir can briefly block.
        session_dir = TMPDIR / f"avatar-session-{session_id}"
        if session_dir.exists():
            try:
                import shutil

                await asyncio.to_thread(shutil.rmtree, str(session_dir), True)
            except Exception as e:
                logger.warning(f"Could not clean session tmp dir for {session_id}: {e}")
        logger.info(f"WebSocket disconnected: {session_id}")

    async def interrupt_active_turn(self, session_id: str) -> bool:
        """
        Cancel any in-flight turn for this session ("barge-in"). Returns
        True if a turn was actually interrupted. Used when fresh user audio
        arrives mid-response — modern voice-AI UX expects sub-100 ms cutoff
        so the user doesn't keep hearing the previous response while talking.
        """
        task = self._active_turns.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            # Tell the client to stop playing the queued video chunks too —
            # otherwise they'd keep arriving from the buffer.
            await self.send_message(
                session_id,
                {
                    "type": "interrupted",
                    "message": "Previous response interrupted",
                },
            )
            return True
        return False

    async def send_message(self, session_id: str, message: dict):
        ws = self.active_connections.get(session_id)
        if not ws:
            return
        lock = self._send_locks.get(session_id)
        try:
            if lock is not None:
                async with lock:
                    await ws.send_json(message)
            else:
                await ws.send_json(message)
        except Exception as e:
            logger.error(f"Send failed [{session_id}]: {e}")
            await self.disconnect(session_id)

    # ── DB persistence helpers ────────────────────────────────────────────────

    async def _persist_message(
        self,
        session_id: str,
        role: str,
        content: str,
        latency: Optional[float] = None,
    ) -> None:
        """Best-effort persist a message; failure must not break the chat pipeline."""
        try:
            from app.database import AsyncSessionLocal
            from app.models import Message

            async with AsyncSessionLocal() as db:
                db.add(
                    Message(
                        session_id=session_id,
                        role=role,
                        content=content,
                        content_type="text",
                        latency=latency,
                    )
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Could not persist {role} message for {session_id}: {e}")

    async def _ensure_conversation_title(self, session_id: str, first_user_text: str) -> None:
        """
        Lazily create a Conversation row for the session and seed its title from
        the first user turn. Idempotent — safe to call on every text input.

        Title heuristic: first 60 chars of the user's message, trimmed at a
        word boundary. Cheap, no extra LLM call.
        """
        try:
            from sqlalchemy import func, select

            from app.database import AsyncSessionLocal
            from app.models import Conversation, Message

            async with AsyncSessionLocal() as db:
                exists = await db.execute(
                    select(Conversation.id).where(Conversation.session_id == session_id).limit(1)
                )
                if exists.scalar_one_or_none():
                    return

                snippet = first_user_text.strip().replace("\n", " ")
                if len(snippet) > 60:
                    cutoff = snippet.rfind(" ", 0, 60)
                    snippet = snippet[: cutoff if cutoff > 30 else 60].rstrip(",.!?;:") + "…"

                count_res = await db.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.session_id == session_id)
                )
                msg_count = int(count_res.scalar() or 0)

                db.add(
                    Conversation(
                        session_id=session_id,
                        title=snippet or "New Conversation",
                        message_count=msg_count,
                    )
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"Could not auto-title conversation for {session_id}: {e}")

    # ── handlers ──────────────────────────────────────────────────────────────

    def _spawn_turn(self, session_id: str, coro) -> None:
        """
        Register `coro` as the session's active turn and schedule it WITHOUT
        awaiting. This is the heart of barge-in: the WebSocket receive loop
        must return to `receive_json()` immediately so it can observe the next
        client message (a new turn or an explicit stop) while this one streams.

        A done-callback clears the slot and logs unhandled errors. We never
        await the task here, so an exception inside it can't crash the WS loop.
        """
        task = asyncio.create_task(coro, name=f"turn-{session_id}")
        self._active_turns[session_id] = task

        def _done(t: asyncio.Task) -> None:
            if self._active_turns.get(session_id) is t:
                self._active_turns.pop(session_id, None)
            if t.cancelled():
                logger.info(f"Turn for {session_id} cancelled (barge-in or disconnect)")
                return
            exc = t.exception()
            if exc is not None:
                logger.error(f"Turn task for {session_id} failed: {exc!r}")

        task.add_done_callback(_done)

    async def handle_audio_input(self, session_id: str, audio_data: str):
        """
        Non-blocking dispatcher: interrupt any prior turn, then run STT +
        the full chat turn inside a tracked task so the WS loop stays free to
        receive the next message (enabling barge-in even mid-transcription).
        """
        await self.interrupt_active_turn(session_id)
        self._spawn_turn(session_id, self._handle_audio_inner(session_id, audio_data))

    async def _handle_audio_inner(self, session_id: str, audio_data: str) -> None:
        tmp_audio = _private_session_dir(session_id) / "input.webm"
        try:
            await self.send_message(
                session_id,
                {"type": "status", "message": "Transcribing audio…", "stage": "transcription"},
            )

            try:
                raw = base64.b64decode(audio_data, validate=False)
            except Exception:
                await self.send_message(
                    session_id, {"type": "error", "message": "Invalid audio data"}
                )
                return

            # 50 MB hard cap so a malicious client cannot OOM the server
            if len(raw) > 50 * 1024 * 1024:
                await self.send_message(
                    session_id, {"type": "error", "message": "Audio payload too large"}
                )
                return

            await asyncio.to_thread(_write_private_bytes, tmp_audio, raw)
            # Transcribe in the session's selected language — otherwise Whisper
            # assumes English and garbles non-English speech.
            language = self.session_data.get(session_id, {}).get("language", "en")
            text = await stt_service.transcribe(str(tmp_audio), language=language)

            if not text:
                await self.send_message(
                    session_id, {"type": "error", "message": "Could not transcribe audio"}
                )
                return

            await self.send_message(session_id, {"type": "transcription", "text": text})
            # Run the text turn directly (we're already inside the tracked task).
            await self._handle_text_input_inner(session_id, text)

        except asyncio.CancelledError:
            raise  # propagate barge-in cancellation cleanly
        except Exception as e:
            logger.error(f"Audio error [{session_id}]: {e}")
            await self.send_message(
                session_id, {"type": "error", "message": "Audio processing failed"}
            )
        finally:
            tmp_audio.unlink(missing_ok=True)

    async def handle_text_input(self, session_id: str, text: str):
        """
        Non-blocking dispatcher for a text turn. Validates inline (so the
        client gets immediate feedback on empty/oversized input), interrupts
        any in-flight turn, then spawns the streaming pipeline as a tracked
        task and returns immediately.

        Pipeline (inside `_handle_text_input_inner`):
          1. Stream LLM tokens → `token` events for live UI display
          2. Detect sentence boundaries → enqueue complete sentences
          3. Consumer runs TTS + animation per sentence, streaming
             `video_chunk` events as each completes (first chunk starts
             before the LLM finishes the full response)
        """
        text = (text or "").strip()
        if not text:
            await self.send_message(session_id, {"type": "error", "message": "Empty message"})
            return
        if len(text) > MAX_TEXT_INPUT_LEN:
            await self.send_message(
                session_id,
                {
                    "type": "error",
                    "message": f"Message too long ({len(text)} chars). Limit is {MAX_TEXT_INPUT_LEN}.",
                },
            )
            return

        await self.interrupt_active_turn(session_id)
        self._spawn_turn(session_id, self._handle_text_input_inner(session_id, text))

    async def _handle_text_input_inner(self, session_id: str, text: str):
        started_at = datetime.now(timezone.utc)

        try:
            data = self.session_data.get(session_id, {})
            data["last_activity"] = started_at
            messages: list[dict] = data.get("messages", [])
            messages.append({"role": "user", "content": text})

            # Cap the conversation window. The system prompt is passed
            # separately to the LLM so we don't need to keep it in `messages`.
            if len(messages) > MAX_CONTEXT_MESSAGES:
                messages = messages[-MAX_CONTEXT_MESSAGES:]

            system_prompt = data.get("system_prompt")

            # Persist the user turn before kicking off generation so it's
            # durable even if the model fails partway through.
            await self._persist_message(session_id, "user", text)
            # Auto-title the conversation from the first user turn (idempotent)
            await self._ensure_conversation_title(session_id, text)

            await self.send_message(
                session_id, {"type": "status", "message": "Thinking…", "stage": "llm"}
            )

            # Bounded queue prevents the LLM producer from racing too far ahead
            sentence_queue: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=4)

            # Parent span for the whole turn — child spans (llm.stream,
            # tts.synthesize, avatar.animate, storage.upload) nest under it
            # so a trace shows exactly where a slow turn spent its time.
            with span("chat.turn", **{"input_chars": len(text)}):
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
                latency = (datetime.now(timezone.utc) - started_at).total_seconds()
                await self._persist_message(session_id, "assistant", response_text, latency=latency)

        except Exception as e:
            logger.error(f"Text error [{session_id}]: {e}")
            await self.send_message(session_id, {"type": "error", "message": "Processing failed"})

    # ── streaming pipeline ────────────────────────────────────────────────────

    async def _llm_producer(
        self,
        session_id: str,
        messages: List[dict],
        system_prompt: Optional[str],
        queue: "asyncio.Queue[Optional[str]]",
    ) -> str:
        """
        Stream LLM tokens, emit `token` events to the frontend, and push
        speakable chunks into the queue as boundaries are reached.

        Latency strategy: the FIRST chunk is split on clause boundaries with
        a low length bar so TTS+animation can start within the opening few
        words; every chunk after that uses sentence boundaries for smoother
        prosody and fewer synthesis calls. Returns the complete response text.
        """
        buf = ""
        full_text = ""
        first_chunk_sent = False

        try:
            with span("llm.stream", **{"history_len": len(messages)}):
                async for token in llm_service.stream_response(messages, system_prompt):
                    if session_id not in self.active_connections:
                        break  # client disconnected

                    full_text += token
                    buf += token

                    # Send live token to frontend
                    await self.send_message(session_id, {"type": "token", "token": token})

                    sep = _SENTENCE_RE if first_chunk_sent else _CLAUSE_RE
                    min_len = _MIN_SENTENCE_LEN if first_chunk_sent else _MIN_FIRST_CHUNK_LEN
                    chunks, buf = _drain_chunks(buf, sep, min_len, _MAX_CHUNK_CHARS)
                    for chunk in chunks:
                        await queue.put(chunk)
                        first_chunk_sent = True

            # Flush any remaining tail — it's the end of the reply, so speak it
            # even if it's short.
            remainder = buf.strip()
            if remainder:
                await queue.put(remainder)

        except Exception as e:
            logger.error(f"LLM producer error [{session_id}]: {e}")
            raise
        finally:
            # Always signal end so the consumer doesn't hang
            await queue.put(None)

        # Send complete assembled message
        await self.send_message(
            session_id, {"type": "message", "role": "assistant", "content": full_text}
        )
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
        # Only warn about TTS fallback once per turn — repeated warnings on
        # every sentence would be noisy. We reset this in the enclosing turn.
        fallback_announced = False

        await self.send_message(
            session_id,
            {
                "type": "video_chunk_start",
                "total_chunks": -1,  # streaming mode — total unknown up front
            },
        )

        while True:
            sentence = await queue.get()
            if sentence is None:
                break

            if session_id not in self.active_connections:
                break  # client disconnected mid-stream

            job_id = uuid.uuid4().hex[:12]
            session_dir = _private_session_dir(session_id)
            tmp_audio = session_dir / f"{job_id}_audio.wav"
            tmp_video = session_dir / f"{job_id}_video.mp4"

            try:
                await self.send_message(
                    session_id,
                    {
                        "type": "status",
                        "message": "Animating…",
                        "stage": "animation",
                    },
                )

                with span(
                    "tts.synthesize",
                    **{"chars": len(sentence), "lang": language, "cloned": bool(speaker_wav)},
                ):
                    synth = await tts_service.synthesize(
                        text=sentence,
                        output_path=str(tmp_audio),
                        speaker_wav=speaker_wav,
                        language=language,
                    )

                # Notify the client exactly once if Chatterbox bailed and
                # we ended up serving the un-cloned gTTS voice instead.
                if synth.fallback and not fallback_announced:
                    fallback_announced = True
                    await self.send_message(
                        session_id,
                        {
                            "type": "tts_fallback",
                            "engine": synth.engine,
                            "voice_cloned": synth.voice_cloned,
                            "message": (
                                "Cloned voice unavailable — using default voice for this reply."
                                if speaker_wav
                                else f"Voice engine fell back to {synth.engine} for this reply."
                            ),
                        },
                    )

                with span("avatar.animate", **{"chunk": chunk_index}):
                    await avatar_animator.animate(
                        avatar_image_path=avatar_image,
                        audio_path=str(tmp_audio),
                        output_path=str(tmp_video),
                    )

                ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                video_key = f"videos/{session_id}/{ts}_c{chunk_index}.mp4"
                with span("storage.upload", **{"chunk": chunk_index}):
                    await storage_service.upload_file(
                        tmp_video.read_bytes(), video_key, content_type="video/mp4"
                    )
                    # S3 objects are private — hand the client a URL it can
                    # actually fetch (presigned on S3, /uploads/ locally).
                    video_url = await storage_service.serving_url(video_key)

                await self.send_message(
                    session_id,
                    {
                        "type": "video_chunk",
                        "chunk_index": chunk_index,
                        "total_chunks": -1,
                        "video_url": video_url,
                        "text": sentence,
                    },
                )
                chunk_index = chunk_index + 1
                sent_any = True
                logger.info(f"Chunk {chunk_index} ready [{session_id}]")

            except Exception as e:
                logger.error(f"Chunk {chunk_index} failed [{session_id}]: {e}")

            finally:
                tmp_audio.unlink(missing_ok=True)
                tmp_video.unlink(missing_ok=True)

        await self.send_message(
            session_id,
            {
                "type": "video_chunk_end",
                "sent_chunks": chunk_index,
            },
        )

        if not sent_any:
            await self.send_message(
                session_id,
                {"type": "error", "message": "Avatar animation failed for all sentences."},
            )

    # ── helpers ───────────────────────────────────────────────────────────────

    async def set_avatar(self, session_id: str, avatar_id: str):
        if session_id in self.session_data:
            self.session_data[session_id]["avatar_id"] = avatar_id

    async def set_voice_by_id(self, session_id: str, voice_id: str) -> bool:
        """
        Resolve a voice ID to its on-disk WAV and attach it to the session.
        Returns True if the voice was found, owned by the requester, and
        assigned. Accepts voice IDs only — raw filesystem paths are NEVER
        accepted from WebSocket clients (path-disclosure / arbitrary-read).
        """
        if session_id not in self.session_data:
            return False
        entry = await self._get_voice_entry(voice_id)
        if not entry:
            return False
        # Cross-tenant guard: only the voice's owner can attach it to their
        # session. Otherwise user A could guess user B's voice UUID and
        # surreptitiously use their cloned voice.
        session_user = self.session_data[session_id].get("user_id")
        voice_user = entry.get("user_id", "demo-user")
        if session_user and voice_user != session_user:
            logger.warning(
                f"WS set_voice rejected: voice {voice_id} owned by "
                f"{voice_user!r} but session belongs to {session_user!r}"
            )
            return False
        wav = entry.get("wav_path")
        if not wav:
            return False
        self.session_data[session_id]["voice_wav"] = wav
        logger.info(f"Voice set [{session_id}]: voice_id={voice_id}")
        return True

    async def set_language(self, session_id: str, language: str):
        """Set TTS language for the session. Falls back to 'en' on unknown codes."""
        # Match voices.py allowed list
        allowed = {
            "ar",
            "da",
            "de",
            "el",
            "en",
            "es",
            "fi",
            "fr",
            "he",
            "hi",
            "it",
            "ja",
            "ko",
            "ms",
            "nl",
            "no",
            "pl",
            "pt",
            "ru",
            "sv",
            "sw",
            "tr",
            "zh",
        }
        lang = (language or "en").lower()
        if lang not in allowed:
            lang = "en"
        if session_id in self.session_data:
            self.session_data[session_id]["language"] = lang
            logger.info(f"Language set [{session_id}]: {lang}")

    # ── stale session cleanup ─────────────────────────────────────────────────

    async def cleanup_stale(self) -> int:
        """
        Reap sessions whose websocket is gone or that have been idle too long.

        We snapshot the candidate list under a lock, then drop the lock while
        calling `disconnect()` for each (disconnect involves async file I/O
        and shouldn't be serialized). The lock prevents the snapshot from
        racing with a fresh `connect()` for the same session id — without it,
        the cleanup loop could observe a half-built session and rip it down
        right after the new connection finished setting up.
        """
        now = datetime.now(timezone.utc)
        async with self._mutation_lock:
            stale: list[str] = []
            for sid, data in self.session_data.items():
                last = data.get("last_activity") or data.get("connected_at") or now
                if sid not in self.active_connections:
                    stale.append(sid)
                    continue
                if (now - last).total_seconds() > STALE_SESSION_TTL_SECS:
                    stale.append(sid)
        for sid in stale:
            await self.disconnect(sid)
        return len(stale)

    def start_cleanup_task(self) -> None:
        if self._cleanup_task is not None:
            return

        async def _loop():
            while True:
                try:
                    await asyncio.sleep(300)
                    reaped = await self.cleanup_stale()
                    if reaped:
                        logger.info(f"Reaped {reaped} stale WS session(s)")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"WS cleanup task error: {e}")

        async def _keepalive():
            """
            Ping every live connection on WS_PING_INTERVAL and drop the ones
            that fail to accept the frame.

            A client that vanishes without a close handshake (laptop lid,
            dropped mobile link) otherwise leaves a socket that looks live
            until the two-hour stale reaper notices. Pinging surfaces the
            dead peer within one interval and frees its slot against the
            connection cap.
            """
            while True:
                try:
                    await asyncio.sleep(settings.WS_PING_INTERVAL)
                    for session_id in list(self.active_connections):
                        ws = self.active_connections.get(session_id)
                        if ws is None:
                            continue
                        try:
                            await asyncio.wait_for(
                                self.send_message(session_id, {"type": "ping"}),
                                timeout=settings.WS_PING_TIMEOUT,
                            )
                        except (asyncio.TimeoutError, Exception):
                            logger.info(f"WS keepalive failed for {session_id} — disconnecting")
                            await self.disconnect(session_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"WS keepalive task error: {e}")

        self._cleanup_task = asyncio.create_task(_loop(), name="ws-cleanup")
        self._keepalive_task = asyncio.create_task(_keepalive(), name="ws-keepalive")

    async def stop_cleanup_task(self) -> None:
        for attr in ("_cleanup_task", "_keepalive_task"):
            task = getattr(self, attr, None)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            setattr(self, attr, None)


websocket_manager = ConnectionManager()
