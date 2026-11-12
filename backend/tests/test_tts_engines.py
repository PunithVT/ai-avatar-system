"""
TTS engine selection and speaker isolation.

Chatterbox itself is not installed in the test environment (it cannot share
an environment with the lip-sync stack), so the model is a stand-in that
reproduces the one behaviour that matters here: the active speaker's
conditioning lives on the model object and persists between calls.
"""

import asyncio
from pathlib import Path

import pytest

import app.services.tts as tts_module
from app.services.tts import TTSService


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
