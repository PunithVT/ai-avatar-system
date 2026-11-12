import asyncio
import base64
import json
import logging
import os
import re
import stat
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import WebSocket

from app.config import settings
from app.services.animator import avatar_animator
from app.services.llm import (
    LLMAuthError,
    LLMError,
    LLMRateLimited,
    LLMRefused,
    LLMUnavailable,
    llm_service,
)
from app.services.storage import storage_service
from app.services.stt import stt_service
from app.services.tts import tts_service
from app.services.turn_taking import MAX_UTTERANCE_SECS, SAMPLE_RATE, MicReader, TurnDetector
from app.services.webrtc import AvatarStream, ClipDropped, mic_enabled, webrtc_enabled
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

# When the window overflows, cut back to this many rather than to the cap.
#
# Trimming to the cap itself meant the very next turn overflowed again: after
# the first overflow EVERY turn evicted two messages and ran a summarisation
# call for them, awaited before the reply even started. The comment on
# _roll_memory promised "once every 30 turns"; it was once per turn from turn
# 31 onward. Cutting back further restores the amortisation — one
# summarisation per (MAX - TRIM) / 2 turns.
TRIM_CONTEXT_TO = 40

# Close code sent to a socket that has been replaced by a newer connection to
# the same session (another tab, or a reconnect that raced the old socket's
# teardown). Distinct from 1000 so the client knows NOT to reconnect — two
# tabs reconnecting in response to each other would never stop.
WS_CLOSE_SUPERSEDED = 4409

# WebRTC signalling bounds. A browser's audio+video offer with every candidate
# inlined is 3-6 KB; 64 KB is far past any real one and still small enough to
# parse without thought.
MAX_SDP_CHARS = 64 * 1024
# Each offer builds a peer connection — sockets, a DTLS certificate, an ICE
# gather. A client needs one, and one more if its first attempt failed; this
# leaves room for that and stops a loop of offers from being a cheap way to
# make the server do expensive work.
MAX_WEBRTC_OFFERS_PER_CONNECTION = 4
# ICE gathering waits on the configured STUN/TURN servers. An unreachable one
# must end in a refusal the client can act on, not an offer left hanging.
WEBRTC_NEGOTIATE_TIMEOUT_SECS = 10


@dataclass(eq=False)
class _StreamClip:
    """
    A sentence that was put on the WebRTC stream instead of being uploaded.

    Kept — file and all — until the stream reports it played, because a clip
    that is queued is not yet a clip that was heard: if the peer connection
    dies first, this is what gets uploaded and sent the ordinary way.
    """

    chunk_index: int
    sentence: str
    path: Path


@dataclass(eq=False)
class _Mic:
    """
    One peer connection's microphone: the track the browser may send on, and
    the turn-taking that listens to it between `mic active:true` and
    `active:false`.

    Belongs to a stream, not to the session — a new offer is a new peer
    connection with a new track, and the client says `active:true` again
    once it has attached its microphone to that one.
    """

    stream: Any
    # Reads the track from the moment it exists (so nothing piles up in
    # aiortc), but passes audio on only while `listening`.
    reader: Optional[MicReader] = None
    reader_task: Optional[asyncio.Task] = None
    detector: Optional[TurnDetector] = None
    # The client asked (active:true) and has not taken it back.
    wanted: bool = False
    listening: bool = False
    # The detector is being built; a second start must not build another.
    starting: bool = False
    # `active:true` frames not yet answered. The client matches each
    # mic_state to the mic frame it sent, in order, so every frame gets
    # exactly one — including one that had to wait for the stream to go
    # live, and one overtaken by an `active:false` while it waited.
    unacked: int = 0
    # Counts utterances, so a partial transcript that comes back late can
    # tell it belongs to one that is over.
    utterance: int = 0
    # The user is mid-utterance: running transcripts may be shown.
    open: bool = False
    # The transcription of this session's audio that is inside Whisper now.
    # Outlives whoever was waiting for it — a Whisper call cannot be stopped —
    # and the next one waits for it, so a session never has two at once.
    stt_job: Optional[asyncio.Task] = None
    # The final transcript, started when the VAD went quiet and before the
    # turn model had answered. Discarded if the speaker carries on.
    speculative: Optional[asyncio.Task] = None
    partial: Optional[asyncio.Task] = None
    # The turn spawned for the last utterance, for as long as it has NOT yet
    # sent its final transcription — and the audio it is transcribing. Until
    # that frame goes out the utterance is still the microphone's business:
    # it can be withdrawn (the listener stopped) or continued (the speaker
    # carried on). After it, the user has seen their words and the turn is a
    # turn like any other; both are None again.
    turn: Optional[asyncio.Task] = None
    turn_audio: Any = None
    # Audio of an utterance that was closed too early: the speaker carried
    # on before its transcript had been sent. Prepended to the utterance now
    # in progress, so the two are transcribed and answered as one.
    carry: Any = None


_INTERRUPTED = {"type": "interrupted", "message": "Previous response interrupted"}
_SPEECH_IGNORED = {"type": "user_speech", "state": "stopped", "reason": "ignored"}

# Soft TTL for an idle (disconnected/abandoned) session in seconds.
STALE_SESSION_TTL_SECS = 60 * 60 * 2  # 2 hours


def _llm_error_message(exc: LLMError) -> str:
    """
    Turn a provider failure into something a user can act on.

    llm.py maps SDK exceptions into this hierarchy precisely so the transport
    can distinguish them, but nothing consumed it — every failure surfaced as
    "Processing failed" regardless of whether the provider was rate-limiting,
    the API key was wrong, or the network was down. Those need different
    reactions: wait, fix your config, or retry.
    """
    if isinstance(exc, LLMRateLimited):
        return "The AI provider is rate-limiting us. Try again in a moment."
    if isinstance(exc, LLMAuthError):
        # Deliberately not "check your API key" — the person talking to the
        # avatar is usually not the person who configured it.
        return "The AI provider rejected our credentials. This needs an admin."
    if isinstance(exc, LLMUnavailable):
        return "Couldn't reach the AI provider. Check your connection and try again."
    if isinstance(exc, LLMRefused):
        return "I can't help with that request. Try asking something else."
    return "The AI provider returned an error. Try again."


# How the carried summary is introduced to the model. Framed as context rather
# than instruction so it informs the reply without the model narrating it back.
_MEMORY_PREAMBLE = "Earlier in this same conversation (summarised):"


def _compose_system_prompt(base: Optional[str], summary: Optional[str]) -> Optional[str]:
    """
    Fold a conversation summary into the avatar's system prompt.

    Kept pure so the composition can be tested without a websocket, an LLM or a
    database — the parts most likely to be wrong here are ordering and what
    happens when either half is missing.
    """
    if not summary:
        return base
    memory_block = f"{_MEMORY_PREAMBLE}\n{summary}"
    if not base:
        return memory_block
    # Persona first: it governs how the avatar speaks, and burying it under a
    # wall of recalled context makes models drift off-character.
    return f"{base}\n\n{memory_block}"


def _llm_view(messages: List[dict]) -> List[dict]:
    """
    The history as it is safe to send to a provider.

    The stored history is whatever actually happened, which is not always a
    clean user/assistant alternation:

      * a turn that failed or was interrupted before the model said anything
        leaves two user messages in a row;
      * trimming, or rehydrating the last N rows after a reconnect, can leave
        an assistant message first.

    Providers differ in how strictly they reject those shapes, and a rejection
    is permanent for the session — every later turn carries the same history.
    So leading assistant messages are dropped and consecutive messages from
    the same role are merged. The stored history is left untouched.
    """
    view: List[dict] = []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role not in ("user", "assistant") or not content:
            continue
        if not view and role != "user":
            continue
        if view and view[-1]["role"] == role:
            view[-1] = {"role": role, "content": f"{view[-1]['content']}\n\n{content}"}
        else:
            view.append({"role": role, "content": content})
    return view


def _trim_window(messages: List[dict]) -> List[dict]:
    """
    Cut an overflowing history back to TRIM_CONTEXT_TO, in place, and return
    what was removed (oldest first) so it can be summarised.

    The cut lands on a user message so the kept window never opens with an
    orphaned assistant reply. Returns [] when nothing needs trimming.
    """
    if len(messages) <= MAX_CONTEXT_MESSAGES:
        return []
    cut = len(messages) - TRIM_CONTEXT_TO
    while cut < len(messages) - 1 and messages[cut].get("role") != "user":
        cut += 1
    dropped = messages[:cut]
    del messages[:cut]
    return dropped


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
        # Start times of recent turns per USER (not per session — one user can
        # open many sessions), for the turn rate limit.
        self._turn_times: Dict[str, deque] = {}
        # Fire-and-forget work (memory summarisation, persisting an interrupted
        # reply). Held here because asyncio keeps only weak references to
        # tasks: an unreferenced one can be garbage-collected mid-flight.
        self._background: set[asyncio.Task] = set()
        # Per session: a callable that records the in-flight turn's reply so
        # far. See _commit_pending_reply.
        self._pending_commits: Dict[str, object] = {}
        # Per session: the WebRTC stream its browser negotiated, if any
        # (MEDIA_TRANSPORT=webrtc). Absent means the session is on clips.
        self._streams: Dict[str, AvatarStream] = {}
        # Sessions that have been TOLD their stream is live. A clip goes onto
        # the stream only for these — the client must have heard
        # media_transport=webrtc before it sees a video_chunk with no URL.
        self._webrtc_live: set[str] = set()
        # Per session: the task re-sending, by URL, the clips a stream that
        # went down had not finished playing. The turn pipeline waits on it
        # before sending a later sentence, so the order survives the switch.
        self._resends: Dict[str, asyncio.Task] = {}
        # Sessions whose CURRENT turn has had a clip delivered by URL. The
        # rest of that turn stays on URLs even if a stream comes up — see
        # _animate_from_queue.
        self._url_turns: set[str] = set()
        # Per session: the microphone of its current stream, when the server
        # takes the turns (see _Mic). Absent on clips, with turn detection
        # off, or when the browser's offer carried no microphone.
        self._mics: Dict[str, _Mic] = {}

    def _spawn_bg(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _allow_turn(self, session_id: str, consume: bool = True) -> bool:
        """
        Sliding-window limit on turns started per user per minute.

        The HTTP rate limiter is middleware and never sees WebSocket frames,
        so nothing bounded this path: each text frame opens an LLM stream and
        each audio frame runs speech-to-text, and a guest account (free, and
        minted without credentials) could loop them. Interrupting the previous
        turn does not help — the provider call has already been made.

        With `consume=False` nothing is recorded: it only answers whether a
        turn started now would be allowed.
        """
        limit = settings.WS_TURNS_PER_MINUTE
        if limit <= 0:
            return True
        data = self.session_data.get(session_id) or {}
        key = data.get("user_id") or f"session:{session_id}"
        now = time.monotonic()
        window = self._turn_times.setdefault(key, deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            return False
        if not consume:
            return True
        window.append(now)
        # Opportunistic cleanup so the map cannot grow without bound.
        if len(self._turn_times) > 10_000:
            for k in [k for k, w in self._turn_times.items() if not w or now - w[-1] > 60]:
                self._turn_times.pop(k, None)
        return True

    async def _reject_rate_limited(self, session_id: str) -> None:
        await self.send_message(
            session_id,
            {
                "type": "error",
                "code": "rate_limited",
                "message": "You're sending messages too quickly. Wait a moment and try again.",
            },
        )

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

        # A second connection to the same session replaces the first. The old
        # socket used to be silently overwritten and left open: its turn kept
        # streaming into the new connection, and when it eventually errored
        # its handler tore down the session by id — closing the NEW socket a
        # few seconds after a successful reconnect.
        previous = self.active_connections.get(session_id)
        if previous is not None and previous is not websocket:
            self._commit_pending_reply(session_id)
            task = self._active_turns.pop(session_id, None)
            if task and not task.done():
                task.cancel()
            # The peer connection was negotiated over the old socket and
            # belongs to that browser tab; the new one makes its own offer.
            self._retire_stream(session_id)
            # Closed in the background, with a deadline, AFTER the new socket
            # is registered below. Awaiting the close here made the new
            # connection wait on the old peer's close handshake — and the
            # usual reason for a reconnect is that the old peer is gone, so
            # that wait ran to the full handshake timeout.
            self._spawn_bg(self._close_quietly(previous, WS_CLOSE_SUPERSEDED))
            logger.info(f"WebSocket for {session_id} superseded by a newer connection")

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
            # Running summary of turns that have fallen out of the context
            # window. None until the conversation is long enough to lose any.
            "memory_summary": None,
            "user_id": user_id,
            "connected_at": datetime.now(timezone.utc),
            "last_activity": datetime.now(timezone.utc),
        }
        await self._load_session_data(session_id)
        logger.info(f"WebSocket connected: {session_id} (user={user_id})")

    async def _roll_memory(self, session_id: str, dropped: list[dict]) -> None:
        """
        Fold the turns just evicted from the context window into the running
        summary, and persist it.

        Runs only when the window overflows and is cut back to
        TRIM_CONTEXT_TO — once every (MAX_CONTEXT_MESSAGES - TRIM_CONTEXT_TO)
        / 2 turns, in the background (see _schedule_memory_roll), so it adds
        neither a call to every reply nor a wait to the one that triggers it.

        Never raises. Losing the summary costs continuity; letting it raise
        would cost the turn, and the turn is the thing the user asked for.
        """
        if not settings.CONVERSATION_MEMORY or not dropped:
            return

        data = self.session_data.get(session_id)
        if data is None:
            return
        previous = data.get("memory_summary")

        lines = [
            f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')}"
            for m in dropped
        ]
        transcript = "\n".join(lines)

        # Feed the old summary back in so this compounds rather than only ever
        # describing the most recently dropped chunk.
        if previous:
            prompt = (
                "Update the running summary of a conversation so it also covers "
                "the newer turns below. Keep it under 150 words, third person, "
                "no preamble. Preserve names, decisions and open questions.\n\n"
                f"Existing summary:\n{previous}\n\nNewer turns:\n{transcript}"
            )
        else:
            prompt = (
                "Summarise the conversation turns below in under 150 words, "
                "third person, no preamble. Preserve names, decisions and open "
                f"questions.\n\n{transcript}"
            )

        try:
            summary = await llm_service.generate_response(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You write concise neutral summaries.",
            )
        except Exception as e:
            # Including LLMError: a provider hiccup here must not surface as a
            # failed turn. The window simply truncates as it did before.
            logger.warning(f"Memory summarisation failed [{session_id}]: {e}")
            return

        summary = (summary or "").strip()
        if not summary:
            return
        if len(summary) > settings.MEMORY_SUMMARY_MAX_CHARS:
            summary = summary[: settings.MEMORY_SUMMARY_MAX_CHARS].rstrip() + "…"

        data["memory_summary"] = summary
        # Guarded here as well as inside the callee. _roll_memory runs on the
        # turn path, so anything escaping it becomes a failed reply — and this
        # method's whole contract is that a memory problem never costs a turn.
        # Relying on the callee's internal handling would make that contract
        # depend on someone not refactoring it away.
        try:
            await self._persist_memory_summary(session_id, summary)
        except Exception as e:
            logger.warning(f"Could not persist memory summary [{session_id}]: {e}")
        logger.info(f"Memory summary updated [{session_id}] ({len(summary)} chars)")

    async def _persist_memory_summary(self, session_id: str, summary: str) -> None:
        """
        Store the summary on the session's Conversation row so it survives a
        reconnect — otherwise a browser refresh would silently drop everything
        the summary was carrying.
        """
        try:
            from sqlalchemy import select

            from app.database import AsyncSessionLocal
            from app.models import Conversation

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Conversation)
                    .where(Conversation.session_id == session_id)
                    .order_by(Conversation.created_at.desc())
                    .limit(1)
                )
                conversation = result.scalar_one_or_none()
                if conversation is None:
                    return
                conversation.summary = summary
                await db.commit()
        except Exception as e:
            logger.warning(f"Could not persist memory summary [{session_id}]: {e}")

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

                # Restore the running summary too. Rehydrating only the last
                # MAX_CONTEXT_MESSAGES rows means everything older is already
                # outside the window on reconnect — without the summary a
                # refresh would quietly lose it.
                if settings.CONVERSATION_MEMORY:
                    from app.models import Conversation

                    conv_result = await db.execute(
                        select(Conversation)
                        .where(Conversation.session_id == session_id)
                        .order_by(Conversation.created_at.desc())
                        .limit(1)
                    )
                    conversation = conv_result.scalar_one_or_none()
                    if conversation is not None and conversation.summary:
                        self.session_data[session_id]["memory_summary"] = conversation.summary
                        logger.info(f"Restored memory summary for session {session_id}")

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
                        # Same ownership rule as an explicit set_voice. The
                        # avatar's voice_id is a free-form string its owner
                        # can set, so without this check a leaked voice UUID
                        # was enough to speak in another user's cloned voice.
                        if await self.set_voice_by_id(session_id, avatar.voice_id):
                            logger.info(
                                f"Auto-loaded voice {avatar.voice_id} for session {session_id}"
                            )
                    logger.info(f"Loaded avatar {avatar.id} for session {session_id}")

        except Exception as e:
            logger.error(f"Failed to load session data for {session_id}: {e}")

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
        await asyncio.to_thread(_write_private_bytes, cache_path, data)
        return str(cache_path)

    async def disconnect(self, session_id: str, websocket: Optional[WebSocket] = None):
        """
        Tear down a session's connection state.

        Pass `websocket` when the caller is acting on behalf of ONE specific
        socket (its receive loop ended, or a send on it failed). If that
        socket has since been replaced by a newer connection this is a no-op:
        the session now belongs to the newer socket and must not be torn down
        because the old one finally noticed it was dead.
        """
        if websocket is not None and self.active_connections.get(session_id) is not websocket:
            return

        # Cancel any in-flight LLM/TTS/animation task for this session so it
        # doesn't keep churning after the client is gone (wasted tokens + GPU).
        self._commit_pending_reply(session_id)
        task = self._active_turns.pop(session_id, None)
        if task and not task.done():
            task.cancel()

        ws = self.active_connections.pop(session_id, None)
        self.session_data.pop(session_id, None)
        self._send_locks.pop(session_id, None)
        self._webrtc_live.discard(session_id)
        self._url_turns.discard(session_id)
        # Nobody is left to re-send anything to.
        self._cancel_resend(session_id)
        self._mic_drop(session_id)
        stream = self._streams.pop(session_id, None)
        if stream is not None:
            self._discard_stream_clips(stream)
            await stream.close()

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

    @staticmethod
    async def _close_quietly(ws: WebSocket, code: int) -> None:
        try:
            await asyncio.wait_for(ws.close(code=code), timeout=2)
        except Exception:
            pass  # already gone, or never going to answer

    def _commit_pending_reply(self, session_id: str) -> None:
        """
        Record what the in-flight turn has said so far, NOW.

        Must run before a new turn appends its user message. Cancellation is
        delivered on a later loop iteration, so leaving this to the cancelled
        task's own handler stored the interrupted reply AFTER the next
        question: the history read user, user, assistant and the next request
        to the model ended on an assistant message.
        """
        commit = self._pending_commits.pop(session_id, None)
        if commit is not None:
            commit()

    async def interrupt_active_turn(self, session_id: str) -> bool:
        """
        Cancel any in-flight turn for this session ("barge-in"). Returns
        True if a turn was actually interrupted. Used when fresh user audio
        arrives mid-response — modern voice-AI UX expects sub-100 ms cutoff
        so the user doesn't keep hearing the previous response while talking.
        """
        if self._cut_active_turn(session_id):
            # Tell the client to stop playing the queued video chunks too —
            # otherwise they'd keep arriving from the buffer.
            await self.send_message(session_id, _INTERRUPTED)
            return True
        return False

    def _cut_active_turn(self, session_id: str) -> bool:
        """
        The interruption itself, with no await in it: commit what the turn
        had said, silence the stream, cancel the task. True if there was
        anything to cut. Telling the client is the caller's.
        """
        self._commit_pending_reply(session_id)
        # On the WebRTC transport the speech still to come is queued HERE, not
        # in the browser, so this is what actually silences the avatar — and
        # it can be mid-sentence long after the turn's task has finished,
        # which is why it does not depend on there being a task to cancel.
        # Before the client is told, so the picture is already back to idle
        # when it hears about it.
        cut_off = self._flush_stream(session_id)
        # Likewise speech that was on a stream which has since gone down and
        # is on its way back as clips: interrupted like the rest, not played
        # over the user a moment later.
        cut_off = self._cancel_resend(session_id) or cut_off
        task = self._active_turns.pop(session_id, None)
        cancelled = bool(task and not task.done())
        if cancelled:
            task.cancel()
        return cancelled or cut_off

    # ── WebRTC transport ──────────────────────────────────────────────────────

    def _live_stream(self, session_id: str) -> Optional[AvatarStream]:
        """The session's stream, if a clip can be put on it right now."""
        if session_id not in self._webrtc_live:
            return None
        stream = self._streams.get(session_id)
        return stream if stream is not None and stream.connected else None

    def _flush_stream(self, session_id: str) -> bool:
        """Drop the session's queued speech. True if any was cut off."""
        stream = self._streams.get(session_id)
        if stream is None:
            return False
        try:
            return bool(stream.flush())
        except Exception as e:
            logger.warning(f"WebRTC flush failed [{session_id}]: {e}")
            return False

    @staticmethod
    def _release_clip(clip: Optional[_StreamClip]) -> None:
        """The stream is done with a clip (played, or flushed): drop its file."""
        if clip is not None:
            clip.path.unlink(missing_ok=True)

    def _discard_stream_clips(self, stream: AvatarStream) -> None:
        """
        Stop `stream` taking clips and delete the ones it had not played,
        for the exits where there is nobody to re-send them to.
        """
        try:
            for clip in stream.retire():
                self._release_clip(clip)
        except Exception as e:
            logger.warning(f"WebRTC retire failed: {e}")

    def _retire_stream(self, session_id: str) -> None:
        """Silence and discard the session's stream without telling anyone."""
        self._webrtc_live.discard(session_id)
        self._url_turns.discard(session_id)
        self._cancel_resend(session_id)
        self._mic_drop(session_id)
        stream = self._streams.pop(session_id, None)
        if stream is None:
            return
        try:
            stream.flush()
        except Exception:
            pass
        self._discard_stream_clips(stream)
        self._spawn_bg(stream.close())

    def _cancel_resend(self, session_id: str) -> bool:
        """
        Abandon any re-send in progress for the session. True if there was
        one — speech the user was still owed has been dropped.
        """
        task = self._resends.pop(session_id, None)
        if task is None or task.done():
            return False
        # Cancels the whole chain: each re-send awaits the one before it.
        # The clips' files go in each task's done-callback.
        task.cancel()
        return True

    def _schedule_resend(
        self, session_id: str, clips: list[_StreamClip], after: asyncio.Event
    ) -> None:
        """
        Deliver `clips` — taken back from a stream that went down — as
        ordinary URL clips, in order, once `after` is set.

        Registered synchronously, so the turn pipeline (which waits on
        self._resends before sending anything by URL) cannot slip a later
        sentence in ahead of them.
        """
        previous = self._resends.get(session_id)
        data = self.session_data.get(session_id)
        task = self._spawn_bg(self._resend_clips(session_id, data, clips, after, previous))
        self._resends[session_id] = task
        # These went by URL, so the turn they belong to is a clips turn from
        # here on (decision recorded in _animate_from_queue).
        self._url_turns.add(session_id)

        def _done(t: asyncio.Task) -> None:
            # In a callback rather than the coroutine's own `finally`: a task
            # cancelled before its first step never runs any of its body.
            for clip in clips:
                clip.path.unlink(missing_ok=True)
            if self._resends.get(session_id) is t:
                self._resends.pop(session_id, None)

        task.add_done_callback(_done)

    async def _resend_clips(
        self,
        session_id: str,
        data: Optional[dict],
        clips: list[_StreamClip],
        after: asyncio.Event,
        previous: Optional[asyncio.Task],
    ) -> None:
        if previous is not None and not previous.done():
            # An earlier stream's leftovers come first.
            await asyncio.gather(previous, return_exceptions=True)
        await after.wait()
        for clip in clips:
            # Same connection as the one the clips were made for: a reconnect
            # installs a new dict, and its client never saw these announced.
            if data is None or self.session_data.get(session_id) is not data:
                return
            try:
                video_url = await self._upload_clip(session_id, clip.chunk_index, clip.path)
            except Exception as e:
                # One clip lost, not the ones after it.
                logger.error(
                    f"Could not re-send chunk {clip.chunk_index} as a clip [{session_id}]: {e}"
                )
                continue
            # The same chunk_index it was announced under on the stream, and
            # `resend` so the client plays it without counting or showing a
            # sentence it already has. Whole, even if the stream was part-way
            # through it: a repeated sentence beats a missing one.
            await self.send_message(
                session_id,
                {
                    "type": "video_chunk",
                    "chunk_index": clip.chunk_index,
                    "total_chunks": -1,
                    "video_url": video_url,
                    "text": clip.sentence,
                    "resend": True,
                },
            )
            clip.path.unlink(missing_ok=True)

    async def _upload_clip(self, session_id: str, chunk_index: int, path: Path) -> str:
        """Store a clip and return a URL the client can fetch it from."""
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        video_key = f"videos/{session_id}/{ts}_c{chunk_index}.mp4"
        with span("storage.upload", **{"chunk": chunk_index}):
            video_bytes = await asyncio.to_thread(path.read_bytes)
            await storage_service.upload_file(video_bytes, video_key, content_type="video/mp4")
            # S3 objects are private — hand the client a URL it can actually
            # fetch (presigned on S3, /uploads/ locally).
            return await storage_service.serving_url(video_key)

    async def _url_clip_may_follow(self, session_id: str) -> None:
        """
        Hold a clip that is about to be sent by URL until it is its turn.

        The browser plays a URL clip the moment it arrives and cannot delay a
        live stream, so order across the two has to be kept here: after the
        stream has finished whatever it still has queued (a single clip can
        fail to go onto a stream that stays up), and after any clips being
        re-sent from a stream that went down. Without either this returns
        without yielding, which is every turn on the default transport.
        """
        stream = self._streams.get(session_id)
        if stream is not None and session_id in self._webrtc_live:
            # Bounded by what is queued. A stream that does not drain in that
            # time is not playing: treat it as finished, so what it still
            # holds is registered for re-sending BEFORE this sentence goes
            # out. Sending regardless let this sentence overtake the ones
            # stuck on the stream once they were recovered.
            if await stream.wait_drained() is False:
                await self._stream_down(session_id, stream)
        while True:
            pending = self._resends.get(session_id)
            if pending is None or pending.done():
                return
            # wait(), not await: if THIS turn is cancelled the re-send must
            # not be cancelled with it by accident — whoever cancels the turn
            # decides that separately.
            await asyncio.wait({pending})

    async def _stream_down(self, session_id: str, stream: AvatarStream) -> None:
        """
        The session's stream is finished (failed, closed by the client, or
        about to be replaced): discard it, put the client back on clips, and
        re-send by URL whatever it had not finished playing.
        """
        if self._streams.get(session_id) is not stream:
            return
        self._streams.pop(session_id, None)
        was_live = session_id in self._webrtc_live
        self._webrtc_live.discard(session_id)
        was_speaking = stream.speaking  # retire() resets it
        # The microphone goes with the peer connection it came up. From this
        # line the server is not listening; the client is told below.
        mic_was_on = self._mic_drop(session_id, stream)

        # Everything up to here and the two statements below run without an
        # await. That matters twice over. The stream must refuse clips from
        # this instant: it used to stay open across the sends further down,
        # and a clip that finished decoding meanwhile was queued onto it,
        # announced as streamed, and then thrown away by the close. And the
        # re-send must be registered before the pipeline can run again, or a
        # later sentence could overtake the ones being recovered.
        unplayed = stream.retire()
        announced = asyncio.Event()
        if unplayed:
            self._schedule_resend(session_id, unplayed, announced)

        async def _announce_and_close() -> None:
            try:
                for _ in range(mic_was_on):
                    # Unrequested, which is the client's cue to take
                    # hands-free back into the browser.
                    await self.send_message(session_id, {"type": "mic_state", "listening": False})
                if was_live:
                    # Say the stream's speech is over, or the client's speaking
                    # indicator and echo threshold stay latched on.
                    if was_speaking:
                        await self.send_message(session_id, {"type": "speaking", "active": False})
                    await self.send_message(
                        session_id, {"type": "media_transport", "transport": "clips"}
                    )
            finally:
                # The recovered clips go out after the client knows it is on
                # clips, not before.
                announced.set()
            await stream.close()

        # As its own task, shielded from the caller. A turn can be the one
        # that discovers its stream is dead, and that turn can be cancelled
        # (the user presses stop) while these sends are in flight. Run
        # inline, the cancellation skipped everything after the await it
        # landed on: the peer connection was never closed and the client was
        # never told it was back on clips.
        await asyncio.shield(self._spawn_bg(_announce_and_close()))

    async def _on_stream_state(self, session_id: str, stream: AvatarStream, state: str) -> None:
        if self._streams.get(session_id) is not stream:
            return
        if state == "connected":
            await self.send_message(session_id, {"type": "media_transport", "transport": "webrtc"})
            # Only now, after the announcement is on the wire, may the
            # pipeline choose the stream for a clip.
            if self._streams.get(session_id) is stream and stream.connected:
                self._webrtc_live.add(session_id)
                # A `mic active:true` that arrived while the media path was
                # still coming up has been waiting for this.
                await self._mic_start(session_id)
        elif state in ("failed", "closed", "disconnected"):
            # "disconnected" can recover in a browser, but aiortc 1.16 never
            # reports it (its connectionState goes straight to "failed"), so
            # it is listed for a version that does — and treated like a
            # failure, with no grace period: a clip put on a stream nobody is
            # receiving is a sentence the user never hears, whereas one sent
            # the old way is at worst a stutter. The client may offer again.
            #
            # aiortc itself only notices a dead UDP path when ICE consent
            # expires, about 30 s on. The prompt signal is the client's:
            # it sends webrtc_close as soon as its own side fails or stays
            # disconnected for a few seconds.
            await self._stream_down(session_id, stream)

    async def _on_stream_speaking(
        self, session_id: str, stream: AvatarStream, active: bool
    ) -> None:
        if self._streams.get(session_id) is stream:
            # What the avatar says comes back up the microphone as echo; the
            # detector raises its bar for as long as it lasts. Only speech on
            # the stream matters here: while the server is listening the
            # avatar is not being played from clips.
            mic = self._mics.get(session_id)
            if mic is not None and mic.detector is not None:
                mic.detector.set_avatar_speaking(active)
            await self.send_message(session_id, {"type": "speaking", "active": active})

    async def _refuse_webrtc(self, session_id: str, message: str) -> None:
        await self.send_message(session_id, {"type": "webrtc_error", "message": message})

    async def handle_webrtc_offer(self, session_id: str, sdp) -> None:
        """
        Start (or replace) the session's WebRTC stream from a browser offer.

        Every way this can go wrong ends in a `webrtc_error` and the session
        carrying on with clips — never in an exception reaching the receive
        loop, which would drop the whole connection over an optional upgrade.
        """
        data = self.session_data.get(session_id)
        if data is None:
            return
        if not webrtc_enabled():
            await self._refuse_webrtc(session_id, "WebRTC delivery is not enabled on this server.")
            return
        if not isinstance(sdp, str) or not sdp.strip() or len(sdp) > MAX_SDP_CHARS:
            await self._refuse_webrtc(session_id, "Invalid WebRTC offer.")
            return
        image = data.get("avatar_image_local")
        refusal = None
        if data.get("_webrtc_offers", 0) >= MAX_WEBRTC_OFFERS_PER_CONNECTION:
            refusal = "Too many WebRTC offers on this connection."
        elif not image:
            # The stream's resting picture is the avatar; without one there
            # is nothing to show between sentences.
            refusal = "This avatar's image is unavailable."
        if refusal is not None:
            # Decided BEFORE the sequence number moves. The number is what a
            # negotiation still in flight checks to see whether it has been
            # superseded; a refused offer used to bump it, so the last offer
            # that was within the limit lost its answer to one that was not.
            # A stream that is already LIVE is a different matter: the
            # browser discarded its end of that when it made this offer, so
            # leaving it up would keep putting sentences on a connection
            # nobody is receiving.
            previous = self._streams.get(session_id)
            if previous is not None and session_id in self._webrtc_live:
                await self._stream_down(session_id, previous)
            await self._refuse_webrtc(session_id, refusal)
            return
        seq = data["_webrtc_offers"] = data.get("_webrtc_offers", 0) + 1

        # A new offer replaces the previous peer connection, now, whether or
        # not the new one succeeds: the browser has already discarded its end.
        previous = self._streams.get(session_id)
        if previous is not None:
            await self._stream_down(session_id, previous)

        # In the background: answering waits on ICE gathering (network round
        # trips to the STUN/TURN servers), and the receive loop must stay
        # free to see a message or a stop in the meantime.
        self._spawn_bg(self._negotiate_webrtc(session_id, data, seq, sdp, image))

    async def _negotiate_webrtc(
        self, session_id: str, data: dict, seq: int, sdp: str, image: str
    ) -> None:
        def current() -> bool:
            # Same connection (a reconnect installs a new dict) and no newer
            # offer has arrived since this one.
            return self.session_data.get(session_id) is data and data.get("_webrtc_offers") == seq

        stream: Optional[AvatarStream] = None
        try:
            stream = await AvatarStream.create(image)
            if not current():
                await stream.close()
                return
            stream.on_state = lambda state, s=stream: self._on_stream_state(session_id, s, state)
            stream.on_speaking = lambda active, s=stream: self._on_stream_speaking(
                session_id, s, active
            )
            stream.on_clip_released = self._release_clip
            if mic_enabled():
                stream.accept_mic = True
                stream.on_remote_audio = lambda track, s=stream: self._on_remote_audio(
                    session_id, s, track
                )
            # Registered before negotiating so that a disconnect or a newer
            # offer arriving meanwhile finds it and closes it.
            self._streams[session_id] = stream
            answer = await asyncio.wait_for(
                stream.handle_offer(sdp), timeout=WEBRTC_NEGOTIATE_TIMEOUT_SECS
            )
            if not current() or self._streams.get(session_id) is not stream:
                # Superseded while it was gathering. Normally whatever
                # superseded it has already unregistered it; make sure, so a
                # closed stream is never left as the session's stream.
                if self._streams.get(session_id) is stream:
                    self._streams.pop(session_id, None)
                    self._webrtc_live.discard(session_id)
                self._mic_drop(session_id, stream)
                await stream.close()
                return
            await self.send_message(session_id, {"type": "webrtc_answer", "sdp": answer})
        except Exception as e:
            logger.warning(f"WebRTC negotiation failed [{session_id}]: {type(e).__name__}: {e}")
            if stream is not None:
                if self._streams.get(session_id) is stream:
                    self._streams.pop(session_id, None)
                self._mic_drop(session_id, stream)
                await stream.close()
            if current():
                # Not the exception text: it can carry SDP and addresses.
                await self._refuse_webrtc(session_id, "Could not start the WebRTC stream.")

    async def handle_webrtc_close(self, session_id: str) -> None:
        """The client closed its peer connection; release ours."""
        stream = self._streams.get(session_id)
        if stream is not None:
            await self._stream_down(session_id, stream)

    async def close_all_streams(self) -> None:
        """Close every peer connection (server shutdown)."""
        self._webrtc_live.clear()
        for session_id in list(self._resends):
            self._cancel_resend(session_id)
        for session_id in list(self._mics):
            self._mic_drop(session_id)
        streams, self._streams = list(self._streams.values()), {}
        for stream in streams:
            self._discard_stream_clips(stream)
        await asyncio.gather(*(s.close() for s in streams), return_exceptions=True)

    # ── the microphone, over the stream ───────────────────────────────────────
    #
    # On the WebRTC transport the browser can send its microphone up the same
    # peer connection, and then the turns are decided here rather than by the
    # browser's loudness detector: Silero VAD finds the speech and the Smart
    # Turn model judges whether the speaker has finished (turn_taking.py).
    # The server listens only between `mic active:true` and `active:false`,
    # and only while the stream is live.

    def _on_remote_audio(self, session_id: str, stream: AvatarStream, track) -> None:
        """The stream's peer connection carries a microphone track."""
        if self._streams.get(session_id) is not stream:
            return
        mic = self._mic_for(session_id, stream)
        if mic.reader is not None:
            return
        # Read from now on, listening or not: aiortc decodes whatever
        # arrives into an unbounded queue, so a track nobody reads is a leak
        # any client can cause. Until `active:true` every frame is dropped
        # as it comes off that queue.
        mic.reader = MicReader(track)
        # Not in _background: that set is for work that finishes, and this
        # runs for as long as the peer connection does. `mic` holds it.
        task = mic.reader_task = asyncio.create_task(mic.reader.run(), name=f"mic-{session_id}")

        def _ended(t: asyncio.Task) -> None:
            # The track ended under a stream that is still up (cancellation
            # is our own teardown, which does its own telling).
            if t.cancelled() or self._mics.get(session_id) is not mic:
                return
            owed = mic.unacked + (1 if mic.listening else 0)
            mic.wanted = False
            self._mic_stop(session_id, mic)
            if owed:
                self._spawn_bg(self._mic_ack(session_id, mic, False, extra=owed - mic.unacked))

        task.add_done_callback(_ended)

    def _mic_for(self, session_id: str, stream: AvatarStream) -> _Mic:
        mic = self._mics.get(session_id)
        if mic is None or mic.stream is not stream:
            if mic is not None:
                self._mic_drop(session_id)
            mic = self._mics[session_id] = _Mic(stream)
        return mic

    async def handle_mic(self, session_id: str, active) -> None:
        """
        `{"type":"mic","active":...}`: the client attached its microphone to
        the stream and wants the server to take the turns, or took it away.
        Always answered with a `mic_state`, sooner or later; never raises.
        """
        if session_id not in self.session_data:
            return
        mic = self._mics.get(session_id)
        if active is not True:
            if mic is not None:
                mic.wanted = False
                self._mic_stop(session_id, mic)
                # First whatever `active:true` was still waiting for its
                # answer, then this frame's own.
                await self._mic_ack(session_id, mic, False, extra=1)
            else:
                await self.send_message(session_id, {"type": "mic_state", "listening": False})
            return

        stream = self._streams.get(session_id)
        if stream is None or not mic_enabled():
            # No peer connection to hear it on (or none wanted here): say so,
            # and the client keeps its own detector.
            await self.send_message(session_id, {"type": "mic_state", "listening": False})
            return
        mic = self._mic_for(session_id, stream)
        mic.wanted = True
        mic.unacked += 1
        if mic.listening:
            await self._mic_ack(session_id, mic, True)
            return
        await self._mic_start(session_id)

    async def _mic_ack(self, session_id: str, mic: _Mic, listening: bool, extra: int = 0) -> None:
        """Answer every `active:true` still waiting, plus `extra` more frames."""
        count, mic.unacked = mic.unacked + extra, 0
        for _ in range(count):
            await self.send_message(session_id, {"type": "mic_state", "listening": listening})

    async def _mic_start(self, session_id: str) -> None:
        """
        Start listening, if everything it takes is in place: the client
        asked, the stream is live, and its offer carried a microphone. Called
        whenever one of those changes, in whatever order they arrive.
        """
        mic = self._mics.get(session_id)
        if mic is None or not mic.wanted or mic.listening or mic.starting:
            return
        if self._live_stream(session_id) is not mic.stream:
            # Asked before the media path was up. Not refused: answered when
            # the stream goes live — or, if it never does, by _stream_down.
            return
        if mic.reader is None or mic.reader.ended:
            # A live stream with no microphone on it: the offer's audio was
            # receive-only, so there is nothing to listen to.
            mic.wanted = False
            await self._mic_ack(session_id, mic, False)
            return

        def current() -> bool:
            return (
                self._mics.get(session_id) is mic
                and mic.wanted
                and self._live_stream(session_id) is mic.stream
                and mic.reader is not None
                and not mic.reader.ended
            )

        mic.starting = True
        detector: Optional[TurnDetector] = None
        try:
            interval = settings.STT_PARTIAL_INTERVAL_SECS if self._partials_enabled() else 0.0
            # Loads the two models: milliseconds, but off the loop.
            detector = await TurnDetector.create(progress_interval_secs=interval)
        except Exception as e:
            logger.warning(f"Could not start turn detection [{session_id}]: {e}")
        finally:
            mic.starting = False
        if detector is None or not current():
            if detector is not None:
                await detector.close()
            if self._mics.get(session_id) is mic and mic.wanted and detector is None:
                mic.wanted = False
                await self._mic_ack(session_id, mic, False)
            return

        detector.on_speech_start = lambda: self._mic_speech_start(session_id, mic)
        detector.on_speech_paused = lambda audio: self._mic_speech_paused(session_id, mic, audio)
        detector.on_speech_resumed = lambda: self._mic_discard_speculative(mic)
        detector.on_speech_progress = lambda audio: self._mic_speech_progress(
            session_id, mic, audio
        )
        detector.on_speech_end = lambda audio: self._mic_speech_end(session_id, mic, audio)
        # The avatar may be mid-sentence already.
        detector.set_avatar_speaking(bool(mic.stream.speaking))
        mic.detector = detector
        mic.listening = True
        assert mic.reader is not None
        mic.reader.listen(detector)
        logger.info(f"Listening to the stream microphone [{session_id}]")
        await self._mic_ack(session_id, mic, True)

    def _mic_stop(self, session_id: str, mic: _Mic) -> None:
        """
        Stop listening, now, and discard everything of the microphone's that
        the user has not yet seen as words: the utterance in progress, audio
        kept from one that closed too early, and an utterance that closed
        but whose transcript has not been sent. The client has been (or is
        about to be) told the server is not listening; a sentence appearing
        and being answered after that is the microphone still at work.

        A turn whose transcript HAS been sent is a turn like any other and
        is not touched. The track keeps being read (and dropped).
        """
        mic.listening = False
        mic.open = False
        mic.carry = None
        if mic.reader is not None:
            mic.reader.stop_listening()
        self._mic_discard_speculative(mic)
        turn, mic.turn, mic.turn_audio = mic.turn, None, None
        if turn is not None and not turn.done():
            if self._active_turns.get(session_id) is turn:
                self._active_turns.pop(session_id, None)
            # It has said nothing and committed nothing, so there is nothing
            # to flush or record: withdrawing it is just this. (Whisper
            # cannot be stopped; its result is dropped.)
            turn.cancel()
            # The client is holding that utterance as "being transcribed".
            # Told in the background (this cannot await), and only if the
            # socket is still the one that utterance was reported to: a
            # reconnect stops the microphone too, and the new tab never
            # heard of it.
            ws = self.active_connections.get(session_id)

            async def _take_back() -> None:
                if ws is not None and self.active_connections.get(session_id) is ws:
                    await self.send_message(session_id, _SPEECH_IGNORED)

            self._spawn_bg(_take_back())
        if mic.partial is not None:
            mic.partial.cancel()
            mic.partial = None
        detector, mic.detector = mic.detector, None
        if detector is not None:
            self._spawn_bg(detector.close())

    def _mic_drop(self, session_id: str, stream: Optional[AvatarStream] = None) -> int:
        """
        Forget the session's microphone (its stream is going). With `stream`,
        only if it is that stream's. Returns how many `mic_state
        listening:false` the client is owed: one for each `active:true` it
        was still waiting on, or one — unrequested — if the server had been
        listening.
        """
        mic = self._mics.get(session_id)
        if mic is None or (stream is not None and mic.stream is not stream):
            return 0
        self._mics.pop(session_id, None)
        was_on = mic.unacked or (1 if mic.listening else 0)
        mic.wanted = False
        mic.unacked = 0
        self._mic_stop(session_id, mic)
        # Not from inside itself: the detector's callbacks run on the reader
        # task, and one of them can be what brought the session down (a send
        # that failed). Cancelling it there would abort that teardown at its
        # next await. It ends by itself when the stream closes the track.
        if mic.reader_task is not None and mic.reader_task is not asyncio.current_task():
            mic.reader_task.cancel()
        return was_on

    def _mic_live(self, session_id: str, mic: _Mic) -> bool:
        return self._mics.get(session_id) is mic and mic.listening

    @staticmethod
    def _partials_enabled() -> bool:
        mode = (settings.STT_PARTIALS or "").strip().lower()
        if mode == "on":
            return True
        # "auto": a partial is a whole Whisper pass over the utterance so
        # far. On a GPU that is a fraction of the interval between them; on
        # CPU it is longer than the interval, and the final would queue
        # behind it.
        return mode == "auto" and stt_service.on_gpu

    async def _mic_transcribe(self, session_id: str, mic: _Mic, audio) -> str:
        """
        Transcribe `audio` in the session's language — after whatever of this
        session's is still inside Whisper, never beside it.
        """
        while self._mic_transcribing(mic):
            await asyncio.wait({mic.stt_job})
        # Otherwise Whisper assumes English and garbles everything else.
        language = self.session_data.get(session_id, {}).get("language", "en")
        job = mic.stt_job = self._spawn_bg(stt_service.transcribe(audio, language=language))
        # Whoever waits below may be gone by the time it fails.
        job.add_done_callback(lambda t: t.cancelled() or t.exception())
        # Shielded: whoever is waiting may be cancelled (the speaker carried
        # on, or barged in), but the call itself runs to the end regardless,
        # and the next one has to be able to see that it is still running.
        return await asyncio.shield(job)

    def _mic_discard_speculative(self, mic: _Mic) -> None:
        task, mic.speculative = mic.speculative, None
        if task is not None:
            task.cancel()

    @staticmethod
    def _mic_transcribing(mic: _Mic) -> bool:
        return mic.stt_job is not None and not mic.stt_job.done()

    @staticmethod
    def _mic_whole(mic: _Mic, audio):
        """
        `audio` of the utterance in progress, behind whatever was kept of
        the one it continues. Nothing is put between them: the kept part
        ends with the VAD's stop window and the new one begins with its
        pre-roll, which is the pause the speaker actually left.
        """
        if mic.carry is None:
            return audio
        # The detector was told to cut this utterance where the two reach
        # the cap together; this is the same cap, to the sample.
        return np.concatenate([mic.carry, audio])[: int(MAX_UTTERANCE_SECS * SAMPLE_RATE)]

    def _mic_turn_over(self, mic: _Mic, task: asyncio.Task) -> None:
        """`task` no longer holds an utterance the microphone could take back."""
        if mic.turn is task:
            mic.turn = None
            mic.turn_audio = None

    async def _mic_speech_start(self, session_id: str, mic: _Mic) -> None:
        if not self._mic_live(session_id, mic):
            return
        mic.utterance += 1
        mic.open = True
        self._mic_discard_speculative(mic)
        turn = mic.turn
        if turn is not None and not turn.done() and self._active_turns.get(session_id) is turn:
            # The previous utterance was closed too early: its turn has not
            # even sent a transcript and the speaker is talking again. (In
            # smart mode that is any two sentences with less than the
            # transcription time between them.) Interrupting it like a reply
            # would throw the first sentence away and answer the second on
            # its own. Its audio is kept instead and this utterance is added
            # to it — one transcription, one turn, one message. Only the
            # work is discarded: the turn's task, and the Whisper pass it was
            # waiting on.
            #
            # A turn that HAS sent its transcript is not here (mic.turn is
            # cleared before that frame goes out): the user has seen those
            # words, and prepending them again would say them twice. That is
            # an ordinary barge-in, below.
            mic.carry, mic.turn, mic.turn_audio = mic.turn_audio, None, None
            self._active_turns.pop(session_id, None)
            turn.cancel()
            if mic.detector is not None:
                # The cap is on what will be transcribed, which is both.
                kept = len(mic.carry) / SAMPLE_RATE
                mic.detector.set_max_utterance_secs(max(0.0, MAX_UTTERANCE_SECS - kept))
        await self.send_message(session_id, {"type": "user_speech", "state": "started"})
        # Barge-in is decided here in this mode: the client does not send
        # `interrupt` for speech the server can hear for itself. Same path,
        # same order — commit what was said, flush the stream, cancel. (After
        # a continuation there is no turn left to cancel and no reply was
        # cut, so nothing is announced.)
        await self.interrupt_active_turn(session_id)

    async def _mic_speech_paused(self, session_id: str, mic: _Mic, audio) -> None:
        """
        The VAD went quiet and the turn model is about to be asked. Begin the
        final transcription now: if the verdict is "finished", the transcript
        is already under way by the time it arrives.
        """
        if not self._mic_live(session_id, mic):
            return
        self._mic_discard_speculative(mic)
        # Not for a turn that is about to be refused anyway. (A continuation
        # is not charged again, so it is not refused either.)
        if mic.carry is None and not self._allow_turn(session_id, consume=False):
            return
        # This is a guess: if the speaker carries on, the pass is wasted, and
        # a Whisper call cannot be stopped. On a GPU a wasted pass is over in
        # a fraction of the pause that follows. On CPU it takes seconds, in
        # which it would hold up real work — this session's own final behind
        # it, or another session's beside it on the same cores. So there it
        # is started only when it is the only transcription in the process,
        # and never waits in line: the final then starts when the turn does.
        if (self._mic_transcribing(mic) or not stt_service.idle) and not stt_service.on_gpu:
            return
        task = mic.speculative = self._spawn_bg(
            self._mic_transcribe(session_id, mic, self._mic_whole(mic, audio))
        )
        # Retrieved by the turn if it is used; otherwise nobody looks.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def _mic_speech_progress(self, session_id: str, mic: _Mic, audio) -> None:
        if not self._mic_live(session_id, mic) or not mic.open:
            return
        # A partial is the lowest priority there is. It never queues — not
        # behind this session's own transcription, and not for one of the
        # shared slots, where it could sit ahead of somebody's final.
        if (mic.partial is not None and not mic.partial.done()) or mic.speculative is not None:
            return
        if self._mic_transcribing(mic) or stt_service.busy:
            return
        if mic.carry is None and not self._allow_turn(session_id, consume=False):
            return
        mic.partial = self._spawn_bg(
            self._mic_partial(session_id, mic, mic.utterance, self._mic_whole(mic, audio))
        )

    async def _mic_partial(self, session_id: str, mic: _Mic, utterance: int, audio) -> None:
        try:
            text = (await self._mic_transcribe(session_id, mic, audio) or "").strip()
        except Exception as e:
            logger.debug(f"Partial transcription failed [{session_id}]: {e}")
            return
        # Checked after the wait, and with no await before the send: once the
        # utterance has ended its final transcript is on the way, and a
        # running one arriving behind it would overwrite it on screen.
        if text and self._mic_live(session_id, mic) and mic.open and mic.utterance == utterance:
            await self.send_message(
                session_id, {"type": "transcription", "text": text, "final": False}
            )

    async def _mic_speech_end(self, session_id: str, mic: _Mic, audio) -> None:
        """The detector closed an utterance: answer it."""
        if not self._mic_live(session_id, mic):
            return
        mic.open = False
        speculative, mic.speculative = mic.speculative, None
        continued = mic.carry is not None
        if continued:
            audio, mic.carry = self._mic_whole(mic, audio), None
            if mic.detector is not None:
                mic.detector.set_max_utterance_secs(MAX_UTTERANCE_SECS)
        # A continuation is the turn that was already counted, resumed.
        if not continued and not self._allow_turn(session_id):
            if speculative is not None:
                speculative.cancel()
            await self._reject_rate_limited(session_id)
            await self.send_message(session_id, _SPEECH_IGNORED)
            return
        # Normally a no-op — speech starting already interrupted. But a typed
        # message can have started a turn while the user was talking. Before
        # "stopped" rather than after it: to the client an `interrupted` ends
        # whatever turn is pending, and from "stopped, turn" on that is this
        # one.
        await self.interrupt_active_turn(session_id)
        await self.send_message(
            session_id, {"type": "user_speech", "state": "stopped", "reason": "turn"}
        )
        # Looked at again, after the awaits above and with none between here
        # and the turn existing: `mic active:false` is handled on another
        # task (the receive loop) and can have landed during either send.
        # Nothing from the microphone starts a turn once it is off.
        if not self._mic_live(session_id, mic):
            if speculative is not None:
                speculative.cancel()
            # "stopped, turn" has gone out; take it back.
            await self.send_message(session_id, _SPEECH_IGNORED)
            return
        task = self._spawn_turn(session_id, self._mic_turn(session_id, mic, audio, speculative))
        mic.turn, mic.turn_audio = task, audio
        # Also when it is cancelled before it ever runs, which no `finally`
        # inside it would see.
        task.add_done_callback(lambda t: self._mic_turn_over(mic, t))

    async def _mic_turn(
        self, session_id: str, mic: _Mic, audio, speculative: Optional[asyncio.Task]
    ) -> None:
        """
        One utterance from the stream microphone, as a turn: transcribe it
        and hand the text to the same pipeline as every other turn.
        """
        try:
            await self.send_message(
                session_id,
                {"type": "status", "message": "Transcribing audio…", "stage": "transcription"},
            )
            text: Optional[str] = None
            if speculative is not None:
                try:
                    text = await speculative
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Speculative transcription failed [{session_id}]: {e}")
            if text is None:
                text = await self._mic_transcribe(session_id, mic, audio)
            text = (text or "").strip()

            # The point of no return, decided with no await between the
            # decision and the send: from here the utterance is the user's
            # words on their screen (or known to have none), and neither the
            # listener stopping nor the speaker carrying on takes it back.
            self._mic_turn_over(mic, asyncio.current_task())

            if not text:
                # The VAD heard a voice and Whisper found no words in it.
                # Not an error worth showing: in hands-free that is a cough.
                await self.send_message(session_id, _SPEECH_IGNORED)
                return

            await self.send_message(session_id, {"type": "transcription", "text": text})
            await self._handle_text_input_inner(session_id, text)

        except asyncio.CancelledError:
            raise  # barge-in: the user started speaking again
        except LLMError as e:
            logger.error(f"Mic turn LLM error [{session_id}]: {type(e).__name__}: {e}")
            await self.send_message(session_id, {"type": "error", "message": _llm_error_message(e)})
        except Exception as e:
            logger.error(f"Mic turn error [{session_id}]: {e}")
            await self.send_message(
                session_id, {"type": "error", "message": "Audio processing failed"}
            )
            await self.send_message(session_id, _SPEECH_IGNORED)

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
            await self.disconnect(session_id, ws)

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

    def _spawn_turn(self, session_id: str, coro) -> asyncio.Task:
        """
        Register `coro` as the session's active turn and schedule it WITHOUT
        awaiting. This is the heart of barge-in: the WebSocket receive loop
        must return to `receive_json()` immediately so it can observe the next
        client message (a new turn or an explicit stop) while this one streams.

        A done-callback clears the slot and logs unhandled errors. We never
        await the task here, so an exception inside it can't crash the WS loop.

        Taking the slot is atomic. Every caller interrupts the previous turn
        first, but that interruption awaits a send, and turns are started
        from two tasks — the receive loop, and the microphone's reader. The
        other one can start a turn during that send; storing over it would
        leave it running with nothing able to stop it (not barge-in, not the
        stop button, not disconnect) and two turns writing one history. So
        whatever is in the slot NOW is cut, the same way and in the same
        order as any interruption, with no await before the new task is in.
        """
        previous = self._active_turns.get(session_id)
        if previous is not None and not previous.done() and self._cut_active_turn(session_id):
            # Created before the new turn's task, so it is sent first.
            self._spawn_bg(self.send_message(session_id, _INTERRUPTED))
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
        return task

    async def handle_audio_input(self, session_id: str, audio_data: str):
        """
        Non-blocking dispatcher: interrupt any prior turn, then run STT +
        the full chat turn inside a tracked task so the WS loop stays free to
        receive the next message (enabling barge-in even mid-transcription).
        """
        # Checked on the encoded string, before decoding: the limit exists so
        # an oversized frame is never expanded into memory in the first place.
        if len(audio_data) > settings.WS_MAX_AUDIO_BYTES * 4 // 3 + 4:
            await self.send_message(
                session_id, {"type": "error", "message": "Audio payload too large"}
            )
            return
        if not self._allow_turn(session_id):
            await self._reject_rate_limited(session_id)
            return
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
        except LLMError as e:
            logger.error(f"Audio turn LLM error [{session_id}]: {type(e).__name__}: {e}")
            await self.send_message(session_id, {"type": "error", "message": _llm_error_message(e)})
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

        if not self._allow_turn(session_id):
            await self._reject_rate_limited(session_id)
            return
        await self.interrupt_active_turn(session_id)
        self._spawn_turn(session_id, self._handle_text_input_inner(session_id, text))

    async def _handle_text_input_inner(self, session_id: str, text: str):
        started_at = datetime.now(timezone.utc)
        # Filled in by the producer as tokens arrive, so that if this turn is
        # cancelled we still know what the model had said so far.
        progress: dict = {"text": "", "committed": False}
        messages: list[dict] = []

        def commit_reply(reply: str) -> None:
            """
            Record the assistant's reply in the history and the database.

            Called the moment the model finishes — NOT after the last clip
            has been delivered. It used to wait for the whole turn, which
            left a window after the client had everything: a follow-up sent
            straight away cancelled this turn mid-save, so the reply was
            stored after the next question, or twice, or not at all.

            Synchronous on purpose (the write itself is handed off): there is
            no await in here for a cancellation to land on.
            """
            reply = (reply or "").strip()
            if not reply or progress["committed"]:
                return
            progress["committed"] = True
            messages.append({"role": "assistant", "content": reply})
            latency = (datetime.now(timezone.utc) - started_at).total_seconds()
            self._spawn_bg(self._persist_message(session_id, "assistant", reply, latency=latency))

        def commit_so_far() -> None:
            commit_reply(progress.get("text") or "")

        self._pending_commits[session_id] = commit_so_far

        try:
            data = self.session_data.get(session_id, {})
            data["last_activity"] = started_at
            # The session's own list, mutated in place: whatever happens to
            # this turn, the history reflects it. (It used to be reassigned
            # only on success, so a failed turn's trim was undone and the same
            # messages were summarised again on the next one.)
            messages = data.setdefault("messages", [])
            messages.append({"role": "user", "content": text})

            # Cap the conversation window. The system prompt is passed
            # separately to the LLM so we don't need to keep it in `messages`.
            dropped = _trim_window(messages)
            if dropped:
                # Roll what was lost into the running summary — in the
                # background. Awaiting it here put a whole extra LLM round
                # trip in front of the reply; the summary is context for
                # LATER turns and loses nothing by arriving a moment after
                # this one starts.
                self._schedule_memory_roll(session_id, dropped)

            system_prompt = _compose_system_prompt(
                data.get("system_prompt"), data.get("memory_summary")
            )

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
                    self._llm_producer(
                        session_id,
                        _llm_view(messages),
                        system_prompt,
                        sentence_queue,
                        progress,
                        commit_reply,
                    ),
                    self._animate_from_queue(session_id, sentence_queue),
                    return_exceptions=True,
                )

            # Check for errors
            for r in results:
                if isinstance(r, Exception):
                    raise r

            # Normally a no-op: the producer committed when the model finished.
            commit_reply(results[0] if isinstance(results[0], str) else "")

        except asyncio.CancelledError:
            # Interrupted (barge-in, stop button, disconnect) before the model
            # finished. Keep what the avatar had already said: it used to
            # vanish from both the context and the database, so the model had
            # no idea it had started an answer and the saved transcript showed
            # a question with no reply.
            commit_reply(progress.get("text") or "")
            raise
        except LLMError as e:
            logger.error(f"Text turn LLM error [{session_id}]: {type(e).__name__}: {e}")
            await self.send_message(session_id, {"type": "error", "message": _llm_error_message(e)})
        except Exception as e:
            logger.error(f"Text error [{session_id}]: {e}")
            await self.send_message(session_id, {"type": "error", "message": "Processing failed"})
        finally:
            if self._pending_commits.get(session_id) is commit_so_far:
                self._pending_commits.pop(session_id, None)

    def _schedule_memory_roll(self, session_id: str, dropped: list[dict]) -> None:
        """
        Summarise `dropped` in the background, after any roll already running
        for this session — each roll folds the previous summary into the next,
        so two running at once would both start from the same one and the
        slower would overwrite the faster.
        """
        data = self.session_data.get(session_id)
        if data is None:
            return
        previous: Optional[asyncio.Task] = data.get("_memory_task")

        async def _chain() -> None:
            if previous is not None and not previous.done():
                await asyncio.gather(previous, return_exceptions=True)
            await self._roll_memory(session_id, dropped)

        data["_memory_task"] = self._spawn_bg(_chain())

    # ── streaming pipeline ────────────────────────────────────────────────────

    async def _llm_producer(
        self,
        session_id: str,
        messages: List[dict],
        system_prompt: Optional[str],
        queue: "asyncio.Queue[Optional[str]]",
        progress: Optional[dict] = None,
        on_complete=None,
    ) -> str:
        """
        Stream LLM tokens, emit `token` events to the frontend, and push
        speakable chunks into the queue as boundaries are reached.

        Latency strategy: the FIRST chunk is split on clause boundaries with
        a low length bar so TTS+animation can start within the opening few
        words; every chunk after that uses sentence boundaries for smoother
        prosody and fewer synthesis calls. Returns the complete response text.

        `progress["text"]` tracks the reply so far, for the caller to keep if
        this turn is cancelled. `on_complete(text)` is called as soon as the
        model has finished, before the remaining sentences are spoken.
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
                    if progress is not None:
                        progress["text"] = full_text

                    # Send live token to frontend
                    await self.send_message(session_id, {"type": "token", "token": token})

                    sep = _SENTENCE_RE if first_chunk_sent else _CLAUSE_RE
                    min_len = _MIN_SENTENCE_LEN if first_chunk_sent else _MIN_FIRST_CHUNK_LEN
                    chunks, buf = _drain_chunks(buf, sep, min_len, _MAX_CHUNK_CHARS)
                    for chunk in chunks:
                        await queue.put(chunk)
                        first_chunk_sent = True

            if on_complete is not None:
                on_complete(full_text)

            # Flush any remaining tail — it's the end of the reply, so speak it
            # even if it's short.
            remainder = buf.strip()
            if remainder:
                await queue.put(remainder)

        except asyncio.CancelledError:
            # The consumer is being cancelled with us, so nothing will ever
            # read this queue again. Do NOT wait on it: a `finally` that awaited
            # put(None) here blocked forever whenever the queue was full (any
            # reply long enough to get ahead of the animation), leaving the
            # turn task and the provider's HTTP stream open for good.
            raise
        except Exception as e:
            logger.error(f"LLM producer error [{session_id}]: {e}")
            # The consumer is still running (gather does not cancel siblings),
            # so it will make room; tell it there is nothing more to wait for.
            await queue.put(None)
            raise

        # Signal end so the consumer finishes once it has spoken what is queued.
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
        Consume sentences from the queue, run TTS + animation for each, and
        stream `video_chunk` events to the frontend as they complete.

        Two stages joined by a small queue, so speech for sentence N+1 is
        synthesised WHILE sentence N is being animated. They used to run
        strictly one after the other — TTS, then animation, then upload, then
        TTS again — which left the synthesiser idle for the whole of every
        animation and put its latency into the gap between sentences.
        """
        data = self.session_data.get(session_id, {})
        avatar_image = data.get("avatar_image_local")
        speaker_wav: Optional[str] = data.get("voice_wav")
        language: str = data.get("language", "en")

        await self.send_message(
            session_id,
            {
                "type": "video_chunk_start",
                "total_chunks": -1,  # streaming mode — total unknown up front
            },
        )

        if not avatar_image:
            # Nothing to animate. Drain so the producer never blocks, then
            # close the turn out explicitly — returning silently left the
            # client showing its "generating" spinner indefinitely.
            logger.warning(f"No avatar image for session {session_id}")
            unspoken = 0
            while await queue.get() is not None:
                unspoken += 1
            await self.send_message(session_id, {"type": "video_chunk_end", "sent_chunks": 0})
            # Only when there was something to say: if the model itself failed
            # the caller reports that, and a second error would bury it.
            if unspoken:
                await self.send_message(
                    session_id,
                    {"type": "error", "message": "This avatar's image is unavailable."},
                )
            return

        session_dir = _private_session_dir(session_id)
        # (sentence, audio path, synthesis result), or None for end-of-stream.
        audio_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        # Every temp file this turn creates, so cancellation at any point
        # (including between the two stages) leaves nothing behind.
        created: set[Path] = set()
        sent_chunks = 0
        # A new turn starts with the choice of transport open again.
        self._url_turns.discard(session_id)

        async def synthesize_stage() -> None:
            try:
                while True:
                    sentence = await queue.get()
                    if sentence is None:
                        break
                    if session_id not in self.active_connections:
                        continue  # client gone: keep draining, do no work
                    tmp_audio = session_dir / f"{uuid.uuid4().hex[:12]}_audio.wav"
                    created.add(tmp_audio)
                    try:
                        with span(
                            "tts.synthesize",
                            **{
                                "chars": len(sentence),
                                "lang": language,
                                "cloned": bool(speaker_wav),
                            },
                        ):
                            synth = await tts_service.synthesize(
                                text=sentence,
                                output_path=str(tmp_audio),
                                speaker_wav=speaker_wav,
                                language=language,
                            )
                    except Exception as e:
                        logger.error(f"TTS failed for a sentence [{session_id}]: {e}")
                        continue
                    await audio_queue.put((sentence, tmp_audio, synth))
            except asyncio.CancelledError:
                raise  # the animate stage is cancelled too; see _llm_producer
            except Exception:
                await audio_queue.put(None)
                raise
            await audio_queue.put(None)

        async def animate_stage() -> None:
            nonlocal sent_chunks
            # Only announce a TTS fallback once per turn — a notice on every
            # sentence would be noise.
            fallback_announced = False
            while True:
                item = await audio_queue.get()
                if item is None:
                    break
                sentence, tmp_audio, synth = item
                tmp_video = session_dir / f"{tmp_audio.stem.split('_')[0]}_video.mp4"
                created.add(tmp_video)
                on_stream = False
                try:
                    if session_id not in self.active_connections:
                        continue

                    await self.send_message(
                        session_id,
                        {"type": "status", "message": "Animating…", "stage": "animation"},
                    )

                    # Notify the client exactly once if the configured engine
                    # bailed and we ended up serving a different voice.
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

                    with span("avatar.animate", **{"chunk": sent_chunks}):
                        await avatar_animator.animate(
                            avatar_image_path=avatar_image,
                            audio_path=str(tmp_audio),
                            output_path=str(tmp_video),
                        )

                    # Decided per clip, at the moment it is ready — but only
                    # in one direction within a turn. A stream that drops
                    # costs nothing but the upload it would have skipped, so
                    # stream -> clips can happen at any sentence. The reverse
                    # cannot: once one clip of this turn has gone by URL
                    # (sent here, or re-sent from a stream that went down)
                    # the rest of the turn does too, and a stream that comes
                    # up meanwhile is used from the NEXT turn. URL clips wait
                    # in the browser's own queue while a stream plays the
                    # instant it is fed, so a streamed sentence after a URL
                    # one would play underneath the clips still queued ahead
                    # of it — silenced, and never heard.
                    duration_ms: Optional[int] = None
                    stream = (
                        None if session_id in self._url_turns else self._live_stream(session_id)
                    )
                    if stream is not None:
                        clip = _StreamClip(sent_chunks, sentence, tmp_video)
                        try:
                            with span("webrtc.enqueue", **{"chunk": sent_chunks}):
                                duration_ms = await stream.enqueue_clip(str(tmp_video), clip)
                        except ClipDropped:
                            # Flushed while it was being prepared: the user
                            # interrupted. Not to be played by either route.
                            continue
                        except Exception as e:
                            # Nothing of the clip reached the stream (it is
                            # queued whole or not at all), so sending it the
                            # old way cannot double it up.
                            logger.warning(
                                f"WebRTC enqueue failed, sending chunk {sent_chunks} "
                                f"as a clip [{session_id}]: {e}"
                            )
                            if not stream.connected:
                                # Not this clip's fault: the stream is
                                # finished. Take it down now rather than when
                                # its state callback gets to run, so the
                                # clips it still held are already on their
                                # way back by the time this one is sent
                                # behind them. (No-op if that has happened.)
                                await self._stream_down(session_id, stream)

                    if duration_ms is not None:
                        # The stream holds the file now, until it has played
                        # the clip or given it back (see _StreamClip). No
                        # await between enqueue_clip returning and here.
                        created.discard(tmp_video)
                        on_stream = True
                        await self.send_message(
                            session_id,
                            {
                                "type": "video_chunk",
                                "chunk_index": sent_chunks,
                                "total_chunks": -1,
                                "transport": "webrtc",
                                "text": sentence,
                                "duration_ms": duration_ms,
                            },
                        )
                    else:
                        self._url_turns.add(session_id)
                        video_url = await self._upload_clip(session_id, sent_chunks, tmp_video)
                        await self._url_clip_may_follow(session_id)
                        await self.send_message(
                            session_id,
                            {
                                "type": "video_chunk",
                                "chunk_index": sent_chunks,
                                "total_chunks": -1,
                                "video_url": video_url,
                                "text": sentence,
                            },
                        )
                    sent_chunks += 1
                    logger.info(f"Chunk {sent_chunks} ready [{session_id}]")

                except Exception as e:
                    logger.error(f"Chunk {sent_chunks} failed [{session_id}]: {e}")

                finally:
                    for f in (tmp_audio,) if on_stream else (tmp_audio, tmp_video):
                        f.unlink(missing_ok=True)
                        created.discard(f)

        try:
            await asyncio.gather(synthesize_stage(), animate_stage())
        finally:
            for f in created:
                f.unlink(missing_ok=True)

        await self.send_message(
            session_id,
            {
                "type": "video_chunk_end",
                "sent_chunks": sent_chunks,
            },
        )

        if sent_chunks == 0:
            await self.send_message(
                session_id,
                {"type": "error", "message": "Avatar animation failed for all sentences."},
            )

    # ── helpers ───────────────────────────────────────────────────────────────

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
        voice_user = entry.get("user_id")
        if not session_user or voice_user != session_user:
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
                            await self.disconnect(session_id, ws)
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
