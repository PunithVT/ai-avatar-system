"""
The turn pipeline's behaviour at its edges: long conversations, interruption,
reconnects, and flooding. Each test here pins a defect found by audit.

Everything external (LLM, TTS, the animator, storage, the database) is
replaced with a recording stand-in, so what is measured is the orchestration.
"""

import asyncio
from pathlib import Path

import numpy as np
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


# ── WebRTC transport (MEDIA_TRANSPORT=webrtc) ────────────────────────────
class FakeStream:
    """Stands in for app.services.webrtc.AvatarStream: records, plays nothing."""

    def __init__(self, socket=None):
        self.connected = True
        self.speaking = False
        self.closed = False
        self.retired = False
        self.enqueued: list[str] = []
        self.flushes: list[int] = []
        self.fail = False
        self.hang = False
        # By default a clip counts as played the moment it is queued. With
        # `hold` set it stays unplayed — a stream nobody is receiving — and
        # is handed back by retire(), as the real one does.
        self.hold = False
        self.unplayed: list = []
        self.on_clip_released = None
        self.drain_waits: list[int] = []
        self._socket = socket

    async def enqueue_clip(self, path, token=None):
        assert Path(path).exists(), "the clip must still be on disk when it is decoded"
        if self.retired:
            raise RuntimeError("stream is closed")
        if self.fail:
            raise RuntimeError("could not decode")
        if self.hang:
            await asyncio.sleep(3600)  # the queue is full and nobody is draining it
        self.enqueued.append(path)
        if self.hold:
            self.unplayed.append(token)
        self.speaking = True
        return 1234

    def play(self, count=1):
        """The stream finished playing its `count` oldest clips."""
        for _ in range(count):
            token = self.unplayed.pop(0)
            if self.on_clip_released:
                self.on_clip_released(token)

    def flush(self):
        # How many messages had gone out when the flush happened.
        self.flushes.append(len(self._socket.sent) if self._socket else -1)
        dropped, self.unplayed = self.unplayed, []
        for token in dropped:
            if self.on_clip_released:
                self.on_clip_released(token)
        was, self.speaking = self.speaking, False
        return was

    def retire(self):
        self.retired, self.connected, self.speaking = True, False, False
        handed_back, self.unplayed = self.unplayed, []
        return handed_back

    async def wait_drained(self):
        self.drain_waits.append(len(self._socket.sent) if self._socket else -1)
        return True

    async def close(self):
        self.retire()
        self.closed = True


def _go_live(rig, stream=None):
    stream = stream or FakeStream(rig.socket)
    stream.on_clip_released = rig.manager._release_clip
    rig.manager._streams["s1"] = stream
    rig.manager._webrtc_live.add("s1")
    return stream


@pytest.fixture
def uploads(rig, monkeypatch):
    """Every storage upload the turn makes."""
    made: list[str] = []

    async def upload(data, key, content_type=None):
        made.append(key)
        return key

    monkeypatch.setattr(wsmod.storage_service, "upload_file", upload)
    return made


CLIP_CHUNK_KEYS = {"type", "chunk_index", "total_chunks", "video_url", "text"}


async def test_without_a_stream_every_message_is_exactly_what_it_was(rig, uploads):
    """The default transport: nothing about a turn may differ."""
    await rig.manager._handle_text_input_inner("s1", "question")

    chunks = [m for m in rig.socket.sent if m["type"] == "video_chunk"]
    assert len(chunks) == len(uploads) == 4
    assert all(set(c) == CLIP_CHUNK_KEYS for c in chunks)
    assert set(rig.socket.types()) == {
        "status",
        "token",
        "message",
        "video_chunk_start",
        "video_chunk",
        "video_chunk_end",
    }


async def test_a_live_stream_takes_the_clips_and_nothing_is_uploaded(rig, uploads):
    stream = _go_live(rig)
    await rig.manager._handle_text_input_inner("s1", "question")

    chunks = [m for m in rig.socket.sent if m["type"] == "video_chunk"]
    assert len(chunks) == len(stream.enqueued) == 4
    assert uploads == [], "a clip that went onto the stream must not also be stored"
    for i, c in enumerate(chunks):
        assert c == {
            "type": "video_chunk",
            "chunk_index": i,
            "total_chunks": -1,
            "transport": "webrtc",
            "text": c["text"],
            "duration_ms": 1234,
        }
    assert " ".join(c["text"] for c in chunks) == rig.reply
    end = [m for m in rig.socket.sent if m["type"] == "video_chunk_end"]
    assert end[0]["sent_chunks"] == 4


async def test_a_stream_that_cannot_take_a_clip_falls_back_without_losing_it(rig, uploads):
    stream = _go_live(rig)
    stream.fail = True
    await rig.manager._handle_text_input_inner("s1", "question")

    chunks = [m for m in rig.socket.sent if m["type"] == "video_chunk"]
    assert len(chunks) == len(uploads) == 4
    assert all(set(c) == CLIP_CHUNK_KEYS for c in chunks)
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2, 3]
    assert " ".join(c["text"] for c in chunks) == rig.reply, "every sentence still reaches the user"


async def test_the_transport_is_chosen_per_clip(rig, uploads):
    """A stream that drops mid-reply costs the rest of the reply nothing."""
    stream = _go_live(rig)
    real = stream.enqueue_clip

    async def drops_after_two(path, token=None):
        if len(stream.enqueued) == 2:
            stream.connected = False
            raise RuntimeError("stream is closed")
        return await real(path, token)

    stream.enqueue_clip = drops_after_two
    await rig.manager._handle_text_input_inner("s1", "question")

    chunks = [m for m in rig.socket.sent if m["type"] == "video_chunk"]
    assert [c.get("transport", "clips") for c in chunks] == ["webrtc", "webrtc", "clips", "clips"]
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2, 3]
    assert len(uploads) == 2
    assert " ".join(c["text"] for c in chunks) == rig.reply


async def test_a_stream_that_has_not_been_announced_is_not_used(rig, uploads):
    """
    The client must hear media_transport=webrtc before it gets a chunk with
    no URL; until then it has nowhere to play one from.
    """
    stream = FakeStream(rig.socket)
    rig.manager._streams["s1"] = stream  # negotiated, but not yet connected
    await rig.manager._handle_text_input_inner("s1", "question")
    assert stream.enqueued == [] and len(uploads) == 4


async def test_an_interrupt_silences_the_stream_before_saying_so(rig):
    stream = _go_live(rig)
    rig.animate_delay = 30
    await rig.manager.handle_text_input("s1", "question")
    await asyncio.sleep(0.1)

    assert await rig.manager.interrupt_active_turn("s1") is True
    interrupted_at = rig.socket.types().index("interrupted")
    assert stream.flushes and stream.flushes[-1] <= interrupted_at, (
        "the flush must come before the client is told"
    )


async def test_a_stop_cuts_off_speech_that_outlived_its_turn(rig):
    """
    The turn's task ends when its last clip is QUEUED; on the stream the
    avatar may be mid-sentence for seconds after. A stop then has no task to
    cancel and must still silence it.
    """
    stream = _go_live(rig)
    rig.reply = "A short answer."
    await rig.manager._handle_text_input_inner("s1", "question")
    assert stream.speaking and "s1" not in rig.manager._active_turns

    assert await rig.manager.interrupt_active_turn("s1") is True
    assert not stream.speaking
    assert rig.socket.types()[-1] == "interrupted"

    # Nothing playing, nothing running: a stop is a no-op, as it always was.
    before = len(rig.socket.sent)
    assert await rig.manager.interrupt_active_turn("s1") is False
    assert len(rig.socket.sent) == before


async def test_a_new_turn_flushes_what_the_previous_one_left_playing(rig):
    stream = _go_live(rig)
    rig.reply = "A short answer."
    await rig.manager._handle_text_input_inner("s1", "first")
    flushes = len(stream.flushes)

    await rig.manager.handle_text_input("s1", "second")
    assert len(stream.flushes) == flushes + 1
    await asyncio.gather(rig.manager._active_turns["s1"], return_exceptions=True)
    history = [x["role"] for x in rig.manager.session_data["s1"]["messages"]]
    assert history == ["user", "assistant"] * 2, "the flush must not disturb the commit order"


async def test_a_turn_cancelled_while_waiting_for_room_on_the_stream_unwinds(rig, tmp_path):
    stream = _go_live(rig)
    stream.hang = True
    task = asyncio.create_task(rig.manager._handle_text_input_inner("s1", "question"))
    await asyncio.sleep(0.2)
    assert not task.done()

    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=3)
    assert done and task.cancelled()
    assert stream.enqueued == []
    assert [p.name for p in (tmp_path / "avatar-session-s1").iterdir()] == []
    assert not [m for m in rig.socket.sent if m["type"] == "video_chunk"]


async def test_a_clip_flushed_while_it_was_being_queued_is_not_sent_the_old_way(rig, uploads):
    """An interrupted sentence must not come back as an MP4."""
    from app.services.webrtc import ClipDropped

    stream = _go_live(rig)

    async def dropped(path, token=None):
        raise ClipDropped()

    stream.enqueue_clip = dropped
    rig.reply = "A short answer."
    await rig.manager._handle_text_input_inner("s1", "question")
    assert uploads == []
    assert not [m for m in rig.socket.sent if m["type"] == "video_chunk"]


async def test_disconnect_closes_the_stream(rig):
    stream = _go_live(rig)
    await rig.manager.disconnect("s1")
    assert stream.closed
    assert "s1" not in rig.manager._streams and "s1" not in rig.manager._webrtc_live


async def test_a_superseding_connection_does_not_inherit_the_old_stream(rig, monkeypatch):
    async def no_load(session_id):
        return None

    monkeypatch.setattr(rig.manager, "_load_session_data", no_load)
    stream = _go_live(rig)
    stream.speaking = True
    new = FakeWS()

    await rig.manager.connect("s1", new, user_id="u1")
    await _settle(rig.manager)
    assert stream.closed and stream.flushes
    assert "s1" not in rig.manager._streams and "s1" not in rig.manager._webrtc_live
    assert new.sent == [], "the old tab's stream ending is no business of the new one"


async def test_a_failed_stream_puts_the_client_back_on_clips(rig):
    stream = _go_live(rig)
    stream.speaking = True
    await rig.manager._on_stream_state("s1", stream, "failed")

    assert rig.socket.sent == [
        {"type": "speaking", "active": False},
        {"type": "media_transport", "transport": "clips"},
    ]
    assert stream.closed and "s1" not in rig.manager._streams
    assert rig.manager._live_stream("s1") is None

    # A state change from a stream that is no longer the session's is ignored.
    await rig.manager._on_stream_state("s1", stream, "connected")
    assert len(rig.socket.sent) == 2


async def test_the_client_closing_its_end_releases_ours(rig):
    stream = _go_live(rig)
    await rig.manager.handle_webrtc_close("s1")
    assert stream.closed
    assert rig.socket.sent == [{"type": "media_transport", "transport": "clips"}]
    await rig.manager.handle_webrtc_close("s1")  # optional and repeatable
    assert len(rig.socket.sent) == 1


async def test_an_offer_is_refused_when_the_transport_is_off(rig, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "clips")
    await rig.manager.handle_webrtc_offer("s1", "v=0\r\n")
    assert rig.socket.types() == ["webrtc_error"]
    assert rig.socket.sent[0]["message"]
    assert "s1" not in rig.manager._streams


@pytest.mark.parametrize("sdp", [None, 42, "", "   ", "v=0\r\n" + "a" * (64 * 1024)])
async def test_a_malformed_offer_is_refused(rig, monkeypatch, sdp):
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    await rig.manager.handle_webrtc_offer("s1", sdp)
    assert rig.socket.types() == ["webrtc_error"]
    assert not rig.manager._background, "nothing may be built for an offer that is refused"


async def test_an_offer_that_cannot_be_honoured_leaves_the_session_on_clips(rig, monkeypatch):
    """
    Here the avatar image is not an image and the SDP is not SDP. Whatever
    aiortc or the decoder makes of them, the receive loop sees no exception
    and the client gets a refusal it can act on.
    """
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    await rig.manager.handle_webrtc_offer("s1", "v=0\r\nthis is not sdp\r\n")
    await _settle(rig.manager)
    assert rig.socket.types() == ["webrtc_error"]
    assert "s1" not in rig.manager._streams

    rig.manager.session_data["s1"]["avatar_image_local"] = None
    await rig.manager.handle_webrtc_offer("s1", "v=0\r\n")
    assert rig.socket.types() == ["webrtc_error", "webrtc_error"]


async def test_offers_are_bounded_per_connection(rig, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    for _ in range(wsmod.MAX_WEBRTC_OFFERS_PER_CONNECTION + 2):
        await rig.manager.handle_webrtc_offer("s1", "v=0\r\n")
    spawned = len(rig.manager._background)
    assert spawned <= wsmod.MAX_WEBRTC_OFFERS_PER_CONNECTION
    # Refused on the spot, before anything was built for them...
    refused = [m["message"] for m in rig.socket.sent]
    assert len(refused) == 2 and all(r.startswith("Too many") for r in refused)
    await _settle(rig.manager)
    # ...and without costing the last offer that WAS within the limit its own
    # reply (here a refusal too: the rig's avatar image is not an image).
    assert rig.socket.types() == ["webrtc_error"] * 3
    assert not rig.socket.sent[-1]["message"].startswith("Too many")


class NegotiatingStream:
    """An AvatarStream whose offer handling waits until the test lets it finish."""

    made: list = []

    def __init__(self):
        self.closed = self.retired = False
        self.connected = self.speaking = False
        self.on_state = self.on_speaking = self.on_clip_released = None
        self.gate = asyncio.Event()
        NegotiatingStream.made.append(self)

    @classmethod
    async def create(cls, image):
        return cls()

    async def handle_offer(self, sdp):
        await self.gate.wait()
        return "answer"

    def flush(self):
        return False

    def retire(self):
        self.retired = True
        return []

    async def close(self):
        self.closed = True


@pytest.fixture
def negotiating(rig, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    monkeypatch.setattr(wsmod, "webrtc_enabled", lambda: True)
    monkeypatch.setattr(wsmod, "AvatarStream", NegotiatingStream)
    NegotiatingStream.made = []
    return NegotiatingStream


async def test_an_offer_over_the_cap_does_not_cost_the_one_in_flight_its_answer(rig, negotiating):
    """
    The refused offer used to bump the offer sequence first, so the fourth
    negotiation — within the limit, still gathering — decided it had been
    superseded: it closed its stream, said nothing, and left it registered.
    """
    m = rig.manager
    for _ in range(wsmod.MAX_WEBRTC_OFFERS_PER_CONNECTION):
        await m.handle_webrtc_offer("s1", "v=0\r\n")
        await asyncio.sleep(0.01)
    fourth = negotiating.made[-1]
    assert m._streams["s1"] is fourth

    await m.handle_webrtc_offer("s1", "v=0\r\n")  # one too many
    assert rig.socket.types() == ["webrtc_error"]
    assert m.session_data["s1"]["_webrtc_offers"] == wsmod.MAX_WEBRTC_OFFERS_PER_CONNECTION
    assert m._streams["s1"] is fourth and not fourth.closed and not fourth.retired

    fourth.gate.set()
    await _settle(m)
    assert rig.socket.sent[-1] == {"type": "webrtc_answer", "sdp": "answer"}
    assert m._streams["s1"] is fourth and not fourth.closed


async def test_a_refused_offer_still_releases_a_stream_the_browser_has_replaced(rig, negotiating):
    """
    A browser that offers again has thrown its old peer connection away. If
    the offer is refused the session must still come off that stream, or
    every sentence goes onto a connection nobody is receiving.
    """
    m = rig.manager
    m.session_data["s1"]["_webrtc_offers"] = wsmod.MAX_WEBRTC_OFFERS_PER_CONNECTION
    live = _go_live(rig)
    await m.handle_webrtc_offer("s1", "v=0\r\n")
    assert live.closed and m._live_stream("s1") is None
    assert rig.socket.sent == [
        {"type": "media_transport", "transport": "clips"},
        {"type": "webrtc_error", "message": "Too many WebRTC offers on this connection."},
    ]
    assert not m._background, "and nothing was built for the refused offer"


async def test_a_refusal_for_a_missing_image_leaves_a_negotiation_alone(rig, negotiating):
    m = rig.manager
    await m.handle_webrtc_offer("s1", "v=0\r\n")
    await asyncio.sleep(0.01)
    first = negotiating.made[-1]
    m.session_data["s1"]["avatar_image_local"] = None
    await m.handle_webrtc_offer("s1", "v=0\r\n")
    assert rig.socket.types() == ["webrtc_error"]

    first.gate.set()
    await _settle(m)
    assert rig.socket.sent[-1] == {"type": "webrtc_answer", "sdp": "answer"}


async def test_a_negotiation_that_was_superseded_does_not_stay_registered(rig, negotiating):
    """Whatever made it stale, a stream it closes is no longer the session's."""
    m = rig.manager
    await m.handle_webrtc_offer("s1", "v=0\r\n")
    await asyncio.sleep(0.01)
    stream = negotiating.made[-1]
    m.session_data["s1"]["_webrtc_offers"] += 1  # stale, by a route that left it registered
    stream.gate.set()
    await _settle(m)
    assert stream.closed and "s1" not in m._streams
    assert rig.socket.sent == []


# ── one order per turn across the two transports ─────────────────────────
def _chunks(rig):
    return [m for m in rig.socket.sent if m["type"] == "video_chunk"]


def _route(chunk):
    if chunk.get("transport") == "webrtc":
        return "webrtc"
    return "resend" if chunk.get("resend") else "url"


def _session_files(tmp_path):
    d = tmp_path / "avatar-session-s1"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


async def test_a_stream_that_comes_up_mid_reply_is_used_from_the_next_turn(rig, monkeypatch):
    """
    Clips sent by URL queue up in the browser; a stream plays the instant it
    is fed. A sentence put on a stream that connected mid-reply would run
    underneath the clips still queued ahead of it and never be heard.
    """
    stream = FakeStream(rig.socket)
    uploads = 0

    async def upload(data, key, content_type=None):
        nonlocal uploads
        uploads += 1
        if uploads == 1:
            _go_live(rig, stream)  # the peer connection comes up during sentence 0
        return key

    monkeypatch.setattr(wsmod.storage_service, "upload_file", upload)
    await rig.manager._handle_text_input_inner("s1", "question")
    assert [_route(c) for c in _chunks(rig)] == ["url"] * 4
    assert stream.enqueued == [], "nothing of this turn may follow a URL clip onto the stream"

    del rig.socket.sent[:]
    await rig.manager._handle_text_input_inner("s1", "another")
    assert [_route(c) for c in _chunks(rig)] == ["webrtc"] * 4, "the next turn does use it"
    assert uploads == 4


async def test_one_clip_the_stream_refuses_keeps_the_rest_of_the_turn_on_urls(rig, uploads):
    """
    webrtc, URL, webrtc would play the third sentence under the second. And
    the URL clip itself must not start until the stream has finished the
    sentence before it, since the browser cannot hold a live stream back.
    """
    stream = _go_live(rig)
    real = stream.enqueue_clip
    drained = asyncio.Event()

    async def refuses_the_second(path, token=None):
        if len(stream.enqueued) == 1:
            raise ValueError("clip is longer than 20s")  # this clip, not the stream
        return await real(path, token)

    async def wait_drained():
        await drained.wait()
        return True

    stream.enqueue_clip = refuses_the_second
    stream.wait_drained = wait_drained
    turn = asyncio.create_task(rig.manager._handle_text_input_inner("s1", "question"))
    await asyncio.sleep(0.1)
    assert [_route(c) for c in _chunks(rig)] == ["webrtc"], "held while the stream is speaking"
    assert len(uploads) == 1, "though already uploaded, so it goes the moment it may"

    drained.set()
    await turn
    assert [_route(c) for c in _chunks(rig)] == ["webrtc", "url", "url", "url"]
    assert [c["chunk_index"] for c in _chunks(rig)] == [0, 1, 2, 3]
    assert len(stream.enqueued) == 1 and stream.connected, "the stream itself is left up"
    assert rig.manager._live_stream("s1") is stream


# ── speech on a stream that goes down is re-sent as clips ────────────────
async def _turn_with_three_on_the_stream(rig, stream):
    """
    Run a four-sentence turn up to the point where three are on the stream
    and the fourth is still being animated. Returns (turn task, release).
    """
    gate = asyncio.Event()
    animated = 0
    real = wsmod.avatar_animator.animate

    async def animate(**kwargs):
        nonlocal animated
        animated += 1
        if animated == 4:
            await gate.wait()
        return await real(**kwargs)

    wsmod.avatar_animator.animate = animate  # the rig's monkeypatch restores it
    turn = asyncio.create_task(rig.manager._handle_text_input_inner("s1", "question"))
    for _ in range(200):
        if len(_chunks(rig)) == 3 and animated == 4:
            break
        await asyncio.sleep(0.01)
    assert [_route(c) for c in _chunks(rig)] == ["webrtc"] * 3
    return turn, gate.set


async def test_unplayed_speech_is_resent_by_url_when_the_stream_goes_down(rig, uploads, tmp_path):
    """
    A, B and C are on the stream. A has played; the stream dies. B and C were
    announced as streamed and never heard, so they come back as ordinary
    clips — same chunk_index, marked as a re-send, in order — and the rest of
    the turn follows them by URL.
    """
    stream = _go_live(rig)
    stream.hold = True
    turn, release = await _turn_with_three_on_the_stream(rig, stream)
    held = lambda: [f for f in _session_files(tmp_path) if f.endswith("_video.mp4")]  # noqa: E731
    assert len(held()) == 3, "each clip on the stream is kept until it has played"
    stream.play(1)
    assert len(held()) == 2, "and not after"

    await rig.manager.handle_webrtc_close("s1")
    release()
    await turn
    await _settle(rig.manager)

    chunks = _chunks(rig)
    assert [(_route(c), c["chunk_index"]) for c in chunks] == [
        ("webrtc", 0),
        ("webrtc", 1),
        ("webrtc", 2),
        ("resend", 1),
        ("resend", 2),
        ("url", 3),
    ]
    for original, again in zip(chunks[1:3], chunks[3:5]):
        assert again == {
            "type": "video_chunk",
            "chunk_index": original["chunk_index"],
            "total_chunks": -1,
            "video_url": again["video_url"],
            "text": original["text"],
            "resend": True,
        }
    assert "resend" not in chunks[5] and set(chunks[5]) == CLIP_CHUNK_KEYS
    assert len(uploads) == 3 and len(set(uploads)) == 3

    types = rig.socket.types()
    assert (
        types.index("media_transport")
        < [i for i, m in enumerate(rig.socket.sent) if m.get("resend")][0]
    ), "the client is told it is on clips before the clips arrive"
    assert _session_files(tmp_path) == [], "nothing is left on disk afterwards"
    assert [m for m in rig.socket.sent if m["type"] == "video_chunk_end"][0]["sent_chunks"] == 4

    # Going down is final: a second close finds nothing to send again.
    before = len(rig.socket.sent)
    await rig.manager.handle_webrtc_close("s1")
    await rig.manager._on_stream_state("s1", stream, "failed")
    await _settle(rig.manager)
    assert len(rig.socket.sent) == before


async def test_resent_clips_arrive_after_the_client_is_told_it_is_on_clips(rig, monkeypatch):
    """
    Storage can be quicker than the socket. The re-send is held until the
    announcements have gone, not merely started after them.
    """
    stream = _go_live(rig)
    stream.hold = True
    rig.reply = "A short answer."
    await rig.manager._handle_text_input_inner("s1", "question")
    del rig.socket.sent[:]

    real_send = rig.socket.send_json

    async def slow_send(msg):
        if msg["type"] == "speaking":
            await asyncio.sleep(0.1)  # long enough for the upload to finish
        await real_send(msg)

    monkeypatch.setattr(rig.socket, "send_json", slow_send)
    await rig.manager.handle_webrtc_close("s1")
    await _settle(rig.manager)
    assert [(m["type"], m.get("resend")) for m in rig.socket.sent] == [
        ("speaking", None),
        ("media_transport", None),
        ("video_chunk", True),
    ]


async def test_a_later_sentence_waits_for_the_resends_ahead_of_it(rig, monkeypatch, tmp_path):
    """Slow storage must not let sentence 3 overtake the recovered 1 and 2."""
    order: list[str] = []
    slow = asyncio.Event()

    async def upload(data, key, content_type=None):
        index = key.rsplit("_c", 1)[1]
        if index != "3.mp4":
            await slow.wait()
        order.append(index)
        return key

    monkeypatch.setattr(wsmod.storage_service, "upload_file", upload)
    stream = _go_live(rig)
    stream.hold = True
    turn, release = await _turn_with_three_on_the_stream(rig, stream)
    stream.play(1)
    await rig.manager.handle_webrtc_close("s1")
    release()
    await asyncio.sleep(0.1)
    assert order == ["3.mp4"], "sentence 3 is uploaded and ready first"
    assert [_route(c) for c in _chunks(rig)] == ["webrtc"] * 3, "and still waits its turn"

    slow.set()
    await turn
    await _settle(rig.manager)
    assert [(_route(c), c["chunk_index"]) for c in _chunks(rig)][3:] == [
        ("resend", 1),
        ("resend", 2),
        ("url", 3),
    ]
    assert _session_files(tmp_path) == []


async def test_a_new_stream_is_not_used_by_a_turn_whose_speech_was_resent(rig, uploads):
    """
    The client's retry offer can connect within the same reply. The re-sent
    clips are queued in the browser by then, so the rule that a turn never
    goes from clips back to the stream covers them too.
    """
    stream = _go_live(rig)
    stream.hold = True
    turn, release = await _turn_with_three_on_the_stream(rig, stream)
    await rig.manager.handle_webrtc_close("s1")
    replacement = _go_live(rig)
    release()
    await turn
    await _settle(rig.manager)
    assert replacement.enqueued == []
    assert [(_route(c), c["chunk_index"]) for c in _chunks(rig)][3:] == [
        ("resend", 0),
        ("resend", 1),
        ("resend", 2),
        ("url", 3),
    ]


async def test_one_resend_that_cannot_be_stored_does_not_lose_the_next(rig, monkeypatch, tmp_path):
    async def upload(data, key, content_type=None):
        if key.endswith("_c0.mp4"):
            raise OSError("bucket unavailable")
        return key

    monkeypatch.setattr(wsmod.storage_service, "upload_file", upload)
    stream = _go_live(rig)
    stream.hold = True
    turn, release = await _turn_with_three_on_the_stream(rig, stream)
    await rig.manager.handle_webrtc_close("s1")
    release()
    await turn
    await _settle(rig.manager)
    assert [(_route(c), c["chunk_index"]) for c in _chunks(rig)][3:] == [
        ("resend", 1),
        ("resend", 2),
        ("url", 3),
    ]
    assert _session_files(tmp_path) == []


async def test_an_interrupt_does_not_bring_flushed_speech_back(rig, uploads, tmp_path):
    """Flushed clips were interrupted by the user: deleted, never re-sent."""
    stream = _go_live(rig)
    stream.hold = True
    rig.manager._spawn_turn("s1", rig.manager._handle_text_input_inner("s1", "question"))
    for _ in range(200):
        if len(_chunks(rig)) == 4:
            break
        await asyncio.sleep(0.01)
    assert len(_session_files(tmp_path)) == 4

    assert await rig.manager.interrupt_active_turn("s1") is True
    assert _session_files(tmp_path) == [], "the flushed clips' files are deleted"
    await rig.manager.handle_webrtc_close("s1")
    await _settle(rig.manager)
    assert uploads == [] and not [c for c in _chunks(rig) if c.get("resend")]


async def test_an_interrupt_cancels_a_resend_in_progress(rig, monkeypatch, tmp_path):
    """
    The stream died and its speech is on its way back as clips when the user
    barges in. Those clips are part of what was interrupted.
    """
    never = asyncio.Event()

    async def upload(data, key, content_type=None):
        await never.wait()

    monkeypatch.setattr(wsmod.storage_service, "upload_file", upload)
    stream = _go_live(rig)
    stream.hold = True
    rig.reply = "A short answer."
    await rig.manager._handle_text_input_inner("s1", "question")
    await rig.manager.handle_webrtc_close("s1")
    await asyncio.sleep(0.05)
    assert "s1" in rig.manager._resends and len(_session_files(tmp_path)) == 1

    # No turn is running, and still the user is told speech was cut off.
    assert await rig.manager.interrupt_active_turn("s1") is True
    assert rig.socket.types()[-1] == "interrupted"
    await _settle(rig.manager)
    assert "s1" not in rig.manager._resends
    assert _session_files(tmp_path) == []
    assert not [c for c in _chunks(rig) if c.get("resend")]


async def test_a_session_that_ends_takes_its_held_clips_with_it(rig, uploads, tmp_path):
    stream = _go_live(rig)
    stream.hold = True
    rig.reply = "A short answer."
    await rig.manager._handle_text_input_inner("s1", "question")
    assert len(_session_files(tmp_path)) == 1

    await rig.manager.disconnect("s1")
    await _settle(rig.manager)
    assert stream.closed and uploads == []
    assert _session_files(tmp_path) == []


async def test_a_superseded_connection_gets_no_resend_and_leaves_no_files(
    rig, uploads, monkeypatch, tmp_path
):
    async def no_load(session_id):
        return None

    monkeypatch.setattr(rig.manager, "_load_session_data", no_load)
    stream = _go_live(rig)
    stream.hold = True
    rig.reply = "A short answer."
    await rig.manager._handle_text_input_inner("s1", "question")
    # Take the flush out of the picture, so that what removes the file here
    # is the stream being retired — the path a clip takes if it is queued
    # after the flush.
    stream.flush = lambda: False

    new = FakeWS()
    await rig.manager.connect("s1", new, user_id="u1")
    await _settle(rig.manager)
    assert stream.closed and uploads == [] and new.sent == []
    assert _session_files(tmp_path) == []


async def test_a_resend_under_way_is_dropped_when_the_connection_is_replaced(
    rig, monkeypatch, tmp_path
):
    """The new tab never saw those sentences announced; it must not get them."""
    hold = asyncio.Event()

    async def upload(data, key, content_type=None):
        await hold.wait()
        return key

    async def no_load(session_id):
        return None

    monkeypatch.setattr(wsmod.storage_service, "upload_file", upload)
    monkeypatch.setattr(rig.manager, "_load_session_data", no_load)
    stream = _go_live(rig)
    stream.hold = True
    rig.reply = "A short answer."
    await rig.manager._handle_text_input_inner("s1", "question")
    await rig.manager.handle_webrtc_close("s1")
    await asyncio.sleep(0.05)

    new = FakeWS()
    await rig.manager.connect("s1", new, user_id="u1")
    hold.set()
    await _settle(rig.manager)
    assert new.sent == [] and not [c for c in _chunks(rig) if c.get("resend")]
    assert _session_files(tmp_path) == []


async def test_shutdown_deletes_held_clips(rig, uploads, tmp_path):
    stream = _go_live(rig)
    stream.hold = True
    rig.reply = "A short answer."
    await rig.manager._handle_text_input_inner("s1", "question")
    await rig.manager.close_all_streams()
    assert stream.closed and uploads == []
    assert _session_files(tmp_path) == []


async def test_a_stream_found_dead_at_enqueue_is_taken_down_there_and_then(rig, uploads):
    """
    The clip that discovers the failure is sent behind the ones the stream
    still held, not ahead of them: the pipeline takes the stream down itself
    instead of waiting for the state callback to be scheduled.
    """
    stream = _go_live(rig)
    stream.hold = True
    real = stream.enqueue_clip

    async def dies_on_the_third(path, token=None):
        if len(stream.enqueued) == 2:
            stream.connected = False
            raise RuntimeError("stream is no longer being sent")
        return await real(path, token)

    stream.enqueue_clip = dies_on_the_third
    await rig.manager._handle_text_input_inner("s1", "question")
    await _settle(rig.manager)
    assert [(_route(c), c["chunk_index"]) for c in _chunks(rig)] == [
        ("webrtc", 0),
        ("webrtc", 1),
        ("resend", 0),
        ("resend", 1),
        ("url", 2),
        ("url", 3),
    ]
    assert stream.closed and "s1" not in rig.manager._streams
    assert {"type": "media_transport", "transport": "clips"} in rig.socket.sent


async def test_a_turn_cancelled_while_taking_its_stream_down_still_closes_it(rig, monkeypatch):
    """
    A turn can be the one that finds its stream dead, and the user can press
    stop while it is telling the client. The teardown used to run inline in
    the turn, so the cancellation skipped the rest of it: the peer connection
    stayed open and the client was never told it was back on clips.
    """
    stream = _go_live(rig)
    real_send = rig.socket.send_json
    in_send = asyncio.Event()

    async def slow_send(msg):
        if msg["type"] == "media_transport":
            in_send.set()
            await asyncio.sleep(0.2)
        await real_send(msg)

    monkeypatch.setattr(rig.socket, "send_json", slow_send)
    caller = asyncio.create_task(rig.manager._stream_down("s1", stream))
    await asyncio.wait_for(in_send.wait(), 2)
    caller.cancel()
    await asyncio.wait({caller}, timeout=2)
    await _settle(rig.manager)

    assert stream.closed, "the stream must be closed even though its caller was cancelled"
    assert {"type": "media_transport", "transport": "clips"} in rig.socket.sent


async def test_a_stream_that_does_not_drain_is_taken_down_before_the_next_url_clip(rig, uploads):
    """
    Sentences 0-1 are stuck on a stream that has stopped playing; sentence 2
    cannot go onto it and waits for it to drain. When that wait gives up the
    stream is finished: its clips must be recovered FIRST, or sentence 2 goes
    out ahead of them and the reply is heard out of order.
    """
    stream = _go_live(rig)
    stream.hold = True  # nothing queued is ever played
    real = stream.enqueue_clip

    async def refuses_the_third(path, token=None):
        if len(stream.enqueued) == 2:
            raise ValueError("clip is longer than 20s")
        return await real(path, token)

    async def never_drains():
        return False

    stream.enqueue_clip = refuses_the_third
    stream.wait_drained = never_drains
    await rig.manager._handle_text_input_inner("s1", "question")
    await _settle(rig.manager)

    url_chunks = [c for c in _chunks(rig) if c.get("video_url")]
    assert [c["chunk_index"] for c in url_chunks] == [0, 1, 2, 3], url_chunks
    assert [bool(c.get("resend")) for c in url_chunks] == [True, True, False, False]
    assert stream.closed


# ── the microphone over the stream (server-side turn-taking) ─────────────
#
# The detector and the track are stand-ins: what is measured is what the
# manager does with "the user started speaking" and "that was an utterance".
# The real models, and real audio over a real peer connection, are in
# test_turn_taking.py.


class FakeDetector:
    """A TurnDetector whose events are fired by the test."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.on_speech_start = self.on_speech_paused = self.on_speech_resumed = None
        self.on_speech_progress = self.on_speech_end = None
        self.in_utterance = False
        self.max_secs: list[float] = []
        self.fed = 0
        self.resets = 0
        self.closed = False
        self.avatar_speaking: list[bool] = []
        # Called with the detector on every feed, from the reader's task.
        self.on_feed = None

    async def feed(self, pcm):
        self.fed += 1
        if self.on_feed is not None:
            await self.on_feed(self)

    def reset(self):
        self.resets += 1

    def set_avatar_speaking(self, active):
        self.avatar_speaking.append(active)

    def set_max_utterance_secs(self, secs):
        self.max_secs.append(secs)

    async def close(self):
        self.closed = True


class MicTrack:
    """A remote audio track that never runs dry, until its stream is closed."""

    kind = "audio"

    def __init__(self, stream):
        self._stream = stream
        self.taken = 0

    async def recv(self):
        import av
        from aiortc.mediastreams import MediaStreamError

        await asyncio.sleep(0.001)
        if self._stream.closed:
            raise MediaStreamError
        self.taken += 1
        frame = av.AudioFrame.from_ndarray(
            np.zeros((1, 1920), np.int16), format="s16", layout="stereo"
        )
        frame.sample_rate = 48000
        return frame


UTTERANCE = np.zeros(16000, np.int16)
STARTED = {"type": "user_speech", "state": "started"}
STOPPED_TURN = {"type": "user_speech", "state": "stopped", "reason": "turn"}
STOPPED_IGNORED = {"type": "user_speech", "state": "stopped", "reason": "ignored"}
LISTENING = {"type": "mic_state", "listening": True}
NOT_LISTENING = {"type": "mic_state", "listening": False}


@pytest.fixture
async def mic(rig, monkeypatch):
    """The rig with a live stream that carries a microphone track."""
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    monkeypatch.setattr(settings, "VOICE_TURN_DETECTION", "smart")
    monkeypatch.setattr(settings, "STT_PARTIALS", "off")
    monkeypatch.setattr(wsmod, "mic_enabled", lambda: True)
    m = rig.manager

    class Mic:
        detectors: list[FakeDetector] = []
        transcript = "what the user said"
        stt_delay = 0.0
        stt_calls: list = []
        stt_running = 0
        stt_peak = 0
        stream = None
        track = None

        @property
        def detector(self) -> FakeDetector:
            return self.detectors[-1]

        def attach(self, stream):
            """`stream`'s offer carried a microphone."""
            self.stream = stream
            self.track = MicTrack(stream)
            m._on_remote_audio("s1", stream, self.track)

        async def on(self):
            await m.handle_mic("s1", True)
            return self.detector

        async def turn(self):
            task = m._active_turns.get("s1")
            if task:
                await asyncio.gather(task, return_exceptions=True)
            await _settle(m)

    x = Mic()
    x.detectors, x.stt_calls = [], []

    async def create(mode=None, **kwargs):
        x.detectors.append(FakeDetector(**kwargs))
        return x.detectors[-1]

    async def transcribe(audio, language="en"):
        n = len(x.stt_calls)
        x.stt_calls.append((audio, language))
        x.stt_running += 1
        x.stt_peak = max(x.stt_peak, x.stt_running)
        try:
            await asyncio.sleep(x.stt_delay)
        finally:
            x.stt_running -= 1
        return x.transcript(n) if callable(x.transcript) else x.transcript

    monkeypatch.setattr(wsmod.TurnDetector, "create", staticmethod(create))
    monkeypatch.setattr(wsmod.stt_service, "transcribe", transcribe)
    x.attach(_go_live(rig))
    yield x
    await m.disconnect("s1")
    await _settle(m)


def _of(rig, kind):
    return [x for x in rig.socket.sent if x["type"] == kind]


async def test_the_microphone_is_heard_only_between_active_true_and_false(rig, mic):
    m = rig.manager
    await asyncio.sleep(0.03)
    # The track is being read — nothing may pile up in aiortc — but no
    # detector exists and nothing has been told to the client.
    assert mic.track.taken > 5
    assert mic.detectors == [] and not _of(rig, "mic_state")

    detector = await mic.on()
    assert _of(rig, "mic_state") == [LISTENING]
    await asyncio.sleep(0.03)
    assert detector.fed > 5

    await m.handle_mic("s1", False)
    assert _of(rig, "mic_state") == [LISTENING, NOT_LISTENING]
    assert detector.resets == 1, "the utterance in progress is discarded"
    fed, taken = detector.fed, mic.track.taken
    await asyncio.sleep(0.03)
    assert detector.fed == fed, "no audio is looked at after active:false"
    assert mic.track.taken > taken, "though the track is still drained"
    await _settle(m)
    assert detector.closed

    # Events from a detector that was mid-frame when it was switched off
    # go nowhere: no message, no transcription, no turn.
    sent = len(rig.socket.sent)
    await detector.on_speech_start()
    await detector.on_speech_paused(UTTERANCE)
    await detector.on_speech_end(UTTERANCE)
    await mic.turn()
    assert len(rig.socket.sent) == sent and mic.stt_calls == [] and rig.llm_calls == []

    # Switched on again: a fresh detector, not the discarded one.
    again = await mic.on()
    assert again is not detector and _of(rig, "mic_state")[-1] == LISTENING


async def test_an_utterance_from_the_stream_microphone_is_a_normal_turn(rig, mic):
    m = rig.manager
    m.session_data["s1"]["language"] = "de"
    rig.reply = "A reply."
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_end(UTTERANCE)
    await mic.turn()

    assert mic.stt_calls == [(UTTERANCE, "de")], "the samples, in the session's language"
    sent = rig.socket.sent
    # Up to the transcript, exactly this and in this order...
    assert sent[:5] == [
        LISTENING,
        STARTED,
        STOPPED_TURN,
        {"type": "status", "message": "Transcribing audio…", "stage": "transcription"},
        # ...the very message a recorded audio turn sends: no `final` key.
        {"type": "transcription", "text": "what the user said"},
    ]
    # ...and from there the turn every other input gets.
    rest = [x["type"] for x in sent[5:]]
    assert rest[0] == "status" and "token" in rest and "message" in rest
    assert rest[-1] == "video_chunk_end" and "video_chunk" in rest
    assert m.session_data["s1"]["messages"] == [
        {"role": "user", "content": "what the user said"},
        {"role": "assistant", "content": "A reply."},
    ]
    assert rig.persisted == [("user", "what the user said"), ("assistant", "A reply.")]


async def test_speech_with_no_words_in_it_is_ignored_not_answered(rig, mic):
    mic.transcript = "  "
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_end(UTTERANCE)
    await mic.turn()
    assert _of(rig, "user_speech") == [STARTED, STOPPED_TURN, STOPPED_IGNORED]
    assert not _of(rig, "transcription") and not _of(rig, "error")
    assert rig.llm_calls == [] and rig.manager.session_data["s1"]["messages"] == []


async def test_the_turn_rate_limit_covers_the_stream_microphone(rig, mic, monkeypatch):
    monkeypatch.setattr(settings, "WS_TURNS_PER_MINUTE", 1)
    rig.reply = "Ok."
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_end(UTTERANCE)
    await mic.turn()
    assert len(rig.llm_calls) == 1 and len(mic.stt_calls) == 1

    await detector.on_speech_start()
    await detector.on_speech_progress(UTTERANCE)
    await detector.on_speech_paused(UTTERANCE)
    await detector.on_speech_end(UTTERANCE)
    await mic.turn()
    # Refused before any of it reached Whisper — not even speculatively.
    assert len(mic.stt_calls) == 1 and len(rig.llm_calls) == 1
    assert [x for x in rig.socket.sent if x.get("code") == "rate_limited"]
    assert _of(rig, "user_speech")[-2:] == [STARTED, STOPPED_IGNORED]
    assert rig.socket.sent[-1] == STOPPED_IGNORED


async def test_speaking_over_a_reply_interrupts_it_and_keeps_history_in_order(rig, mic):
    """
    Barge-in is the server's in this mode. The interrupted reply is committed
    before the new question — the ordering interrupt_active_turn exists for.
    """
    m = rig.manager
    rig.reply = "Here is a long answer that keeps going. " * 6
    rig.token_delay = 0.01
    rig.animate_delay = 30
    detector = await mic.on()
    await m.handle_text_input("s1", "first question")
    await asyncio.sleep(0.2)  # mid-reply
    first_turn = m._active_turns["s1"]
    mic.stream.speaking = True  # and the avatar is talking

    await detector.on_speech_start()
    assert first_turn.cancelling() or first_turn.done()
    kinds = [x["type"] for x in rig.socket.sent]
    assert kinds.index("user_speech") < kinds.index("interrupted")
    # Flushed before the client was told, as for a client's own `interrupt`.
    assert mic.stream.flushes and mic.stream.flushes[-1] <= kinds.index("interrupted")
    assert m.session_data["s1"]["messages"][-1]["role"] == "assistant", "committed at once"

    rig.reply = "Second answer."
    rig.token_delay = rig.animate_delay = 0
    mic.transcript = "second question"
    await detector.on_speech_end(UTTERANCE)
    await mic.turn()

    history = m.session_data["s1"]["messages"]
    assert [x["role"] for x in history] == ["user", "assistant", "user", "assistant"], history
    assert history[1]["content"].startswith("Here is a long answer")
    assert history[2]["content"] == "second question"
    assert rig.llm_calls[-1][-1] == {"role": "user", "content": "second question"}
    assert [r for r, _ in rig.persisted] == ["user", "assistant", "user", "assistant"]
    assert len(_of(rig, "interrupted")) == 1


async def test_speech_while_the_avatar_talks_after_its_turn_finished_still_cuts_it_off(rig, mic):
    m = rig.manager
    detector = await mic.on()
    mic.stream.speaking = True  # speech queued by a turn whose task is long done
    assert "s1" not in m._active_turns
    await detector.on_speech_start()
    assert mic.stream.flushes, "the stream is what holds the speech"
    assert _of(rig, "interrupted")


async def test_speech_with_nothing_to_interrupt_interrupts_nothing(rig, mic):
    detector = await mic.on()
    await detector.on_speech_start()
    assert not _of(rig, "interrupted")


async def test_the_detector_follows_the_avatars_speech_on_the_stream(rig, mic):
    m = rig.manager
    mic.stream.speaking = True
    detector = await mic.on()
    assert detector.avatar_speaking == [True], "it may already be mid-sentence"
    await m._on_stream_speaking("s1", mic.stream, False)
    await m._on_stream_speaking("s1", mic.stream, True)
    assert detector.avatar_speaking == [True, False, True]
    assert [x["active"] for x in _of(rig, "speaking")] == [False, True]


async def test_the_final_transcription_starts_at_the_pause_and_is_not_repeated(rig, mic):
    rig.reply = "Ok."
    mic.stt_delay = 0.05
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_paused(UTTERANCE)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(mic.stt_calls) == 1, "under way before the turn model has answered"
    assert not _of(rig, "transcription"), "but nothing is shown until the turn is real"
    await detector.on_speech_end(UTTERANCE)
    await mic.turn()
    assert len(mic.stt_calls) == 1, "the turn used it rather than transcribing again"
    assert _of(rig, "transcription") == [{"type": "transcription", "text": "what the user said"}]
    assert len(rig.llm_calls) == 1


async def test_a_speculative_transcript_is_discarded_when_the_speaker_carries_on(rig, mic):
    rig.reply = "Ok."
    mic.stt_delay = 0.05
    mic.transcript = lambda n: ["tell me about", "tell me about the weather"][n]
    detector = await mic.on()
    longer = np.zeros(32000, np.int16)
    await detector.on_speech_start()
    await detector.on_speech_paused(UTTERANCE)
    await asyncio.sleep(0.01)
    detector.on_speech_resumed()
    await detector.on_speech_paused(longer)
    await detector.on_speech_end(longer)
    await mic.turn()

    assert [len(a) for a, _ in mic.stt_calls] == [16000, 32000]
    # The first call could not be stopped; the second waited for it.
    assert mic.stt_peak == 1, "never two transcriptions of one session at once"
    assert _of(rig, "transcription") == [
        {"type": "transcription", "text": "tell me about the weather"}
    ]
    assert rig.llm_calls[-1][-1]["content"] == "tell me about the weather"
    assert len(rig.llm_calls) == 1, "the half-sentence never became a turn"


async def test_a_failed_speculative_transcript_is_retried_for_the_turn(rig, mic):
    rig.reply = "Ok."

    def transcript(n):
        if n == 0:
            raise RuntimeError("cuda out of memory")
        return "second time lucky"

    mic.transcript = transcript
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_paused(UTTERANCE)
    await asyncio.sleep(0.01)
    await detector.on_speech_end(UTTERANCE)
    await mic.turn()
    assert _of(rig, "transcription") == [{"type": "transcription", "text": "second time lucky"}]


async def test_running_transcripts_are_sent_while_speaking_and_start_nothing(rig, mic, monkeypatch):
    monkeypatch.setattr(settings, "STT_PARTIALS", "on")
    monkeypatch.setattr(settings, "STT_PARTIAL_INTERVAL_SECS", 1.25)
    rig.reply = "Ok."
    mic.transcript = lambda n: f"words so far {n}"
    detector = await mic.on()
    assert detector.kwargs == {"progress_interval_secs": 1.25}

    await detector.on_speech_start()
    await detector.on_speech_progress(UTTERANCE)
    await _settle(rig.manager)
    await detector.on_speech_progress(UTTERANCE)
    await _settle(rig.manager)
    assert _of(rig, "transcription") == [
        {"type": "transcription", "text": "words so far 0", "final": False},
        {"type": "transcription", "text": "words so far 1", "final": False},
    ]
    assert rig.llm_calls == [] and "s1" not in rig.manager._active_turns
    assert rig.manager.session_data["s1"]["messages"] == [], "a partial is not a message"

    await detector.on_speech_end(UTTERANCE)
    await mic.turn()
    assert _of(rig, "transcription")[-1] == {"type": "transcription", "text": "words so far 2"}
    assert len(rig.llm_calls) == 1


async def test_a_running_transcript_never_arrives_after_the_final_one(rig, mic, monkeypatch):
    """
    A partial still inside Whisper when the turn ends: it cannot be stopped,
    the final waits its turn behind it, and its text is dropped on arrival.
    """
    monkeypatch.setattr(settings, "STT_PARTIALS", "on")
    rig.reply = "Ok."
    mic.stt_delay = 0.05
    mic.transcript = lambda n: ["a stale partial", "the final words"][n]
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_progress(UTTERANCE)
    await asyncio.sleep(0.01)  # the partial is in flight
    assert mic.stt_running == 1
    await detector.on_speech_progress(UTTERANCE)  # a second does not queue behind it
    await detector.on_speech_end(UTTERANCE)
    await mic.turn()

    assert len(mic.stt_calls) == 2 and mic.stt_peak == 1
    transcripts = _of(rig, "transcription")
    assert transcripts == [{"type": "transcription", "text": "the final words"}], transcripts
    assert len(rig.llm_calls) == 1


async def test_running_transcripts_default_to_gpu_only(rig, mic, monkeypatch):
    monkeypatch.setattr(settings, "STT_PARTIALS", "auto")
    monkeypatch.setattr(type(wsmod.stt_service), "on_gpu", property(lambda self: False))
    detector = await mic.on()
    assert detector.kwargs == {"progress_interval_secs": 0.0}
    await rig.manager.handle_mic("s1", False)
    monkeypatch.setattr(type(wsmod.stt_service), "on_gpu", property(lambda self: True))
    detector = await mic.on()
    assert detector.kwargs == {"progress_interval_secs": settings.STT_PARTIAL_INTERVAL_SECS}


async def test_a_partial_does_not_take_a_slot_another_sessions_final_may_need(
    rig, mic, monkeypatch
):
    monkeypatch.setattr(settings, "STT_PARTIALS", "on")
    monkeypatch.setattr(type(wsmod.stt_service), "busy", property(lambda self: True))
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_progress(UTTERANCE)
    await _settle(rig.manager)
    assert mic.stt_calls == []


async def test_the_listener_goes_when_the_stream_goes_down(rig, mic):
    m = rig.manager
    detector = await mic.on()
    reader_task = m._mics["s1"].reader_task
    await m._stream_down("s1", mic.stream)
    await _settle(m)
    await asyncio.gather(reader_task, return_exceptions=True)

    assert "s1" not in m._mics and reader_task.done()
    assert detector.resets == 1 and detector.closed
    # Unrequested, and before the client hears it is back on clips: its cue
    # to take hands-free back into the browser.
    kinds = [(x["type"], x.get("listening", x.get("transport"))) for x in rig.socket.sent]
    assert kinds.index(("mic_state", False)) < kinds.index(("media_transport", "clips"))
    sent = len(rig.socket.sent)
    await detector.on_speech_start()
    await detector.on_speech_end(UTTERANCE)
    assert len(rig.socket.sent) == sent and mic.stt_calls == []

    # Asking again without a stream is refused; the client keeps its own detector.
    await m.handle_mic("s1", True)
    assert rig.socket.sent[-1] == NOT_LISTENING and len(mic.detectors) == 1


async def test_a_stream_that_was_never_listened_to_goes_down_without_a_mic_state(rig, mic):
    await rig.manager._stream_down("s1", mic.stream)
    await _settle(rig.manager)
    assert not _of(rig, "mic_state") and "s1" not in rig.manager._mics


async def test_the_listener_goes_on_disconnect_and_on_shutdown(rig, mic):
    m = rig.manager
    detector = await mic.on()
    reader_task = m._mics["s1"].reader_task
    await m.disconnect("s1")
    await _settle(m)
    await asyncio.gather(reader_task, return_exceptions=True)
    assert "s1" not in m._mics and reader_task.done() and detector.closed

    m.active_connections["s2"] = rig.socket
    m.session_data["s2"] = {"messages": [], "user_id": "u1"}
    stream = FakeStream(rig.socket)
    m._streams["s2"] = stream
    m._webrtc_live.add("s2")
    m._on_remote_audio("s2", stream, MicTrack(stream))
    await m.handle_mic("s2", True)
    task = m._mics["s2"].reader_task
    await m.close_all_streams()
    await _settle(m)
    await asyncio.gather(task, return_exceptions=True)
    assert m._mics == {} and task.done() and mic.detector.closed
    m.active_connections.pop("s2")
    m.session_data.pop("s2")


async def test_a_superseding_connection_does_not_inherit_the_microphone(rig, mic, monkeypatch):
    m = rig.manager
    detector = await mic.on()

    async def no_load(session_id):
        return None

    monkeypatch.setattr(m, "_load_session_data", no_load)
    newer = FakeWS()
    await m.connect("s1", newer, user_id="u1")
    await _settle(m)
    assert "s1" not in m._mics and detector.closed
    assert not [x for x in newer.sent if x["type"] == "mic_state"]
    await detector.on_speech_start()
    assert newer.sent == [], "the old tab's voice does not reach the new one"


async def test_asking_before_the_stream_is_live_is_answered_when_it_is(rig, mic):
    m = rig.manager
    await m.disconnect("s1")  # start again without a live stream
    m.active_connections["s1"] = rig.socket
    m._send_locks["s1"] = asyncio.Lock()
    m.session_data["s1"] = {"messages": [], "user_id": "u1", "language": "en"}
    rig.socket.sent.clear()

    stream = FakeStream(rig.socket)
    m._streams["s1"] = stream  # negotiated, media path still coming up
    mic.attach(stream)
    await m.handle_mic("s1", True)
    assert not _of(rig, "mic_state"), "neither refused nor claimed yet"
    assert len(mic.detectors) == 0

    await m._on_stream_state("s1", stream, "connected")
    assert [x["type"] for x in rig.socket.sent] == ["media_transport", "mic_state"]
    assert rig.socket.sent[-1] == LISTENING

    # Or the stream never makes it: then the answer is no.
    other = FakeStream(rig.socket)
    await m._stream_down("s1", stream)
    m._streams["s1"] = other
    mic.attach(other)
    rig.socket.sent.clear()
    await m.handle_mic("s1", True)
    await m._stream_down("s1", other)
    await _settle(m)
    assert _of(rig, "mic_state") == [NOT_LISTENING]


async def test_a_stream_whose_offer_carried_no_microphone_cannot_be_listened_to(rig, monkeypatch):
    monkeypatch.setattr(wsmod, "mic_enabled", lambda: True)
    _go_live(rig)  # audio was recvonly: no remote track was ever reported
    await rig.manager.handle_mic("s1", True)
    assert rig.socket.sent == [NOT_LISTENING]
    assert rig.manager._mics["s1"].wanted is False


async def test_mic_messages_are_refused_where_the_server_does_not_take_turns(rig, monkeypatch):
    m = rig.manager
    monkeypatch.setattr(wsmod, "mic_enabled", lambda: False)
    stream = _go_live(rig)
    # Even if a track somehow arrived, nothing is built for it.
    await m.handle_mic("s1", True)
    await m.handle_mic("s1", False)
    await m.handle_mic("s1", "yes")  # only a real `true` switches it on
    assert rig.socket.sent == [NOT_LISTENING] * 3
    assert m._mics == {} and stream.flushes == []


async def test_a_send_that_fails_from_the_readers_own_task_still_tears_everything_down(rig, mic):
    """
    The detector's events run on the reader task. One of them can be what
    discovers the socket is dead — and the teardown that follows must not be
    cut short by cancelling the task it is running on.
    """
    m = rig.manager
    stream = mic.stream

    async def dead(msg):
        if msg.get("type") == "user_speech":
            raise RuntimeError("socket closed")
        rig.socket.sent.append(msg)

    rig.socket.send_json = dead
    fired = asyncio.Event()

    async def speak(detector):
        if not fired.is_set():
            fired.set()
            await detector.on_speech_start()

    detector = await mic.on()
    reader_task = m._mics["s1"].reader_task
    detector.on_feed = speak
    await asyncio.wait_for(asyncio.gather(reader_task, return_exceptions=True), 2)
    await _settle(m)
    assert "s1" not in m.session_data and "s1" not in m._mics
    assert stream.closed, "the peer connection was released"
    assert rig.socket.closed_with == 1000


async def test_every_mic_frame_gets_exactly_one_mic_state_in_order(rig, mic):
    """
    The client matches acknowledgements to the frames it sent by counting.
    One that had to wait for the stream, and one overtaken while it waited,
    are each still owed their own.
    """
    m = rig.manager
    await m.disconnect("s1")
    m.active_connections["s1"] = rig.socket
    m._send_locks["s1"] = asyncio.Lock()
    m.session_data["s1"] = {"messages": [], "user_id": "u1", "language": "en"}
    rig.socket.sent.clear()
    stream = FakeStream(rig.socket)
    m._streams["s1"] = stream  # not live yet
    mic.attach(stream)

    await m.handle_mic("s1", True)  # waits for the stream
    await m.handle_mic("s1", False)  # overtakes it
    assert rig.socket.sent == [NOT_LISTENING, NOT_LISTENING]

    await m.handle_mic("s1", True)
    await m.handle_mic("s1", True)  # asked twice while waiting
    assert len(rig.socket.sent) == 2
    await m._on_stream_state("s1", stream, "connected")
    assert rig.socket.sent[2:] == [
        {"type": "media_transport", "transport": "webrtc"},
        LISTENING,
        LISTENING,
    ]
    assert len(mic.detectors) == 1

    await m.handle_mic("s1", True)  # already listening: confirmed, nothing rebuilt
    assert rig.socket.sent[-1] == LISTENING and len(mic.detectors) == 1
    await m.handle_mic("s1", False)
    await m.handle_mic("s1", False)
    assert rig.socket.sent[-2:] == [NOT_LISTENING, NOT_LISTENING]
    assert len(_of(rig, "mic_state")) == 7, "seven frames, seven answers"


# ── defects found in review of the stream microphone ─────────────────────


def _hold(rig, match):
    """
    Park the first send that matches, as a slow socket would, until released.
    Returns (parked, release). The stock FakeWS never yields inside a send,
    so nothing can happen "during" one; a real socket does.
    """
    parked, release = asyncio.Event(), asyncio.Event()
    plain = rig.socket.send_json
    held = []

    async def send_json(msg):
        if not held and match(msg):
            held.append(msg)
            parked.set()
            await release.wait()
        await plain(msg)

    rig.socket.send_json = send_json
    return parked, release


FIRST = np.full(16000, 1, np.int16)
SECOND = np.full(16000, 2, np.int16)


def _both_or_one(mic):
    """A transcriber that says which of FIRST and SECOND it was given."""

    def transcript(n):
        audio = mic.stt_calls[n][0]
        words = [w for w, mark in (("first", 1), ("second", 2)) if mark in audio]
        return " ".join(words)

    return transcript


async def test_speech_that_resumes_before_the_transcript_is_one_turn_with_all_the_words(
    rig, mic, monkeypatch
):
    """
    Smart Turn calls a finished-sounding sentence complete at once; the next
    sentence starts before the first has been transcribed. That used to be a
    barge-in on the first sentence's own turn: its words were dropped and
    only the continuation was answered.
    """
    # One turn's worth of budget: the continuation is not charged again.
    monkeypatch.setattr(settings, "WS_TURNS_PER_MINUTE", 1)
    m = rig.manager
    rig.reply = "Answer."
    mic.stt_delay = 0.2
    mic.transcript = _both_or_one(mic)
    detector = await mic.on()

    await detector.on_speech_start()
    await detector.on_speech_paused(FIRST)
    await detector.on_speech_end(FIRST)
    await asyncio.sleep(0.05)  # inside Whisper
    first_turn = m._active_turns["s1"]
    await detector.on_speech_start()
    assert first_turn.cancelling() or first_turn.done(), "its work is discarded"
    await detector.on_speech_paused(SECOND)
    await detector.on_speech_end(SECOND)
    await mic.turn()

    whole = mic.stt_calls[-1][0]
    assert len(whole) == 32000 and np.array_equal(whole, np.concatenate([FIRST, SECOND]))
    assert mic.stt_peak == 1
    assert _of(rig, "user_speech") == [STARTED, STOPPED_TURN, STARTED, STOPPED_TURN]
    assert _of(rig, "transcription") == [{"type": "transcription", "text": "first second"}]
    # No reply was cut, so none is announced as cut; and nothing was refused.
    assert not _of(rig, "interrupted") and not _of(rig, "error")
    assert m.session_data["s1"]["messages"] == [
        {"role": "user", "content": "first second"},
        {"role": "assistant", "content": "Answer."},
    ]
    assert len(rig.llm_calls) == 1 and rig.llm_calls[0][-1]["content"] == "first second"
    assert rig.persisted == [("user", "first second"), ("assistant", "Answer.")]
    mic_state = m._mics["s1"]
    assert mic_state.carry is None and mic_state.turn is None and mic_state.turn_audio is None


async def test_a_third_sentence_joins_the_first_two(rig, mic):
    rig.reply = "Answer."
    mic.stt_delay = 0.2
    mic.transcript = lambda n: f"{len(mic.stt_calls[n][0]) // 16000} sentences"
    detector = await mic.on()
    for _ in range(3):
        await detector.on_speech_start()
        await detector.on_speech_end(FIRST)
        await asyncio.sleep(0.02)
    await mic.turn()
    assert _of(rig, "transcription") == [{"type": "transcription", "text": "3 sentences"}]
    assert len(rig.llm_calls) == 1


async def test_speech_after_the_transcript_was_sent_is_a_barge_in_and_repeats_nothing(rig, mic):
    """The other ordering: the user has SEEN the first sentence. It is not prepended."""
    m = rig.manager
    rig.reply = "Here is a long answer that keeps going. " * 6
    rig.token_delay = 0.01
    rig.animate_delay = 30
    mic.transcript = _both_or_one(mic)
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_end(FIRST)
    await asyncio.sleep(0.15)  # transcribed, sent, and the reply under way
    assert _of(rig, "transcription") == [{"type": "transcription", "text": "first"}]
    assert m._mics["s1"].turn is None

    await detector.on_speech_start()
    assert len(_of(rig, "interrupted")) == 1
    rig.reply, rig.token_delay, rig.animate_delay = "Second answer.", 0, 0
    await detector.on_speech_end(SECOND)
    await mic.turn()

    assert mic.stt_calls[-1][0] is SECOND, "only the new utterance"
    assert [x["text"] for x in _of(rig, "transcription")] == ["first", "second"]
    users = [x["content"] for x in m.session_data["s1"]["messages"] if x["role"] == "user"]
    assert users == ["first", "second"]


async def test_audio_kept_for_a_continuation_goes_when_the_listener_stops(rig, mic):
    m = rig.manager
    rig.reply = "Answer."
    mic.stt_delay = 0.2
    mic.transcript = _both_or_one(mic)
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_end(FIRST)
    await asyncio.sleep(0.02)
    await detector.on_speech_start()
    kept = m._mics["s1"]
    assert kept.carry is FIRST

    await m.handle_mic("s1", False)
    assert kept.carry is None and kept.turn is None and kept.turn_audio is None
    await asyncio.sleep(0.25)
    assert not _of(rig, "transcription") and rig.llm_calls == []

    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_end(SECOND)
    await mic.turn()
    assert mic.stt_calls[-1][0] is SECOND
    assert _of(rig, "transcription") == [{"type": "transcription", "text": "second"}]


async def test_the_length_cap_covers_a_continuation_and_what_it_continues(rig, mic):
    rig.reply = "Answer."
    mic.stt_delay = 0.2
    long_first = np.full(20 * 16000, 1, np.int16)
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_end(long_first)
    await asyncio.sleep(0.02)
    await detector.on_speech_start()
    # The detector is told how much room is left, so it cuts the utterance
    # there (and does not let the rest of it barge in on the reply)...
    assert detector.max_secs == [10.0]
    # ...and even if it handed on more, no more than the cap is transcribed.
    await detector.on_speech_progress(np.full(15 * 16000, 2, np.int16))
    await detector.on_speech_end(np.full(15 * 16000, 2, np.int16))
    assert detector.max_secs == [10.0, 30.0], "the next utterance has the whole cap again"
    await mic.turn()
    whole = mic.stt_calls[-1][0]
    assert len(whole) == 30 * 16000
    assert np.array_equal(whole[: 20 * 16000], long_first) and set(whole[20 * 16000 :]) == {2}


async def test_on_cpu_a_speculative_transcript_never_queues_behind_another(rig, mic, monkeypatch):
    """
    A discarded guess cannot be stopped. One per session is the most that can
    ever be in the way of the real transcript: while it is inside Whisper no
    second guess is lined up behind it.
    """
    monkeypatch.setattr(type(wsmod.stt_service), "on_gpu", property(lambda self: False))
    rig.reply = "Ok."
    mic.stt_delay = 0.05
    mic.transcript = lambda n: f"pass {n}"
    detector = await mic.on()
    longer = np.zeros(32000, np.int16)
    await detector.on_speech_start()
    await detector.on_speech_paused(UTTERANCE)  # nothing else running: started
    await asyncio.sleep(0.01)
    assert len(mic.stt_calls) == 1
    detector.on_speech_resumed()
    await detector.on_speech_paused(longer)  # the stale pass is still in Whisper
    assert rig.manager._mics["s1"].speculative is None
    await asyncio.sleep(0.1)
    assert len(mic.stt_calls) == 1, "nothing was waiting to start behind it"

    await detector.on_speech_end(longer)
    await mic.turn()
    assert [len(a) for a, _ in mic.stt_calls] == [16000, 32000]
    assert _of(rig, "transcription") == [{"type": "transcription", "text": "pass 1"}]


async def test_on_cpu_no_speculative_transcript_starts_beside_someone_elses(rig, mic, monkeypatch):
    stt = type(wsmod.stt_service)
    monkeypatch.setattr(stt, "on_gpu", property(lambda self: False))
    monkeypatch.setattr(stt, "idle", property(lambda self: False))  # another session's
    rig.reply = "Ok."
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_paused(UTTERANCE)
    await _settle(rig.manager)
    assert mic.stt_calls == []
    # The turn is not affected: it transcribes for itself, once.
    await detector.on_speech_end(UTTERANCE)
    await mic.turn()
    assert len(mic.stt_calls) == 1 and len(rig.llm_calls) == 1

    # On a GPU the pass is cheap enough to guess with, whatever else runs.
    monkeypatch.setattr(stt, "on_gpu", property(lambda self: True))
    await detector.on_speech_start()
    await detector.on_speech_paused(UTTERANCE)
    await _settle(rig.manager)
    assert len(mic.stt_calls) == 2


async def test_starting_a_turn_cuts_whatever_is_in_the_slot_first(rig):
    """
    _spawn_turn on its own, with no interrupt before it: the previous turn
    is cancelled and its reply committed before the new question is added.
    """
    m = rig.manager
    rig.reply = "Here is a long answer that keeps going. " * 6
    rig.token_delay = 0.01
    rig.animate_delay = 30
    first = m._spawn_turn("s1", m._handle_text_input_inner("s1", "one"))
    await asyncio.sleep(0.2)
    rig.reply, rig.token_delay, rig.animate_delay = "Two.", 0, 0
    second = m._spawn_turn("s1", m._handle_text_input_inner("s1", "two"))
    assert first.cancelling() and m._active_turns["s1"] is second
    await asyncio.gather(first, second, return_exceptions=True)
    await _settle(m)
    history = m.session_data["s1"]["messages"]
    assert [x["role"] for x in history] == ["user", "assistant", "user", "assistant"], history
    assert history[1]["content"].startswith("Here is a long answer")
    assert rig.socket.types().count("interrupted") == 1


async def test_two_tasks_starting_turns_cannot_orphan_one(rig, mic):
    """
    The receive loop and the microphone both run interrupt-then-spawn, and
    the interrupt awaits a send. A typed message handled during the
    microphone's send found nothing to interrupt and started its turn; the
    microphone then stored its own over it, and the typed turn ran on with
    nothing able to stop it.
    """
    m = rig.manager
    rig.reply = "Here is a long answer that keeps going. " * 6
    rig.token_delay = 0.01
    rig.animate_delay = 30
    spawned: list[asyncio.Task] = []
    plain_spawn = m._spawn_turn

    def spawn(session_id, coro):
        spawned.append(plain_spawn(session_id, coro))
        return spawned[-1]

    m._spawn_turn = spawn
    detector = await mic.on()
    await detector.on_speech_start()
    await m.handle_text_input("s1", "typed one")  # typed while the user is talking
    await asyncio.sleep(0.1)

    parked, release = _hold(rig, lambda msg: msg["type"] == "interrupted")
    ending = asyncio.create_task(detector.on_speech_end(UTTERANCE))
    await parked.wait()  # the microphone has cut "typed one" and is telling the client
    await m.handle_text_input("s1", "typed two")  # nothing left to interrupt: spawns
    typed_two = m._active_turns["s1"]
    release.set()
    await ending
    await asyncio.sleep(0.05)

    assert len(spawned) == 3
    alive = [t for t in spawned if not t.done()]
    assert alive == [m._active_turns["s1"]], "one turn, and it is the one that can be stopped"
    assert typed_two.cancelled()
    assert await m.interrupt_active_turn("s1") is True
    await asyncio.gather(*spawned, return_exceptions=True)
    assert all(t.done() for t in spawned)
    assert len(rig.llm_calls) <= 2, "typed two never got as far as the model"


async def test_mic_off_during_the_stopped_send_starts_no_turn(rig, mic):
    """`active:false` handled between the utterance closing and its turn being spawned."""
    m = rig.manager
    mic.stt_delay = 0.05
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_paused(UTTERANCE)
    parked, release = _hold(rig, lambda msg: msg == STOPPED_TURN)
    ending = asyncio.create_task(detector.on_speech_end(UTTERANCE))
    await parked.wait()
    switching_off = asyncio.create_task(m.handle_mic("s1", False))
    await asyncio.sleep(0)  # the listener is stopped; its answer waits for the socket
    release.set()
    await asyncio.gather(ending, switching_off)
    await asyncio.sleep(0.1)
    await _settle(m)

    assert "s1" not in m._active_turns
    assert not _of(rig, "transcription") and not _of(rig, "status") and rig.llm_calls == []
    # "stopped, turn" had gone out; it is taken back.
    assert _of(rig, "user_speech") == [STARTED, STOPPED_TURN, STOPPED_IGNORED]
    assert _of(rig, "mic_state") == [LISTENING, NOT_LISTENING]


async def test_mic_off_withdraws_an_utterance_that_has_not_been_shown(rig, mic):
    """
    The utterance closed, its turn is waiting on Whisper, and the user
    switches hands-free off. A sentence transcribed and answered after
    `mic_state listening:false` is the microphone still at work.
    """
    m = rig.manager
    mic.stt_delay = 0.2
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_end(UTTERANCE)
    await asyncio.sleep(0.05)
    turn = m._active_turns["s1"]
    await m.handle_mic("s1", False)
    assert "s1" not in m._active_turns and turn.cancelling()
    await asyncio.sleep(0.3)  # Whisper finishes; nobody is waiting
    await _settle(m)

    assert turn.cancelled()
    assert not _of(rig, "transcription") and rig.llm_calls == []
    assert m.session_data["s1"]["messages"] == [] and rig.persisted == []
    # Not announced as an interruption — no reply was cut — but the client
    # is told the utterance it was waiting on is not coming.
    assert not _of(rig, "interrupted")
    assert _of(rig, "user_speech") == [STARTED, STOPPED_TURN, STOPPED_IGNORED]
    assert rig.socket.sent[-2:] == [NOT_LISTENING, STOPPED_IGNORED]


async def test_mic_off_leaves_a_turn_whose_words_were_shown(rig, mic):
    m = rig.manager
    rig.reply = "A reply that takes a moment."
    rig.token_delay = 0.02
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_end(UTTERANCE)
    await asyncio.sleep(0.05)
    assert _of(rig, "transcription"), "the user has seen their words"
    turn = m._active_turns["s1"]
    await m.handle_mic("s1", False)
    assert m._active_turns.get("s1") is turn and not turn.cancelling()
    await mic.turn()
    assert m.session_data["s1"]["messages"] == [
        {"role": "user", "content": "what the user said"},
        {"role": "assistant", "content": "A reply that takes a moment."},
    ]
    assert STOPPED_IGNORED not in rig.socket.sent


async def test_a_reconnect_does_not_tell_the_new_tab_about_the_old_ones_utterance(
    rig, mic, monkeypatch
):
    m = rig.manager
    mic.stt_delay = 0.2
    detector = await mic.on()
    await detector.on_speech_start()
    await detector.on_speech_end(UTTERANCE)
    await asyncio.sleep(0.02)

    async def no_load(session_id):
        return None

    monkeypatch.setattr(m, "_load_session_data", no_load)
    newer = FakeWS()
    await m.connect("s1", newer, user_id="u1")
    await asyncio.sleep(0.3)
    await _settle(m)
    assert newer.sent == [] and rig.llm_calls == []
