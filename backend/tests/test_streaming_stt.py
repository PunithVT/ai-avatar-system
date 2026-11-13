"""
Streaming speech-to-text (services/streaming_stt.py) and its place in the
stream microphone's path (websocket.py _mic_*).

Three layers:

  - the provider's queueing, on a fake recognizer: what feed() returns and
    when, what a failure does, who gets the decoder next;
  - the microphone path in the manager, on the same fake: which utterances go
    to the streaming engine and which to Whisper, and that every rule the
    Whisper path keeps (continuation, the rate limit, listening stopping) is
    kept here too;
  - the REAL model, over the recording. The model is a 475 MB download that
    is not in the repository, so these skip wherever it has not been fetched
    (CI) and run wherever scripts/setup_streaming_stt.sh has been.
"""

import asyncio
import logging
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import app.services.streaming_stt as sstt
import app.services.turn_taking as tt
import app.websocket as wsmod
from app.config import settings
from app.services.streaming_stt import (
    MAX_BACKLOG_SECS,
    MAX_SESSION_LAG_SECS,
    MODEL_FILES,
    REPLAY_GRACE,
    SAMPLE_RATE,
    SUPPORTED_LANGUAGES,
    StreamingSTT,
    StreamingSTTError,
)
from app.services.turn_taking import TurnDetector
from tests.test_turn_pipeline import (  # noqa: F401 — rig and mic are fixtures
    LISTENING,
    STARTED,
    STOPPED_IGNORED,
    STOPPED_TURN,
    _of,
    _settle,
    mic,
    rig,
)
from tests.test_turn_taking import Recorder, _scripted, _silence, _speech, _word

# The real one, kept from before the `mic` fixture puts a fake in its place.
_CREATE_DETECTOR = TurnDetector.create

FIXTURE = Path(__file__).parent / "fixtures" / "speech_two_sentences.flac"
KEY_WORDS = ("birch", "canoe", "glue", "sheet", "blue")


# ── a recognizer that reads its transcript off the samples ───────────────


def _words(*ids: int, secs: float = 0.1) -> np.ndarray:
    """Audio the fake recognizer hears as "w<id> w<id> …": one block per word."""
    return np.concatenate([np.full(int(secs * SAMPLE_RATE), i, np.int16) for i in ids])


class FakeStream:
    def __init__(self):
        self.options: dict = {}
        self.inbox: list[np.ndarray] = []
        self.heard: list[int] = []  # word ids, in the order they were decoded
        self.samples = 0
        self.finished = False

    def set_option(self, key, value):
        assert not self.samples, "the language is set before any audio"
        self.options[key] = value

    def accept_waveform(self, rate, samples):
        assert rate == SAMPLE_RATE and samples.dtype == np.float32
        assert np.abs(samples).max(initial=0) <= 1.0
        self.inbox.append(samples)
        self.samples += len(samples)

    def input_finished(self):
        self.finished = True


class FakeRecognizer:
    """sherpa-onnx's OnlineRecognizer, as far as the provider uses it."""

    def __init__(self):
        self.streams: list[FakeStream] = []
        self.order: list[FakeStream] = []  # which stream each decode was for
        self.threads: set[str] = set()
        # Cleared to hold the decoder inside a decode until set again.
        self.gate = threading.Event()
        self.gate.set()
        self.fail_decode = False
        self.fail_finish = False

    def create_stream(self):
        self.streams.append(FakeStream())
        return self.streams[-1]

    def is_ready(self, stream):
        return bool(stream.inbox)

    def decode_stream(self, stream):
        self.threads.add(threading.current_thread().name)
        self.gate.wait(10)
        if self.fail_decode or (self.fail_finish and stream.finished):
            stream.inbox.clear()
            raise RuntimeError("onnxruntime fell over")
        self.order.append(stream)
        for value in np.rint(stream.inbox.pop(0) * 32768).astype(int):
            if value and (not stream.heard or stream.heard[-1] != value):
                stream.heard.append(int(value))

    def get_result(self, stream):
        return " ".join(f"w{i}" for i in stream.heard)


@pytest.fixture
def engine(monkeypatch):
    """A provider that is configured, "installed", and runs the fake recognizer."""
    monkeypatch.setattr(settings, "STT_STREAMING", "sherpa")
    provider = StreamingSTT()
    provider.fake = FakeRecognizer()
    monkeypatch.setattr(provider, "_missing", lambda: None)
    monkeypatch.setattr(provider, "_load", lambda: provider.fake)
    yield provider
    provider.fake.gate.set()
    provider._executor.shutdown(wait=True)


async def _idle(session, timeout=5.0):
    """Wait for the decoder to have nothing left of `session`'s to do."""
    deadline = time.monotonic() + timeout
    while session._scheduled:
        assert time.monotonic() < deadline, "the decoder never went idle"
        await asyncio.sleep(0.002)


async def _held(session, timeout=5.0):
    """
    With the gate closed: wait until the decoder has taken what `session`
    had queued and is stuck inside it. Whatever is fed after this stays
    queued — without it, how much the decoder's thread happened to pick up
    first would be a matter of scheduling.
    """
    deadline = time.monotonic() + timeout
    while session._pending or not session._scheduled or session.decoded_samples == 0:
        assert time.monotonic() < deadline, "the decoder never took the audio"
        await asyncio.sleep(0.002)


# ── the provider, on the fake ────────────────────────────────────────────


async def test_feed_returns_the_words_so_far_only_when_they_change(engine):
    session = engine.open("en")
    assert session.feed(_words(1)) is None, "nothing decoded yet; feed does not wait for it"
    await _idle(session)
    # The decoder is held while the two calls are compared: left running, it
    # sometimes finished the first w2 between them, and the second call then
    # (rightly) returned "w1 w2".
    engine.fake.gate.clear()
    assert session.feed(_words(2)) == "w1"
    assert session.feed(_words(2)) is None, "unchanged since the last call"
    engine.fake.gate.set()
    await _idle(session)
    assert session.feed(_words(3)) == "w1 w2"
    assert await session.finish() == "w1 w2 w3"
    stream = engine.fake.streams[0]
    assert stream.options == {"language": "en"} and stream.finished
    # Silence behind the audio, so the model lets go of the last word.
    padding = int(sstt.LEAD_IN_SECS * SAMPLE_RATE) + int(sstt.TAIL_PADDING_SECS * SAMPLE_RATE)
    assert stream.samples == len(_words(1, 2, 2, 3)) + padding
    assert engine.fake.threads and all(t.startswith("stt-stream") for t in engine.fake.threads)


async def test_nothing_is_returned_by_feed_once_finish_has_been_called(engine):
    """Partials are in order and never follow the final text."""
    session = engine.open("en")
    session.feed(_words(1))
    await _idle(session)
    engine.fake.gate.clear()  # the final decode is held...
    final = asyncio.ensure_future(session.finish())
    await asyncio.sleep(0.02)
    # ...and "w1", decoded and never handed out, is not handed out now.
    assert session.feed(_words(9)) is None
    engine.fake.gate.set()
    assert await final == "w1", "audio offered after finish() is not part of the utterance"
    assert session.feed(_words(9)) is None


async def test_finish_waits_for_audio_that_was_still_queued(engine):
    engine.fake.gate.clear()
    session = engine.open("en")
    for word in (1, 2, 3, 4):
        session.feed(_words(word))
    final = asyncio.ensure_future(session.finish())
    await asyncio.sleep(0.02)
    assert not final.done()
    engine.fake.gate.set()
    assert await final == "w1 w2 w3 w4"
    assert engine.backlog_secs == 0


async def test_feed_does_not_block_while_the_decoder_is_busy(engine):
    engine.fake.gate.clear()
    session = engine.open("en")
    started = time.perf_counter()
    for _ in range(200):
        session.feed(_words(1, secs=0.02))
    assert time.perf_counter() - started < 0.2, "200 frames queued while a decode is stuck"
    engine.fake.gate.set()
    assert await session.finish() == "w1"


async def test_an_engine_failure_marks_the_session_and_leaves_the_others_alone(engine):
    broken, healthy = engine.open("en"), engine.open("en")
    engine.fake.fail_decode = True
    broken.feed(_words(1))
    await _idle(broken)
    assert broken.failed and broken.feed(_words(2)) is None
    with pytest.raises(StreamingSTTError):
        await broken.finish()
    engine.fake.fail_decode = False
    healthy.feed(_words(5))
    assert await healthy.finish() == "w5"
    assert not healthy.failed and engine.backlog_secs == 0


async def test_a_failure_while_finishing_reaches_whoever_is_waiting(engine):
    session = engine.open("en")
    engine.fake.fail_finish = True
    session.feed(_words(1))
    with pytest.raises(StreamingSTTError, match="onnxruntime fell over"):
        await session.finish()


async def test_discard_drops_what_was_queued_and_ends_a_pending_finish(engine):
    engine.fake.gate.clear()
    session = engine.open("en")
    for word in range(1, 30):
        session.feed(_words(word, secs=0.5))
    final = asyncio.ensure_future(session.finish())
    await asyncio.sleep(0.02)
    session.discard()
    session.discard()  # idempotent
    engine.fake.gate.set()
    await asyncio.wait({final}, timeout=2)
    assert final.cancelled()
    await _idle(session)
    assert session.decoded_samples < session.fed_samples, "the rest was never decoded"
    assert engine.backlog_secs == 0 and not engine.fake.streams[0].finished
    with pytest.raises(StreamingSTTError):
        await session.finish()


async def test_sessions_take_turns_on_the_decoder_a_slice_at_a_time(engine):
    """One session arriving with a lot at once does not hold up another's partials."""
    engine.fake.gate.clear()
    long, short = engine.open("en"), engine.open("en")
    long.feed(_words(1, secs=5.0))  # carried audio arrives as one piece
    short.feed(_words(2, secs=0.5))
    engine.fake.gate.set()
    assert await short.finish() == "w2" and await long.finish() == "w1"
    first, second = engine.fake.streams
    order = engine.fake.order
    # The short one's audio was decoded after one slice of the long one,
    # not after all five seconds of it.
    assert order.index(second) <= 2 and order.count(first) >= 5, [
        engine.fake.streams.index(s) for s in order
    ]
    assert len(engine.fake.threads) == 1, "one decode at a time, on one thread"


async def test_a_decoder_that_is_behind_takes_on_no_new_utterances(engine):
    engine.fake.gate.clear()
    session = engine.open("en")
    session.feed(_words(1, secs=MAX_BACKLOG_SECS + 2))
    assert engine.open("en") is None, "this one goes to Whisper"
    engine.fake.gate.set()
    await session.finish()
    assert engine.open("en") is not None


@pytest.fixture
def clock(monkeypatch):
    """The clock a session's lateness is measured on, moved by the test."""

    class Clock:
        now = 1000.0

    c = Clock()
    monkeypatch.setattr(sstt, "_now", lambda: c.now)
    return c


async def test_speakers_who_started_together_are_given_up_as_they_fall_behind(engine, clock):
    """
    The backlog is looked at when an utterance starts, when its speaker has
    said nothing: six people who start in the same moment are all taken on.
    A decoder that cannot keep up with them used to be found out only when
    each one stopped, as a final transcript that never came.
    """
    engine.fake.gate.clear()  # far too slow for six
    sessions = [engine.open("en") for _ in range(6)]
    assert all(s is not None for s in sessions), "nobody was behind when they were asked"
    for second in range(3):
        clock.now = 1000.0 + second
        for s in sessions:
            s.feed(_words(1, secs=1.0))
        if not second:
            await _held(sessions[0])  # the decoder is inside the first one's first second
    assert not any(s.failed for s in sessions), "late, but not yet too late"

    clock.now = 1000.0 + MAX_SESSION_LAG_SECS + 0.1
    for s in sessions:
        assert s.feed(_words(2, secs=0.032)) is None
    # Each at its own next frame, mid-utterance. Not yet the one the decoder
    # is inside: its first second has been taken, so it is a second less late.
    assert [s.failed for s in sessions] == [False] + [True] * 5
    clock.now += 1.0
    sessions[0].feed(_words(2, secs=0.032))
    assert sessions[0].failed
    assert engine.backlog_secs == 0, "and what they had queued is not decoded"
    with pytest.raises(StreamingSTTError, match="behind"):
        await sessions[0].finish()

    engine.fake.gate.set()
    for s in sessions:
        await _idle(s)
    # Only the slice the decoder was already inside was heard.
    assert [s.decoded_samples for s in sessions] == [SAMPLE_RATE, 0, 0, 0, 0, 0]
    assert engine.open("en") is not None, "the next utterance is taken on again"


async def test_lateness_is_how_long_audio_has_waited_not_how_much_arrived_at_once(engine, clock):
    # A decoder that keeps up is never late, however long the utterance.
    session = engine.open("en")
    for second in range(12):
        clock.now = 1000.0 + second
        session.feed(_words(1 + second, secs=1.0))
        await _idle(session)
    assert not session.failed
    # Nor is one handed ten seconds in a single piece: it is not late until
    # time has passed and the decoder has still not got to it.
    engine.fake.gate.clear()
    session.feed(_words(20, secs=10.0))
    session.feed(_words(21))
    assert not session.failed
    engine.fake.gate.set()
    assert (await session.finish()).endswith("w20 w21")


async def test_a_replay_is_not_live_backlog_and_turns_nobody_away(engine, clock):
    """
    A continued utterance arrives with everything already said, in one piece.
    That is not people talking at once, and it used to send every other
    user's new utterance to Whisper for as long as it took to work off.
    """
    engine.fake.gate.clear()
    continued = engine.open("en")
    continued.replay(_words(1, secs=MAX_BACKLOG_SECS * 3))
    assert engine.backlog_secs == 0
    other = engine.open("en")
    assert other is not None, "another speaker's utterance is still streamed"

    # The live audio behind the replay waits for it, and is allowed to:
    # REPLAY_GRACE for every second replayed, on top of the usual bound.
    continued.feed(_words(2))
    assert engine.backlog_secs == pytest.approx(0.1), "live audio does count"
    allowed = MAX_SESSION_LAG_SECS + REPLAY_GRACE * MAX_BACKLOG_SECS * 3
    clock.now += allowed - 0.1
    continued.feed(_words(3))
    assert not continued.failed

    engine.fake.gate.set()
    assert await continued.finish() == "w1 w2 w3", "the replay first, then what was said after it"
    assert await other.finish() == ""
    assert engine.backlog_secs == 0


async def test_a_replay_buys_time_only_until_the_session_has_caught_up(engine, clock):
    session = engine.open("en")
    session.replay(_words(1, secs=20.0))
    session.feed(_words(2))
    await _idle(session)  # all of it decoded: level with the speaker
    engine.fake.gate.clear()
    before = session.decoded_samples
    session.feed(_words(3))
    while session._pending or session.decoded_samples == before:
        await asyncio.sleep(0.002)  # the decoder is inside w3, and stays there
    session.feed(_words(4))
    clock.now += MAX_SESSION_LAG_SECS + 0.1
    session.feed(_words(5))
    assert session.failed, "held to the same bound as any other session from here"

    # And one that never catches up is given up too, later.
    engine.fake.gate.set()
    await _idle(session)
    engine.fake.gate.clear()
    slow = engine.open("en")
    slow.replay(_words(1, secs=4.0))
    slow.feed(_words(2))
    clock.now += MAX_SESSION_LAG_SECS + REPLAY_GRACE * 4.0 + 0.1
    slow.feed(_words(3))
    assert slow.failed


def test_only_the_languages_the_model_is_good_at_are_supported(engine):
    assert SUPPORTED_LANGUAGES == {
        *("en", "es", "fr", "it", "pt", "nl", "de", "tr", "ru", "ar", "hi", "ja", "ko", "vi", "uk")
    }
    assert engine.supports("en") and engine.supports(" DE ")
    # The model card's "broad coverage" tier, and languages it does not list.
    for language in ("pl", "sv", "zh", "da", "fi", "no", "he", "el", "", None):
        assert not engine.supports(language), language
        assert engine.open(language) is None
    assert engine.open("ja").language == "ja"


def test_off_by_default_and_anything_unknown_is_off(monkeypatch):
    assert settings.STT_STREAMING == "off", "the default"
    provider = StreamingSTT()
    assert not provider.configured and not provider.available() and provider.open("en") is None
    for value, mode in [
        ("sherpa", "sherpa"),
        (" Sherpa ", "sherpa"),
        ("whisper", "off"),
        ("", "off"),
    ]:
        monkeypatch.setattr(settings, "STT_STREAMING", value)
        assert sstt.streaming_mode() == mode


def test_a_missing_model_is_reported_once_with_the_path_and_the_script(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setattr(settings, "STT_STREAMING", "sherpa")
    monkeypatch.setattr(settings, "STT_STREAMING_MODEL_DIR", str(tmp_path / "nowhere"))
    provider = StreamingSTT()
    with caplog.at_level(logging.WARNING, logger="app.services.streaming_stt"):
        assert not provider.available()
        assert provider.open("en") is None and provider.open("en") is None
        assert not provider.installed()
    assert len(caplog.records) == 1, [r.message for r in caplog.records]
    line = caplog.records[0].message
    assert str(tmp_path / "nowhere") in line and "scripts/setup_streaming_stt.sh" in line

    # Three of the four files is still not a model.
    partial = tmp_path / "partial"
    partial.mkdir()
    for name in MODEL_FILES[:-1]:
        (partial / name).write_bytes(b"x")
    monkeypatch.setattr(settings, "STT_STREAMING_MODEL_DIR", str(partial))
    assert provider.model_dir() is None and not provider.available()
    (partial / MODEL_FILES[-1]).write_bytes(b"x")
    assert provider.model_dir() == partial.resolve()


def test_a_missing_package_is_unavailable_not_a_crash(monkeypatch, caplog):
    monkeypatch.setattr(settings, "STT_STREAMING", "sherpa")
    monkeypatch.setattr(sstt.importlib.util, "find_spec", lambda name: None)
    provider = StreamingSTT()
    with caplog.at_level(logging.WARNING, logger="app.services.streaming_stt"):
        assert not provider.available() and not provider.installed()
    assert "sherpa-onnx package is not installed" in caplog.records[0].message


def test_the_model_directory_resolves_against_backend_like_musetalk(monkeypatch):
    backend = Path(sstt.__file__).resolve().parent.parent.parent
    assert sstt._find_model_dir("no/such/place") is None
    default = backend / settings.STT_STREAMING_MODEL_DIR
    if all((default / name).is_file() for name in MODEL_FILES):
        monkeypatch.chdir("/")  # not found as given; found against backend/
        assert sstt._find_model_dir(settings.STT_STREAMING_MODEL_DIR) == default.resolve()


async def test_a_model_that_will_not_load_fails_the_utterance_and_is_not_retried(engine, caplog):
    loads = []

    def load():
        loads.append(1)
        raise OSError("bad model file")

    engine._load = load
    session = engine.open("en")
    session.feed(_words(1))
    with caplog.at_level(logging.WARNING, logger="app.services.streaming_stt"):
        with pytest.raises(StreamingSTTError):
            await session.finish()
    assert session.failed
    assert not engine.available() and engine.open("en") is None
    await engine.warm()  # never raises, and does not try again
    assert loads == [1]


async def test_warm_loads_the_model_once_and_never_raises(engine, monkeypatch):
    assert not engine.loaded
    await engine.warm()
    assert engine.loaded and engine._recognizer is engine.fake
    engine._load = lambda: pytest.fail("loaded twice")
    await engine.warm()

    monkeypatch.setattr(settings, "STT_STREAMING", "off")
    cold = StreamingSTT()
    cold._load = lambda: pytest.fail("loaded while switched off")
    await cold.warm()
    assert not cold.loaded


# ── the detector hands the utterance on as it arrives ────────────────────


async def test_the_detector_reports_the_utterance_live_from_the_pre_roll_on():
    rec, _ = _scripted(smart=False)
    live: list[tuple[int, np.ndarray]] = []
    rec.detector.on_speech_audio = lambda audio: live.append((len(rec.events), audio))
    lead_in = np.arange(1, 8001, dtype=np.int16) % 500  # recognisable, below the VAD's bar
    await rec.feed(np.concatenate([lead_in, _speech(1.0), _silence(1.0)]))

    assert rec.kinds() == ["start", "end"]
    heard = np.concatenate([audio for _, audio in live])
    assert heard.dtype == np.int16
    final = rec.of("end")[0][2]
    # Everything the utterance turned out to hold, in order, from its very
    # first sample — the pre-roll included...
    assert np.array_equal(heard[: len(final)], final)
    assert len(live[0][1]) >= int((tt.PRE_ROLL_SECS + tt.VAD_START_SECS) * SAMPLE_RATE) - 512
    assert live[0][1][0] != 900, "it opens with what came before the speech"
    # ...the first piece right after the start was reported, and none of it
    # after the end.
    assert live[0][0] == 1 and all(seen == 1 for seen, _ in live)


async def test_live_audio_covers_a_pause_the_turn_model_waited_through():
    from tests.test_turn_taking import Verdict

    rec, _ = _scripted(Verdict.INCOMPLETE, Verdict.COMPLETE)
    live: list[np.ndarray] = []
    rec.detector.on_speech_audio = live.append
    await rec.feed(
        np.concatenate([_silence(0.5), _speech(1.0), _silence(0.6), _speech(1.0), _silence(1.0)])
    )
    final = rec.of("end")[0][2]
    assert np.array_equal(np.concatenate(live)[: len(final)], final)


async def test_no_live_audio_follows_a_reset_from_the_start_handler():
    rec, _ = _scripted(smart=False)
    live: list[np.ndarray] = []
    rec.detector.on_speech_audio = live.append
    rec.detector.on_speech_start = rec.detector.reset  # as `mic active:false` does
    await rec.feed(np.concatenate([_silence(0.5), _speech(0.3)]))
    assert live == []


# ── the microphone path, on the fake ─────────────────────────────────────


@pytest.fixture
def streaming(engine, monkeypatch):
    monkeypatch.setattr(wsmod, "streaming_stt", engine)
    return engine


def _partials(rig):
    return [x["text"] for x in _of(rig, "transcription") if x.get("final") is False]


def _finals(rig):
    return [x["text"] for x in _of(rig, "transcription") if "final" not in x]


async def _hear(m, detector, audio, piece=512):
    """Report `audio` as the detector does: a frame at a time."""
    for i in range(0, len(audio), piece):
        await detector.on_speech_audio(audio[i : i + piece])


async def test_an_utterance_is_transcribed_as_it_is_spoken_and_whisper_is_not_called(
    rig, mic, streaming, monkeypatch
):
    monkeypatch.setattr(wsmod, "STREAM_PARTIAL_MIN_INTERVAL_SECS", 0.0)
    m = rig.manager
    rig.reply = "A reply."
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt
    assert session is not None and session.language == "en"

    spoken = _words(1, 2, 3)
    for word in (1, 2, 3):
        await _hear(m, detector, _words(word))
        await _idle(session)
    await detector.on_speech_audio(_silence(0.032))  # a later frame collects the last words
    assert _partials(rig) == ["w1", "w1 w2", "w1 w2 w3"]
    assert not rig.llm_calls, "a running transcript starts nothing"

    await detector.on_speech_end(spoken)
    await mic.turn()
    assert mic.stt_calls == [], "Whisper was not asked"
    sent = rig.socket.sent
    assert sent[:2] == [LISTENING, STARTED]
    after = [x for x in sent[2:] if x.get("final") is not False]
    assert after[:3] == [
        STOPPED_TURN,
        {"type": "status", "message": "Transcribing audio…", "stage": "transcription"},
        # The frame the Whisper path sends: no `final` key.
        {"type": "transcription", "text": "w1 w2 w3"},
    ]
    assert m.session_data["s1"]["messages"] == [
        {"role": "user", "content": "w1 w2 w3"},
        {"role": "assistant", "content": "A reply."},
    ]
    kinds = [(x["type"], x.get("final")) for x in sent]
    assert max(i for i, k in enumerate(kinds) if k == ("transcription", False)) < kinds.index(
        ("transcription", None)
    ), "no running transcript after the final one"
    assert m._mics["s1"].stream_stt is None


async def test_running_transcripts_are_rate_limited_and_the_newest_one_wins(
    rig, mic, streaming, monkeypatch
):
    monkeypatch.setattr(wsmod, "STREAM_PARTIAL_MIN_INTERVAL_SECS", 3600.0)
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt
    for word in (1, 2, 3, 4):
        await _hear(m, detector, _words(word))
        await _idle(session)
    await detector.on_speech_audio(_silence(0.032))
    assert _partials(rig) == ["w1"], "the first goes out at once; the rest wait"

    monkeypatch.setattr(wsmod, "STREAM_PARTIAL_MIN_INTERVAL_SECS", 0.0)
    await detector.on_speech_audio(_silence(0.032))
    assert _partials(rig) == ["w1", "w1 w2 w3 w4"], "then the latest, not each one held back"
    await detector.on_speech_audio(_silence(0.032))
    assert len(_partials(rig)) == 2, "and nothing is repeated"


def test_the_default_rate_is_four_a_second():
    assert wsmod.STREAM_PARTIAL_MIN_INTERVAL_SECS == 0.25


async def test_a_held_back_running_transcript_is_dropped_when_the_utterance_ends(
    rig, mic, streaming, monkeypatch
):
    monkeypatch.setattr(wsmod, "STREAM_PARTIAL_MIN_INTERVAL_SECS", 3600.0)
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt
    for word in (1, 2):
        await _hear(m, detector, _words(word))
        await _idle(session)
    await detector.on_speech_audio(_silence(0.032))
    await detector.on_speech_end(_words(1, 2))
    await mic.turn()
    assert _partials(rig) == ["w1"] and _finals(rig) == ["w1 w2"]
    assert rig.socket.sent.index(
        {"type": "transcription", "text": "w1", "final": False}
    ) < rig.socket.sent.index({"type": "transcription", "text": "w1 w2"})
    # A frame from a detector still mid-flight goes nowhere.
    sent = len(rig.socket.sent)
    await detector.on_speech_audio(_words(7))
    assert len(rig.socket.sent) == sent


@pytest.mark.parametrize("language, streamed", [("en", True), ("de", True), ("pl", False)])
async def test_the_session_language_decides_which_engine_hears_it(
    rig, mic, streaming, language, streamed
):
    m = rig.manager
    await m.set_language("s1", language)
    rig.reply = "Ok."
    detector = await mic.on()
    await detector.on_speech_start()
    await _hear(m, detector, _words(1, 2))
    await detector.on_speech_end(_words(1, 2))
    await mic.turn()
    if streamed:
        assert mic.stt_calls == [] and _finals(rig) == ["w1 w2"]
        assert streaming.fake.streams[0].options == {"language": language}
    else:
        assert streaming.fake.streams == [], "not one sample reached the streaming engine"
        assert [lang for _, lang in mic.stt_calls] == ["pl"]
        assert _finals(rig) == ["what the user said"] and not _partials(rig)
    assert len(rig.llm_calls) == 1


async def test_an_unsupported_language_keeps_whispers_speculative_pass_and_partials(
    rig, mic, streaming, monkeypatch
):
    monkeypatch.setattr(settings, "STT_PARTIALS", "on")
    m = rig.manager
    await m.set_language("s1", "sv")
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_progress(_words(1))
    await _settle(m)
    assert len(mic.stt_calls) == 1 and _partials(rig) == ["what the user said"]
    await detector.on_speech_paused(_words(1))
    await detector.on_speech_end(_words(1))
    await mic.turn()
    assert len(mic.stt_calls) == 2, "the speculative pass, used as the final"


async def test_whispers_speculative_pass_and_partials_are_not_run_beside_the_stream(
    rig, mic, streaming, monkeypatch
):
    monkeypatch.setattr(settings, "STT_PARTIALS", "on")
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    await _hear(m, detector, _words(1))
    await detector.on_speech_progress(_words(1))
    await detector.on_speech_paused(_words(1))
    await _settle(m)
    assert mic.stt_calls == [] and m._mics["s1"].speculative is None
    await detector.on_speech_end(_words(1))
    await mic.turn()
    assert mic.stt_calls == [] and _finals(rig) == ["w1"]


async def test_an_engine_failure_mid_utterance_hands_the_utterance_to_whisper(
    rig, mic, streaming, caplog
):
    m = rig.manager
    rig.reply = "Ok."
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt
    streaming.fake.fail_decode = True
    await _hear(m, detector, _words(1))
    await _idle(session)
    assert session.failed
    with caplog.at_level(logging.WARNING, logger="app.websocket"):
        await detector.on_speech_audio(_words(2))
    assert m._mics["s1"].stream_stt is None, "let go of at the next frame"
    assert "using Whisper" in caplog.text
    # From here it is an ordinary Whisper utterance — speculative pass and all.
    spoken = _words(1, 2)
    await detector.on_speech_paused(spoken)
    await detector.on_speech_end(spoken)
    await mic.turn()
    assert len(mic.stt_calls) == 1 and mic.stt_calls[0][0] is spoken
    assert _finals(rig) == ["what the user said"] and len(rig.llm_calls) == 1
    assert not _of(rig, "error")


async def test_a_failure_while_finishing_still_gets_the_user_their_turn(rig, mic, streaming):
    m = rig.manager
    rig.reply = "Ok."
    streaming.fake.fail_finish = True
    detector = await mic.on()
    await detector.on_speech_start()
    spoken = _words(1, 2)
    await _hear(m, detector, spoken)
    await detector.on_speech_end(spoken)
    await mic.turn()
    assert len(mic.stt_calls) == 1 and mic.stt_calls[0][0] is spoken, "the buffered utterance"
    assert _finals(rig) == ["what the user said"] and len(rig.llm_calls) == 1
    assert not _of(rig, "error")


async def test_a_final_transcript_that_never_comes_falls_back_to_whisper(
    rig, mic, streaming, monkeypatch
):
    monkeypatch.setattr(wsmod, "STREAM_FINAL_TIMEOUT_SECS", 0.05)
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt
    streaming.fake.gate.clear()  # the decoder hangs
    await _hear(m, detector, _words(1))
    await detector.on_speech_end(_words(1))
    await mic.turn()
    assert len(mic.stt_calls) == 1 and _finals(rig) == ["what the user said"]
    assert session._discarded, "and the stuck session is not left decoding"


async def test_an_empty_streaming_transcript_is_no_answer_and_whisper_is_asked(rig, mic, streaming):
    """
    "Okay." The streaming model regularly returns nothing at all for one
    short word, which used to be taken for "no words": the user's answer was
    dropped as a cough, every time they repeated it. Whisper hears it in the
    audio the turn was given.
    """
    m = rig.manager
    rig.reply = "Ok."
    mic.transcript = "Okay."
    detector = await mic.on()
    await detector.on_speech_start()
    spoken = _silence(0.5)  # the fake recognizer hears no word in it
    await _hear(m, detector, spoken)
    await detector.on_speech_end(spoken)
    await mic.turn()
    assert len(streaming.fake.streams) == 1 and streaming.fake.streams[0].finished
    assert len(mic.stt_calls) == 1 and mic.stt_calls[0][0] is spoken, "the buffered utterance"
    assert _of(rig, "user_speech") == [STARTED, STOPPED_TURN]
    assert _finals(rig) == ["Okay."] and len(rig.llm_calls) == 1
    assert not _of(rig, "error")


async def test_an_utterance_neither_engine_hears_a_word_in_is_ignored(rig, mic, streaming):
    """Whisper returning nothing is what makes it a cough."""
    m = rig.manager
    mic.transcript = ""
    detector = await mic.on()
    await detector.on_speech_start()
    await _hear(m, detector, _silence(0.5))  # a cough: a voice, no words
    await detector.on_speech_end(_silence(0.5))
    await mic.turn()
    assert _of(rig, "user_speech") == [STARTED, STOPPED_TURN, STOPPED_IGNORED]
    assert not _of(rig, "transcription") and not _of(rig, "error")
    assert len(mic.stt_calls) == 1 and rig.llm_calls == []


async def test_speech_resuming_before_the_transcript_went_out_is_one_transcription(
    rig, mic, streaming
):
    """
    The continuation rule. The first part's session was closed when its turn
    began, so the second session hears the kept audio first and then the
    rest: one transcript, one turn.
    """
    m = rig.manager
    rig.reply = "Ok."
    detector = await mic.on()
    await detector.on_speech_start()
    first = m._mics["s1"].stream_stt
    part_one, part_two = _words(1, 2), _words(3)
    await _hear(m, detector, part_one)
    await _idle(first)
    streaming.fake.gate.clear()  # the final text is still being decoded...
    await detector.on_speech_end(part_one)
    turn = m._active_turns["s1"]
    await asyncio.sleep(0.02)
    assert not _finals(rig)

    await detector.on_speech_start()  # ...when the speaker carries on
    streaming.fake.gate.set()
    await asyncio.wait({turn}, timeout=2)
    assert turn.cancelled() and first._discarded
    second = m._mics["s1"].stream_stt
    assert second is not None and second is not first
    assert detector.max_secs, "the length cap now covers both parts"
    await _hear(m, detector, part_two)
    await detector.on_speech_end(part_two)
    await mic.turn()

    assert _finals(rig) == ["w1 w2 w3"], "both parts, once"
    assert mic.stt_calls == [] and len(rig.llm_calls) == 1
    assert streaming.fake.streams[1].heard == [1, 2, 3], "the kept audio, then the new"
    assert [x for x in _of(rig, "user_speech")] == [STARTED, STOPPED_TURN, STARTED, STOPPED_TURN]
    assert not _of(rig, "interrupted"), "nothing was said that could be cut off"
    assert m.session_data["s1"]["messages"][0] == {"role": "user", "content": "w1 w2 w3"}


async def test_a_continuation_falls_back_to_whisper_with_both_parts_too(rig, mic, streaming):
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    part_one, part_two = _words(1, 2), _words(3)
    streaming.fake.gate.clear()
    await detector.on_speech_end(part_one)
    await asyncio.sleep(0.01)
    await detector.on_speech_start()
    streaming.fake.gate.set()
    streaming.fake.fail_finish = True
    await _hear(m, detector, part_two)
    await detector.on_speech_end(part_two)
    await mic.turn()
    assert len(mic.stt_calls) == 1
    assert np.array_equal(mic.stt_calls[0][0], np.concatenate([part_one, part_two]))


async def test_a_continuations_replay_does_not_send_other_utterances_to_whisper(
    rig, mic, streaming
):
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    part_one = _words(1, secs=MAX_BACKLOG_SECS + 2)
    streaming.fake.gate.clear()
    await detector.on_speech_end(part_one)
    await asyncio.sleep(0.01)
    await detector.on_speech_start()  # the speaker carries on
    second = m._mics["s1"].stream_stt
    assert second is not None and second.fed_samples == len(part_one), "it hears part one again"
    assert streaming.backlog_secs == 0
    assert streaming.open("en") is not None, "somebody else starting to speak is still streamed"
    streaming.fake.gate.set()


async def test_an_utterance_the_decoder_falls_behind_on_goes_to_whisper_at_once(
    rig, mic, streaming, clock
):
    """
    While it is being spoken — not as STREAM_FINAL_TIMEOUT_SECS of nothing
    after it ends, followed by a Whisper pass that only starts then.
    """
    m = rig.manager
    rig.reply = "Ok."
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt
    streaming.fake.gate.clear()  # the decoder is not keeping up
    await detector.on_speech_audio(_words(1))
    await _held(session)
    await _hear(m, detector, _words(2))
    clock.now += MAX_SESSION_LAG_SECS + 0.5
    await detector.on_speech_audio(_words(3))
    assert session.failed
    await detector.on_speech_audio(_words(3))
    assert m._mics["s1"].stream_stt is None and session._discarded, "let go of at the next frame"

    # Whisper's from here, speculative pass and all, on the whole utterance.
    spoken = _words(1, 2, 3, 3)
    began = time.perf_counter()
    await detector.on_speech_paused(spoken)
    await detector.on_speech_end(spoken)
    await mic.turn()
    assert time.perf_counter() - began < wsmod.STREAM_FINAL_TIMEOUT_SECS / 4
    assert len(mic.stt_calls) == 1 and mic.stt_calls[0][0] is spoken
    assert _finals(rig) == ["what the user said"] and len(rig.llm_calls) == 1
    assert not _of(rig, "error")
    streaming.fake.gate.set()


async def test_falling_behind_on_the_last_frame_does_not_wait_for_a_final_either(
    rig, mic, streaming, clock
):
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    streaming.fake.gate.clear()
    await detector.on_speech_audio(_words(1))
    await _held(m._mics["s1"].stream_stt)
    await _hear(m, detector, _words(2))
    clock.now += MAX_SESSION_LAG_SECS + 0.5
    await detector.on_speech_audio(_words(3))  # given up here; no frame follows
    began = time.perf_counter()
    await detector.on_speech_end(_words(1, 2, 3))
    await mic.turn()
    assert time.perf_counter() - began < wsmod.STREAM_FINAL_TIMEOUT_SECS / 4
    assert len(mic.stt_calls) == 1 and _finals(rig) == ["what the user said"]
    streaming.fake.gate.set()


async def test_speaking_after_the_transcript_went_out_is_a_barge_in_not_a_continuation(
    rig, mic, streaming
):
    m = rig.manager
    rig.reply = "Here is a long answer that keeps going. " * 6
    rig.token_delay = 0.01
    rig.animate_delay = 30
    detector = await mic.on()
    await detector.on_speech_start()
    await _hear(m, detector, _words(1))
    await detector.on_speech_end(_words(1))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if _of(rig, "token"):
            break
    assert _finals(rig) == ["w1"]

    await detector.on_speech_start()
    assert _of(rig, "interrupted"), "the reply was cut off"
    rig.reply, rig.animate_delay = "Ok.", 0
    await _hear(m, detector, _words(2))
    await detector.on_speech_end(_words(2))
    await mic.turn()
    assert _finals(rig) == ["w1", "w2"], "the second is its own message, without the first"
    assert streaming.fake.streams[1].heard == [2]


async def test_when_listening_stops_the_session_is_discarded_and_nothing_is_sent(
    rig, mic, streaming, monkeypatch
):
    monkeypatch.setattr(wsmod, "STREAM_PARTIAL_MIN_INTERVAL_SECS", 0.0)
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt
    await _hear(m, detector, _words(1))
    await _idle(session)

    await m.handle_mic("s1", False)
    assert session._discarded and m._mics["s1"].stream_stt is None
    sent, heard = len(rig.socket.sent), session.fed_samples
    # A detector that was mid-frame when it was switched off.
    await detector.on_speech_audio(_words(2))
    await detector.on_speech_end(_words(1, 2))
    await mic.turn()
    assert len(rig.socket.sent) == sent, "no transcript, running or final"
    assert session.fed_samples == heard, "and no audio reached the recognizer after the stop"
    assert mic.stt_calls == [] and rig.llm_calls == []


async def test_listening_stopping_while_the_final_text_is_decoding_withdraws_the_utterance(
    rig, mic, streaming
):
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt
    await _hear(m, detector, _words(1, 2))
    streaming.fake.gate.clear()
    await detector.on_speech_end(_words(1, 2))
    await asyncio.sleep(0.02)

    await m.handle_mic("s1", False)
    streaming.fake.gate.set()
    await mic.turn()
    assert session._discarded
    assert not _of(rig, "transcription") and rig.llm_calls == [] and mic.stt_calls == []
    assert _of(rig, "user_speech")[-1] == STOPPED_IGNORED


async def test_the_stream_going_down_discards_the_session(rig, mic, streaming):
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt
    await m._stream_down("s1", mic.stream)
    await _settle(m)
    assert session._discarded
    await detector.on_speech_audio(_words(1))
    assert session.fed_samples == 0


async def test_a_session_the_end_of_speech_handler_holds_is_discarded_if_the_handler_is_cancelled(
    rig, mic, streaming
):
    """
    The handler takes the session off the microphone and only later gives it
    to a turn. In between it awaits sends, on the reader task — which is
    cancelled when the stream goes down. The session was then nobody's:
    nothing discarded it, and all its queued audio was decoded for no one.
    """
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt
    streaming.fake.gate.clear()  # what was said is still queued
    spoken = _words(*range(1, 40))
    await _hear(m, detector, spoken)

    sending, release = asyncio.Event(), asyncio.Event()
    send_json = rig.socket.send_json

    async def held_up(message):
        if message == STOPPED_TURN:
            sending.set()
            await release.wait()
        await send_json(message)

    rig.socket.send_json = held_up
    # As in production: the detector's callbacks run on the reader task.
    handler = asyncio.ensure_future(detector.on_speech_end(spoken))
    m._mics["s1"].reader_task = handler
    await sending.wait()
    await m._stream_down("s1", mic.stream)
    await asyncio.gather(handler, return_exceptions=True)
    assert handler.cancelled()
    assert session._discarded, "nobody was left holding it"

    release.set()
    streaming.fake.gate.set()
    await _idle(session)
    await _settle(m)
    assert session.decoded_samples <= SAMPLE_RATE, "at most the slice already being decoded"
    assert streaming.backlog_secs == 0
    assert mic.stt_calls == [] and rig.llm_calls == [] and not _of(rig, "transcription")


async def test_a_session_is_discarded_if_the_end_of_speech_handler_raises(
    rig, mic, streaming, monkeypatch
):
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    session = m._mics["s1"].stream_stt

    async def broken(session_id):
        raise RuntimeError("the interrupt failed")

    monkeypatch.setattr(m, "interrupt_active_turn", broken)
    with pytest.raises(RuntimeError):
        await detector.on_speech_end(_words(1))
    assert session._discarded and m._mics["s1"].stream_stt is None


async def test_an_utterance_the_rate_limit_will_refuse_is_never_streamed(
    rig, mic, streaming, monkeypatch
):
    monkeypatch.setattr(settings, "WS_TURNS_PER_MINUTE", 1)
    m = rig.manager
    rig.reply = "Ok."
    detector = await mic.on()
    await detector.on_speech_start()
    await _hear(m, detector, _words(1))
    await detector.on_speech_end(_words(1))
    await mic.turn()
    assert len(rig.llm_calls) == 1 and len(streaming.fake.streams) == 1

    await detector.on_speech_start()
    assert m._mics["s1"].stream_stt is None
    await detector.on_speech_audio(_words(2))
    await detector.on_speech_end(_words(2))
    await mic.turn()
    assert len(streaming.fake.streams) == 1 and mic.stt_calls == [] and len(rig.llm_calls) == 1
    assert [x for x in rig.socket.sent if x.get("code") == "rate_limited"]
    assert rig.socket.sent[-1] == STOPPED_IGNORED


async def test_an_engine_that_is_unavailable_leaves_everything_to_whisper(
    rig, mic, streaming, monkeypatch
):
    monkeypatch.setattr(streaming, "_missing", lambda: "model files not found")
    m = rig.manager
    detector = await mic.on()
    await detector.on_speech_start()
    assert m._mics["s1"].stream_stt is None
    await detector.on_speech_audio(_words(1))
    await detector.on_speech_end(_words(1))
    await mic.turn()
    assert streaming.fake.streams == [] and len(mic.stt_calls) == 1
    assert _finals(rig) == ["what the user said"] and not _partials(rig)


async def test_with_streaming_off_the_microphone_path_is_the_whisper_path(rig, mic, monkeypatch):
    """The default. Nothing is wired to the detector and no session is ever opened."""
    assert settings.STT_STREAMING == "off"
    opened = []
    monkeypatch.setattr(wsmod.streaming_stt, "open", lambda language: opened.append(language))
    m = rig.manager
    rig.reply = "A reply."
    detector = await mic.on()
    assert getattr(detector, "on_speech_audio", None) is None, "no per-frame listener"
    await detector.on_speech_start()
    await detector.on_speech_paused(_words(1))
    await detector.on_speech_end(_words(1))
    await mic.turn()
    assert opened == [] and m._mics["s1"].stream_stt is None
    assert len(mic.stt_calls) == 1, "the speculative pass, used as the final"
    # Byte for byte what test_an_utterance_from_the_stream_microphone_is_a_
    # normal_turn pins for this path.
    assert rig.socket.sent[:5] == [
        LISTENING,
        STARTED,
        STOPPED_TURN,
        {"type": "status", "message": "Transcribing audio…", "stage": "transcription"},
        {"type": "transcription", "text": "what the user said"},
    ]


# ── startup and /health ──────────────────────────────────────────────────


async def test_health_reports_what_is_configured_and_whether_it_could_run(client, monkeypatch):
    data = (await client.get("/health")).json()
    assert data["stt_streaming"] == "off"
    assert isinstance(data["stt_streaming_available"], bool)

    import main

    monkeypatch.setattr(settings, "STT_STREAMING", "sherpa")
    monkeypatch.setattr(main.streaming_stt, "_missing", lambda: "no model")
    data = (await client.get("/health")).json()
    assert data["stt_streaming"] == "sherpa" and data["stt_streaming_available"] is False
    assert not main.streaming_stt.loaded, "asking does not load the model"


async def test_the_model_is_preloaded_only_where_it_will_be_used(engine, monkeypatch):
    import main

    async def nothing():
        return None

    for service in (wsmod.stt_service, wsmod.tts_service, wsmod.avatar_animator):
        monkeypatch.setattr(service, "warm", nothing)
    monkeypatch.setattr(tt, "warm", nothing)
    monkeypatch.setattr(main, "streaming_stt", engine)

    monkeypatch.setattr(main, "mic_enabled", lambda: False)
    await main._warm_models()
    assert not engine.loaded, "no stream microphone, nobody to transcribe"

    monkeypatch.setattr(main, "mic_enabled", lambda: True)
    monkeypatch.setattr(settings, "STT_STREAMING", "off")
    await main._warm_models()
    assert not engine.loaded

    monkeypatch.setattr(settings, "STT_STREAMING", "sherpa")
    await main._warm_models()
    assert engine.loaded


# ── the real model ───────────────────────────────────────────────────────
#
# Skipped where the model has not been downloaded (it is 475 MB and lives in
# the git-ignored backend/models/): CI. Run where it has.

_REAL = StreamingSTT()
needs_model = pytest.mark.skipif(
    not _REAL.installed(),
    reason="streaming STT model not downloaded (run scripts/setup_streaming_stt.sh)",
)


@pytest.fixture
def real(monkeypatch):
    """The provider on the real model, loaded once for the module."""
    monkeypatch.setattr(settings, "STT_STREAMING", "sherpa")
    assert _REAL.available()
    return _REAL


@pytest.fixture(scope="module")
def recording() -> np.ndarray:
    audio, rate = sf.read(str(FIXTURE), dtype="int16")
    assert rate == SAMPLE_RATE and audio.ndim == 1
    return audio


# Where the turn detector would open the utterance: its pre-roll before the
# first word, which is spoken 0.7 s into the file.
LEAD = 0.3


@pytest.fixture(scope="module")
def utterance(recording) -> np.ndarray:
    """
    The recording as the microphone path hands it over: from the pre-roll.

    Not the file from its first sample. The model is sensitive to where a
    word falls against its 560 ms chunks, and with 1.1 s of silence in front
    (the file's 0.7 s plus the session's own lead-in) "birch" lands on a
    boundary and comes out as "bird". That is a property of the model worth
    knowing about, not of the queueing these tests are for.
    """
    return recording[int((0.7 - LEAD) * SAMPLE_RATE) :]


def _has_key_words(text: str) -> bool:
    return all(word in text.lower() for word in KEY_WORDS)


@needs_model
async def test_real_partials_arrive_while_the_audio_is_still_coming_and_grow(real, utterance):
    """
    Fed at the speed it was spoken — the one place here that keeps time,
    because "before the audio ends" means nothing otherwise.
    """
    await real.warm()
    session = real.open("en")
    piece = 320  # 20 ms, an Opus frame
    partials: list[tuple[float, str]] = []
    began = time.perf_counter()
    for i in range(0, len(utterance), piece):
        text = session.feed(utterance[i : i + piece])
        if text:
            partials.append((time.perf_counter() - began, text))
        ahead = (i + piece) / SAMPLE_RATE - (time.perf_counter() - began)
        if ahead > 0:
            await asyncio.sleep(ahead)
    audio_ended = time.perf_counter()
    final = await session.finish()
    to_final = time.perf_counter() - audio_ended
    print(
        f"\n[streaming STT] first partial {partials[0][0] - LEAD:.2f}s after speech starts "
        f"({partials[0][0]:.2f}s into the audio): {partials[0][1]!r}; {len(partials)} partials; "
        f"final {to_final:.2f}s after the audio ended: {final!r}"
    )

    assert len(partials) >= 4, partials
    # The first words are up within a second or so of being spoken, long
    # before the 6.8 s utterance is over.
    assert partials[0][0] - LEAD < 2.0, partials[0]
    assert "birch" in partials[1][1].lower() or "birch" in partials[2][1].lower(), partials[:3]
    words = [len(text.split()) for _, text in partials]
    assert words == sorted(words) and words[-1] > words[0], "they grow"
    # The first sentence is on screen while the second is still being said.
    assert any("canoe" in text.lower() and at < 4.1 for at, text in partials), partials
    assert _has_key_words(final), final
    assert to_final < 2.0, f"the final took {to_final:.2f}s after the audio ended"
    assert session.feed(utterance[:piece]) is None


@needs_model
async def test_real_final_is_the_same_however_the_audio_is_cut_up(real, utterance):
    finals = []
    for piece in (len(utterance), 512, 320, 7777, 13):
        session = real.open("en")
        if piece == 13:  # a ragged start, then the rest
            session.feed(utterance[:13])
            session.feed(utterance[13:])
        else:
            for i in range(0, len(utterance), piece):
                session.feed(utterance[i : i + piece])
        finals.append(await session.finish())
    assert _has_key_words(finals[0]), finals[0]
    assert len(set(finals)) == 1, finals


@needs_model
async def test_real_discard_stops_the_decoding(real, utterance):
    session = real.open("en")
    session.feed(np.tile(utterance, 4))  # 27 s: several seconds of decoding
    await asyncio.sleep(0.3)
    session.discard()
    await _idle(session)
    done = session.decoded_samples
    assert 0 < done < session.fed_samples, "stopped part-way"
    await asyncio.sleep(0.3)
    assert session.decoded_samples == done and real.backlog_secs == 0
    with pytest.raises(StreamingSTTError):
        await session.finish()
    # And the model is none the worse for it.
    after = real.open("en")
    after.feed(utterance)
    assert _has_key_words(await after.finish())


@needs_model
async def test_real_two_sessions_at_once_both_finish_correctly(real, utterance):
    one, two = real.open("en"), real.open("en")
    # Interleaved, as two people speaking at once arrive; the second is only
    # the second sentence.
    second_sentence = utterance[int(3.5 * SAMPLE_RATE) :]
    for i in range(0, len(utterance), 1600):
        one.feed(utterance[i : i + 1600])
        two.feed(second_sentence[i : i + 1600])
    first, second = await asyncio.gather(one.finish(), two.finish())
    assert _has_key_words(first), first
    assert "glue" in second.lower() and "blue" in second.lower(), second
    assert "birch" not in second.lower(), "each stream hears only its own audio"


@needs_model
async def test_real_decoding_does_not_run_on_the_event_loop(real, utterance):
    session = real.open("en")
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.ensure_future(ticker())
    began = time.perf_counter()
    session.feed(utterance)
    await session.finish()
    took = time.perf_counter() - began
    task.cancel()
    # A loop blocked for the whole decode would have ticked once or twice.
    assert took > 0.3 and ticks > took / 0.005 / 4, (ticks, took)


@needs_model
@pytest.mark.skipif(not tt.turn_taking_available(), reason="pipecat not installed")
async def test_real_detector_and_real_model_hear_the_first_word(real, recording):
    """
    The recording through the real VAD into the real recognizer, the way the
    microphone path wires them: the utterance's first word is spoken before
    the VAD confirms speech, and reaches the recognizer through the pre-roll.
    """
    detector = await TurnDetector.create("vad", stop_secs=1.6)
    rec = Recorder(detector)
    sessions = []
    detector.on_speech_start = lambda: sessions.append(real.open("en"))
    detector.on_speech_audio = lambda audio: sessions[-1].feed(audio)
    finals = []

    async def ended(audio):
        finals.append(await sessions[-1].finish())

    detector.on_speech_end = ended
    try:
        await rec.feed(np.concatenate([recording, _silence(2.5)]))
    finally:
        await detector.close()
    assert len(finals) == 1
    assert finals[0].lower().startswith("the birch"), finals[0]
    assert _has_key_words(finals[0]), finals[0]


def _real_whisper():
    """
    The app's Whisper service on the "base" model, if that is already on
    this machine; None otherwise. Never downloaded for a test.
    """
    global _WHISPER
    if _WHISPER is None:
        try:
            from huggingface_hub import try_to_load_from_cache

            cached = isinstance(
                try_to_load_from_cache("Systran/faster-whisper-base", "model.bin"), str
            )
        except Exception:
            cached = False
        if not cached:
            _WHISPER = False
        else:
            from faster_whisper import WhisperModel

            from app.services.stt import STTService

            _WHISPER = STTService()
            _WHISPER.model = WhisperModel(
                "base", device="cpu", compute_type="int8", local_files_only=True
            )
            _WHISPER.device = "cpu"
    return _WHISPER or None


_WHISPER = None


@needs_model
@pytest.mark.skipif(not tt.turn_taking_available(), reason="pipecat not installed")
@pytest.mark.parametrize("whisper", ["stub", "real"])
@pytest.mark.parametrize("mode", ["smart", "vad"])
@pytest.mark.parametrize("offset", [0.8, 1.0, 1.4, 2.0, 4.7])
async def test_real_one_short_word_is_a_turn_in_streaming_mode(
    rig, mic, real, recording, monkeypatch, whisper, mode, offset
):
    """
    "Yes." "No." "Okay." — 0.4 s of the recording, through the real turn
    detector into the manager's own microphone handlers, with the real
    streaming model listening. For most of these the model returns no text at
    all, and the utterance used to be dropped as a cough. It must become a
    turn: from the streaming text if there is any, otherwise from Whisper on
    the audio the detector handed over.

    Twice over. With Whisper stubbed, every word is a turn. With the real
    Whisper (the small "base" model, where it is cached) the outcome is
    Whisper's own: a turn whenever it hears a word, which is what the same
    audio gets with streaming switched off.
    """
    heard: list[str] = []
    if whisper == "real":
        service = _real_whisper()
        if service is None:
            pytest.skip("the Whisper base model is not cached on this machine")

        async def transcribe(audio, language="en"):
            mic.stt_calls.append((audio, language))
            heard.append(await service.transcribe(audio, language=language))
            return heard[-1]

        monkeypatch.setattr(wsmod.stt_service, "transcribe", transcribe)
    else:
        mic.transcript = "Okay."
    monkeypatch.setattr(wsmod, "streaming_stt", real)
    await real.warm()
    rig.reply = "Ok."
    wired = await mic.on()  # the manager's handlers, as it attached them
    detector = await _CREATE_DETECTOR(mode, max_wait_secs=1.0)
    for name in (
        "on_speech_start",
        "on_speech_audio",
        "on_speech_paused",
        "on_speech_resumed",
        "on_speech_progress",
        "on_speech_end",
    ):
        setattr(detector, name, getattr(wired, name))
    audio = np.concatenate([_silence(1.0), _word(recording, offset, 0.4), _silence(3.0)])
    try:
        for i in range(0, len(audio), 320):
            await detector.feed(audio[i : i + 320].tobytes())
    finally:
        await detector.close()
    await mic.turn()

    speech = _of(rig, "user_speech")
    assert speech[:2] == [STARTED, STOPPED_TURN], speech
    assert len(mic.stt_calls) <= 1
    if mic.stt_calls:
        # Whisper was asked, so the streaming model had nothing — and it was
        # given the utterance itself, the word and its pre-roll.
        asked = mic.stt_calls[0][0]
        assert len(asked) / SAMPLE_RATE >= 0.4 and np.abs(asked).max() > 1000
    finals = _finals(rig)
    print(
        f"\n[short word] {mode} @{offset}: "
        + (f"Whisper ({whisper}) {(heard or [mic.transcript])[-1]!r}" if mic.stt_calls else "")
        + (f"streamed {finals[0]!r}" if finals and not mic.stt_calls else "")
    )
    if heard and not heard[0].strip():
        # Neither engine found a word in this fragment. That, and only
        # that, is what "ignored" means.
        assert speech == [STARTED, STOPPED_TURN, STOPPED_IGNORED] and not finals
        assert rig.llm_calls == []
        return
    assert speech == [STARTED, STOPPED_TURN], "not ignored"
    assert len(finals) == 1 and finals[0].strip(), finals
    assert len(rig.llm_calls) == 1 and rig.llm_calls[0][-1]["content"] == finals[0]
    assert not _of(rig, "error")
