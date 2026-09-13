"""
Regression tests for gaps found auditing the service layer.

Each pins a defect that was live: a stream chunk shape that killed a turn
after the user had seen the whole reply, a typed error hierarchy nothing
consumed, a "never silent" fallback chain that fell silent, and a provider
typo that surfaced as an AttributeError from the wrong place.
"""

import pytest

from app.services.llm import (
    LLMAuthError,
    LLMError,
    LLMRateLimited,
    LLMService,
    LLMUnavailable,
)


# ── OpenAI-compatible stream chunk shapes ────────────────────────────────
class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, choices):
        self.choices = choices


def _svc() -> LLMService:
    """An LLMService with the client stubbed, skipping __init__'s provider setup."""
    svc = object.__new__(LLMService)
    svc.provider = "openai"
    svc.model = "m"
    svc.temperature = 0.7
    svc.max_tokens = 128
    svc.reasoning_effort = None
    return svc


def _client_streaming(chunks):
    async def gen():
        for c in chunks:
            yield c

    class C:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kw):
                    return gen()

    return C()


async def test_terminal_usage_chunk_does_not_kill_the_stream():
    """
    OpenAI sends a final usage-only chunk with an empty `choices` list when
    include_usage is set, and Ollama/vLLM/OpenRouter emit one unconditionally.
    Indexing [0] blindly raised IndexError on it, failing the turn *after* the
    full reply had already been streamed to the user.
    """
    svc = _svc()
    svc.client = _client_streaming(
        [_Chunk([_Choice("Hello ")]), _Chunk([_Choice("world")]), _Chunk([])]
    )
    out = [t async for t in svc._stream_openai([{"role": "user", "content": "hi"}])]
    assert "".join(out) == "Hello world"


async def test_interleaved_empty_chunks_are_skipped():
    svc = _svc()
    svc.client = _client_streaming(
        [_Chunk([]), _Chunk([_Choice("a")]), _Chunk([]), _Chunk([_Choice("b")]), _Chunk([])]
    )
    out = [t async for t in svc._stream_openai([{"role": "user", "content": "hi"}])]
    assert "".join(out) == "ab"


async def test_response_with_no_choices_raises_a_typed_error():
    """A choice-less non-streaming response must be an LLMError, not IndexError."""
    svc = _svc()

    class C:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kw):
                    return type("R", (), {"choices": []})()

    svc.client = C()
    with pytest.raises(LLMError):
        await svc._generate_openai([{"role": "user", "content": "hi"}])


# ── provider validation ──────────────────────────────────────────────────
def test_unknown_provider_fails_at_construction(monkeypatch):
    """
    A typo in LLM_PROVIDER used to leave `self.client` unset and surface much
    later as an AttributeError from inside the turn pipeline.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "gpt5-turbo-max")
    with pytest.raises(LLMError, match="Unsupported LLM_PROVIDER"):
        LLMService()


# ── typed errors reach the user ──────────────────────────────────────────
@pytest.mark.parametrize(
    "exc,expected_fragment",
    [
        (LLMRateLimited("429"), "rate-limiting"),
        (LLMAuthError("401"), "credentials"),
        (LLMUnavailable("timeout"), "Couldn't reach"),
        (LLMError("weird"), "returned an error"),
    ],
)
def test_llm_errors_get_distinct_user_messages(exc, expected_fragment):
    """
    llm.py builds this hierarchy so the transport can tell the cases apart,
    but nothing consumed it — every failure surfaced as "Processing failed",
    whether the fix was to wait, to reconfigure, or to retry.
    """
    from app.websocket import _llm_error_message

    assert expected_fragment.lower() in _llm_error_message(exc).lower()


def test_llm_error_messages_are_all_distinct():
    from app.websocket import _llm_error_message

    msgs = {
        _llm_error_message(e)
        for e in (LLMRateLimited("x"), LLMAuthError("x"), LLMUnavailable("x"), LLMError("x"))
    }
    assert len(msgs) == 4


# ── the fallback chain must never end in silence ─────────────────────────
def test_gtts_language_falls_back_for_unsupported_codes():
    """
    Hebrew is in the app's 23-language set but gTTS rejects it with
    ValueError. gTTS is the last link in the chain, so raising there turned a
    supported language into silence — the exact outcome the chain exists to
    prevent.
    """
    from app.services.tts import TTSService

    assert TTSService._gtts_language("he") == "en"
    assert TTSService._gtts_language("zz") == "en"


@pytest.mark.parametrize("lang", ["en", "fr", "de", "es", "ja"])
def test_gtts_language_passes_through_supported_codes(lang):
    from app.services.tts import TTSService

    assert TTSService._gtts_language(lang) == lang


def test_every_offered_language_survives_the_last_fallback():
    """Every language the WebSocket accepts must reach gTTS as something valid."""
    from gtts.lang import tts_langs

    from app.services.tts import TTSService

    offered = {
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
    supported = set(tts_langs())
    for lang in sorted(offered):
        assert TTSService._gtts_language(lang) in supported, lang


# ── uploads are capped while streaming, on every endpoint ────────────────
class _FakeUpload:
    """Minimal UploadFile stand-in that records how much was actually read."""

    def __init__(self, total: int):
        self._remaining = total
        self.bytes_read = 0

    async def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        n = self._remaining if size is None or size < 0 else min(size, self._remaining)
        self._remaining -= n
        self.bytes_read += n
        return b"\0" * n


async def test_read_capped_stops_early_instead_of_buffering_everything():
    """
    The point of streaming is that an oversized body is refused *before* it is
    held in memory. `await file.read()` then checking the length happily
    buffered gigabytes first.
    """
    import pytest as _pytest
    from fastapi import HTTPException

    from app.uploads import read_capped

    limit = 256 * 1024
    upload = _FakeUpload(64 * 1024 * 1024)  # 64 MB claimed

    with _pytest.raises(HTTPException) as exc:
        await read_capped(upload, limit)

    assert exc.value.status_code == 413
    # Read no more than the limit plus the chunk that crossed it.
    assert upload.bytes_read <= limit + 64 * 1024, upload.bytes_read


async def test_read_capped_returns_content_within_the_limit():
    from app.uploads import read_capped

    upload = _FakeUpload(1000)
    assert len(await read_capped(upload, 4096)) == 1000


async def test_voice_clone_rejects_oversized_audio(client, auth_headers, monkeypatch):
    """
    The voice endpoint had the same unbounded read the avatar endpoint did —
    fixed there, then written again here. Both now share one helper.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "MAX_VOICE_UPLOAD_SIZE", 128 * 1024)
    r = await client.post(
        "/api/v1/voices/clone",
        data={"name": "big", "language": "en"},
        files={"audio": ("s.wav", b"\0" * (512 * 1024), "audio/wav")},
        headers=auth_headers,
    )
    assert r.status_code == 413, r.text


async def test_voice_upload_limit_follows_the_setting(client, auth_headers, monkeypatch):
    """A hardcoded 20 MB meant changing the setting did nothing."""
    from app.config import settings

    payload = b"\0" * 8192
    monkeypatch.setattr(settings, "MAX_VOICE_UPLOAD_SIZE", len(payload) - 1)
    r = await client.post(
        "/api/v1/voices/clone",
        data={"name": "n", "language": "en"},
        files={"audio": ("s.wav", payload, "audio/wav")},
        headers=auth_headers,
    )
    assert r.status_code == 413, r.text
