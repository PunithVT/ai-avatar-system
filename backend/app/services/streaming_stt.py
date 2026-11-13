"""
Streaming speech-to-text for the microphone that arrives over WebRTC.

Whisper transcribes an utterance once it is over, and a "partial" there is a
whole pass over everything said so far. A streaming recognizer takes the audio
as it arrives instead: the words so far cost nothing extra to read out, and
the final text is ready a fraction of a second after the last word, because
all but the last half second has already been decoded.

The engine is sherpa-onnx running NVIDIA's Nemotron 3.5 ASR Streaming 0.6B
(int8, 560 ms chunks). Neither is re-implemented here: this module is the
queueing between an event loop that must never block and a decoder that is
synchronous and CPU-bound.

Like turn_taking.py it knows nothing about the WebSocket manager: PCM goes in,
text comes out.

    session = streaming_stt.open("en")      # None -> use Whisper
    partial = session.feed(pcm)             # per piece of audio, never blocks
    text = await session.finish()           # the final transcript
    session.discard()                       # or: nobody wants it any more

Two bounds keep it honest when more people speak than the machine can decode
for. A new utterance is not taken on while the live audio already queued is
long (MAX_BACKLOG_SECS), and an utterance already taken on is given up as
soon as its own text is running late (MAX_SESSION_LAG_SECS). Either way the
utterance is Whisper's, from the audio its owner still holds.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# The files of an exported sherpa-onnx transducer; all four must be present.
MODEL_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")

# Languages this model transcribes well. From the model card
# (huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b), which separates
# these from a second, "broad coverage" tier (pl, sv, cs, nb, da, bg, fi, hr,
# sk, zh, hu, ro, et) whose accuracy is markedly worse. That tier is left out
# on purpose: Whisper is better there, and a wrong transcript is a wrong
# answer. Anything not listed here keeps using Whisper.
SUPPORTED_LANGUAGES = frozenset(
    {"en", "es", "fr", "it", "pt", "nl", "de", "tr", "ru", "ar", "hi", "ja", "ko", "vi", "uk"}
)

# Silence fed ahead of the utterance. The model needs roughly half a second
# of audio before the first word or it drops that word: the test recording
# cut 0.25 s before its first word comes out as "Birch canoe…", and with this
# much silence in front as "The birch canoe…". The turn detector's pre-roll
# keeps about 0.3 s, which is enough for Whisper and not for this. It costs
# a tenth of a second of decoding, at the start, while the user is speaking.
LEAD_IN_SECS = 0.4

# Silence fed behind the last of the audio before the stream is closed. The
# model emits a word only once it has seen the chunk after it, so without
# this the final word of an utterance is the one most likely to be missing.
TAIL_PADDING_SECS = 0.8

# How much of one session's audio is decoded before the next session gets its
# turn. Small enough that nobody's partials wait long behind a session that
# arrives with a lot at once (an utterance being continued replays what was
# already said); large enough that a turn is more decoding than bookkeeping.
SLICE_SECS = 1.0

# LIVE audio queued and not yet decoded, across every session, past which a
# new utterance is not taken on (it goes to Whisper instead). Decoding runs
# several times faster than real time, so a backlog this long means more
# people are speaking at once than this machine can keep up with, and one
# more would make everyone's transcript later still.
#
# Replayed audio (see StreamingSession.replay) is not in this figure. It is
# one speaker's own past, arriving in a lump of up to half a minute: it says
# nothing about how many people are speaking, it is worked off faster than
# it was spoken, and slicing already limits what it costs anyone else to one
# slice per round. Counted here, a single continued sentence shut every
# other user out of streaming for seconds on a decoder with nothing wrong.
# The alternative — decoding replays only when nobody's live audio is
# waiting — was not taken: it needs a second queue, and it makes the late
# transcript the one of the speaker who has already been cut off once.
MAX_BACKLOG_SECS = 4.0

# How long a session's own live audio may have been waiting for the decoder
# before the session is given up (it fails, and its owner sends the
# utterance to Whisper). The check above is made when an utterance starts,
# when the speaker has contributed nothing yet: any number of people who
# start within the same moment pass it, and nothing after it stopped them
# falling behind together — found out only at the end of each utterance, as
# a final transcript that did not come. So it is also measured per session,
# while the audio is arriving.
#
# In time, not samples: what matters is how late the words are, and a caller
# that hands over several seconds in one piece is not late until the decoder
# has had those seconds and still not got to them. Three seconds is no
# longer "as it is spoken", is about what a Whisper pass over a sentence
# costs — so past it Whisper is the faster way to the text — and is far more
# than one other session's slice or a pause in the decoder accounts for.
MAX_SESSION_LAG_SECS = 3.0

# A session that begins with a replay has that to get through before its
# live audio, which waits meanwhile through no fault of the decoder. For
# every second replayed, this much extra lateness is allowed until the
# session first catches up: a decoder at half real time, twice as slow as
# the one measured here, still clears a replay inside it.
REPLAY_GRACE = 0.5

# The clock the lag is measured on (a name of its own so tests can move it).
_now = time.monotonic


class StreamingSTTError(RuntimeError):
    """The streaming engine could not transcribe this utterance."""


def streaming_mode() -> str:
    """STT_STREAMING, normalised: "sherpa" or "off"."""
    mode = (settings.STT_STREAMING or "").strip().lower()
    # An unrecognised value means off, like the other optional features: a
    # typo leaves the app as it was.
    return mode if mode == "sherpa" else "off"


def _find_model_dir(config_path: str) -> Optional[Path]:
    """Resolved like MUSETALK_PATH: as given, then relative to backend/."""
    for base in (Path(config_path), Path(__file__).resolve().parent.parent.parent / config_path):
        if all((base / name).is_file() for name in MODEL_FILES):
            return base.resolve()
    return None


class StreamingSession:
    """
    One utterance. feed() and discard() are called from the event loop and
    never block; the decoding they cause happens on the provider's thread.
    """

    def __init__(self, provider: "StreamingSTT", language: str) -> None:
        self._provider = provider
        self._language = language
        # One lock for the provider and all its sessions: everything done
        # under it is a few assignments, and one lock cannot be taken in the
        # wrong order.
        self._lock = provider._lock
        # (samples, when they were fed); the time is None for replayed audio.
        self._pending: deque[tuple[np.ndarray, Optional[float]]] = deque()
        # Extra lateness allowed while a replay is being worked off (secs).
        self._grace = 0.0
        # A step for this session is queued on, or running in, the decoder.
        self._scheduled = False
        self._closing = False
        self._discarded = False
        self._error: Optional[BaseException] = None
        self._final: Optional[Future] = None
        # The latest hypothesis, and the one feed() last handed out.
        self._partial = ""
        self._reported = ""
        # The recognizer's stream. Touched only on the decoder thread.
        self._stream: Any = None
        # Samples taken in / samples the recognizer has been given.
        self.fed_samples = 0
        self.decoded_samples = 0

    @property
    def language(self) -> str:
        return self._language

    @property
    def failed(self) -> bool:
        """
        The engine raised on this utterance, or fell too far behind on it;
        its audio must go elsewhere.
        """
        return self._error is not None

    def feed(self, pcm: np.ndarray) -> Optional[str]:
        """
        Queue the next piece of the utterance (int16, 16 kHz, mono, any
        length). Returns the words so far when they have changed since the
        last call, else None — so what it returns is always in order, and
        nothing is returned once finish() or discard() has been called.

        If audio fed earlier has waited longer than MAX_SESSION_LAG_SECS for
        the decoder, the session fails here instead (see `failed`).
        """
        late = 0.0
        with self._lock:
            if self._closing or self._discarded or self._error is not None:
                return None
            if len(pcm):
                now = _now()
                late = self._lag(now)
                if late > MAX_SESSION_LAG_SECS + self._grace:
                    # Now, while the speaker is still talking, rather than
                    # as a timeout after they stop: the owner lets go at its
                    # next frame and Whisper has the whole utterance.
                    self._error = StreamingSTTError(
                        f"the decoder is {late:.1f}s behind on this utterance"
                    )
                    self._drop_pending()
                else:
                    late = 0.0
                    self._pending.append((pcm, now))
                    self.fed_samples += len(pcm)
                    self._provider._backlog += len(pcm)
                    self._kick()
            if not late and self._partial and self._partial != self._reported:
                self._reported = self._partial
                return self._partial
        if late:
            logger.info(f"Streaming STT is {late:.1f}s behind on an utterance; it goes to Whisper")
        return None

    def replay(self, pcm: np.ndarray) -> None:
        """
        Queue audio this utterance opens with that was heard once already,
        by a session that has since been closed (a turn being continued).
        Before any feed(). It is decoded like any other audio, but it is not
        live: it stays out of the shared backlog and is not itself "late",
        and the live audio queued behind it is allowed to be later for it
        (MAX_BACKLOG_SECS and REPLAY_GRACE say why).
        """
        with self._lock:
            if self._closing or self._discarded or self._error is not None or not len(pcm):
                return
            # Only at the head of the line is it a replay. After live audio
            # it would be audio like any other (nothing calls it so).
            stamp = None if not self.fed_samples else _now()
            self._pending.append((pcm, stamp))
            self.fed_samples += len(pcm)
            if stamp is None:
                self._grace += REPLAY_GRACE * len(pcm) / SAMPLE_RATE
            else:
                self._provider._backlog += len(pcm)
            self._kick()

    async def finish(self) -> str:
        """
        No more audio: decode what is queued and return the final transcript
        ("" when there were no words). Raises StreamingSTTError if the engine
        failed at any point in the utterance.
        """
        with self._lock:
            if self._error is not None:
                raise StreamingSTTError(str(self._error)) from self._error
            if self._discarded:
                raise StreamingSTTError("the session was discarded")
            if self._final is None:
                self._closing = True
                self._final = Future()
                self._kick()
            final = self._final
        return await asyncio.wrap_future(final)

    def discard(self) -> None:
        """
        Nobody wants this utterance's text. Queued audio is dropped unheard;
        at most the slice being decoded right now still completes. Idempotent,
        and harmless after finish() has returned.
        """
        with self._lock:
            if self._discarded:
                return
            self._discarded = True
            self._drop_pending()
            final = self._final
        if final is not None and not final.done():
            final.cancel()

    # ── under the lock ───────────────────────────────────────────────────

    def _kick(self) -> None:
        if not self._scheduled:
            self._scheduled = True
            self._provider._submit(self)

    def _lag(self, now: float) -> float:
        """How long the oldest live audio still queued has been waiting."""
        for _, fed_at in self._pending:
            if fed_at is not None:
                return now - fed_at
        return 0.0

    def _drop_pending(self) -> None:
        self._provider._backlog -= sum(
            len(chunk) for chunk, fed_at in self._pending if fed_at is not None
        )
        self._pending.clear()

    def _take_slice(self) -> Optional[np.ndarray]:
        limit = int(SLICE_SECS * SAMPLE_RATE)
        parts: list[np.ndarray] = []
        taken = live = 0
        while self._pending and taken < limit:
            chunk, fed_at = self._pending[0]
            room = limit - taken
            if len(chunk) > room:
                # A long piece (carried audio arrives as one): take what
                # fits and leave the rest at the head of the line.
                chunk = chunk[:room]
                self._pending[0] = (self._pending[0][0][room:], fed_at)
            else:
                self._pending.popleft()
            parts.append(chunk)
            taken += len(chunk)
            if fed_at is not None:
                live += len(chunk)
        self._provider._backlog -= live
        if not self._pending:
            # Caught up: whatever was replayed is behind it, and from here
            # this session is held to the same lateness as any other.
            self._grace = 0.0
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    # ── on the decoder thread ────────────────────────────────────────────

    def _advance(self, recognizer: Any) -> bool:
        """Decode one slice. True when there is more to do for this session."""
        with self._lock:
            if self._discarded or self._error is not None:
                self._scheduled = False
                return False
            samples = self._take_slice()
            # Only when the queue was already empty: audio and the end of
            # the stream are separate steps, so a partial is never computed
            # from audio that finish() has not yet been asked to include.
            closing = self._closing and samples is None

        if self._stream is None:
            self._stream = recognizer.create_stream()
            # Per stream and before any audio: the model is multilingual and
            # is told the language rather than left to guess it.
            self._stream.set_option("language", self._language)
            self._stream.accept_waveform(
                SAMPLE_RATE, np.zeros(int(SAMPLE_RATE * LEAD_IN_SECS), dtype=np.float32)
            )
        stream = self._stream

        if samples is not None:
            stream.accept_waveform(SAMPLE_RATE, _as_float(samples))
            self.decoded_samples += len(samples)
        elif closing:
            stream.accept_waveform(
                SAMPLE_RATE, np.zeros(int(SAMPLE_RATE * TAIL_PADDING_SECS), dtype=np.float32)
            )
            stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = (recognizer.get_result(stream) or "").strip()

        with self._lock:
            # (Failed, here, is feed() having given the session up while
            # this slice was decoding.)
            if self._discarded or self._error is not None:
                self._scheduled = False
                self._stream = None
                return False
            if not closing:
                self._partial = text
                # More audio — or finish() — may have arrived while this
                # slice decoded; whoever brought it saw a step in flight and
                # did not queue another.
                more = bool(self._pending) or self._closing
                self._scheduled = more
                return more
            self._scheduled = False
            self._stream = None
            final = self._final
        _resolve(final, text)
        return False

    def _fail(self, error: BaseException) -> None:
        with self._lock:
            self._error = error
            self._scheduled = False
            self._stream = None
            self._drop_pending()
            final = self._final
        if final is not None and not final.done():
            try:
                final.set_exception(StreamingSTTError(f"{type(error).__name__}: {error}"))
            except Exception:
                pass  # cancelled by whoever stopped waiting


def _as_float(pcm: np.ndarray) -> np.ndarray:
    """What accept_waveform takes: float32 in [-1, 1]."""
    if np.issubdtype(pcm.dtype, np.integer):
        return pcm.astype(np.float32) / 32768.0
    return np.ascontiguousarray(pcm, dtype=np.float32)


def _resolve(final: Optional[Future], text: str) -> None:
    if final is None:
        return
    try:
        final.set_result(text)
    except Exception:
        # Whoever was waiting has gone (a cancelled wait cancels the future).
        pass


class StreamingSTT:
    """
    The streaming recognizer: one model, shared by every session.

    All decoding, for all sessions, runs on ONE thread. Two reasons:

      - sherpa-onnx ships as a compiled library and nothing in the installed
        package says whether decode_stream may run on two streams of one
        recognizer at once. It did, in a trial, give correct text — but with
        no gain worth having (two concurrent utterances took 2.7 s against
        3.4 s back to back), because the model already spreads each decode
        over STT_STREAMING_THREADS cores. One thread needs no such guarantee.
      - the bound that matters is CPU, and this is it: one decode at a time,
        each as wide as the setting allows.

    Sessions share that thread a slice (SLICE_SECS of audio) at a time, in
    turn. Decoding runs several times faster than real time, so a handful of
    people speaking at once all keep up; past that everyone's text falls
    behind together, so MAX_BACKLOG_SECS sends new utterances to Whisper and
    MAX_SESSION_LAG_SECS sends the ones in progress there as they fall behind.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-stream")
        self._recognizer: Any = None
        # Undecoded live samples queued across all sessions (not replays).
        self._backlog = 0
        # (mode, configured directory) -> usable, so the check — and the one
        # log line when it fails — happens once per configuration.
        self._checked: Optional[tuple[tuple[str, str], bool]] = None
        # The model was found but would not load; do not try per utterance.
        self._broken = False

    # ── availability ─────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return streaming_mode() == "sherpa"

    @property
    def loaded(self) -> bool:
        return self._recognizer is not None

    def model_dir(self) -> Optional[Path]:
        """The model's directory, or None when its files are not all there."""
        return _find_model_dir(settings.STT_STREAMING_MODEL_DIR)

    def installed(self) -> bool:
        """
        Package importable and model files present, whatever STT_STREAMING
        says. Imports nothing (for /health, which must not be what loads a
        second onnxruntime into a server that never uses it) and logs nothing.
        """
        return self._missing() is None

    def _missing(self) -> Optional[str]:
        try:
            if importlib.util.find_spec("sherpa_onnx") is None:
                return "the sherpa-onnx package is not installed"
        except Exception as e:
            return f"the sherpa-onnx package is not importable ({e})"
        if self.model_dir() is None:
            return (
                f"model files not found at {settings.STT_STREAMING_MODEL_DIR!r} "
                f"(expected {', '.join(MODEL_FILES)}); run scripts/setup_streaming_stt.sh"
            )
        return None

    def available(self) -> bool:
        """True when STT_STREAMING=sherpa and the engine can actually be used."""
        if not self.configured or self._broken:
            return False
        key = (streaming_mode(), settings.STT_STREAMING_MODEL_DIR)
        if self._checked is not None and self._checked[0] == key:
            return self._checked[1]
        missing = self._missing()
        if missing is not None:
            # Once: this is asked at the start of every utterance.
            logger.warning(
                f"STT_STREAMING=sherpa is set but streaming speech-to-text is unavailable: "
                f"{missing}. Whisper is used instead."
            )
        self._checked = (key, missing is None)
        return missing is None

    def supports(self, language: str) -> bool:
        """Whether the model is good enough in `language` to replace Whisper."""
        return (language or "").strip().lower() in SUPPORTED_LANGUAGES

    @property
    def backlog_secs(self) -> float:
        return self._backlog / SAMPLE_RATE

    # ── sessions ─────────────────────────────────────────────────────────

    def open(self, language: str) -> Optional[StreamingSession]:
        """
        A session for one utterance in `language`, or None when this
        utterance should go to Whisper: the feature is off or unavailable,
        the language is not one the model is good at, or the decoder is
        already behind.
        """
        if not self.available() or not self.supports(language):
            return None
        if self.backlog_secs > MAX_BACKLOG_SECS:
            logger.info(
                f"Streaming STT is {self.backlog_secs:.1f}s behind; this utterance uses Whisper"
            )
            return None
        return StreamingSession(self, language.strip().lower())

    async def warm(self) -> None:
        """Load the model ahead of the first utterance. Never raises."""
        try:
            if not self.available() or self.loaded:
                return
            # On the decoder's own thread, so it cannot race the first step.
            await asyncio.get_running_loop().run_in_executor(self._executor, self._get_recognizer)
        except Exception as e:
            logger.warning(f"Streaming STT warm-up skipped: {type(e).__name__}: {e}")

    # ── the decoder thread ───────────────────────────────────────────────

    def _load(self) -> Any:
        """Build the recognizer (blocking: about 1.5 s, ~630 MB)."""
        import sherpa_onnx

        model = self.model_dir()
        if model is None:
            raise FileNotFoundError(f"no model at {settings.STT_STREAMING_MODEL_DIR!r}")
        threads = max(1, int(settings.STT_STREAMING_THREADS))
        logger.info(f"Loading streaming STT model from {model} ({threads} threads)…")
        recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(model / "tokens.txt"),
            encoder=str(model / "encoder.int8.onnx"),
            decoder=str(model / "decoder.int8.onnx"),
            joiner=str(model / "joiner.int8.onnx"),
            num_threads=threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
        )
        logger.info("Streaming STT model loaded")
        return recognizer

    def _get_recognizer(self) -> Any:
        if self._recognizer is None:
            if self._broken:
                raise StreamingSTTError("the streaming model failed to load")
            try:
                self._recognizer = self._load()
            except Exception as e:
                self._broken = True
                logger.warning(
                    f"Streaming STT model failed to load ({type(e).__name__}: {e}); "
                    f"Whisper is used instead."
                )
                raise
        return self._recognizer

    def _submit(self, session: StreamingSession) -> None:
        self._executor.submit(self._step, session)

    def _step(self, session: StreamingSession) -> None:
        try:
            more = session._advance(self._get_recognizer())
        except Exception as e:
            # Kept on the session: its owner still has the audio and sends
            # it to Whisper. Other sessions are not affected.
            session._fail(e)
            return
        if more:
            # To the back of the line, behind every other session's step.
            self._submit(session)


streaming_stt = StreamingSTT()
