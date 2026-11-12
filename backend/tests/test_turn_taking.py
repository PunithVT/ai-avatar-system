"""
Server-side turn-taking for the WebRTC microphone (services/turn_taking.py).

Four layers, cheapest first:

  - the detector's own logic, driven by scripted stand-ins for the two models,
    so every branch (a pause that is not the end, the silence limit, the
    length cap) can be reached deterministically;
  - the detector on the REAL Silero and Smart Turn models, over a real
    recording — voice-activity models do not react to synthetic tones;
  - the reader that pulls an aiortc track, with a fake track;
  - a loopback in which a second aiortc peer plays the browser: it sends the
    recording up a real peer connection (Opus over RTP on localhost) and the
    server-side reader and detector report the utterance.

Nothing here reads a clock to decide anything: the detector counts samples,
so audio is fed as fast as the machine takes it.
"""

import asyncio
import enum
import fractions
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

import app.services.turn_taking as tt
import app.services.webrtc as rtc
from app.config import settings
from app.services.turn_taking import (
    ECHO_TAIL_SECS,
    PRE_ROLL_SECS,
    SAMPLE_RATE,
    VAD_CONFIDENCE,
    VAD_CONFIDENCE_AVATAR_SPEAKING,
    MicReader,
    TurnDetector,
)

FIXTURE = Path(__file__).parent / "fixtures" / "speech_two_sentences.flac"
FRAME = 512  # samples the VAD takes at 16 kHz (32 ms)
FRAME_SECS = FRAME / SAMPLE_RATE

needs_pipecat = pytest.mark.skipif(not tt.turn_taking_available(), reason="pipecat not installed")


class Recorder:
    """Collects a detector's events, each stamped with the audio time fed so far."""

    def __init__(self, detector):
        self.events: list[tuple] = []
        self.fed = 0  # samples
        self.detector = detector
        detector.on_speech_start = lambda: self._add("start")
        detector.on_speech_paused = lambda audio: self._add("paused", audio)
        detector.on_speech_resumed = lambda: self._add("resumed")
        detector.on_speech_progress = lambda audio: self._add("progress", audio)
        detector.on_speech_end = lambda audio: self._add("end", audio)

    def _add(self, kind, payload=None):
        self.events.append((kind, self.fed / SAMPLE_RATE, payload))

    def kinds(self):
        return [e[0] for e in self.events]

    def of(self, kind):
        return [e for e in self.events if e[0] == kind]

    async def feed(self, samples: np.ndarray, piece: int = 320):
        """In 20 ms pieces by default — what an Opus frame resamples to."""
        for i in range(0, len(samples), piece):
            chunk = samples[i : i + piece]
            self.fed += len(chunk)
            await self.detector.feed(chunk.astype(np.int16).tobytes())


# ── the detector's logic, on scripted analyzers ──────────────────────────


class State(enum.Enum):
    QUIET = 1
    STARTING = 2
    SPEAKING = 3
    STOPPING = 4


class Verdict(enum.Enum):
    COMPLETE = 1
    INCOMPLETE = 2


class ScriptedVAD:
    """
    Pipecat's VAD state machine with the model replaced by the audio itself:
    a frame's "confidence" is its first sample / 1000. So `_speech(secs)` is
    speech at 0.9 and `_speech(secs, 800)` is speech the raised bar rejects.
    """

    def __init__(self, start_secs=0.2, stop_secs=0.2):
        self.params = SimpleNamespace(confidence=VAD_CONFIDENCE)
        self._start = round(start_secs / FRAME_SECS)
        self._stop = round(stop_secs / FRAME_SECS)
        self._state = State.QUIET
        self._count = 0
        self.frames = 0
        self.cleaned = False

    def num_frames_required(self):
        return FRAME

    async def analyze_audio(self, buffer: bytes):
        assert len(buffer) == FRAME * 2, "the detector must re-frame its input"
        await asyncio.sleep(0)  # a real analyzer yields to its executor
        self.frames += 1
        confidence = int(np.frombuffer(buffer, np.int16)[0]) / 1000
        if confidence >= self.params.confidence:
            if self._state is State.QUIET:
                self._state, self._count = State.STARTING, 1
            elif self._state is State.STARTING:
                self._count += 1
            elif self._state is State.STOPPING:
                self._state = State.SPEAKING
        else:
            if self._state is State.STARTING:
                self._state = State.QUIET
            elif self._state is State.SPEAKING:
                self._state, self._count = State.STOPPING, 1
            elif self._state is State.STOPPING:
                self._count += 1
        if self._state is State.STARTING and self._count >= self._start:
            self._state = State.SPEAKING
        if self._state is State.STOPPING and self._count >= self._stop:
            self._state = State.QUIET
        return self._state

    async def cleanup(self):
        self.cleaned = True


class ScriptedTurn:
    """A turn model that answers from a script, and records how it was driven."""

    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.calls: list[str] = []
        self.speech_flags: list[bool] = []
        self.asked_after: list[int] = []  # frames appended when each verdict was asked for

    def append_audio(self, buffer, is_speech):
        self.speech_flags.append(is_speech)
        return Verdict.INCOMPLETE

    async def analyze_end_of_turn(self):
        await asyncio.sleep(0)
        self.calls.append("analyze")
        self.asked_after.append(len(self.speech_flags))
        return (self.verdicts.pop(0) if self.verdicts else Verdict.COMPLETE), None

    def update_vad_start_secs(self, secs):
        self.calls.append("start_secs")

    def clear(self):
        self.calls.append("clear")

    async def cleanup(self):
        self.calls.append("cleanup")


def _speech(secs: float, level: int = 900) -> np.ndarray:
    return np.full(round(secs * SAMPLE_RATE), level, np.int16)


def _silence(secs: float) -> np.ndarray:
    return np.zeros(round(secs * SAMPLE_RATE), np.int16)


def _scripted(*verdicts, smart=True, **kwargs):
    turn = ScriptedTurn(*verdicts) if smart else None
    detector = TurnDetector(ScriptedVAD(), turn, **kwargs)
    return Recorder(detector), turn


async def test_a_pause_the_model_calls_unfinished_keeps_the_utterance_open():
    rec, turn = _scripted(Verdict.INCOMPLETE, Verdict.COMPLETE)
    await rec.feed(
        np.concatenate([_silence(0.5), _speech(1.0), _silence(0.6), _speech(1.0), _silence(1.0)])
    )
    assert rec.kinds() == ["start", "paused", "resumed", "paused", "end"]
    # ONE utterance, holding both halves and the pause between them.
    audio = rec.of("end")[0][2]
    assert audio.dtype == np.int16
    assert 2.6 + PRE_ROLL_SECS - 0.1 < len(audio) / SAMPLE_RATE < 2.6 + PRE_ROLL_SECS + 0.5
    assert turn.calls.count("analyze") == 2


async def test_silence_after_an_unfinished_verdict_ends_the_turn_at_the_limit():
    rec, turn = _scripted(Verdict.INCOMPLETE, max_wait_secs=1.0)
    await rec.feed(np.concatenate([_silence(0.5), _speech(1.0), _silence(3.0)]))
    assert rec.kinds() == ["start", "paused", "end"]
    paused, end = rec.of("paused")[0], rec.of("end")[0]
    # Measured in audio fed, not on a clock: the limit after the pause.
    assert end[1] - paused[1] == pytest.approx(1.0, abs=0.05)
    # What is handed on stops at the pause; the second waited through is not
    # sent to be transcribed. It is exactly what the pause announced.
    assert np.array_equal(end[2], paused[2])
    assert turn.calls.count("analyze") == 1, "the model is asked once per pause, not polled"


async def test_the_analyzers_are_driven_in_pipecats_order():
    """
    VAD first; on quiet the turn model is asked BEFORE that frame is appended;
    and every frame is appended labelled with the speaking state.
    """
    rec, turn = _scripted(Verdict.COMPLETE)
    await rec.feed(np.concatenate([_silence(0.32), _speech(0.96), _silence(0.64)]), piece=FRAME)
    assert len(turn.speech_flags) == rec.detector._vad.frames, "every frame reaches the turn model"
    first_speech = turn.speech_flags.index(True)
    last_speech = len(turn.speech_flags) - 1 - turn.speech_flags[::-1].index(True)
    # Speaking from the frame that confirmed it (start window after onset)...
    assert first_speech == 10 + 6 - 1
    # ...through the stop window, and the verdict was asked for at that point.
    assert turn.asked_after == [last_speech + 1]
    assert turn.calls[0] == "start_secs"


async def test_an_utterance_is_cut_at_the_length_cap_and_its_tail_starts_nothing():
    rec, _ = _scripted(max_utterance_secs=2.0)
    await rec.feed(np.concatenate([_silence(0.5), _speech(5.0), _silence(1.0)]))
    # One start, one end: the three seconds of speech after the cut belong to
    # the utterance that was cut, and must not barge in on its own reply.
    assert rec.kinds() == ["start", "end"]
    assert len(rec.of("end")[0][2]) / SAMPLE_RATE == pytest.approx(2.0, abs=0.05)
    # After the speaker has paused, the next utterance is heard normally.
    await rec.feed(np.concatenate([_speech(1.0), _silence(1.0)]))
    assert rec.kinds() == ["start", "end", "start", "paused", "end"]


async def test_whatever_the_vad_confirmed_as_speech_becomes_an_utterance_however_short():
    """
    There is no second length filter behind the VAD's start window. One used
    to drop anything under 0.3 s of confirmed voice — after the start had
    been reported and the avatar interrupted for it — and "yes" is shorter.
    """
    rec, turn = _scripted()
    await rec.feed(np.concatenate([_silence(0.5), _speech(0.25), _silence(1.0)]))
    assert rec.kinds() == ["start", "paused", "end"]
    assert turn.calls.count("analyze") == 1
    # The whole word, with its pre-roll.
    assert 900 in rec.of("end")[0][2]
    assert 0.25 <= len(rec.of("end")[0][2]) / SAMPLE_RATE <= 0.25 + PRE_ROLL_SECS + 0.3


async def test_the_length_cap_can_be_moved_for_the_utterance_in_progress():
    """For a caller that joins this utterance to audio it already holds."""
    rec, _ = _scripted()
    rec.detector.on_speech_start = lambda: (
        rec._add("start"),
        rec.detector.set_max_utterance_secs(1.0),
    )
    await rec.feed(np.concatenate([_silence(0.5), _speech(3.0), _silence(1.0)]))
    assert rec.kinds() == ["start", "end"]
    assert len(rec.of("end")[0][2]) / SAMPLE_RATE == pytest.approx(1.0, abs=0.05)
    # No room at all still ends at once, on the first frame, rather than never.
    rec.detector.on_speech_start = lambda: (
        rec._add("start"),
        rec.detector.set_max_utterance_secs(0.0),
    )
    await rec.feed(np.concatenate([_speech(1.0), _silence(1.0)]))
    assert rec.kinds() == ["start", "end"] * 2
    assert len(rec.of("end")[1][2]) / SAMPLE_RATE < 0.6


@pytest.mark.parametrize("smart", [True, False])
async def test_audio_that_stops_arriving_mid_speech_closes_the_utterance(smart):
    rec, _ = _scripted(Verdict.COMPLETE, smart=smart)
    await rec.feed(np.concatenate([_silence(0.5), _speech(1.0)]))
    assert rec.kinds() == ["start"] and rec.detector.in_utterance
    await rec.detector.audio_idle()
    assert rec.kinds() == (["start", "paused", "end"] if smart else ["start", "end"])
    assert not rec.detector.in_utterance
    # What was heard before the gap, and only that.
    assert 1.0 <= len(rec.of("end")[0][2]) / SAMPLE_RATE <= 1.0 + PRE_ROLL_SECS + 0.5
    # Nothing open: nothing to do.
    await rec.detector.audio_idle()
    assert len(rec.events) == (3 if smart else 2)
    # And what comes after the gap is a new utterance, not more of the old.
    await rec.feed(np.concatenate([_speech(0.8, 950), _silence(1.0)]))
    second = rec.of("end")[1][2]
    assert 950 in second and 900 not in second


async def test_a_gap_after_an_unfinished_verdict_closes_the_utterance_too():
    """The silence limit is counted in audio; with none arriving it never runs out."""
    rec, turn = _scripted(Verdict.INCOMPLETE, Verdict.INCOMPLETE, max_wait_secs=2.0)
    # Stalled while still speaking: the model says "unfinished", and the gap
    # is waited out as silence all the same.
    await rec.feed(np.concatenate([_silence(0.5), _speech(1.0)]))
    await rec.detector.audio_idle()
    assert rec.kinds() == ["start", "paused", "end"]
    # Stalled during the wait that follows such a verdict.
    await rec.feed(np.concatenate([_speech(1.0), _silence(0.5)]))
    assert rec.kinds()[3:] == ["start", "paused"] and rec.detector.in_utterance
    await rec.detector.audio_idle()
    assert rec.kinds()[3:] == ["start", "paused", "end"]
    assert turn.calls.count("analyze") == 2


async def test_a_blip_shorter_than_the_start_window_is_not_reported_at_all():
    rec, _ = _scripted()
    await rec.feed(np.concatenate([_silence(0.5), _speech(0.1), _silence(1.0)]))
    assert rec.events == []


async def test_each_utterance_starts_from_clean_state():
    rec, turn = _scripted(Verdict.COMPLETE, Verdict.COMPLETE)
    await rec.feed(np.concatenate([_silence(0.5), _speech(1.0, 900), _silence(2.0)]))
    await rec.feed(np.concatenate([_speech(0.8, 950), _silence(1.0)]))
    first, second = (e[2] for e in rec.of("end"))
    assert 900 in first and 950 not in first
    assert 950 in second and 900 not in second, "the second utterance carries none of the first"
    # Voiced time does not carry over either: 0.8 s alone is an utterance,
    # and was judged on its own.
    assert turn.calls.count("clear") >= 2


async def test_without_a_turn_model_the_stop_window_ends_the_turn():
    rec, _ = _scripted(smart=False)
    await rec.feed(np.concatenate([_silence(0.5), _speech(1.0), _silence(1.0)]))
    assert rec.kinds() == ["start", "end"]


async def test_input_may_arrive_in_pieces_of_any_size():
    audio = np.concatenate([_silence(0.5), _speech(1.0), _silence(1.0)])
    results = []
    for piece in (1, 160, 320, 512, 777, 5000, len(audio)):
        rec, _ = _scripted()
        await rec.feed(audio, piece=piece)
        results.append((rec.kinds(), len(rec.of("end")[0][2])))
    assert all(r == results[0] for r in results), results


async def test_the_bar_is_raised_while_the_avatar_speaks_and_lowered_after_its_echo():
    rec, _ = _scripted()
    detector = rec.detector
    quiet_voice = 800  # passes the normal bar (0.7), not the raised one (0.85)

    detector.set_avatar_speaking(True)
    assert detector.confidence == VAD_CONFIDENCE_AVATAR_SPEAKING
    await rec.feed(np.concatenate([_speech(1.0, quiet_voice), _silence(0.5)]))
    assert rec.events == [], "echo-level speech does not count as the user"

    # Someone actually talking over the avatar still does.
    await rec.feed(np.concatenate([_speech(1.0, 950), _silence(0.5)]))
    assert rec.kinds() == ["start", "paused", "end"]

    # The avatar stops; the bar stays up for the echo still in the room...
    detector.set_avatar_speaking(False)
    assert detector.confidence == VAD_CONFIDENCE_AVATAR_SPEAKING
    await rec.feed(_silence(ECHO_TAIL_SECS + 0.1))
    # ...and then comes back down.
    assert detector.confidence == VAD_CONFIDENCE
    await rec.feed(np.concatenate([_speech(1.0, quiet_voice), _silence(0.5)]))
    assert rec.kinds() == ["start", "paused", "end"] * 2


async def test_raising_the_bar_does_not_cut_an_utterance_in_progress():
    rec, _ = _scripted()
    await rec.feed(np.concatenate([_silence(0.3), _speech(0.6, 950)]))
    rec.detector.set_avatar_speaking(True)
    rec.detector.set_avatar_speaking(False)
    await rec.feed(np.concatenate([_speech(0.6, 950), _silence(0.5)]))
    assert rec.kinds() == ["start", "paused", "end"]


async def test_reset_discards_the_utterance_in_progress_without_reporting_it():
    rec, turn = _scripted()
    await rec.feed(np.concatenate([_silence(0.3), _speech(1.0)]))
    assert rec.kinds() == ["start"]
    rec.detector.reset()
    await rec.feed(_silence(1.0))
    assert rec.kinds() == ["start"], "nothing is reported for what was discarded"
    assert not rec.detector.in_utterance and "clear" in turn.calls


async def test_a_reset_from_inside_a_callback_stops_the_frame_being_processed():
    """`mic active:false` can arrive while the detector is awaiting a listener."""
    rec, turn = _scripted()
    rec.detector.on_speech_paused = lambda audio: rec.detector.reset()
    await rec.feed(np.concatenate([_silence(0.3), _speech(1.0), _silence(1.0)]))
    assert rec.kinds() == ["start"]
    assert "analyze" not in turn.calls


async def test_progress_is_reported_at_the_interval_only_while_speaking():
    rec, _ = _scripted(Verdict.INCOMPLETE, progress_interval_secs=0.5, max_wait_secs=2.0)
    await rec.feed(np.concatenate([_silence(0.3), _speech(1.8), _silence(3.0)]))
    progress = rec.of("progress")
    assert len(progress) == 3
    lengths = [len(p[2]) / SAMPLE_RATE for p in progress]
    assert all(b - a == pytest.approx(0.5, abs=0.05) for a, b in zip(lengths, lengths[1:]))
    assert rec.kinds()[-2:] == ["paused", "end"], "none during the silence that followed"


async def test_a_failing_listener_does_not_stop_the_microphone():
    rec, _ = _scripted()

    def boom():
        raise RuntimeError("listener bug")

    rec.detector.on_speech_start = boom
    await rec.feed(np.concatenate([_silence(0.3), _speech(1.0), _silence(1.0)]))
    assert rec.kinds() == ["paused", "end"]


async def test_a_turn_model_that_fails_answers_rather_than_waiting_out_the_limit():
    rec, turn = _scripted()

    async def broken():
        raise RuntimeError("onnx said no")

    turn.analyze_end_of_turn = broken
    await rec.feed(np.concatenate([_silence(0.3), _speech(1.0), _silence(0.5)]))
    assert rec.kinds() == ["start", "paused", "end"]


async def test_close_releases_the_analyzers():
    rec, turn = _scripted()
    await rec.detector.close()
    assert rec.detector._vad.cleaned and "cleanup" in turn.calls


# ── settings ─────────────────────────────────────────────────────────────


def test_the_mode_is_normalised_and_anything_unknown_is_off(monkeypatch):
    for raw, mode in (("smart", "smart"), (" VAD ", "vad"), ("off", "off"), ("clever", "off")):
        monkeypatch.setattr(settings, "VOICE_TURN_DETECTION", raw)
        assert tt.turn_detection_mode() == mode


def test_the_stop_window_defaults_per_mode_and_can_be_set(monkeypatch):
    monkeypatch.setattr(settings, "VAD_STOP_SECS", 0.0)
    assert tt.vad_stop_secs("smart") == 0.2, "Pipecat's VAD default; Smart Turn is tuned to it"
    assert tt.vad_stop_secs("vad") == 0.8, "silence alone needs longer before it means the end"
    monkeypatch.setattr(settings, "VAD_STOP_SECS", 0.5)
    assert tt.vad_stop_secs("smart") == tt.vad_stop_secs("vad") == 0.5


def test_without_pipecat_turn_taking_is_unavailable_not_a_crash(monkeypatch):
    monkeypatch.setattr(tt, "_available", None)

    def no_pipecat():
        raise ImportError("No module named 'pipecat'")

    monkeypatch.setattr(tt, "_Pipecat", no_pipecat)
    monkeypatch.setattr(settings, "VOICE_TURN_DETECTION", "smart")
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    assert tt.turn_taking_available() is False
    assert tt.TURN_TAKING_AVAILABLE is False
    assert tt.voice_turns_enabled() is False
    # The transport itself is unaffected; only the microphone is not offered.
    assert rtc.media_descriptor()["webrtc"] is rtc.WEBRTC_AVAILABLE
    assert rtc.media_descriptor()["mic"] is False


def test_the_microphone_is_offered_only_on_webrtc_with_turn_detection_on(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "clips")
    monkeypatch.setattr(settings, "VOICE_TURN_DETECTION", "smart")
    assert rtc.media_descriptor()["mic"] is False
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    assert rtc.media_descriptor()["mic"] is (rtc.WEBRTC_AVAILABLE and tt.turn_taking_available())
    monkeypatch.setattr(settings, "VOICE_TURN_DETECTION", "off")
    assert rtc.media_descriptor()["mic"] is False


# ── the real models, on a real recording ─────────────────────────────────


@pytest.fixture(scope="module")
def recording() -> np.ndarray:
    audio, rate = sf.read(str(FIXTURE), dtype="int16")
    assert rate == SAMPLE_RATE and audio.ndim == 1
    return audio


async def _real(mode, **kwargs):
    detector = await TurnDetector.create(mode, **kwargs)
    return Recorder(detector)


@needs_pipecat
async def test_real_vad_reports_the_start_of_speech_where_it_is(recording):
    rec = await _real("vad", stop_secs=0.5)
    try:
        await rec.feed(recording)
        # Speech begins at 0.7 s; the VAD confirms it its start window later.
        assert 0.7 <= rec.of("start")[0][1] <= 1.1
    finally:
        await rec.detector.close()


@needs_pipecat
async def test_real_vad_with_a_short_stop_window_hears_two_utterances(recording):
    """The 1.1 s pause between the sentences is longer than the window."""
    rec = await _real("vad", stop_secs=0.5)
    try:
        await rec.feed(np.concatenate([recording, _silence(1.0)]))
        assert rec.kinds() == ["start", "end", "start", "end"]
        first, second = rec.of("end")
        # Sentence one runs 0.7-3.2 s. The audio handed on starts before it
        # (pre-roll: the first phoneme is not clipped) and ends after it.
        start = first[1] - len(first[2]) / SAMPLE_RATE
        assert 0.2 <= start <= 0.65, start
        assert 3.2 <= first[1] <= 4.0
        assert 2.5 <= len(first[2]) / SAMPLE_RATE <= 3.6
        # Sentence two runs 4.35-6.6 s.
        start = second[1] - len(second[2]) / SAMPLE_RATE
        assert 3.8 <= start <= 4.3, start
        assert second[2].dtype == np.int16 and np.abs(second[2]).max() > 1000
    finally:
        await rec.detector.close()


@needs_pipecat
async def test_real_vad_with_a_long_stop_window_hears_one(recording):
    rec = await _real("vad", stop_secs=1.6)
    try:
        await rec.feed(np.concatenate([recording, _silence(2.5)]))
        assert rec.kinds() == ["start", "end"]
        audio = rec.of("end")[0][2]
        assert 6.0 <= len(audio) / SAMPLE_RATE <= 8.2, "both sentences, in one utterance"
    finally:
        await rec.detector.close()


@needs_pipecat
async def test_real_smart_turn_produces_utterances_that_cover_the_speech(recording):
    """
    Only what does not depend on the turn model's opinion of a sentence: by
    the end of the recording plus the silence limit, everything spoken has
    been handed on, with its pre-roll, as one utterance or two.
    """
    rec = await _real("smart", max_wait_secs=1.0)
    try:
        await rec.feed(np.concatenate([recording, _silence(2.0)]))
        ends = rec.of("end")
        assert 1 <= len(ends) <= 2
        assert len(rec.of("start")) == len(ends)
        assert not rec.detector.in_utterance
        first_start = ends[0][1] - len(ends[0][2]) / SAMPLE_RATE
        assert 0.2 <= first_start <= 0.65, "starts before the first word, not at it"
        heard = sum(len(e[2]) for e in ends) / SAMPLE_RATE
        # 4.75 s of speech (2.5 + 2.25) and, if it was one utterance, the
        # pause between the sentences too.
        assert 4.75 <= heard <= 7.5, heard
        # Every end was preceded by its pause — where the transcription
        # starts speculatively — carrying the very audio the end delivers.
        for end in ends:
            paused = [p for p in rec.of("paused") if p[1] <= end[1]][-1]
            assert np.array_equal(paused[2], end[2])
    finally:
        await rec.detector.close()


@needs_pipecat
@pytest.mark.parametrize("mode", ["smart", "vad"])
async def test_real_models_report_nothing_for_silence_or_a_click(mode):
    rec = await _real(mode)
    try:
        rng = np.random.default_rng(0)
        hiss = (rng.standard_normal(SAMPLE_RATE * 3) * 30).astype(np.int16)  # a quiet room
        await rec.feed(np.concatenate([_silence(2.0), hiss]))
        assert rec.events == []
        # 100 ms of full-scale noise: a door, a dropped pen.
        click = (rng.standard_normal(SAMPLE_RATE // 10) * 12000).astype(np.int16)
        await rec.feed(np.concatenate([click, _silence(2.0)]))
        assert "end" not in rec.kinds() and "paused" not in rec.kinds(), rec.kinds()
    finally:
        await rec.detector.close()


def _word(recording: np.ndarray, offset: float, secs: float) -> np.ndarray:
    """A piece of the recording's speech with 10 ms fades: about one short word."""
    piece = recording[int(offset * SAMPLE_RATE) : int((offset + secs) * SAMPLE_RATE)]
    piece = piece.astype(np.float64)
    ramp = np.linspace(0, 1, 160)
    piece[:160] *= ramp
    piece[-160:] *= ramp[::-1]
    return piece.astype(np.int16)


@needs_pipecat
@pytest.mark.parametrize("mode", ["smart", "vad"])
@pytest.mark.parametrize("offset", [0.8, 1.0, 1.4, 2.0, 4.7])
async def test_real_models_take_a_word_of_0_4_seconds_as_a_turn(recording, mode, offset):
    """
    "Yes." "No." "Okay." Silero does not score a word's onset and decay as
    speech, so 0.4 s of it is under 0.3 s of confirmed voice — which a second
    length filter used to throw away, after the avatar had been interrupted.
    The click above is still nothing; a short word is a turn.
    """
    rec = await _real(mode, max_wait_secs=1.0)
    try:
        await rec.feed(
            np.concatenate([_silence(1.0), _word(recording, offset, 0.4), _silence(3.0)])
        )
        assert rec.kinds()[0] == "start" and rec.kinds()[-1] == "end", rec.kinds()
        assert len(rec.of("start")) == len(rec.of("end")) == 1
        audio = rec.of("end")[0][2]
        # The word and its pre-roll, not clipped to nothing.
        assert len(audio) / SAMPLE_RATE >= 0.4 and np.abs(audio).max() > 1000
    finally:
        await rec.detector.close()


@needs_pipecat
async def test_real_speech_still_registers_over_the_raised_bar(recording):
    """Barge-in has to work: the user talking over the avatar is clean speech."""
    rec = await _real("vad", stop_secs=0.5)
    try:
        rec.detector.set_avatar_speaking(True)
        await rec.feed(recording[: 4 * SAMPLE_RATE])
        assert rec.kinds() == ["start", "end"]
        assert 0.7 <= rec.of("start")[0][1] <= 1.2
    finally:
        await rec.detector.close()


@needs_pipecat
async def test_real_inference_does_not_run_on_the_event_loop(recording):
    """
    A ticker on the loop keeps ticking while six seconds of audio go through
    both models: the longest it is ever held up is far below one inference.
    """
    rec = await _real("smart")
    gaps: list[float] = []
    stop = False

    async def ticker():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.001)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    try:
        await rec.feed(recording)
        stop = True
        await task
        assert rec.of("paused"), "the turn model ran"
        # Smart Turn takes ~90 ms a pass here. On the loop, that would be the gap.
        assert max(gaps) < 0.05, max(gaps)
    finally:
        await rec.detector.close()


@needs_pipecat
async def test_warm_loads_the_models_and_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_TURN_DETECTION", "smart")
    await tt.warm()

    async def broken(*a, **k):
        raise RuntimeError("no model file")

    monkeypatch.setattr(TurnDetector, "create", broken)
    await tt.warm()


# ── the reader ───────────────────────────────────────────────────────────


def _frames_48k_stereo(mono_16k: np.ndarray) -> list:
    """`mono_16k` as the 20 ms, 48 kHz stereo s16 frames aiortc's Opus decoder emits."""
    import av

    resampler = av.AudioResampler(format="s16", layout="mono", rate=48000, frame_size=960)
    source = av.AudioFrame.from_ndarray(mono_16k.reshape(1, -1), format="s16", layout="mono")
    source.sample_rate = SAMPLE_RATE
    frames = []
    for mono in list(resampler.resample(source)) + list(resampler.resample(None)):
        # The same signal on both channels, as a mono microphone arrives
        # (letting ffmpeg upmix would pan it down by 3 dB a side).
        packed = np.repeat(mono.to_ndarray().reshape(-1), 2).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(packed, format="s16", layout="stereo")
        frame.sample_rate = 48000
        frames.append(frame)
    return frames


class FakeTrack:
    """An aiortc remote track: frames from a queue, MediaStreamError at the end."""

    kind = "audio"

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    def put(self, frames):
        for f in frames:
            self.queue.put_nowait(f)

    def end(self):
        self.queue.put_nowait(None)

    async def recv(self):
        from aiortc.mediastreams import MediaStreamError

        frame = await self.queue.get()
        if frame is None:
            raise MediaStreamError
        if isinstance(frame, Exception):
            raise frame
        return frame


class Sink:
    """Stands in for a TurnDetector: keeps what it is fed."""

    in_utterance = False

    def __init__(self):
        self.pcm = bytearray()
        self.resets = 0
        self.idles = 0

    async def feed(self, pcm):
        self.pcm += pcm

    async def audio_idle(self):
        self.idles += 1

    def reset(self):
        self.resets += 1

    def samples(self) -> np.ndarray:
        return np.frombuffer(bytes(self.pcm), np.int16)


async def _drained(track):
    while not track.queue.empty():
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)


async def test_frames_are_dropped_unseen_until_the_reader_is_told_to_listen():
    track, sink = FakeTrack(), Sink()
    reader = MicReader(track)
    task = asyncio.create_task(reader.run())
    tone = (np.sin(np.arange(SAMPLE_RATE) * 2 * np.pi * 440 / SAMPLE_RATE) * 8000).astype(np.int16)
    frames = _frames_48k_stereo(tone)

    track.put(frames)
    await _drained(track)
    # Taken off the track (nothing piles up in aiortc) and thrown away.
    assert reader.frames_read == len(frames) and reader.frames_heard == 0
    assert reader._resampler is None, "not even resampled"

    reader.listen(sink)
    track.put(frames)
    await _drained(track)
    assert reader.frames_heard == len(frames)
    heard = len(sink.pcm)
    assert heard > 0

    reader.stop_listening()
    assert sink.resets == 1, "what the detector had heard is discarded"
    track.put(frames)
    await _drained(track)
    assert len(sink.pcm) == heard, "nothing reaches the detector after it is told to stop"
    assert reader.frames_read == 3 * len(frames)

    track.end()
    await task


async def test_48k_stereo_is_resampled_to_16k_mono():
    track, sink = FakeTrack(), Sink()
    reader = MicReader(track)
    reader.listen(sink)
    tone = (np.sin(np.arange(SAMPLE_RATE) * 2 * np.pi * 440 / SAMPLE_RATE) * 8000).astype(np.int16)
    track.put(_frames_48k_stereo(tone))
    track.end()
    await reader.run()

    out = sink.samples()
    # One second in, one second out (give or take the resampler's delay).
    assert abs(len(out) - SAMPLE_RATE) < 800, len(out)
    # Still a 440 Hz tone at the same level: mono, at the right rate.
    spectrum = np.abs(np.fft.rfft(out[2000:14000].astype(np.float64)))
    peak_hz = np.argmax(spectrum) * SAMPLE_RATE / 12000
    assert abs(peak_hz - 440) < 5, peak_hz
    assert 7000 < np.abs(out[2000:14000]).max() < 9000


async def test_the_reader_ends_cleanly_when_the_track_does():
    track, sink = FakeTrack(), Sink()
    reader = MicReader(track)
    reader.listen(sink)
    track.end()
    await reader.run()  # returns; MediaStreamError does not escape
    assert reader.ended and not reader.listening


async def test_the_reader_ends_cleanly_when_cancelled():
    track, sink = FakeTrack(), Sink()
    reader = MicReader(track)
    reader.listen(sink)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert reader.ended and not reader.listening and sink.resets == 1


async def test_a_track_that_fails_some_other_way_does_not_raise_either():
    track = FakeTrack()
    reader = MicReader(track)
    track.put([RuntimeError("decoder exploded")])
    await reader.run()
    assert reader.ended


async def test_a_stall_mid_utterance_ends_it_and_what_follows_is_a_new_one():
    """
    Packets stop while the user is talking (a network stall). Nothing else
    would end the utterance — every other limit is counted in audio — so the
    reader gives up waiting after its idle time, as Pipecat's does.
    """
    track = FakeTrack()
    rec, _ = _scripted(Verdict.COMPLETE, Verdict.COMPLETE)
    reader = MicReader(track, idle_secs=0.1)
    reader.listen(rec.detector)
    task = asyncio.create_task(reader.run())
    try:
        track.put(_frames_48k_stereo(np.concatenate([_silence(0.5), _speech(1.0)])))
        await _drained(track)
        assert rec.kinds() == ["start"], "mid-utterance when the audio stops"

        await _until("the utterance to be closed", lambda: "end" in rec.kinds(), timeout=2)
        assert rec.kinds() == ["start", "paused", "end"]
        first = rec.of("end")[0][2]
        assert 0.9 <= len(first) / SAMPLE_RATE <= 1.0 + PRE_ROLL_SECS + 0.5
        assert reader._resampler is None, "the resampler's tail is from before the gap"

        # Nothing is open now, so a silent track is waited on for as long as
        # it likes, and no frame was lost to the deadline: the next one in
        # is the next one heard.
        await asyncio.sleep(0.25)
        read = reader.frames_read
        frames = _frames_48k_stereo(np.concatenate([_silence(0.3), _speech(0.8), _silence(1.0)]))
        track.put(frames)
        await _drained(track)
        assert reader.frames_read == read + len(frames)
        assert rec.kinds() == ["start", "paused", "end"] * 2
        second = rec.of("end")[1][2]
        assert len(second) / SAMPLE_RATE <= 0.8 + PRE_ROLL_SECS + 0.5, "none of the first in it"
    finally:
        track.end()
        await task
    assert reader.ended


async def test_the_reader_waits_without_a_deadline_between_utterances():
    track, sink = FakeTrack(), Sink()
    reader = MicReader(track, idle_secs=0.02)
    reader.listen(sink)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.1)
    assert sink.idles == 0, "a quiet track with nothing open is not a stall"
    # Mid-utterance it is — and the reader can still be cancelled while it
    # waits against the deadline.
    sink.in_utterance = True
    tone = np.full(SAMPLE_RATE // 10, 900, np.int16)
    track.put(_frames_48k_stereo(tone))
    await asyncio.sleep(0.1)
    assert sink.idles >= 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and reader.ended


# ── a real peer connection: the recording, up the wire ───────────────────


def _udp_loopback_works() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a:
            a.bind(("127.0.0.1", 0))
            a.settimeout(1)
            a.sendto(b"x", a.getsockname())
            return a.recv(1) == b"x"
    except OSError:
        return False


needs_aiortc = pytest.mark.skipif(not rtc.WEBRTC_AVAILABLE, reason="aiortc is not installed")
# Skipped only where the sandbox forbids UDP altogether; on a developer
# machine or a CI runner this runs.
needs_udp = pytest.mark.skipif(not _udp_loopback_works(), reason="no UDP loopback here")


@pytest.fixture
def idle_image(tmp_path) -> str:
    from PIL import Image

    path = tmp_path / "idle.png"
    Image.new("RGB", (64, 64), (0, 0, 255)).save(path)
    return str(path)


def _microphone(samples_16k: np.ndarray):
    """
    A browser's microphone track: silence, paced in real time, until play()
    — then the recording, then silence again.
    """
    from aiortc import MediaStreamTrack

    frames = _frames_48k_stereo(samples_16k)

    class Microphone(MediaStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self._queue: list = []
            self._n = 0
            self._epoch = None

        def play(self):
            self._queue = list(frames)

        async def recv(self):
            import av

            if self._epoch is None:
                self._epoch = time.monotonic()
            wait = self._epoch + self._n * 0.02 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if self._queue:
                frame = self._queue.pop(0)
            else:
                frame = av.AudioFrame.from_ndarray(
                    np.zeros((1, 1920), np.int16), format="s16", layout="stereo"
                )
                frame.sample_rate = 48000
            frame.pts = self._n * 960
            frame.time_base = fractions.Fraction(1, 48000)
            self._n += 1
            return frame

    return Microphone()


class TalkingBrowser:
    """A browser in hands-free on the stream: audio sendrecv, video recvonly."""

    def __init__(self, microphone):
        from aiortc import RTCConfiguration, RTCPeerConnection

        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.pc.addTrack(microphone)  # one audio transceiver, sendrecv
        self.pc.addTransceiver("video", direction="recvonly")
        self.received = {"audio": 0, "video": 0}
        self._tasks: list[asyncio.Task] = []

        @self.pc.on("track")
        def on_track(track):
            self._tasks.append(asyncio.create_task(self._read(track)))

    async def _read(self, track):
        try:
            while True:
                await track.recv()
                self.received[track.kind] += 1
        except Exception:
            pass

    async def offer(self) -> str:
        await self.pc.setLocalDescription(await self.pc.createOffer())
        return self.pc.localDescription.sdp

    async def accept(self, answer_sdp: str) -> None:
        from aiortc import RTCSessionDescription

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

    async def close(self):
        for t in self._tasks:
            t.cancel()
        await self.pc.close()


async def _until(what: str, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _media_sections(sdp: str) -> dict[str, list[str]]:
    """Direction attributes per m-line kind, in order."""
    out: dict[str, list[str]] = {}
    kind = None
    for line in sdp.splitlines():
        if line.startswith("m="):
            kind = line[2:].split(" ")[0]
            out.setdefault(kind, [])
        elif kind and line in ("a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"):
            out[kind].append(line[2:])
    return out


@needs_aiortc
@pytest.mark.parametrize("accept", [True, False])
async def test_a_sendrecv_offer_is_answered_on_one_audio_line(idle_image, monkeypatch, accept):
    """No media flows here; this is about the shape of the answer."""
    monkeypatch.setattr(settings, "WEBRTC_ICE_SERVERS", "[]")
    tracks: list = []
    stream = await rtc.AvatarStream.create(idle_image, size=64, fps=25)
    stream.accept_mic = accept
    stream.on_remote_audio = tracks.append
    browser = TalkingBrowser(_microphone(_silence(0.1)))
    try:
        answer = await stream.handle_offer(await browser.offer())
        sections = _media_sections(answer)
        if accept:
            # The avatar's voice and the user's microphone share the line:
            # one audio m-line, both directions. Video is never received.
            assert sections == {"audio": ["sendrecv"], "video": ["sendonly"]}
            assert len(tracks) == 1 and tracks[0].kind == "audio"
        else:
            # Refused in the answer, so the browser does not send and aiortc
            # does not decode: exactly the answer a recvonly offer gets.
            assert sections == {"audio": ["sendonly"], "video": ["sendonly"]}
            assert tracks == []
        senders = [t.sender.track for t in stream._pc.getTransceivers() if t.kind == "audio"]
        assert senders == [stream.audio], "our own track rides the same transceiver"
    finally:
        await browser.close()
        await stream.close()


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self, code=1000):
        pass

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == kind]


@needs_pipecat
@needs_aiortc
@needs_udp
async def test_speech_sent_up_a_real_peer_connection_becomes_a_turn(
    idle_image, recording, monkeypatch, tmp_path
):
    """
    End to end through real RTP: the browser's microphone plays the first
    sentence of the recording into a sendrecv audio transceiver; the server
    answers, listens once told to, and the real VAD and turn model hand the
    utterance to the (stubbed) transcriber, whose text becomes a turn.
    """
    import app.websocket as wsmod
    from app.websocket import ConnectionManager

    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    monkeypatch.setattr(settings, "VOICE_TURN_DETECTION", "smart")
    monkeypatch.setattr(settings, "TURN_MAX_WAIT_SECS", 1.0)
    monkeypatch.setattr(settings, "WEBRTC_ICE_SERVERS", "[]")
    monkeypatch.setattr(settings, "AVATAR_RESOLUTION", 64)
    monkeypatch.setattr(wsmod, "TMPDIR", tmp_path)

    heard: list[np.ndarray] = []
    turns: list[str] = []

    async def transcribe(audio, language="en"):
        heard.append(audio)
        return "the birch canoe slid on the smooth planks"

    m = ConnectionManager()

    async def run_turn(session_id, text):
        turns.append(text)

    monkeypatch.setattr(wsmod.stt_service, "transcribe", transcribe)
    monkeypatch.setattr(m, "_handle_text_input_inner", run_turn)

    ws = FakeWS()
    m.active_connections["s1"] = ws
    m._send_locks["s1"] = asyncio.Lock()
    m.session_data["s1"] = {
        "messages": [],
        "avatar_image_local": idle_image,
        "user_id": "u1",
        "language": "en",
    }

    microphone = _microphone(recording[: int(3.6 * SAMPLE_RATE)])
    browser = TalkingBrowser(microphone)
    try:
        await m.handle_webrtc_offer("s1", await browser.offer())
        await _until("the answer", lambda: ws.of("webrtc_answer"))
        answer = ws.of("webrtc_answer")[0]["sdp"]
        assert _media_sections(answer) == {"audio": ["sendrecv"], "video": ["sendonly"]}
        await browser.accept(answer)
        await _until("the stream", lambda: ws.of("media_transport"))
        # Both directions on the one connection: the avatar still arrives.
        await _until("the avatar's media", lambda: all(browser.received.values()))

        # The microphone is already sending (silence). Not asked to listen
        # yet: its audio is read off the track and dropped.
        mic = m._mics["s1"]
        await _until("microphone audio arriving", lambda: mic.reader.frames_read > 10)
        assert mic.reader.frames_heard == 0 and not ws.of("mic_state")

        await m.handle_mic("s1", True)
        assert ws.of("mic_state") == [{"type": "mic_state", "listening": True}]
        microphone.play()

        await _until("speech to be noticed", lambda: ws.of("user_speech"), timeout=6)
        assert ws.of("user_speech")[0] == {"type": "user_speech", "state": "started"}
        await _until("the turn", lambda: turns, timeout=12)
        assert turns == ["the birch canoe slid on the smooth planks"]
        assert {"type": "user_speech", "state": "stopped", "reason": "turn"} in ws.sent
        assert ws.of("transcription") == [
            {"type": "transcription", "text": "the birch canoe slid on the smooth planks"}
        ]

        # What reached the transcriber is the sentence itself, through Opus
        # and back: 16 kHz int16, about as long as the speech plus pre-roll.
        audio = heard[-1]
        assert isinstance(audio, np.ndarray) and audio.dtype == np.int16 and audio.ndim == 1
        assert 2.4 <= len(audio) / SAMPLE_RATE <= 3.6, len(audio) / SAMPLE_RATE
        assert np.abs(audio).max() > 1000

        # Told to stop: the server says so and stops looking at the audio.
        await m.handle_mic("s1", False)
        assert ws.of("mic_state")[-1] == {"type": "mic_state", "listening": False}
        seen = mic.reader.frames_heard
        read = mic.reader.frames_read
        await _until("more audio to arrive", lambda: mic.reader.frames_read > read + 10)
        assert mic.reader.frames_heard == seen

        await m.disconnect("s1")
        assert "s1" not in m._mics
    finally:
        await browser.close()
        for t in list(m._background):
            t.cancel()
        await asyncio.gather(*list(m._background), return_exceptions=True)
