"""
Which request parameters each Claude generation is sent.

Models up to 4.6 accept `temperature` and take a thinking token budget;
models from 4.7 on reject both (400) and are steered with `effort` instead.
Sending the wrong set fails every turn of every conversation, so the mapping
is pinned here.
"""

import pytest

import app.services.llm as llm_module
from app.services.llm import LLMRefused, LLMService, _is_legacy_claude


@pytest.mark.parametrize(
    "model",
    [
        "claude-3-5-sonnet-20241022",
        "claude-3-haiku-20240307",
        "claude-sonnet-4-20250514",
        "claude-opus-4-1",
        "claude-opus-4-1-20250805",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
    ],
)
def test_models_through_4_6_are_legacy(model):
    assert _is_legacy_claude(model)


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-sonnet-5-5",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-haiku-5-5",
        "claude-fable-5-2",
        "",
    ],
)
def test_models_from_4_7_are_current(model):
    assert not _is_legacy_claude(model)


def _service(monkeypatch, model, effort="low"):
    monkeypatch.setattr(llm_module.settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(llm_module.settings, "LLM_MODEL", model)
    monkeypatch.setattr(llm_module.settings, "LLM_EFFORT", effort)
    monkeypatch.setattr(llm_module.settings, "ANTHROPIC_API_KEY", "test-key")
    return LLMService()


_MSGS = [{"role": "user", "content": "hi"}]


def test_current_models_get_effort_and_no_sampling_parameters(monkeypatch):
    kwargs = _service(monkeypatch, "claude-sonnet-5-5")._anthropic_kwargs(_MSGS, None)
    assert "temperature" not in kwargs and "top_p" not in kwargs
    assert kwargs["output_config"] == {"effort": "low"}
    assert "thinking" not in kwargs, "the conversational path leaves thinking to the default"


def test_empty_effort_setting_sends_no_effort(monkeypatch):
    kwargs = _service(monkeypatch, "claude-sonnet-5-5", effort="")._anthropic_kwargs(_MSGS, None)
    assert "output_config" not in kwargs


def test_current_models_never_get_a_thinking_budget(monkeypatch):
    kwargs = _service(monkeypatch, "claude-opus-5-5")._anthropic_kwargs(_MSGS, None, thinking=True)
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "high"}
    assert "temperature" not in kwargs


def test_legacy_models_get_neither_temperature_nor_effort(monkeypatch):
    svc = _service(monkeypatch, "claude-sonnet-4-6")
    kwargs = svc._anthropic_kwargs(_MSGS, None)
    assert "temperature" not in kwargs
    assert "output_config" not in kwargs

    thinking = svc._anthropic_kwargs(_MSGS, None, thinking=True)
    assert thinking["thinking"]["type"] == "enabled"
    assert thinking["thinking"]["budget_tokens"] < thinking["max_tokens"]
    assert "temperature" not in thinking


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-sonnet-4-6", "claude-haiku-4-5"])
@pytest.mark.parametrize("thinking", [False, True])
def test_every_parameter_sent_is_one_the_installed_sdk_accepts(monkeypatch, model, thinking):
    """
    The failure this guards against was not an API error but a TypeError: the
    SDK had dropped an argument this module still passed. Check the real
    signatures, so the next removal fails here instead of in production.
    """
    import inspect

    from anthropic.resources.messages import AsyncMessages

    kwargs = _service(monkeypatch, model)._anthropic_kwargs(_MSGS, "sys", thinking=thinking)
    for method in ("stream", "create"):
        accepted = set(inspect.signature(getattr(AsyncMessages, method)).parameters)
        assert set(kwargs) <= accepted, f"{method}() does not accept {set(kwargs) - accepted}"


def test_default_model_is_a_current_one():
    from app.config import Settings

    assert not _is_legacy_claude(Settings.model_fields["LLM_MODEL"].default)


class _FakeStream:
    def __init__(self, texts, stop_reason):
        self._texts, self._stop = texts, stop_reason

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def gen():
            for t in self._texts:
                yield t

        return gen()

    async def get_final_message(self):
        class Final:
            usage = None

        Final.stop_reason = self._stop
        return Final()


async def test_a_refusal_with_no_text_is_an_error_not_an_empty_reply(monkeypatch):
    svc = _service(monkeypatch, "claude-sonnet-5-5")
    monkeypatch.setattr(svc.client.messages, "stream", lambda **kw: _FakeStream([], "refusal"))
    with pytest.raises(LLMRefused):
        async for _ in svc.stream_response(_MSGS):
            pass


async def test_a_normal_stream_yields_its_text(monkeypatch):
    svc = _service(monkeypatch, "claude-sonnet-5-5")
    seen = {}

    def stream(**kw):
        seen.update(kw)
        return _FakeStream(["Hel", "lo"], "end_turn")

    monkeypatch.setattr(svc.client.messages, "stream", stream)
    out = [t async for t in svc.stream_response(_MSGS, "Be brief.")]
    assert out == ["Hel", "lo"]
    assert "temperature" not in seen and seen["output_config"] == {"effort": "low"}
