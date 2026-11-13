"""
Server-side turn-taking for the microphone that arrives over WebRTC.

The browser's hands-free mode decides turns from loudness alone, so it cannot
tell a finished thought from a pause for breath, and it has to guess at the
avatar's own voice coming back through the speakers. When the avatar is
delivered as a WebRTC stream the microphone can ride the same peer connection
instead, and the decision moves here, to two models from the Pipecat
framework:

  - Silero voice activity detection says whether a 32 ms frame is speech;
  - Smart Turn listens to the utterance so far, once the speaker pauses, and
    says whether it sounds finished.

Neither is re-implemented here. This module is the small state machine that
drives them — in the same order Pipecat's own TurnAnalyzerUserTurnStopStrategy
does (pipecat/turns/user_stop/turn_analyzer_user_turn_stop_strategy.py, fed by
pipecat/audio/vad/vad_controller.py) — and turns their verdicts into
utterances.

Like the WebRTC module it knows nothing about the WebSocket manager: audio
goes in as PCM, events come out through callbacks.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from typing import Any, Callable, Optional

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # what both models take
_BYTES_PER_SAMPLE = 2

# Pipecat's VADParams defaults, restated so they are visible here.
VAD_CONFIDENCE = 0.7
VAD_START_SECS = 0.2

# While the avatar is speaking, some of its voice survives the browser's echo
# cancellation and comes back up the microphone. It is real speech, so the
# only thing separating it from the user is how sure the model is: residual
# echo is quiet and smeared and scores lower than a person talking into the
# microphone. The bar is raised for as long as the avatar speaks — and for a
# moment after, because the server stops feeding the stream some hundreds of
# milliseconds before the last of it has left the browser's jitter buffer and
# the room. 0.85 still passes the test recording (clean speech scores above
# 0.9); it has NOT been tuned against echo from a real loudspeaker.
VAD_CONFIDENCE_AVATAR_SPEAKING = 0.85
ECHO_TAIL_SECS = 0.3

# Audio kept from before the VAD confirmed speech. Confirmation comes
# VAD_START_SECS after the first voiced frame, and the first phoneme (an
# unvoiced consonant, a soft onset) starts before that frame, so the
# utterance handed on begins this long before the start window.
PRE_ROLL_SECS = 0.3

# There is deliberately no minimum length for an utterance on top of the
# VAD's start window. One used to be applied (0.3 s of confirmed voice) and
# it threw away real answers — "yes", "no", "okay" — after the avatar had
# already been interrupted for them. VAD_START_SECS is what rejects a click
# or a dropped pen (it has to be applied BEFORE speech is reported, since the
# report is what interrupts), and a noise that does get through is dropped
# when the transcriber finds no words in it.

# No single utterance runs longer than this, whatever the models say — the
# same cap the browser's hands-free mode applies. It bounds the audio held
# per session (30 s is under 1 MB) and the length of one transcription.
MAX_UTTERANCE_SECS = 30.0

# Silence that ends a turn when no turn model is consulted (VOICE_TURN_
# DETECTION=vad). Pipecat's own silence-only strategy waits its VAD stop
# window (0.2 s) plus a user_speech_timeout of 0.6 s.
_VAD_MODE_STOP_SECS = 0.8
# In "smart" mode the VAD stop window is only the pause after which the turn
# model is asked. 0.2 s is Pipecat's default and the value Smart Turn is
# tuned against (the stop strategy warns about any other).
_SMART_MODE_STOP_SECS = 0.2

# Mid-utterance, how long the track may deliver nothing before the utterance
# is closed anyway — the audio_idle_timeout of Pipecat's VADController. All
# other time here is counted in audio, so when packets stop arriving (a
# network stall, a browser that suspends the track) nothing else would ever
# end the utterance: the user's "speaking" state would hang and whatever came
# after the gap would be glued onto what came before.
AUDIO_IDLE_SECS = 1.0


# ── availability ────────────────────────────────────────────────────────────

# None until the import has been attempted. Importing Pipecat pulls in
# onnxruntime and takes a noticeable fraction of a second, so it happens the
# first time turn-taking is actually wanted, never on `import app`.
_available: Optional[bool] = None
_pipecat: Any = None


class _Pipecat:
    """The handful of Pipecat names this module uses."""

    def __init__(self) -> None:
        # Pipecat logs through loguru, whose default sink prints everything
        # from DEBUG up to stderr: an import banner, and then a line per
        # end-of-turn analysis. Silenced for that package only; this
        # module's own logging reports what matters.
        from loguru import logger as loguru_logger

        loguru_logger.disable("pipecat")

        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
        from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams, VADState

        self.EndOfTurnState = EndOfTurnState
        self.SmartTurnParams = SmartTurnParams
        self.LocalSmartTurnAnalyzerV3 = LocalSmartTurnAnalyzerV3
        self.SileroVADAnalyzer = SileroVADAnalyzer
        self.VADParams = VADParams
        self.VADState = VADState


def turn_taking_available() -> bool:
    """True when the Pipecat analyzers import. Imports them on first call."""
    global _available, _pipecat
    if _available is None:
        try:
            _pipecat = _Pipecat()
            _available = True
        except Exception as e:
            # Anything, not just ImportError: onnxruntime is a native wheel.
            # Turn-taking is optional — the microphone simply is not offered
            # and hands-free stays in the browser.
            logger.info(f"Server-side turn detection unavailable: {e}")
            _available = False
    return _available


def turn_taking_installed() -> bool:
    """
    Whether turn-taking looks available, WITHOUT importing Pipecat when that
    has not happened yet. For /health, which must not be the thing that loads
    onnxruntime into a server that never uses it.
    """
    if _available is not None:
        return _available
    try:
        import importlib.util

        return importlib.util.find_spec("pipecat") is not None
    except Exception:
        return False


def __getattr__(name: str) -> Any:
    # TURN_TAKING_AVAILABLE as a module attribute, resolved on first access.
    if name == "TURN_TAKING_AVAILABLE":
        return turn_taking_available()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def turn_detection_mode() -> str:
    """VOICE_TURN_DETECTION, normalised: "smart", "vad" or "off"."""
    mode = (settings.VOICE_TURN_DETECTION or "").strip().lower()
    # An unrecognised value means off, like an unrecognised MEDIA_TRANSPORT
    # means clips: a typo leaves the app as it was before the feature.
    return mode if mode in ("smart", "vad") else "off"


def voice_turns_enabled() -> bool:
    """True when this server will take turns from a WebRTC microphone."""
    return turn_detection_mode() != "off" and turn_taking_available()


def vad_stop_secs(mode: Optional[str] = None) -> float:
    """The VAD's stop window: VAD_STOP_SECS, or the default for the mode."""
    if settings.VAD_STOP_SECS > 0:
        return float(settings.VAD_STOP_SECS)
    mode = mode or turn_detection_mode()
    return _VAD_MODE_STOP_SECS if mode == "vad" else _SMART_MODE_STOP_SECS


def _build_analyzers(mode: str, stop_secs: float, max_wait_secs: float) -> tuple[Any, Any]:
    """Load the models (blocking: two ONNX sessions, tens of milliseconds)."""
    if not turn_taking_available():
        raise RuntimeError("pipecat is not installed")
    p = _pipecat
    vad = p.SileroVADAnalyzer(
        params=p.VADParams(
            confidence=VAD_CONFIDENCE, start_secs=VAD_START_SECS, stop_secs=stop_secs
        )
    )
    vad.set_sample_rate(SAMPLE_RATE)
    turn = None
    if mode == "smart":
        turn = p.LocalSmartTurnAnalyzerV3(params=p.SmartTurnParams(stop_secs=max_wait_secs))
        turn.set_sample_rate(SAMPLE_RATE)
    return vad, turn


async def warm() -> None:
    """Import Pipecat and load both models once, ahead of the first utterance. Never raises."""
    try:
        mode = turn_detection_mode()
        if mode == "off":
            return
        detector = await TurnDetector.create(mode)
        await detector.close()
    except Exception as e:
        logger.warning(f"Turn-detection warm-up skipped: {e}")


# ── the detector ────────────────────────────────────────────────────────────


def _is_complete(verdict: Any) -> bool:
    # By name, so the detector's logic does not need Pipecat's enums in hand
    # (it is driven with scripted analyzers in tests).
    return getattr(verdict, "name", verdict) == "COMPLETE"


class TurnDetector:
    """
    16 kHz mono s16 PCM in, in pieces of any size; utterances out.

    Callbacks (assign after construction; each may be a coroutine function,
    and is awaited before the next frame is looked at, so events arrive in
    order):
      on_speech_start()            — the VAD confirmed voice activity
      on_speech_paused(audio)      — "smart" mode only: the VAD went quiet and
                                     the turn model is about to be asked.
                                     `audio` is what on_speech_end will carry
                                     if the turn ends here, so work on it can
                                     start now
      on_speech_resumed()          — the speaker carried on after such a
                                     pause; same utterance
      on_speech_audio(audio)       — the utterance's audio as it arrives,
                                     for a listener that transcribes while
                                     the user is still speaking: first the
                                     pre-roll and the start window in one
                                     piece (right after on_speech_start),
                                     then each frame as it is added. Covers
                                     what on_speech_end will carry, plus any
                                     silence waited through after its end
      on_speech_progress(audio)    — every `progress_interval_secs` of an
                                     utterance while the user is speaking:
                                     everything heard so far
      on_speech_end(audio)         — the turn is over. int16, 16 kHz, from
                                     PRE_ROLL_SECS before the detected start
                                     to the end of the last speech

    Every on_speech_start is followed by exactly one on_speech_end, unless
    reset() discards the utterance in between.

    Time is counted in samples fed, never read from a clock, so the detector
    behaves identically whether audio arrives in real time, in bursts after a
    stall, or as fast as a test can push it. (One exception lives inside
    Pipecat: Smart Turn picks the audio it analyses by wall-clock arrival
    time. Fed faster than real time it simply sees more lead-in.) Audio that
    stops arriving altogether is the reader's to notice: see audio_idle().

    All inference runs on the analyzers' own executor threads; nothing here
    blocks the event loop.
    """

    def __init__(
        self,
        vad: Any,
        turn: Any = None,
        *,
        stop_secs: float = _SMART_MODE_STOP_SECS,
        max_wait_secs: float = 3.0,
        max_utterance_secs: float = MAX_UTTERANCE_SECS,
        progress_interval_secs: float = 0.0,
    ) -> None:
        self._vad = vad
        self._turn = turn
        self._frame_samples = int(vad.num_frames_required())
        self._frame_bytes = self._frame_samples * _BYTES_PER_SAMPLE
        frame_secs = self._frame_samples / SAMPLE_RATE

        def frames(secs: float) -> int:
            return max(1, round(secs / frame_secs))

        self._frames = frames
        # As the VAD itself rounds them (VADAnalyzer.set_params).
        self._start_frames = round(VAD_START_SECS / frame_secs)
        self._stop_frames = round(stop_secs / frame_secs)
        self._max_wait_frames = frames(max_wait_secs)
        self._max_frames = frames(max_utterance_secs)
        self._progress_frames = frames(progress_interval_secs) if progress_interval_secs > 0 else 0
        self._echo_tail_frames = frames(ECHO_TAIL_SECS)

        self.on_speech_start: Optional[Callable[[], Any]] = None
        self.on_speech_paused: Optional[Callable[[np.ndarray], Any]] = None
        self.on_speech_resumed: Optional[Callable[[], Any]] = None
        self.on_speech_audio: Optional[Callable[[np.ndarray], Any]] = None
        self.on_speech_progress: Optional[Callable[[np.ndarray], Any]] = None
        self.on_speech_end: Optional[Callable[[np.ndarray], Any]] = None

        self._buf = bytearray()
        # The frames just before speech, for the pre-roll. Sized to hold the
        # VAD's start window as well: by the time speech is confirmed, the
        # frames that confirmed it are in here too.
        self._ring: deque[bytes] = deque(maxlen=self._start_frames + frames(PRE_ROLL_SECS))
        # Bumped by reset(). Whatever was awaiting a model or a callback when
        # that happened finds the number changed and stops.
        self._generation = 0
        # Frames left before the confidence bar comes back down; 0 = not pending.
        self._restore_in = 0
        self._clear()

    @classmethod
    async def create(
        cls,
        mode: Optional[str] = None,
        *,
        stop_secs: Optional[float] = None,
        max_wait_secs: Optional[float] = None,
        progress_interval_secs: float = 0.0,
    ) -> "TurnDetector":
        """A detector on the real models, for `mode` ("smart" or "vad")."""
        mode = mode or turn_detection_mode()
        if mode not in ("smart", "vad"):
            raise ValueError(f"no turn detection in mode {mode!r}")
        stop = vad_stop_secs(mode) if stop_secs is None else stop_secs
        wait = settings.TURN_MAX_WAIT_SECS if max_wait_secs is None else max_wait_secs
        vad, turn = await asyncio.to_thread(_build_analyzers, mode, stop, wait)
        return cls(
            vad,
            turn,
            stop_secs=stop,
            max_wait_secs=wait,
            progress_interval_secs=progress_interval_secs,
        )

    # ── state ────────────────────────────────────────────────────────────

    def _clear(self) -> None:
        # The utterance in progress as a list of frames, or None between them.
        self._utterance: Optional[list[bytes]] = None
        # Pipecat's `vad_user_speaking`: from the VAD confirming speech until
        # it confirms quiet (so the stop window counts as speaking).
        self._vad_speaking = False
        # Waiting, mid-utterance, to see whether the speaker carries on.
        self._paused = False
        self._silence_frames = 0
        # How much of the utterance to hand on: up to the last pause, so the
        # silence waited through afterwards is not sent to be transcribed.
        self._end_frames = 0
        self._next_progress = 0
        # After the length cap cut an utterance off mid-speech: nothing new
        # starts until the VAD has gone quiet once.
        self._await_quiet = False

    @property
    def in_utterance(self) -> bool:
        return self._utterance is not None

    def reset(self) -> None:
        """
        Forget everything heard: the utterance in progress, the pre-roll and
        unframed input. Nothing is reported for what is discarded.
        """
        self._generation += 1
        self._buf.clear()
        self._ring.clear()
        self._clear()
        if self._turn is not None:
            try:
                self._turn.clear()
            except Exception as e:
                logger.debug(f"Turn analyzer clear failed: {e}")

    def set_avatar_speaking(self, active: bool) -> None:
        """
        Raise the VAD's confidence bar while the avatar speaks (see
        VAD_CONFIDENCE_AVATAR_SPEAKING); lower it ECHO_TAIL_SECS of audio
        after it stops.

        Only the threshold is touched, through the analyzer's own params —
        VADAnalyzer.set_params would also reset its state, and cut in two an
        utterance that is under way (the user barging in is exactly when the
        avatar stops speaking).
        """
        if active:
            self._restore_in = 0
            self._set_confidence(VAD_CONFIDENCE_AVATAR_SPEAKING)
        elif self.confidence != VAD_CONFIDENCE:
            self._restore_in = self._echo_tail_frames

    def set_max_utterance_secs(self, secs: float) -> None:
        """
        Change the length cap, from the next frame on — including for the
        utterance in progress. For a caller that will join this utterance to
        audio it already holds and needs the cap to cover both.
        """
        self._max_frames = self._frames(secs)

    @property
    def confidence(self) -> float:
        return float(self._vad.params.confidence)

    def _set_confidence(self, value: float) -> None:
        self._vad.params.confidence = value

    async def close(self) -> None:
        """Release the analyzers' threads. Idempotent; never raises."""
        self.reset()
        for analyzer in (self._vad, self._turn):
            if analyzer is None:
                continue
            try:
                await analyzer.cleanup()
            except Exception as e:
                logger.debug(f"Analyzer cleanup failed: {e}")

    # ── input ────────────────────────────────────────────────────────────

    async def feed(self, pcm: bytes) -> None:
        """
        Take the next piece of audio. Returns when every whole frame in it
        has been analysed and its events delivered. One caller at a time.
        """
        generation = self._generation
        self._buf += pcm
        while len(self._buf) >= self._frame_bytes and generation == self._generation:
            frame = bytes(self._buf[: self._frame_bytes])
            del self._buf[: self._frame_bytes]
            await self._process(frame, generation)

    async def audio_idle(self) -> None:
        """
        No audio has arrived for AUDIO_IDLE_SECS. If an utterance is open,
        close it by treating the gap as silence: silent frames go through
        the normal path until it ends — the VAD's stop window, the turn
        model, and if that says "unfinished", the silence limit. So the
        utterance ends the way it would have had the speaker simply stopped,
        with the same events. Pipecat's VADController does the equivalent
        (it forces the VAD to QUIET); feeding the silence instead keeps the
        analyzers' own state in step with ours. One caller at a time, like
        feed().
        """
        if self._utterance is None:
            return
        generation = self._generation
        # Part of a frame from before the gap: not worth joining to silence.
        self._buf.clear()
        silence = bytes(self._frame_bytes)
        for _ in range(self._stop_frames + self._max_wait_frames + 1):
            if self._utterance is None or generation != self._generation:
                return
            await self._process(silence, generation)
        if self._utterance is not None and generation == self._generation:
            # A VAD that will not call silence quiet. Not expected; the
            # utterance still must not stay open.
            self._vad_speaking = False
            await self._finish(generation)

    async def _emit(self, callback: Optional[Callable], *args: Any) -> None:
        if callback is None:
            return
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A listener's problem must not stop the microphone.
            logger.warning(f"Turn detector callback failed: {type(e).__name__}: {e}")

    def _audio(self, frames: Optional[int] = None) -> np.ndarray:
        assert self._utterance is not None
        chunk = self._utterance if frames is None else self._utterance[:frames]
        return np.frombuffer(b"".join(chunk), dtype=np.int16)

    async def _process(self, frame: bytes, generation: int) -> None:
        if self._restore_in:
            self._restore_in -= 1
            if not self._restore_in:
                self._set_confidence(VAD_CONFIDENCE)

        # The order below is Pipecat's: the VAD sees the frame first and its
        # start/stop is acted on (including asking the turn model), and only
        # then is the frame appended to the turn analyzer, labelled with the
        # speaking state the VAD just arrived at.
        state = await self._vad.analyze_audio(frame)
        if generation != self._generation:
            return
        name = getattr(state, "name", state)
        # STARTING and STOPPING are the VAD making up its mind; only the two
        # settled states are transitions (cf. VADController._handle_vad).
        started = name == "SPEAKING" and not self._vad_speaking
        stopped = name == "QUIET" and self._vad_speaking

        if started:
            self._vad_speaking = True
            if self._await_quiet:
                pass
            elif self._utterance is None:
                self._utterance = list(self._ring)
                self._ring.clear()
                self._next_progress = len(self._utterance) + self._progress_frames
                if self._turn is not None:
                    self._turn.update_vad_start_secs(VAD_START_SECS)
                await self._emit(self.on_speech_start)
                # What was heard before speech was confirmed — without it a
                # live listener would miss the first word. Not if the start
                # handler reset the detector: the utterance is gone.
                if self.on_speech_audio is not None and generation == self._generation:
                    await self._emit(self.on_speech_audio, self._audio())
            else:
                self._paused = False
                self._silence_frames = 0
                await self._emit(self.on_speech_resumed)
            if generation != self._generation:
                return

        if self._utterance is not None:
            self._utterance.append(frame)
            if self.on_speech_audio is not None:
                await self._emit(self.on_speech_audio, np.frombuffer(frame, dtype=np.int16))
                if generation != self._generation:
                    return
        else:
            self._ring.append(frame)

        if stopped:
            await self._vad_went_quiet(generation)
            if generation != self._generation:
                return

        if self._turn is not None:
            verdict = self._turn.append_audio(frame, self._vad_speaking)
            # A batch analyzer returns COMPLETE here only when its own
            # silence limit (stop_secs = TURN_MAX_WAIT_SECS) runs out.
            if self._paused and _is_complete(verdict):
                await self._finish(generation)
                return

        if self._paused:
            # The same limit, counted here as well, so it does not depend on
            # the analyzer's bookkeeping surviving an analysis in between.
            self._silence_frames += 1
            if self._silence_frames >= self._max_wait_frames:
                await self._finish(generation)
                return

        if self._utterance is None:
            return
        if len(self._utterance) >= self._max_frames:
            self._end_frames = len(self._utterance)
            # Still talking: what follows is the rest of THIS speech, which
            # must not open a new utterance (and barge in on the reply to
            # the one just cut off).
            still_speaking = self._vad_speaking
            await self._finish(generation)
            if generation == self._generation and still_speaking:
                self._vad_speaking = True
                self._await_quiet = True
            return
        if (
            self._progress_frames
            and self._vad_speaking
            and len(self._utterance) >= self._next_progress
        ):
            self._next_progress = len(self._utterance) + self._progress_frames
            await self._emit(self.on_speech_progress, self._audio())

    async def _vad_went_quiet(self, generation: int) -> None:
        self._vad_speaking = False
        if self._utterance is None:
            # The tail of speech that was cut off at the length cap.
            self._await_quiet = False
            if self._turn is not None:
                self._turn.clear()
            return

        self._end_frames = len(self._utterance)
        if self._turn is None:
            # "vad" mode: the stop window IS the silence that ends a turn.
            await self._finish(generation)
            return

        self._paused = True
        self._silence_frames = 0
        # Before the model runs, so a listener's work overlaps with it.
        await self._emit(self.on_speech_paused, self._audio(self._end_frames))
        if generation != self._generation or not self._paused:
            return
        try:
            verdict, _ = await self._turn.analyze_end_of_turn()
        except Exception as e:
            # Fail towards answering: a turn model that cannot run must not
            # leave the user waiting out the full silence limit every time.
            logger.warning(f"End-of-turn analysis failed: {type(e).__name__}: {e}")
            verdict = "COMPLETE"
        if generation != self._generation:
            return
        if _is_complete(verdict):
            await self._finish(generation)
        # INCOMPLETE: the utterance stays open. Speech resuming continues it;
        # silence for TURN_MAX_WAIT_SECS ends it.

    def _discard(self) -> None:
        """Drop the utterance; the next one starts from clean state."""
        speaking = self._vad_speaking
        self._clear()
        self._vad_speaking = speaking
        if self._turn is not None:
            self._turn.clear()

    async def _finish(self, generation: int) -> None:
        if generation != self._generation or self._utterance is None:
            return
        audio = self._audio(self._end_frames or None)
        self._discard()
        await self._emit(self.on_speech_end, audio)


# ── the reader ──────────────────────────────────────────────────────────────


class MicReader:
    """
    Pulls an aiortc remote audio track and feeds a TurnDetector.

    The track is read for as long as it lives, because aiortc decodes what
    arrives into an unbounded queue whether or not anyone is reading. But
    frames are only LOOKED AT while a detector is attached (listen()); the
    rest of the time each one is dropped as it comes off the queue —
    not resampled, not buffered, not analysed.
    """

    def __init__(self, track: Any, idle_secs: float = AUDIO_IDLE_SECS) -> None:
        self._track = track
        self._idle_secs = idle_secs
        self._detector: Optional[TurnDetector] = None
        self._resampler: Any = None
        self.ended = False
        # Frames taken off the track / frames passed on to a detector.
        self.frames_read = 0
        self.frames_heard = 0

    @property
    def listening(self) -> bool:
        return self._detector is not None

    def listen(self, detector: TurnDetector) -> None:
        """Start passing audio to `detector`, from the next frame on."""
        self._detector = detector
        # A fresh resampler: the old one holds the tail of audio from before
        # a gap of unknown length.
        self._resampler = None

    def stop_listening(self) -> None:
        """Stop passing audio on, and discard what the detector had heard."""
        detector, self._detector = self._detector, None
        self._resampler = None
        if detector is not None:
            detector.reset()

    def _to_pcm(self, frame: Any) -> bytes:
        """Whatever aiortc decoded (48 kHz stereo s16 for Opus) as 16 kHz mono s16."""
        import av

        for attempt in (0, 1):
            if self._resampler is None:
                self._resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            try:
                return b"".join(
                    out.to_ndarray().tobytes() for out in self._resampler.resample(frame)
                )
            except Exception:
                # The input format changed under it (a renegotiated codec);
                # a resampler is bound to the first format it sees.
                self._resampler = None
                if attempt:
                    raise
        return b""

    async def run(self) -> None:
        """Read until the track ends. Never raises (cancellation aside)."""
        from aiortc.mediastreams import MediaStreamError

        # A recv() that outlived its deadline. Kept, not cancelled: it is
        # the next frame, and the next wait is for the same one.
        pending: Optional[asyncio.Future] = None
        try:
            while True:
                detector = self._detector
                if pending is None and not (detector is not None and detector.in_utterance):
                    # The ordinary case, and the only one between utterances:
                    # however long the track is silent, nothing is open.
                    frame = await self._track.recv()
                else:
                    # Mid-utterance the wait is bounded. asyncio.wait, not
                    # wait_for: wait_for cancels what it waits on, and can
                    # swallow a cancellation of this task on Python 3.11.
                    if pending is None:
                        pending = asyncio.ensure_future(self._track.recv())
                    done, _ = await asyncio.wait({pending}, timeout=self._idle_secs)
                    if not done:
                        if detector is not None and detector is self._detector:
                            await detector.audio_idle()
                            # Like listen(): what it holds is from before a
                            # gap of unknown length.
                            self._resampler = None
                        continue
                    frame, pending = pending.result(), None
                self.frames_read += 1
                detector = self._detector
                if detector is None:
                    continue
                pcm = self._to_pcm(frame)
                self.frames_heard += 1
                if pcm:
                    await detector.feed(pcm)
        except MediaStreamError:
            pass  # the track ended with its peer connection
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Microphone reader stopped: {type(e).__name__}: {e}")
        finally:
            if pending is not None:
                pending.cancel()
            self.ended = True
            self.stop_listening()
