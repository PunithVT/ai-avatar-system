"""
Speech on the WebRTC stream survives the stream: the turn pipeline, the
manager and a REAL AvatarStream together, with only the peer connection left
out (the tracks are pulled by hand, as aiortc's senders would pull them).

The rule being pinned: a sentence put on the stream is recoverable until it
has played. When the stream goes down, every clip it had not finished is
uploaded and sent as an ordinary URL clip — same chunk_index, `resend: true`,
in order — and nothing is played twice or sent twice.
"""

import asyncio
import shutil
import threading

import pytest

import app.services.webrtc as rtc
import app.websocket as wsmod
from app.services.webrtc import AvatarStream
from tests.test_turn_pipeline import _settle, rig, uploads  # noqa: F401
from tests.test_webrtc_stream import FPS, SIZE, _pump, media  # noqa: F401

pytestmark = [
    pytest.mark.skipif(not rtc.WEBRTC_AVAILABLE, reason="aiortc is not installed"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"),
]


class Unwired(AvatarStream):
    """
    A real stream with no peer connection under it. Everything else is the
    production code: the queues, the per-clip accounting, retire and close.
    """

    @property
    def connected(self) -> bool:
        return not self._retired and not self._failed and self._tracks_live


@pytest.fixture
async def live(rig, media, monkeypatch):  # noqa: F811
    """The rig's session on a real stream, animating real (0.4 s) clips."""

    async def animate(avatar_image_path, audio_path, output_path, cache_key=None):
        await asyncio.to_thread(shutil.copyfile, media.short, output_path)
        return output_path

    monkeypatch.setattr(wsmod.avatar_animator, "animate", animate)
    m = rig.manager
    stream = await Unwired.create(media.idle, size=SIZE, fps=FPS)
    # Wired as _negotiate_webrtc wires it.
    stream.on_state = lambda state, s=stream: m._on_stream_state("s1", s, state)
    stream.on_speaking = lambda active, s=stream: m._on_stream_speaking("s1", s, active)
    stream.on_clip_released = m._release_clip
    m._streams["s1"] = stream
    m._webrtc_live.add("s1")
    # Both tracks are being pulled before any speech, as on a live connection.
    await stream.video.recv()
    await stream.audio.recv()
    yield stream
    await _settle(m)
    await stream.close()


def _chunks(rig):  # noqa: F811
    return [m for m in rig.socket.sent if m["type"] == "video_chunk"]


def _route(chunk):
    if chunk.get("transport") == "webrtc":
        return "webrtc"
    return "resend" if chunk.get("resend") else "url"


def _clips_on_disk(tmp_path):
    d = tmp_path / "avatar-session-s1"
    return sorted(p.name for p in d.glob("*.mp4")) if d.exists() else []


async def _until(predicate, what, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def test_a_played_b_half_played_c_queued_and_the_stream_dies(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    live,
    monkeypatch,
    tmp_path,
):
    """
    Sentences A, B and C are on the stream. A has played to the end, B is
    half-way through, C has not started; the client reports its peer
    connection gone. Exactly B and C come back by URL — B whole — and the
    fourth sentence of the same turn follows them by URL.
    """
    m = rig.manager
    gate = asyncio.Event()
    animated = 0
    copy = wsmod.avatar_animator.animate

    async def animate(**kwargs):
        nonlocal animated
        animated += 1
        if animated == 4:
            await gate.wait()  # the fourth sentence is still being made
        return await copy(**kwargs)

    monkeypatch.setattr(wsmod.avatar_animator, "animate", animate)
    turn = asyncio.create_task(m._handle_text_input_inner("s1", "question"))
    await _until(lambda: len(_chunks(rig)) == 3 and animated == 4, "three sentences queued")
    assert [_route(c) for c in _chunks(rig)] == ["webrtc"] * 3
    assert uploads == [] and len(_clips_on_disk(tmp_path)) == 3
    a, b, c = live.queued_clips

    # Play all of A and half of B.
    await _pump(live, until=lambda: live.queued_clips == [b, c] and live.queued_video_frames <= 15)
    assert 11 <= live.queued_video_frames <= 15, "B is part-way through"
    assert not a.path.exists(), "a clip that has played is no longer kept"
    assert b.path.exists() and c.path.exists()

    await m.handle_webrtc_close("s1")
    assert live.closed and live.queued_video_frames == 0, "what is re-sent is not also played"
    gate.set()
    await turn
    await _settle(m)

    chunks = _chunks(rig)
    assert [(_route(x), x["chunk_index"]) for x in chunks] == [
        ("webrtc", 0),
        ("webrtc", 1),
        ("webrtc", 2),
        ("resend", 1),
        ("resend", 2),
        ("url", 3),
    ]
    assert [x["text"] for x in chunks[3:5]] == [x["text"] for x in chunks[1:3]]
    assert all(x["video_url"] and "transport" not in x for x in chunks[3:])
    assert "resend" not in chunks[5]
    assert " ".join(x["text"] for x in chunks[:3] + chunks[5:]) == rig.reply
    assert len(uploads) == 3, "B, C and the fourth sentence — and A was not stored"

    sent = rig.socket.sent
    first_resend = next(i for i, x in enumerate(sent) if x.get("resend"))
    told = sent.index({"type": "media_transport", "transport": "clips"})
    assert told < first_resend
    assert {"type": "speaking", "active": False} in sent[:told], "the stream's speech is over"
    assert _clips_on_disk(tmp_path) == [], "every held file is gone once re-sent"

    # Nothing is re-sent twice, whichever signal arrives second.
    before = len(sent)
    await m.handle_webrtc_close("s1")
    await m._on_stream_state("s1", live, "failed")
    await _settle(m)
    assert len(sent) == before and len(uploads) == 3


async def test_speech_that_plays_to_the_end_leaves_nothing_behind(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    live,
    tmp_path,
):
    await rig.manager._handle_text_input_inner("s1", "question")
    assert len(_clips_on_disk(tmp_path)) == 4
    await _pump(live, until=lambda: not live.speaking, timeout=10)
    assert _clips_on_disk(tmp_path) == [] and live.queued_clips == []

    await rig.manager.handle_webrtc_close("s1")
    await _settle(rig.manager)
    assert uploads == [] and not [c for c in _chunks(rig) if c.get("resend")]


async def test_a_barge_in_deletes_the_flushed_clips_and_they_are_never_resent(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    live,
    tmp_path,
):
    await rig.manager._handle_text_input_inner("s1", "question")
    assert len(_clips_on_disk(tmp_path)) == 4

    assert await rig.manager.interrupt_active_turn("s1") is True
    assert _clips_on_disk(tmp_path) == []
    await rig.manager.handle_webrtc_close("s1")
    await _settle(rig.manager)
    assert uploads == [] and not [c for c in _chunks(rig) if c.get("resend")]


async def test_a_clip_still_decoding_when_the_stream_goes_down_is_sent_by_url(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    live,
    monkeypatch,
):
    """
    The stream used to stay open while the client was being told it was on
    clips. A clip that finished decoding during those sends was queued onto
    it, announced as streamed, and thrown away by the close a moment later:
    delivered by neither route.
    """
    decoding, release = threading.Event(), threading.Event()
    real_decode = rtc.decode_clip

    def slow_decode(*args, **kwargs):
        decoding.set()
        release.wait(5)
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(rtc, "decode_clip", slow_decode)

    real_send = rig.socket.send_json

    async def slow_send(msg):
        if msg["type"] == "media_transport":
            # The decode lands while this send is on the wire.
            release.set()
            await asyncio.sleep(0.2)
        await real_send(msg)

    monkeypatch.setattr(rig.socket, "send_json", slow_send)
    rig.reply = "A short answer."
    turn = asyncio.create_task(rig.manager._handle_text_input_inner("s1", "question"))
    await asyncio.to_thread(decoding.wait, 5)

    await rig.manager.handle_webrtc_close("s1")
    await turn
    await _settle(rig.manager)

    chunks = _chunks(rig)
    assert [_route(c) for c in chunks] == ["url"], "once, by URL, and not as a re-send"
    assert len(uploads) == 1
    types = rig.socket.types()
    assert types.index("media_transport") < types.index("video_chunk")


async def test_a_stream_that_stops_draining_mid_reply_falls_back_with_everything(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    media,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    """
    Nobody is pulling the tracks (a dead sender under a "connected" peer).
    The turn used to announce clips as streamed until the queue was full and
    then wait for ever. Now the wait runs out, the stream is taken down, and
    the whole reply — what was queued and what was not — arrives as clips.
    """
    monkeypatch.setattr(rtc, "ROOM_WAIT_MARGIN_SECONDS", 0.2)

    async def animate(avatar_image_path, audio_path, output_path, cache_key=None):
        await asyncio.to_thread(shutil.copyfile, media.short, output_path)
        return output_path

    monkeypatch.setattr(wsmod.avatar_animator, "animate", animate)
    m = rig.manager
    # Room for two 0.4 s clips, not three.
    stream = await Unwired.create(media.idle, size=SIZE, fps=FPS, max_queue_seconds=0.6)
    stream.on_state = lambda state, s=stream: m._on_stream_state("s1", s, state)
    stream.on_clip_released = m._release_clip
    m._streams["s1"] = stream
    m._webrtc_live.add("s1")

    await asyncio.wait_for(m._handle_text_input_inner("s1", "question"), 10)
    await _settle(m)

    chunks = _chunks(rig)
    assert [(_route(c), c["chunk_index"]) for c in chunks] == [
        ("webrtc", 0),
        ("webrtc", 1),
        ("resend", 0),
        ("resend", 1),
        ("url", 2),
        ("url", 3),
    ]
    assert len(uploads) == 4
    assert stream.closed and m._live_stream("s1") is None
    assert {"type": "media_transport", "transport": "clips"} in rig.socket.sent
    assert _clips_on_disk(tmp_path) == []
