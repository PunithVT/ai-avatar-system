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
