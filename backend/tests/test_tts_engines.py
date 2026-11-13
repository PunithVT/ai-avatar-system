"""
TTS engine selection and speaker isolation.

Chatterbox itself is not installed in the test environment (it cannot share
an environment with the lip-sync stack), so the model is a stand-in that
reproduces the one behaviour that matters here: the active speaker's
conditioning lives on the model object and persists between calls.
"""

import asyncio
import threading
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import app.services.tts as tts_module
import app.websocket as wsmod
from app.services.tts import NothingToSay, TTSService, sayable
from tests.test_turn_pipeline import _session_files, rig  # noqa: F401 — rig is a fixture


class FakeChatterbox:
    """Mimics Chatterbox: generate() without a prompt reuses the last speaker."""

    sr = 24000

    def __init__(self):
        self.conds = "builtin-voice"
        self.prepared: list[str] = []
        self.spoken_as: list[str] = []

    def prepare_conditionals(self, path):
        self.prepared.append(path)
        self.conds = f"clone:{Path(path).name}"

    def generate(self, text, language_id="en", audio_prompt_path=None):
        if audio_prompt_path:
            self.prepare_conditionals(audio_prompt_path)
        self.spoken_as.append(self.conds)
        return f"wav<{self.conds}>"


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "chatterbox")
    s = TTSService()
    s.model = FakeChatterbox()
    s._default_conds = s.model.conds
    return s


def test_an_uncloned_session_never_inherits_the_previous_speakers_voice(service, tmp_path):
    """One user's cloned voice used to come out of the next user's avatar."""
    alice = tmp_path / "alice.wav"
    alice.write_bytes(b"x")

    service._generate_sync("hello", "en", str(alice))
    service._generate_sync("hello", "en", None)

    assert service.model.spoken_as == ["clone:alice.wav", "builtin-voice"]


def test_a_reference_recording_is_encoded_once_not_per_sentence(service, tmp_path):
    alice = tmp_path / "alice.wav"
    alice.write_bytes(b"x")
    bob = tmp_path / "bob.wav"
    bob.write_bytes(b"y")

    for wav in (alice, alice, bob, alice, bob):
        service._generate_sync("hello", "en", str(wav))

    assert service.model.prepared == [str(alice), str(bob)]
    assert service.model.spoken_as == [
        "clone:alice.wav",
        "clone:alice.wav",
        "clone:bob.wav",
        "clone:alice.wav",
        "clone:bob.wav",
    ]


def test_a_replaced_recording_is_re_encoded(service, tmp_path):
    wav = tmp_path / "voice.wav"
    wav.write_bytes(b"first take")
    service._generate_sync("hello", "en", str(wav))
    wav.write_bytes(b"a second, longer take")
    service._generate_sync("hello", "en", str(wav))
    assert len(service.model.prepared) == 2


def test_a_model_without_the_conditioning_api_still_gets_the_prompt(tmp_path, monkeypatch):
    """If a release drops prepare_conditionals/conds, fall back to per-call prompts."""

    class Bare:
        sr = 24000

        def __init__(self):
            self.calls = []

        def generate(self, text, language_id="en", audio_prompt_path=None):
            self.calls.append(audio_prompt_path)
            return "wav"

    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "chatterbox")
    s = TTSService()
    s.model = Bare()
    s._generate_sync("hi", "en", "/voices/a.wav")
    s._generate_sync("hi", "en", None)
    assert s.model.calls == ["/voices/a.wav", None]


def _stub_network(monkeypatch, service, edge_fails=False):
    calls = []

    async def edge(text, output_path, language="en"):
        calls.append("edge")
        if edge_fails:
            raise RuntimeError("no network")
        Path(output_path).write_bytes(b"wav")
        return output_path

    async def gtts(text, output_path, language="en"):
        calls.append("gtts")
        Path(output_path).write_bytes(b"wav")
        return output_path

    monkeypatch.setattr(service, "_edge_fallback", edge)
    monkeypatch.setattr(service, "_gtts_fallback", gtts)
    return calls


async def test_edge_provider_is_a_first_class_engine_not_a_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "edge")
    s = TTSService()
    calls = _stub_network(monkeypatch, s)

    async def must_not_load():
        raise AssertionError("chatterbox must not be loaded when TTS_PROVIDER=edge")

    monkeypatch.setattr(s, "initialize", must_not_load)
    result = await s.synthesize("hello", str(tmp_path / "o.wav"))

    assert calls == ["edge"]
    assert (result.engine, result.fallback) == ("edge-tts", False)


async def test_edge_provider_still_reports_a_cloned_voice_it_cannot_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "edge")
    s = TTSService()
    _stub_network(monkeypatch, s)
    result = await s.synthesize("hello", str(tmp_path / "o.wav"), speaker_wav="/v/a.wav")
    assert result.fallback is True and result.voice_cloned is False


async def test_edge_failure_falls_through_to_gtts_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "edge")
    s = TTSService()
    calls = _stub_network(monkeypatch, s, edge_fails=True)
    result = await s.synthesize("hello", str(tmp_path / "o.wav"))
    assert calls == ["edge", "gtts"]
    assert (result.engine, result.fallback) == ("gtts", True)


async def test_missing_chatterbox_is_discovered_once_not_per_sentence(monkeypatch, tmp_path):
    """
    Default config, package absent. The import used to be retried — and an
    error logged, and a fallback notice sent — for every sentence of every reply.
    """
    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "chatterbox")
    s = TTSService()
    calls = _stub_network(monkeypatch, s)
    attempts = []
    real_initialize = s.initialize

    async def counting_initialize():
        attempts.append(1)
        await real_initialize()

    monkeypatch.setattr(s, "initialize", counting_initialize)

    results = [await s.synthesize(f"sentence {i}", str(tmp_path / f"{i}.wav")) for i in range(4)]

    assert len(attempts) == 1
    assert calls == ["edge"] * 4
    # Nothing was lost (no cloned voice was requested), so after the first
    # sentence there is nothing to announce.
    assert [r.fallback for r in results[1:]] == [False, False, False]

    cloned = await s.synthesize("hi", str(tmp_path / "c.wav"), speaker_wav="/v/a.wav")
    assert cloned.fallback is True, "a cloned voice that cannot be applied is always reported"


async def test_warm_never_raises(monkeypatch):
    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "chatterbox")
    s = TTSService()
    await asyncio.wait_for(s.warm(), timeout=30)
    assert s.model is None and s._chatterbox_missing is True


# ── Supertonic (TTS_PROVIDER=supertonic) ─────────────────────────────────
class FakeSupertonic:
    """The package's TTS object: 44.1 kHz float32 of shape (1, n), blocking."""

    sample_rate = 44100

    def __init__(self, seconds=0.5, error=None):
        self.seconds = seconds
        self.error = error
        self.calls: list[dict] = []
        self.threads: list[str] = []

    def get_voice_style(self, name):
        return f"style:{name}"

    def synthesize(self, text, voice_style, total_steps=8, lang=None, **kwargs):
        import threading

        self.calls.append({"text": text, "voice": voice_style, "steps": total_steps, "lang": lang})
        self.threads.append(threading.current_thread().name)
        if self.error is not None:
            raise self.error
        n = int(self.seconds * self.sample_rate)
        tone = 0.4 * np.sin(2 * np.pi * 440 * np.arange(n) / self.sample_rate)
        return tone.astype(np.float32).reshape(1, n), np.array([self.seconds])


@pytest.fixture
def supertonic(monkeypatch):
    """A service configured for Supertonic with the engine already 'loaded'."""
    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "supertonic")
    s = TTSService()
    s._supertonic = FakeSupertonic()
    s._supertonic_style = "style:F1"

    def must_not_load():
        raise AssertionError("the real engine must never be loaded here")

    monkeypatch.setattr(s, "_load_supertonic", must_not_load)
    s.network = _stub_network(monkeypatch, s)
    return s


async def test_supertonic_is_a_first_class_engine_and_needs_no_network(supertonic, tmp_path):
    out = tmp_path / "o.wav"
    result = await supertonic.synthesize("Hello there.", str(out), language="en")

    assert supertonic.network == []
    assert (result.engine, result.fallback, result.voice_cloned) == ("supertonic", False, False)
    assert result.output_path == str(out)
    assert supertonic._supertonic.calls == [
        {"text": "Hello there.", "voice": "style:F1", "steps": 5, "lang": "en"}
    ]
    assert supertonic._supertonic.threads != [threading.current_thread().name], (
        "synthesis blocks for hundreds of milliseconds: never on the event loop"
    )


async def test_supertonic_audio_is_written_at_the_stream_rate_as_16_bit_mono(supertonic, tmp_path):
    out = tmp_path / "o.wav"
    await supertonic.synthesize("Hello there.", str(out))

    info = sf.info(str(out))
    assert (info.samplerate, info.channels, info.subtype) == (48000, 1, "PCM_16")
    # 0.5 s at 44.1 kHz is 0.5 s at 48 kHz: resampled, not relabelled.
    assert abs(info.frames - 24000) <= 2
    samples, _ = sf.read(str(out), dtype="float32")
    assert samples.shape == (info.frames,)
    spectrum = np.abs(np.fft.rfft(samples))
    peak_hz = np.argmax(spectrum) * 48000 / len(samples)
    assert abs(peak_hz - 440) < 5, f"the pitch moved to {peak_hz:.0f} Hz"
    assert 0.35 < np.abs(samples).max() < 0.45, "and the level did not"


async def test_supertonic_steps_follow_the_setting(supertonic, tmp_path, monkeypatch):
    monkeypatch.setattr(tts_module.settings, "SUPERTONIC_STEPS", 8)
    await supertonic.synthesize("Hello.", str(tmp_path / "o.wav"))
    assert supertonic._supertonic.calls[0]["steps"] == 8


@pytest.mark.parametrize("language", ["he", "ms", "no", "sw", "zh"])
async def test_a_language_supertonic_does_not_speak_goes_to_the_edge_chain(
    supertonic, tmp_path, language
):
    result = await supertonic.synthesize("shalom", str(tmp_path / "o.wav"), language=language)
    assert supertonic._supertonic.calls == [], "not even attempted"
    assert supertonic.network == ["edge"]
    # Not a fallback: Edge is what this provider is configured to use for
    # this language, and nothing failed. (This used to say True, and the
    # user was told "voice engine fell back" on every reply of the session.)
    assert (result.engine, result.fallback) == ("edge-tts", False)


async def test_an_unspoken_language_still_reports_what_was_really_lost(
    monkeypatch, supertonic, tmp_path
):
    cloned = await supertonic.synthesize(
        "ni hao", str(tmp_path / "a.wav"), speaker_wav="/v/a.wav", language="zh"
    )
    assert (cloned.engine, cloned.fallback, cloned.voice_cloned) == ("edge-tts", True, False)

    supertonic.network = _stub_network(monkeypatch, supertonic, edge_fails=True)
    result = await supertonic.synthesize("ni hao", str(tmp_path / "b.wav"), language="zh")
    assert supertonic.network == ["edge", "gtts"]
    assert (result.engine, result.fallback) == ("gtts", True), "Edge itself fell through"


async def test_a_session_in_an_unspoken_language_is_not_told_of_a_fallback_every_reply(
    rig,  # noqa: F811
    supertonic,
    monkeypatch,
):
    monkeypatch.setattr(wsmod.tts_service, "synthesize", supertonic.synthesize)
    m = rig.manager
    m.session_data["s1"]["language"] = "zh"
    await m._handle_text_input_inner("s1", "question")
    assert len([x for x in rig.socket.sent if x["type"] == "video_chunk"]) == 4
    assert "tts_fallback" not in rig.socket.types()

    # A sentence the engine really could not speak is still announced, once.
    m.session_data["s1"]["language"] = "en"
    supertonic._supertonic.error = ValueError("Found 1 unsupported character(s)")
    await m._handle_text_input_inner("s1", "another question")
    notices = [x for x in rig.socket.sent if x["type"] == "tts_fallback"]
    assert len(notices) == 1 and notices[0]["engine"] == "edge-tts"


def test_supertonic_covers_exactly_the_documented_languages():
    assert tts_module.SUPERTONIC_LANGUAGES == set(tts_module._EDGE_VOICES) - {
        "he",
        "ms",
        "no",
        "sw",
        "zh",
    }


async def test_a_sentence_supertonic_refuses_falls_through_for_that_sentence_only(
    supertonic, tmp_path
):
    """Characters outside its alphabet raise ValueError; the next sentence is fine."""
    engine = supertonic._supertonic
    engine.error = ValueError("Found 1 unsupported character(s): ['中']")
    refused = await supertonic.synthesize("Say 中 aloud.", str(tmp_path / "a.wav"))
    engine.error = None
    spoken = await supertonic.synthesize("And then this.", str(tmp_path / "b.wav"))

    assert supertonic.network == ["edge"]
    assert (refused.engine, refused.fallback) == ("edge-tts", True)
    assert (spoken.engine, spoken.fallback) == ("supertonic", False)
    assert supertonic._supertonic_unavailable is False


async def test_supertonic_failing_and_edge_failing_still_ends_in_speech(
    monkeypatch, supertonic, tmp_path
):
    supertonic._supertonic.error = RuntimeError("onnxruntime fell over")
    supertonic.network = _stub_network(monkeypatch, supertonic, edge_fails=True)
    result = await supertonic.synthesize("hello", str(tmp_path / "o.wav"))
    assert supertonic.network == ["edge", "gtts"]
    assert (result.engine, result.fallback) == ("gtts", True)


class GatedSupertonic(FakeSupertonic):
    """Held inside synthesize() until released, as a real synthesis is for seconds."""

    def __init__(self):
        super().__init__()
        self.entered, self.release, self.left = (threading.Event() for _ in range(3))

    def synthesize(self, *args, **kwargs):
        self.entered.set()
        assert self.release.wait(10)
        try:
            return super().synthesize(*args, **kwargs)
        finally:
            self.left.set()


async def test_a_synthesis_abandoned_by_a_cancelled_caller_writes_no_file(supertonic, tmp_path):
    """
    A thread cannot be stopped, so a cancelled synthesis runs to its end. It
    used to end by writing the WAV — after the cancelled turn had already
    cleaned up its temp files, so nothing ever removed it.
    """
    engine = supertonic._supertonic = GatedSupertonic()
    out = tmp_path / "o.wav"
    task = asyncio.ensure_future(supertonic.synthesize("Hello there.", str(out)))
    assert await asyncio.to_thread(engine.entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    engine.release.set()
    assert await asyncio.to_thread(engine.left.wait, 5)
    await asyncio.sleep(0.3)  # the thread is past where it used to write
    assert not out.exists()
    assert engine.calls, "the synthesis itself did run to the end"


async def test_a_caller_cancelled_during_the_write_itself_leaves_no_file_either(
    supertonic, tmp_path, monkeypatch
):
    writing, release, written = (threading.Event() for _ in range(3))
    write = supertonic._write_wav

    def slow(path, samples):
        writing.set()
        assert release.wait(10)
        write(path, samples)
        written.set()

    monkeypatch.setattr(supertonic, "_write_wav", slow)
    out = tmp_path / "o.wav"
    task = asyncio.ensure_future(supertonic.synthesize("Hello there.", str(out)))
    assert await asyncio.to_thread(writing.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(written.wait, 5), "the write cannot be stopped"
    for _ in range(200):
        if not out.exists():
            break
        await asyncio.sleep(0.01)
    assert not out.exists(), "removed once the thread had finished with it"


async def test_a_barge_in_during_supertonic_synthesis_leaves_the_session_directory_empty(
    rig,  # noqa: F811
    supertonic,
    monkeypatch,
    tmp_path,
):
    engine = supertonic._supertonic = GatedSupertonic()
    monkeypatch.setattr(wsmod.tts_service, "synthesize", supertonic.synthesize)
    m = rig.manager
    task = asyncio.create_task(m._handle_text_input_inner("s1", "question"))
    m._active_turns["s1"] = task
    assert await asyncio.to_thread(engine.entered.wait, 5), "the first sentence is in the engine"
    await m.interrupt_active_turn("s1")
    await asyncio.gather(task, return_exceptions=True)
    assert _session_files(tmp_path) == []
    engine.release.set()
    assert await asyncio.to_thread(engine.left.wait, 5)
    await asyncio.sleep(0.3)
    assert _session_files(tmp_path) == [], "the abandoned synthesis left its WAV behind"


# ── text with nothing in it to say ───────────────────────────────────────


def test_sayable_means_a_letter_or_a_digit_in_any_script():
    for text in ("Hi.", "5", "ok 🙂", "你好", "مرحبا", "नमस्ते", "é", "こんにちは", "Ω"):
        assert sayable(text), text
    for text in ("🙂", "😀😀 🎉", "...", "…", "—", "*", "?!", "", "  \n", None):
        assert not sayable(text), repr(text)


@pytest.mark.parametrize("provider", ["chatterbox", "supertonic", "edge", "gtts"])
@pytest.mark.parametrize("text", ["🙂", "😀😀 🎉", "...", "—", "*"])
async def test_text_with_nothing_to_say_reaches_no_engine(monkeypatch, tmp_path, provider, text):
    """
    Supertonic turns these into 1.3 s of hiss or a grunt; the others spend a
    network round trip on them. None is asked — whichever is configured.
    """
    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", provider)
    s = TTSService()
    s.model = FakeChatterbox()
    s._default_conds = s.model.conds
    s._supertonic = FakeSupertonic()
    s._supertonic_style = "style:F1"
    network = _stub_network(monkeypatch, s)
    out = tmp_path / "session" / "o.wav"

    with pytest.raises(NothingToSay):
        await s.synthesize(text, str(out))
    assert network == [] and s._supertonic.calls == [] and s.model.spoken_as == []
    assert not out.parent.exists(), "nor is anything made on disk for it"


async def test_supertonic_reports_a_cloned_voice_it_cannot_apply(supertonic, tmp_path):
    result = await supertonic.synthesize(
        "hello", str(tmp_path / "o.wav"), speaker_wav="/v/a.wav", language="en"
    )
    assert supertonic.network == [], "it still speaks, in its own voice"
    assert (result.engine, result.fallback, result.voice_cloned) == ("supertonic", True, False)


async def test_supertonic_that_cannot_be_loaded_is_discovered_once(monkeypatch, tmp_path):
    """No package, or no weights and no network: the app keeps talking, via Edge."""
    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "supertonic")
    s = TTSService()
    calls = _stub_network(monkeypatch, s)
    attempts = []

    def load():
        attempts.append(1)
        raise ModuleNotFoundError("No module named 'supertonic'")

    monkeypatch.setattr(s, "_load_supertonic", load)
    results = [await s.synthesize(f"sentence {i}", str(tmp_path / f"{i}.wav")) for i in range(3)]

    assert len(attempts) == 1
    assert calls == ["edge"] * 3
    assert [r.engine for r in results] == ["edge-tts"] * 3
    # The sentence that found out says so; after that nothing new is lost.
    assert [r.fallback for r in results] == [True, False, False]
    cloned = await s.synthesize("hi", str(tmp_path / "c.wav"), speaker_wav="/v/a.wav")
    assert cloned.fallback is True


async def test_supertonic_loads_once_under_concurrent_first_sentences(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "supertonic")
    monkeypatch.setattr(tts_module.settings, "SUPERTONIC_VOICE", "M2")
    s = TTSService()
    _stub_network(monkeypatch, s)
    loads = []

    def load():
        loads.append(1)
        engine = FakeSupertonic()
        return engine, engine.get_voice_style(tts_module.settings.SUPERTONIC_VOICE)

    monkeypatch.setattr(s, "_load_supertonic", load)
    assert not s.loaded
    results = await asyncio.gather(
        *(s.synthesize("hello", str(tmp_path / f"{i}.wav")) for i in range(4))
    )
    assert len(loads) == 1 and s.loaded
    assert {r.engine for r in results} == {"supertonic"}
    assert {c["voice"] for c in s._supertonic.calls} == {"style:M2"}


async def test_warm_loads_supertonic_only_when_it_is_the_provider(monkeypatch):
    loads = []

    def load():
        loads.append(1)
        engine = FakeSupertonic()
        return engine, "style"

    for provider, expected in (("edge", 0), ("gtts", 0), ("supertonic", 1)):
        monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", provider)
        s = TTSService()
        monkeypatch.setattr(s, "_load_supertonic", load)
        loads.clear()
        await s.warm()
        assert len(loads) == expected, provider

    def broken():
        raise RuntimeError("download failed")

    monkeypatch.setattr(s, "_load_supertonic", broken)
    s._supertonic = None
    await s.warm()  # never raises
    assert s._supertonic_unavailable is True


def test_importing_the_service_does_not_import_supertonic():
    """The app must start without the package, and must not fetch weights at import."""
    import subprocess
    import sys

    code = (
        "import sys, app.services.tts as t; "
        "assert 'supertonic' not in sys.modules; "
        "assert t.tts_service._supertonic is None"
    )
    backend = Path(tts_module.__file__).resolve().parents[2]
    subprocess.run([sys.executable, "-c", code], check=True, cwd=backend, timeout=120)


def _supertonic_weights_cached() -> bool:
    try:
        from supertonic.config import get_model_cache_dir
        from supertonic.loader import has_all_onnx_modules

        # get_model_cache_dir, not the loader's get_cache_dir: that one
        # creates the directory, and asking must leave no trace.
        return has_all_onnx_modules(get_model_cache_dir("supertonic-3"))
    except Exception:
        return False


def _whisper_base_cached() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        return isinstance(try_to_load_from_cache("Systran/faster-whisper-base", "model.bin"), str)
    except Exception:
        return False


@pytest.mark.skipif(
    not _supertonic_weights_cached(),
    reason="Supertonic weights not cached (they download on first use of TTS_PROVIDER=supertonic)",
)
async def test_real_supertonic_speaks_an_intelligible_sentence(monkeypatch, tmp_path):
    """The real model, end to end: text in, a WAV out that says the text."""
    import time

    monkeypatch.setattr(tts_module.settings, "TTS_PROVIDER", "supertonic")
    s = TTSService()
    _stub_network(monkeypatch, s)
    out = tmp_path / "real.wav"
    sentence = "The quick brown fox jumps over the lazy dog."

    await s.warm()
    assert s.loaded, "the weights are cached, so this loads without a download"
    started = time.monotonic()
    result = await s.synthesize(sentence, str(out), language="en")
    took = time.monotonic() - started

    assert (result.engine, result.fallback) == ("supertonic", False)
    info = sf.info(str(out))
    assert (info.samplerate, info.channels, info.subtype) == (48000, 1, "PCM_16")
    assert 1.5 < info.duration < 6.0, f"{info.duration:.2f}s for a nine-word sentence"
    samples, _ = sf.read(str(out), dtype="float32")
    assert np.abs(samples).max() > 0.05, "silence is not speech"
    print(
        f"\nsupertonic: {info.duration:.2f}s of audio in {took:.2f}s (RTF {took / info.duration:.2f})"
    )

    if not _whisper_base_cached():
        return  # intelligibility needs a recogniser; never download one for a test
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8", local_files_only=True)
    segments, _ = await asyncio.to_thread(model.transcribe, str(out), language="en")
    heard = " ".join(seg.text for seg in segments).lower()
    print(f"whisper heard: {heard!r}")
    for word in ("quick", "brown", "fox", "lazy", "dog"):
        assert word in heard, f"{word!r} did not come back: {heard!r}"
