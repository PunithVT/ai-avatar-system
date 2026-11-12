"""
The turn pipeline's behaviour at its edges: long conversations, interruption,
reconnects, and flooding. Each test here pins a defect found by audit.

Everything external (LLM, TTS, the animator, storage, the database) is
replaced with a recording stand-in, so what is measured is the orchestration.
"""

import asyncio
from pathlib import Path

import pytest

import app.websocket as wsmod
from app.config import settings
from app.services.tts import SynthResult
from app.websocket import (
    MAX_CONTEXT_MESSAGES,
    TRIM_CONTEXT_TO,
    WS_CLOSE_SUPERSEDED,
    ConnectionManager,
    _llm_view,
    _trim_window,
)


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed_with = None
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self, code=1000):
        self.closed_with = code

    def types(self):
        return [m["type"] for m in self.sent]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """A manager with one live session and every dependency stubbed."""
    m = ConnectionManager()
    ws = FakeWS()
    m.active_connections["s1"] = ws
    m._send_locks["s1"] = asyncio.Lock()
    image = tmp_path / "avatar.jpg"
    image.write_bytes(b"jpg")
    m.session_data["s1"] = {
        "messages": [],
        "avatar_image_local": str(image),
        "voice_wav": None,
        "language": "en",
        "system_prompt": None,
        "memory_summary": None,
        "user_id": "u1",
    }
    monkeypatch.setattr(wsmod, "TMPDIR", tmp_path)

    class Rig:
        manager = m
        socket = ws
        reply = "First part, and then more. Second sentence here. Third one too."
        token_delay = 0.0
        tts_delay = 0.0
        animate_delay = 0.0
        events: list[str] = []
        llm_calls: list[list[dict]] = []
        persisted: list[tuple] = []
        summaries: list[str] = []

    r = Rig()
    r.events, r.llm_calls, r.persisted, r.summaries = [], [], [], []

    async def stream(messages, system_prompt=None):
        r.llm_calls.append([dict(x) for x in messages])
        for word in r.reply.split(" "):
            if r.token_delay:
                await asyncio.sleep(r.token_delay)
            yield word + " "

    async def generate(messages, system_prompt=None, thinking=False):
        r.summaries.append(messages[0]["content"])
        return "summary"

    async def synth(text, output_path, speaker_wav=None, language="en"):
        r.events.append(f"tts-start:{text[:6]}")
        await asyncio.sleep(r.tts_delay)
        Path(output_path).write_bytes(b"wav")
        r.events.append(f"tts-end:{text[:6]}")
        return SynthResult(output_path=output_path, engine="x", fallback=False, voice_cloned=False)

    async def animate(avatar_image_path, audio_path, output_path, cache_key=None):
        r.events.append("anim-start")
        await asyncio.sleep(r.animate_delay)
        Path(output_path).write_bytes(b"mp4")
        r.events.append("anim-end")
        return output_path

    async def upload(data, key, content_type=None):
        return key

    async def serving_url(key):
        return f"http://test/{key}"

    async def persist(session_id, role, content, latency=None):
        r.persisted.append((role, content))

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(wsmod.llm_service, "stream_response", stream)
    monkeypatch.setattr(wsmod.llm_service, "generate_response", generate)
    monkeypatch.setattr(wsmod.tts_service, "synthesize", synth)
    monkeypatch.setattr(wsmod.avatar_animator, "animate", animate)
    monkeypatch.setattr(wsmod.storage_service, "upload_file", upload)
    monkeypatch.setattr(wsmod.storage_service, "serving_url", serving_url)
    monkeypatch.setattr(m, "_persist_message", persist)
    monkeypatch.setattr(m, "_ensure_conversation_title", noop)
    monkeypatch.setattr(m, "_persist_memory_summary", noop)
    return r


async def _settle(manager):
    while manager._background:
        await asyncio.gather(*list(manager._background), return_exceptions=True)


# ── history shaping ──────────────────────────────────────────────────────
def test_llm_view_drops_a_leading_assistant_message():
    msgs = [
        {"role": "assistant", "content": "orphan"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    assert [m["role"] for m in _llm_view(msgs)] == ["user", "assistant"]


def test_llm_view_merges_consecutive_messages_from_one_role():
    """What a failed or interrupted turn leaves behind: two user messages."""
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "a"},
    ]
    view = _llm_view(msgs)
    assert [m["role"] for m in view] == ["user", "assistant"]
    assert view[0]["content"] == "first\n\nsecond"
    assert msgs[0]["content"] == "first", "the stored history must not be rewritten"


def test_trim_window_cuts_back_past_the_cap_and_opens_on_a_user_message():
    msgs = []
    for i in range(31):
        msgs.append({"role": "user", "content": f"u{i}"})
        if i < 30:
            msgs.append({"role": "assistant", "content": f"a{i}"})
    assert len(msgs) == MAX_CONTEXT_MESSAGES + 1

    dropped = _trim_window(msgs)

    assert len(msgs) <= TRIM_CONTEXT_TO
    assert msgs[0]["role"] == "user"
    assert dropped and dropped[0]["content"] == "u0"
    assert dropped[-1]["role"] == "assistant", "a pair must not be split across the cut"


def test_trim_window_leaves_a_short_history_alone():
    msgs = [{"role": "user", "content": "q"}] * MAX_CONTEXT_MESSAGES
    assert _trim_window(msgs) == []
    assert len(msgs) == MAX_CONTEXT_MESSAGES


async def test_a_long_conversation_summarises_occasionally_not_every_turn(rig):
    """
    The window used to be trimmed to exactly the cap, so from the first
    overflow on EVERY turn evicted two messages and paid for a summarisation
    call, awaited before the reply started.
    """
    rig.reply = "Fine."
    turns = 80
    for i in range(turns):
        await rig.manager._handle_text_input_inner("s1", f"question {i}")
        await _settle(rig.manager)

    history = rig.manager.session_data["s1"]["messages"]
    assert len(history) <= MAX_CONTEXT_MESSAGES
    # 80 turns = 160 messages. One roll per (60 - 40) / 2 = 10 turns after the
    # first overflow at turn 31 → 5 or 6, not the ~50 the old code made.
    assert 1 <= len(rig.summaries) <= 7, len(rig.summaries)
    for call in rig.llm_calls:
        assert call[0]["role"] == "user", "the provider must never see an assistant-first history"
        roles = [m["role"] for m in call]
        assert all(a != b for a, b in zip(roles, roles[1:])), "roles must alternate"


async def test_the_summary_does_not_delay_the_reply_that_triggers_it(rig, monkeypatch):
    """The roll is context for later turns; it must run behind this one."""
    rig.reply = "Fine."
    gate = asyncio.Event()

    async def slow_summary(messages, system_prompt=None, thinking=False):
        await gate.wait()
        return "summary"

    monkeypatch.setattr(wsmod.llm_service, "generate_response", slow_summary)
    history = rig.manager.session_data["s1"]["messages"]
    for i in range(30):
        history.append({"role": "user", "content": f"u{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})

    # Would hang here forever if the turn awaited the summary.
    await asyncio.wait_for(rig.manager._handle_text_input_inner("s1", "overflow"), timeout=5)
    assert "message" in rig.socket.types()
    gate.set()
    await _settle(rig.manager)
    assert rig.manager.session_data["s1"]["memory_summary"] == "summary"


# ── interruption ─────────────────────────────────────────────────────────
async def test_cancelling_a_turn_with_a_full_sentence_queue_does_not_hang(rig):
    """
    The producer's `finally` awaited queue.put(None). With the queue full and
    its consumer already cancelled, that never returned: the turn task and the
    provider's HTTP stream stayed open forever.
    """
    rig.reply = " ".join(f"Sentence number {i} is right here." for i in range(12))
    rig.animate_delay = 30  # the consumer is stuck on the first chunk
    task = asyncio.create_task(rig.manager._handle_text_input_inner("s1", "tell me a lot"))
    await asyncio.sleep(0.3)  # producer fills the queue and blocks on put()

    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=3)
    assert done, "the cancelled turn never finished"
    assert task.cancelled()


async def test_an_interrupted_reply_is_kept_in_history_and_persisted(rig):
    rig.reply = "Here is a long answer that will be cut off. " * 5
    rig.token_delay = 0.01
    rig.animate_delay = 30
    task = asyncio.create_task(rig.manager._handle_text_input_inner("s1", "question"))
    await asyncio.sleep(0.25)
    task.cancel()
    await asyncio.wait({task}, timeout=3)
    await _settle(rig.manager)

    history = rig.manager.session_data["s1"]["messages"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["content"].startswith("Here is a long answer")
    assert ("assistant", history[1]["content"]) in rig.persisted


async def test_an_interrupted_turn_leaves_no_temp_files(rig, tmp_path):
    rig.reply = " ".join(f"Sentence number {i} is right here." for i in range(6))
    rig.animate_delay = 30
    task = asyncio.create_task(rig.manager._handle_text_input_inner("s1", "question"))
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.wait({task}, timeout=3)

    leftovers = [p.name for p in (tmp_path / "avatar-session-s1").iterdir()]
    assert leftovers == [], leftovers


async def test_a_follow_up_sent_the_moment_the_last_clip_arrives_keeps_history_in_order(rig):
    """
    The reply used to be saved only after the final clip was delivered, so a
    client that answered immediately cancelled the save: the transcript came
    out as user, user, assistant — or with the reply stored twice.

    The follow-up is fired from INSIDE the send of video_chunk_end, while the
    first turn's task is still running, so the overlap is real rather than
    left to scheduling luck.
    """
    rig.reply = "A short answer."
    m = rig.manager
    questions = ["question 1", "question 2"]
    interrupted_live_turn = []
    real_send = rig.socket.send_json

    async def send_and_reply(msg):
        await real_send(msg)
        if msg["type"] == "video_chunk_end" and questions:
            task = m._active_turns.get("s1")
            interrupted_live_turn.append(task is not None and not task.done())
            # Scheduled, as a client's frame arriving on the receive loop
            # would be: it runs while this turn is still unwinding.
            asyncio.get_running_loop().create_task(m.handle_text_input("s1", questions.pop(0)))

    rig.socket.send_json = send_and_reply
    await m.handle_text_input("s1", "question 0")
    for _ in range(300):
        await asyncio.sleep(0.01)
        if not questions and not m._active_turns:
            break
    await _settle(m)

    assert interrupted_live_turn and all(interrupted_live_turn), (
        "the follow-up must arrive while the previous turn's task is still alive"
    )
    roles = [r for r, _ in rig.persisted]
    assert roles == ["user", "assistant"] * 3, roles
    history = [x["role"] for x in m.session_data["s1"]["messages"]]
    assert history == ["user", "assistant"] * 3, history


async def test_cancelling_after_the_model_finished_does_not_store_the_reply_twice(rig):
    rig.reply = "First sentence here. Second sentence here."
    rig.animate_delay = 30  # the model is long done; animation is not
    task = asyncio.create_task(rig.manager._handle_text_input_inner("s1", "question"))
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.wait({task}, timeout=3)
    await _settle(rig.manager)

    assert [r for r, _ in rig.persisted] == ["user", "assistant"]
    assert [x["role"] for x in rig.manager.session_data["s1"]["messages"]] == ["user", "assistant"]


# ── pipelining ───────────────────────────────────────────────────────────
async def test_speech_for_the_next_sentence_is_synthesised_during_animation(rig):
    """TTS used to sit idle for the whole of every animation."""
    rig.tts_delay = 0.02
    rig.animate_delay = 0.2
    await rig.manager._handle_text_input_inner("s1", "question")

    ev = rig.events
    first_anim_end = ev.index("anim-end")
    second_tts_start = [i for i, e in enumerate(ev) if e.startswith("tts-start")][1]
    assert second_tts_start < first_anim_end, ev

    chunks = [m for m in rig.socket.sent if m["type"] == "video_chunk"]
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    spoken = " ".join(c["text"] for c in chunks)
    assert spoken == rig.reply, "every sentence is spoken, in order"
    assert rig.socket.types()[-1] == "video_chunk_end"


async def test_one_failed_sentence_does_not_lose_the_rest(rig, monkeypatch):
    real = wsmod.tts_service.synthesize

    async def flaky(text, output_path, speaker_wav=None, language="en"):
        if text.startswith("Second"):
            raise RuntimeError("tts blew up")
        return await real(text, output_path, speaker_wav=speaker_wav, language=language)

    monkeypatch.setattr(wsmod.tts_service, "synthesize", flaky)
    await rig.manager._handle_text_input_inner("s1", "question")

    chunks = [m for m in rig.socket.sent if m["type"] == "video_chunk"]
    # Four chunks in the reply (the opening clause splits off); one failed.
    assert len(chunks) == 3
    assert not any(c["text"].startswith("Second") for c in chunks)
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2]


async def test_missing_avatar_image_closes_the_turn_out(rig):
    """Returning silently left the client's spinner up indefinitely."""
    rig.manager.session_data["s1"]["avatar_image_local"] = None
    await rig.manager._handle_text_input_inner("s1", "question")
    end = [m for m in rig.socket.sent if m["type"] == "video_chunk_end"]
    assert end and end[0]["sent_chunks"] == 0
    assert "error" in rig.socket.types()


# ── reconnects ───────────────────────────────────────────────────────────
async def test_a_reconnect_closes_the_old_socket_and_survives_its_teardown(rig, monkeypatch):
    async def no_load(session_id):
        return None

    monkeypatch.setattr(rig.manager, "_load_session_data", no_load)
    old = rig.socket
    new = FakeWS()

    await rig.manager.connect("s1", new, user_id="u1")
    assert rig.manager.active_connections["s1"] is new
    await _settle(rig.manager)  # the old socket is closed in the background
    assert old.closed_with == WS_CLOSE_SUPERSEDED

    # The old socket's receive loop now notices it is dead and cleans up.
    await rig.manager.disconnect("s1", old)
    assert rig.manager.active_connections.get("s1") is new, "the new connection was torn down"
    assert new.closed_with is None
    assert "s1" in rig.manager.session_data


async def test_a_reconnect_cancels_the_turn_still_streaming_to_the_old_socket(rig, monkeypatch):
    async def no_load(session_id):
        return None

    monkeypatch.setattr(rig.manager, "_load_session_data", no_load)
    rig.animate_delay = 30
    rig.manager._spawn_turn("s1", rig.manager._handle_text_input_inner("s1", "question"))
    await asyncio.sleep(0.1)
    turn = rig.manager._active_turns["s1"]

    await rig.manager.connect("s1", FakeWS(), user_id="u1")
    await asyncio.wait({turn}, timeout=3)
    assert turn.cancelled()


async def test_a_reconnect_does_not_wait_for_a_dead_peer_to_acknowledge_the_close(rig, monkeypatch):
    """The usual reason for a reconnect is that the old peer is gone."""

    async def no_load(session_id):
        return None

    monkeypatch.setattr(rig.manager, "_load_session_data", no_load)

    async def never_returns(code=1000):
        await asyncio.sleep(3600)

    rig.socket.close = never_returns
    new = FakeWS()
    await asyncio.wait_for(rig.manager.connect("s1", new, user_id="u1"), timeout=1)
    assert rig.manager.active_connections["s1"] is new
    for t in list(rig.manager._background):
        t.cancel()


async def test_barge_in_stores_the_interrupted_reply_before_the_new_question(rig):
    """
    Cancellation lands on a later loop iteration than the new turn starts, so
    leaving the commit to the cancelled task put the old reply AFTER the new
    question — and the next request to the model ended on an assistant turn.
    """
    rig.reply = "Here is a long answer that keeps going. " * 6
    rig.token_delay = 0.01
    rig.animate_delay = 30
    m = rig.manager
    await m.handle_text_input("s1", "first question")
    await asyncio.sleep(0.2)  # mid-reply

    rig.reply = "Second answer."
    rig.token_delay = 0
    rig.animate_delay = 0
    await m.handle_text_input("s1", "second question")  # barge-in
    await asyncio.gather(m._active_turns["s1"], return_exceptions=True)
    await _settle(m)

    history = m.session_data["s1"]["messages"]
    assert [x["role"] for x in history] == ["user", "assistant", "user", "assistant"], history
    assert history[1]["content"].startswith("Here is a long answer")
    assert history[2]["content"] == "second question"
    second_request = rig.llm_calls[-1]
    assert second_request[-1] == {"role": "user", "content": "second question"}
    assert [r for r, _ in rig.persisted] == ["user", "assistant", "user", "assistant"]


# ── flooding ─────────────────────────────────────────────────────────────
async def test_turns_are_rate_limited_per_user(rig, monkeypatch):
    monkeypatch.setattr(settings, "WS_TURNS_PER_MINUTE", 3)
    rig.reply = "Ok."
    for i in range(5):
        await rig.manager.handle_text_input("s1", f"message {i}")
        task = rig.manager._active_turns.get("s1")
        if task:
            await asyncio.gather(task, return_exceptions=True)

    assert len(rig.llm_calls) == 3, "only the allowed turns may reach the provider"
    limited = [m for m in rig.socket.sent if m.get("code") == "rate_limited"]
    assert len(limited) == 2


async def test_the_limit_follows_the_user_across_sessions(rig, monkeypatch):
    """Opening more sessions must not buy more turns."""
    monkeypatch.setattr(settings, "WS_TURNS_PER_MINUTE", 2)
    m = rig.manager
    m.session_data["s2"] = dict(m.session_data["s1"], messages=[])
    assert m._allow_turn("s1") and m._allow_turn("s2")
    assert not m._allow_turn("s1")
    assert not m._allow_turn("s2")
    m.session_data["s3"] = dict(m.session_data["s1"], user_id="someone-else")
    assert m._allow_turn("s3")


async def test_oversized_audio_is_refused_before_it_is_decoded(rig, monkeypatch):
    monkeypatch.setattr(settings, "WS_MAX_AUDIO_BYTES", 1024)
    await rig.manager.handle_audio_input("s1", "A" * 4096)
    assert rig.socket.sent[-1] == {"type": "error", "message": "Audio payload too large"}
    assert "s1" not in rig.manager._active_turns


# ── voice ownership ──────────────────────────────────────────────────────
async def test_a_voice_owned_by_someone_else_is_not_attached(rig, monkeypatch):
    async def entry(voice_id):
        return {"id": voice_id, "user_id": "victim", "wav_path": "/voices/victim.wav"}

    monkeypatch.setattr(rig.manager, "_get_voice_entry", entry)
    assert await rig.manager.set_voice_by_id("s1", "v1") is False
    assert rig.manager.session_data["s1"]["voice_wav"] is None


async def test_an_unowned_legacy_voice_entry_is_not_attached(rig, monkeypatch):
    async def entry(voice_id):
        return {"id": voice_id, "wav_path": "/voices/legacy.wav"}  # no user_id

    monkeypatch.setattr(rig.manager, "_get_voice_entry", entry)
    assert await rig.manager.set_voice_by_id("s1", "v1") is False
