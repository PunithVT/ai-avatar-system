"""
Tests for the rolling conversation summary.

The LLM context is a fixed window; anything older is dropped, so a long
conversation silently forgot its own beginning. Turns leaving the window are
now folded into a running summary carried in the system prompt.

What matters here is not summary quality — that is the model's job — but that
the summary reaches the prompt in the right shape, that it is only produced
when the window actually overflows, and above all that nothing about it can
fail a turn.
"""

import pytest

from app.config import settings
from app.websocket import ConnectionManager, _compose_system_prompt


# ── prompt composition ───────────────────────────────────────────────────
def test_no_summary_leaves_the_prompt_untouched():
    assert _compose_system_prompt("You are Nova.", None) == "You are Nova."
    assert _compose_system_prompt("You are Nova.", "") == "You are Nova."


def test_summary_is_appended_after_the_persona():
    """
    Persona first: it governs how the avatar speaks, and burying it under
    recalled context makes models drift off-character.
    """
    out = _compose_system_prompt("You are Nova.", "They discussed sailboats.")
    assert out.index("You are Nova.") < out.index("They discussed sailboats.")
    assert "Earlier in this same conversation" in out


def test_summary_alone_still_produces_a_usable_prompt():
    """An avatar with no persona set must still receive its memory."""
    out = _compose_system_prompt(None, "They discussed sailboats.")
    assert "They discussed sailboats." in out
    assert out.startswith("Earlier in this same conversation")


def test_no_persona_and_no_summary_is_none():
    assert _compose_system_prompt(None, None) is None


# ── when the summary is produced ─────────────────────────────────────────
@pytest.fixture
def manager():
    m = ConnectionManager()
    m.session_data["s1"] = {"memory_summary": None, "system_prompt": "You are Nova."}
    return m


async def test_nothing_dropped_means_no_llm_call(manager, monkeypatch):
    """Summarising every turn would double the LLM cost of a conversation."""
    called = {"n": 0}

    async def _spy(**_kw):
        called["n"] += 1
        return "x"

    monkeypatch.setattr("app.websocket.llm_service.generate_response", _spy)
    await manager._roll_memory("s1", [])
    assert called["n"] == 0


async def test_disabled_setting_means_no_llm_call(manager, monkeypatch):
    called = {"n": 0}

    async def _spy(**_kw):
        called["n"] += 1
        return "x"

    monkeypatch.setattr("app.websocket.llm_service.generate_response", _spy)
    monkeypatch.setattr(settings, "CONVERSATION_MEMORY", False)
    await manager._roll_memory("s1", [{"role": "user", "content": "hi"}])
    assert called["n"] == 0


async def test_dropped_turns_produce_a_summary(manager, monkeypatch):
    async def _fake(**_kw):
        return "  They planned a trip to Lisbon.  "

    monkeypatch.setattr("app.websocket.llm_service.generate_response", _fake)
    monkeypatch.setattr(manager, "_persist_memory_summary", _noop)
    await manager._roll_memory("s1", [{"role": "user", "content": "hi"}])
    assert manager.session_data["s1"]["memory_summary"] == "They planned a trip to Lisbon."


async def test_existing_summary_is_carried_into_the_next_one(manager, monkeypatch):
    """
    The summary must compound. Without feeding the old one back in, it would
    only ever describe the most recently dropped chunk and the earliest part of
    the conversation would still be lost.
    """
    seen = {}

    async def _capture(messages, **_kw):
        seen["prompt"] = messages[0]["content"]
        return "updated"

    manager.session_data["s1"]["memory_summary"] = "They met in Porto."
    monkeypatch.setattr("app.websocket.llm_service.generate_response", _capture)
    monkeypatch.setattr(manager, "_persist_memory_summary", _noop)
    await manager._roll_memory("s1", [{"role": "user", "content": "and then?"}])
    assert "They met in Porto." in seen["prompt"]
    assert "and then?" in seen["prompt"]


async def test_summary_is_length_capped(manager, monkeypatch):
    """A runaway summary would crowd out the recent turns it exists to supplement."""

    async def _long(**_kw):
        return "x" * 10_000

    monkeypatch.setattr("app.websocket.llm_service.generate_response", _long)
    monkeypatch.setattr(manager, "_persist_memory_summary", _noop)
    monkeypatch.setattr(settings, "MEMORY_SUMMARY_MAX_CHARS", 100)
    await manager._roll_memory("s1", [{"role": "user", "content": "hi"}])
    assert len(manager.session_data["s1"]["memory_summary"]) <= 101  # cap + ellipsis


# ── failure must never cost the turn ─────────────────────────────────────
async def _noop(*_a, **_kw):
    return None


@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("provider exploded"),
        TimeoutError("took too long"),
    ],
)
async def test_llm_failure_does_not_raise(manager, monkeypatch, boom):
    """
    Losing the summary costs continuity. Raising would cost the turn, which is
    the thing the user actually asked for.
    """

    async def _fail(**_kw):
        raise boom

    monkeypatch.setattr("app.websocket.llm_service.generate_response", _fail)
    await manager._roll_memory("s1", [{"role": "user", "content": "hi"}])
    assert manager.session_data["s1"]["memory_summary"] is None


async def test_llm_error_subclass_does_not_raise(manager, monkeypatch):
    from app.services.llm import LLMRateLimited

    async def _fail(**_kw):
        raise LLMRateLimited("429")

    monkeypatch.setattr("app.websocket.llm_service.generate_response", _fail)
    await manager._roll_memory("s1", [{"role": "user", "content": "hi"}])
    assert manager.session_data["s1"]["memory_summary"] is None


async def test_empty_summary_is_ignored(manager, monkeypatch):
    """A blank reply must not overwrite a good existing summary with nothing."""

    async def _blank(**_kw):
        return "   "

    manager.session_data["s1"]["memory_summary"] = "They met in Porto."
    monkeypatch.setattr("app.websocket.llm_service.generate_response", _blank)
    monkeypatch.setattr(manager, "_persist_memory_summary", _noop)
    await manager._roll_memory("s1", [{"role": "user", "content": "hi"}])
    assert manager.session_data["s1"]["memory_summary"] == "They met in Porto."


async def test_persist_failure_does_not_raise(manager, monkeypatch):
    """
    A database problem must not fail the turn either.

    Writing this test caught the implementation relying on the callee's own
    try/except: _roll_memory awaited _persist_memory_summary unguarded, so any
    future refactor of that method would have turned a storage hiccup into a
    failed reply. The guard now lives at the call site too.
    """

    async def _fake(**_kw):
        return "a summary"

    async def _boom(*_a, **_kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr("app.websocket.llm_service.generate_response", _fake)
    monkeypatch.setattr(manager, "_persist_memory_summary", _boom)

    await manager._roll_memory("s1", [{"role": "user", "content": "hi"}])

    # The live session keeps its summary; only a reconnect would lose it.
    assert manager.session_data["s1"]["memory_summary"] == "a summary"


async def test_unknown_session_is_a_noop(manager, monkeypatch):
    async def _spy(**_kw):
        raise AssertionError("should not be called")

    monkeypatch.setattr("app.websocket.llm_service.generate_response", _spy)
    await manager._roll_memory("does-not-exist", [{"role": "user", "content": "hi"}])
