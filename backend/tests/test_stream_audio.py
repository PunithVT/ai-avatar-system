"""
Sound straight onto the WebRTC stream (AvatarStream.enqueue_audio).

With the still-image engine a sentence's clip is the avatar's picture with
the audio under it — exactly what the stream shows when it is idle. So the
audio goes onto the audio track by itself and no clip is encoded or decoded;
one is rendered only if the stream dies with the sentence unplayed and it has
to be re-sent by URL.

Three layers: the stream alone, the turn pipeline against a recording stream,
and the pipeline with a real stream pulled by hand.
"""

import asyncio
import shutil
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import app.services.webrtc as rtc
import app.websocket as wsmod
from app.services.animator import AvatarAnimator
from app.services.tts import SynthResult
from app.services.webrtc import AUDIO_FRAME_SAMPLES, AUDIO_RATE, AvatarStream, ClipDropped
from tests.test_turn_pipeline import (  # noqa: F401
    CLIP_CHUNK_KEYS,
    FakeStream,
    _chunks,
    _go_live,
    _route,
    _session_files,
    _settle,
    rig,
    uploads,
)
from tests.test_webrtc_recovery import Unwired
from tests.test_webrtc_stream import (  # noqa: F401
    FPS,
    SAMPLES_PER_FRAME,
    SIZE,
    _is_blue,
    _is_red,
    _level,
    _pump,
    media,
    stream,
)

pytestmark = [
    pytest.mark.skipif(not rtc.WEBRTC_AVAILABLE, reason="aiortc is not installed"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"),
]


def _tone(seconds: float, rate: int = AUDIO_RATE, level: int = 8000) -> np.ndarray:
    """A square-ish int16 signal with no zero samples, so its extent is exact."""
    n = round(seconds * rate)
    return np.where((np.arange(n) // 24) % 2 == 0, level, -level).astype(np.int16)


def _wav(path, seconds: float, rate: int = 44100) -> str:
    sf.write(str(path), _tone(seconds, rate), rate, subtype="PCM_16")
    return str(path)


def _samples(frames) -> np.ndarray:
    if not frames:
        return np.zeros(0, np.int16)
    return np.concatenate([f.to_ndarray().reshape(-1) for f in frames])


def _ends(s):
    video = (s.video.position + s.queued_video_frames) * SAMPLES_PER_FRAME
    audio = s.audio.position * AUDIO_FRAME_SAMPLES + s.queued_audio_samples
    return video, audio


# ── the stream ───────────────────────────────────────────────────────────


async def test_audio_alone_plays_under_the_idle_picture_for_exactly_its_length(stream):  # noqa: F811
    """
    0.5 s of sound: every one of its 24000 samples comes out, in order, with
    the still face over it at the full frame rate, and the speaking signal
    fires once each way.
    """
    events: list[bool] = []
    stream.on_speaking = events.append
    released: list = []
    stream.on_clip_released = released.append
    pcm = _tone(0.5)

    before_v, before_a = await _pump(stream, until=lambda: stream.video.position is not None)
    duration_ms = await stream.enqueue_audio(pcm, "A")
    # 12.5 frames of sound is 13 frames on the timeline: a clip is a whole
    # number of frames, so the one after it starts with sound and picture
    # together.
    assert duration_ms == 520
    assert stream.speaking and events == [True] and stream.queued_clips == ["A"]

    during_v, during_a = await _pump(stream, until=lambda: not stream.speaking)
    resumed = stream.video.position
    after_v, after_a = await _pump(stream, until=lambda: stream.video.position > resumed + 1)
    assert events == [True, False] and released == ["A"]

    heard = _samples(before_a + during_a + after_a)
    loud = np.flatnonzero(heard)
    assert len(loud) == len(pcm) == 24000, "not a sample more or fewer"
    assert np.array_equal(heard[loud[0] : loud[-1] + 1], pcm), "and none of them changed"

    assert all(_is_blue(f) for f in before_v + during_v + after_v), "the idle face throughout"
    assert len(during_v) >= 13
    tick = 90000 // FPS
    pts = [f.pts for f in during_v]
    assert {b - a for a, b in zip(pts, pts[1:])} == {tick}, "at the full rate while speaking"


async def test_an_audio_file_is_resampled_onto_the_stream(stream, tmp_path):  # noqa: F811
    await stream.video.recv()
    await stream.audio.recv()
    assert await stream.enqueue_audio(_wav(tmp_path / "a.wav", 0.4), "A") == 400
    assert stream.queued_video_frames == 10
    video, audio = await _pump(stream, until=lambda: not stream.speaking)
    loud = [f for f in audio if _level(f) > 500]
    assert 19 <= len(loud) <= 21, f"0.4 s is 20 audio frames, got {len(loud)}"
    assert all(_is_blue(f) for f in video)


async def test_samples_at_another_rate_and_as_floats_are_accepted(stream):  # noqa: F811
    floats = (_tone(0.4, rate=16000) / 32768).astype(np.float32)
    assert await stream.enqueue_audio(floats, "A", sample_rate=16000) == 400
    assert stream.queued_audio_samples == 10 * SAMPLES_PER_FRAME
    assert (
        np.abs(
            _samples([f for f in (await _pump(stream, until=lambda: not stream.speaking))[1]])
        ).max()
        > 6000
    )


async def test_a_picture_in_the_file_is_not_shown(stream, media):  # noqa: F811
    """enqueue_audio is sound only, whatever the file also holds."""
    await stream.enqueue_audio(media.short, "A")
    video, audio = await _pump(stream, until=lambda: not stream.speaking)
    assert video and all(_is_blue(f) for f in video) and not any(_is_red(f) for f in video)
    assert any(_level(f) > 500 for f in audio)


async def test_audio_and_clips_share_one_timeline_without_drift(stream, media, tmp_path):  # noqa: F811
    """
    Sentences with and without a picture, interleaved. Each one's sound must
    still start under its own first frame twenty sentences in.
    """
    stream._max_queue_seconds = 1e9
    await stream.video.recv()
    for _ in range(3):  # the tracks are a frame and a half apart to begin with
        await stream.audio.recv()
    odd = _wav(tmp_path / "odd.wav", 0.4337)  # not a whole number of frames
    tokens = []
    frames = 0
    for i in range(20):
        token = f"c{i}"
        tokens.append(token)
        kind = i % 4
        if kind == 0:
            ms = await stream.enqueue_audio(odd, token)
        elif kind == 1:
            ms = await stream.enqueue_clip(media.long_audio, token)
        elif kind == 2:
            ms = await stream.enqueue_audio(_tone(0.25), token)
        else:
            ms = await stream.enqueue_clip(media.short, token)
        frames += round(ms * FPS / 1000)
        video, audio = _ends(stream)
        assert video == audio, f"after clip {i} the tracks end {audio - video} samples apart"

    assert stream.queued_clips == tokens
    assert stream.queued_video_frames == frames + 1, "only the one levelling frame was added"
    # 0.4337 s is 10.84 frames -> 11; 0.25 s is 6.25 -> 7.
    assert frames == 5 * (11 + 15 + 7 + 10)


async def test_a_flush_drops_queued_audio_and_releases_it(stream):  # noqa: F811
    released: list = []
    stream.on_clip_released = released.append
    await stream.video.recv()
    await stream.audio.recv()
    for token in "AB":
        await stream.enqueue_audio(_tone(0.4), token)
    assert stream.speaking
    assert stream.flush() is True
    assert released == ["A", "B"] and not stream.speaking
    assert stream.queued_audio_samples == 0 and stream.queued_video_frames == 0
    assert _level(await stream.audio.recv()) == 0, "silence from the very next frame"
    assert stream.retire() == []


async def test_a_flush_during_the_decode_drops_the_audio(stream, tmp_path, monkeypatch):  # noqa: F811
    real = rtc.decode_clip

    def slow(*args, **kwargs):
        time.sleep(0.2)
        return real(*args, **kwargs)

    monkeypatch.setattr(rtc, "decode_clip", slow)
    task = asyncio.create_task(stream.enqueue_audio(_wav(tmp_path / "a.wav", 0.4), "A"))
    await asyncio.sleep(0.05)
    stream.flush()
    with pytest.raises(ClipDropped):
        await task
    assert stream.queued_clips == [] and not stream.speaking


async def test_unplayed_audio_is_handed_back_when_the_stream_goes_down(stream):  # noqa: F811
    """A played, B part-way, C untouched: B and C come back, B whole."""
    released: list = []
    stream.on_clip_released = released.append
    await stream.video.recv()
    await stream.audio.recv()
    for token in "ABC":
        assert await stream.enqueue_audio(_tone(0.4), token) == 400
    await _pump(stream, until=lambda: stream.queued_video_frames <= 15 and released == ["A"])
    assert stream.queued_video_frames >= 11, "B is part-way through"

    assert stream.retire() == ["B", "C"]
    assert released == ["A"]
    assert _level(await stream.audio.recv()) == 0, "what is handed back is not also played"
    with pytest.raises(RuntimeError):
        await stream.enqueue_audio(_tone(0.4), "D")


async def test_queued_audio_counts_against_the_cap(media):  # noqa: F811
    """Backpressure is the same: a second of sound is a second in the queue."""
    s = await AvatarStream.create(media.idle, size=SIZE, fps=FPS, max_queue_seconds=0.5)
    try:
        await s.enqueue_audio(_tone(1.0), "A")
        assert s.queued_seconds == pytest.approx(1.0)
        waiting = asyncio.create_task(s.enqueue_audio(_tone(0.4), "B"))
        await asyncio.sleep(0.1)
        assert not waiting.done(), "over the cap: it waits"
        await _pump(s, until=waiting.done)
        assert await waiting == 400 and s.queued_clips[-1] == "B"
    finally:
        await s.close()


async def test_audio_longer_than_a_clip_may_be_is_refused_whole(stream, tmp_path):  # noqa: F811
    too_long = rtc.MAX_CLIP_SECONDS + 1
    with pytest.raises(ValueError, match="longer than"):
        await stream.enqueue_audio(np.zeros(int(too_long * 8000), np.int16), "A", sample_rate=8000)
    with pytest.raises(ValueError, match="longer than"):
        await stream.enqueue_audio(_wav(tmp_path / "long.wav", too_long, rate=8000), "B")
    with pytest.raises(Exception):
        await stream.enqueue_audio(media_garbage(tmp_path), "C")
    assert stream.queued_clips == [] and not stream.speaking


def media_garbage(tmp_path) -> str:
    path = tmp_path / "garbage.wav"
    path.write_bytes(b"this is not audio" * 64)
    return str(path)


# ── what the animator says about itself ──────────────────────────────────


@pytest.fixture
def animator(monkeypatch, tmp_path):
    def make(engine: str, installed: bool = True) -> AvatarAnimator:
        monkeypatch.setattr(wsmod.settings, "AVATAR_ENGINE", engine)
        a = AvatarAnimator()
        a.device = "cpu"
        musetalk = tmp_path / "MuseTalk"
        if installed:
            (musetalk / "scripts").mkdir(parents=True, exist_ok=True)
            (musetalk / "scripts" / "inference.py").write_text("")
        monkeypatch.setattr(wsmod.settings, "MUSETALK_PATH", str(musetalk))

        async def must_not_start():
            raise AssertionError("asking must never start a lip-sync worker")

        monkeypatch.setattr(a, "_spawn_worker", must_not_start)
        return a

    return make


async def test_the_simple_engine_is_still_image_only(animator):
    assert await animator("simple").still_image_only() is True


async def test_a_lip_sync_engine_that_is_not_installed_is_still_image_only(animator):
    a = animator("musetalk", installed=False)
    assert await a.still_image_only() is True
    assert a.engine == "simple"
    assert await animator("liveavatar").still_image_only() is True  # needs CUDA and its checkout
    assert await animator("no-such-engine").still_image_only() is True


async def test_musetalk_is_still_image_only_just_while_its_worker_is_in_cool_off(animator):
    a = animator("musetalk")
    assert await a.still_image_only() is False, "never tried: lip-sync may well work"
    assert a._worker_start is None, "and asking did not try"

    a._worker_failed_at = time.monotonic()
    a._worker_failure = "CUDA out of memory"
    assert await a.still_image_only() is True, "animate() would fall back without trying"

    a._worker_failed_at = time.monotonic() - a._WORKER_RETRY_COOLDOWN_SECS - 1
    assert await a.still_image_only() is False, "the next sentence gets a fresh attempt"

    a._worker_failed_at = time.monotonic()
    a._worker_start = asyncio.get_running_loop().create_future()  # a start under way
    assert await a.still_image_only() is False
    a._worker_start.cancel()

    class Running:
        returncode = None

    a._worker_proc = Running()
    assert await a.still_image_only() is False


async def test_animate_still_renders_the_still_whatever_the_engine(animator, monkeypatch, tmp_path):
    a = animator("musetalk")
    calls = []

    async def simple(image, audio, output):
        calls.append((image, audio, output))
        return output

    monkeypatch.setattr(a, "_animate_simple", simple)
    out = tmp_path / "deep" / "o.mp4"
    assert await a.animate_still("i.jpg", "a.wav", str(out)) == str(out)
    assert calls == [("i.jpg", "a.wav", str(out))] and out.parent.is_dir()


# ── the turn pipeline, against a recording stream ────────────────────────


class AudioStream(FakeStream):
    """FakeStream that also takes audio by itself, as the real one does."""

    def __init__(self, socket=None):
        super().__init__(socket)
        self.audio: list[str] = []
        self.fail_audio = False
        self.drop_audio = False

    async def enqueue_audio(self, source, token=None, *, sample_rate=AUDIO_RATE):
        assert Path(source).exists(), "the audio must still be on disk when it is decoded"
        if self.retired:
            raise RuntimeError("stream is closed")
        if self.drop_audio:
            raise ClipDropped()
        if self.fail_audio:
            raise RuntimeError("could not decode")
        self.audio.append(source)
        if self.hold:
            self.unplayed.append(token)
        self.speaking = True
        return 4321


@pytest.fixture
def still(rig, monkeypatch):  # noqa: F811
    """The rig with the still-image engine: what the animator is asked, and renders."""

    class Still:
        asked = 0
        answer = True
        rendered: list[tuple[str, str, str]] = []

    s = Still()
    s.rendered = []

    async def still_image_only():
        s.asked += 1
        return s.answer

    async def animate_still(image, audio, output):
        assert Path(audio).exists(), "the retained audio is what the clip is rendered from"
        s.rendered.append((image, audio, output))
        Path(output).write_bytes(b"mp4")
        return output

    monkeypatch.setattr(wsmod.avatar_animator, "still_image_only", still_image_only)
    monkeypatch.setattr(wsmod.avatar_animator, "animate_still", animate_still)
    return s


def _animations(rig):  # noqa: F811
    return rig.events.count("anim-start")


async def test_with_the_still_engine_a_live_stream_takes_the_audio_and_no_clip_is_made(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    still,
    tmp_path,
):
    live = _go_live(rig, AudioStream(rig.socket))
    live.hold = True
    await rig.manager._handle_text_input_inner("s1", "question")

    chunks = _chunks(rig)
    assert _animations(rig) == 0 and still.rendered == [], "the animator is not called at all"
    assert len(live.audio) == 4 and live.enqueued == [] and uploads == []
    for i, c in enumerate(chunks):
        assert c == {
            "type": "video_chunk",
            "chunk_index": i,
            "total_chunks": -1,
            "transport": "webrtc",
            "text": c["text"],
            "duration_ms": 4321,
        }
    assert " ".join(c["text"] for c in chunks) == rig.reply
    assert [m for m in rig.socket.sent if m["type"] == "video_chunk_end"][0]["sent_chunks"] == 4
    assert "error" not in rig.socket.types()

    # The audio is what is kept now, under the same rule: until it has played.
    files = _session_files(tmp_path)
    assert len(files) == 4 and all(f.endswith("_audio.wav") for f in files)
    live.play(3)
    assert len(_session_files(tmp_path)) == 1
    live.play(1)
    assert _session_files(tmp_path) == []


async def test_unplayed_audio_is_rendered_and_resent_by_url_when_the_stream_dies(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    still,
    tmp_path,
):
    """
    Four sentences went on as audio; one has played and the stream dies. The
    other three were announced as streamed and never heard. A URL needs an
    MP4, so each is rendered now — from the audio that was kept, over the
    session's avatar — and sent with its original chunk_index and resend.
    """
    live = _go_live(rig, AudioStream(rig.socket))
    live.hold = True
    await rig.manager._handle_text_input_inner("s1", "question")
    assert still.rendered == []
    live.play(1)

    await rig.manager.handle_webrtc_close("s1")
    await _settle(rig.manager)

    chunks = _chunks(rig)
    assert [(_route(c), c["chunk_index"]) for c in chunks] == [
        ("webrtc", 0),
        ("webrtc", 1),
        ("webrtc", 2),
        ("webrtc", 3),
        ("resend", 1),
        ("resend", 2),
        ("resend", 3),
    ]
    for original, again in zip(chunks[1:4], chunks[4:]):
        assert again == {
            "type": "video_chunk",
            "chunk_index": original["chunk_index"],
            "total_chunks": -1,
            "video_url": again["video_url"],
            "text": original["text"],
            "resend": True,
        }
    image = rig.manager.session_data["s1"]["avatar_image_local"]
    assert [r[0] for r in still.rendered] == [image] * 3
    assert [r[1] for r in still.rendered] == live.audio[1:], "from the retained audio, in order"
    assert _animations(rig) == 0, "as a still — never through the lip-sync engine"
    assert len(uploads) == 3 and len(set(uploads)) == 3

    sent = rig.socket.sent
    told = sent.index({"type": "media_transport", "transport": "clips"})
    assert told < next(i for i, m in enumerate(sent) if m.get("resend"))
    assert _session_files(tmp_path) == [], "neither the audio nor the rendered clips are left"


async def test_the_rest_of_a_turn_whose_stream_died_is_rendered_and_sent_by_url(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    still,
    tmp_path,
):
    live = _go_live(rig, AudioStream(rig.socket))
    live.hold = True
    real = live.enqueue_audio

    async def dies_on_the_third(source, token=None, **kwargs):
        if len(live.audio) == 2:
            live.connected = False
            raise RuntimeError("stream is no longer being sent")
        return await real(source, token, **kwargs)

    live.enqueue_audio = dies_on_the_third
    await rig.manager._handle_text_input_inner("s1", "question")
    await _settle(rig.manager)

    chunks = _chunks(rig)
    assert [(_route(c), c["chunk_index"]) for c in chunks] == [
        ("webrtc", 0),
        ("webrtc", 1),
        ("resend", 0),
        ("resend", 1),
        ("url", 2),
        ("url", 3),
    ]
    assert len(still.rendered) == 2 and _animations(rig) == 2
    assert " ".join(c["text"] for c in chunks[2:]) == rig.reply
    assert _session_files(tmp_path) == []


async def test_audio_the_stream_refuses_is_sent_as_a_clip_and_nothing_is_lost(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    still,
    tmp_path,
):
    """E.g. a sentence longer than the stream takes: the stream stays up."""
    live = _go_live(rig, AudioStream(rig.socket))
    live.fail_audio = True
    await rig.manager._handle_text_input_inner("s1", "question")

    chunks = _chunks(rig)
    assert len(chunks) == len(uploads) == 4 and _animations(rig) == 4
    assert all(set(c) == CLIP_CHUNK_KEYS for c in chunks)
    assert live.enqueued == [], "refused as audio, it is not then tried as a clip on the stream"
    # Once one sentence has gone by URL the rest of the turn does, unasked.
    assert still.asked == 1
    assert " ".join(c["text"] for c in chunks) == rig.reply
    assert _session_files(tmp_path) == []


async def test_audio_flushed_while_it_was_being_queued_is_not_sent_either_way(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    still,
    tmp_path,
):
    live = _go_live(rig, AudioStream(rig.socket))
    live.drop_audio = True
    await rig.manager._handle_text_input_inner("s1", "question")
    assert _chunks(rig) == [] and uploads == [] and _animations(rig) == 0
    assert _session_files(tmp_path) == []


async def test_a_lip_sync_engine_still_makes_its_clip_for_the_stream(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    still,
):
    """The round trip carries real video there; nothing changes."""
    still.answer = False
    live = _go_live(rig, AudioStream(rig.socket))
    await rig.manager._handle_text_input_inner("s1", "question")
    assert _animations(rig) == 4 and len(live.enqueued) == 4 and live.audio == []
    assert all(p.endswith("_video.mp4") for p in live.enqueued)
    assert [_route(c) for c in _chunks(rig)] == ["webrtc"] * 4 and uploads == []


async def test_on_the_clips_transport_the_still_engine_changes_nothing(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    still,
):
    await rig.manager._handle_text_input_inner("s1", "question")
    chunks = _chunks(rig)
    assert len(chunks) == len(uploads) == _animations(rig) == 4
    assert all(set(c) == CLIP_CHUNK_KEYS for c in chunks)
    assert still.asked == 0, "without a stream the question is never even put"

    # Nor on a stream that is negotiated but not yet announced.
    rig.manager._streams["s1"] = AudioStream(rig.socket)
    await rig.manager._handle_text_input_inner("s1", "again")
    assert still.asked == 0 and rig.manager._streams["s1"].audio == []


async def test_an_interrupt_or_the_end_of_the_session_deletes_held_audio(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    still,
    tmp_path,
):
    live = _go_live(rig, AudioStream(rig.socket))
    live.hold = True
    await rig.manager._handle_text_input_inner("s1", "question")
    assert len(_session_files(tmp_path)) == 4
    assert await rig.manager.interrupt_active_turn("s1") is True
    assert _session_files(tmp_path) == []

    await rig.manager._handle_text_input_inner("s1", "again")
    assert len(_session_files(tmp_path)) == 4
    await rig.manager.disconnect("s1")
    await _settle(rig.manager)
    assert _session_files(tmp_path) == [] and uploads == [] and still.rendered == []


async def test_an_interrupt_during_the_render_of_a_resend_leaves_no_files(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    still,
    monkeypatch,
    tmp_path,
):
    live = _go_live(rig, AudioStream(rig.socket))
    live.hold = True
    rig.reply = "A short answer."
    await rig.manager._handle_text_input_inner("s1", "question")
    rendering = asyncio.Event()

    async def slow(image, audio, output):
        Path(output).write_bytes(b"half an mp4")
        rendering.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(wsmod.avatar_animator, "animate_still", slow)
    await rig.manager.handle_webrtc_close("s1")
    await asyncio.wait_for(rendering.wait(), 2)
    assert any(f.endswith(".mp4") for f in _session_files(tmp_path))

    assert await rig.manager.interrupt_active_turn("s1") is True
    await _settle(rig.manager)
    assert _session_files(tmp_path) == [] and uploads == []
    assert not [c for c in _chunks(rig) if c.get("resend")]


# ── the pipeline with a real stream ──────────────────────────────────────


@pytest.fixture
async def real(rig, media, still, monkeypatch):  # noqa: F811
    """Real 0.4 s WAVs from the synthesiser onto a real stream, pulled by hand."""

    async def synth(text, output_path, speaker_wav=None, language="en"):
        await asyncio.to_thread(_wav, output_path, 0.4, 48000)
        return SynthResult(output_path=output_path, engine="x", fallback=False, voice_cloned=False)

    async def animate_still(image, audio, output):
        assert Path(audio).exists()
        still.rendered.append((image, audio, output))
        await asyncio.to_thread(shutil.copyfile, media.short, output)
        return output

    monkeypatch.setattr(wsmod.tts_service, "synthesize", synth)
    monkeypatch.setattr(wsmod.avatar_animator, "animate_still", animate_still)
    m = rig.manager
    s = await Unwired.create(media.idle, size=SIZE, fps=FPS)
    s.on_state = lambda state, s=s: m._on_stream_state("s1", s, state)
    s.on_speaking = lambda active, s=s: m._on_stream_speaking("s1", s, active)
    s.on_clip_released = m._release_clip
    m._streams["s1"] = s
    m._webrtc_live.add("s1")
    await s.video.recv()
    await s.audio.recv()
    yield s
    await _settle(m)
    await s.close()


async def test_a_reply_is_heard_from_the_stream_with_the_face_held_still(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    real,
    still,
    tmp_path,
):
    await rig.manager._handle_text_input_inner("s1", "question")
    chunks = _chunks(rig)
    assert [_route(c) for c in chunks] == ["webrtc"] * 4
    assert [c["duration_ms"] for c in chunks] == [400] * 4
    assert len(_session_files(tmp_path)) == 4

    video, audio = await _pump(real, until=lambda: not real.speaking, timeout=10)
    heard = _samples(audio)
    assert np.count_nonzero(heard) == 4 * round(0.4 * AUDIO_RATE), "all four sentences, whole"
    assert all(_is_blue(f) for f in video)
    assert _session_files(tmp_path) == [] and real.queued_clips == []
    assert _animations(rig) == 0 and still.rendered == [] and uploads == []
    sent = rig.socket.sent
    assert sent.count({"type": "speaking", "active": True}) == 1
    assert sent.count({"type": "speaking", "active": False}) == 1


async def test_audio_half_played_when_the_stream_dies_comes_back_whole_by_url(
    rig,  # noqa: F811
    uploads,  # noqa: F811
    real,
    still,
    tmp_path,
):
    m = rig.manager
    await m._handle_text_input_inner("s1", "question")
    a, b, c, d = real.queued_clips
    await _pump(
        real, until=lambda: real.queued_clips == [b, c, d] and real.queued_video_frames <= 25
    )
    assert 21 <= real.queued_video_frames <= 25, "B is part-way through"
    assert not a.path.exists() and b.path.exists()

    await m.handle_webrtc_close("s1")
    assert real.closed and real.queued_audio_samples == 0, "what is re-sent is not also played"
    await _settle(m)

    chunks = _chunks(rig)
    assert [(_route(x), x["chunk_index"]) for x in chunks[4:]] == [
        ("resend", 1),
        ("resend", 2),
        ("resend", 3),
    ]
    assert [x["text"] for x in chunks[4:]] == [x["text"] for x in chunks[1:4]]
    assert [r[1] for r in still.rendered] == [str(x.path) for x in (b, c, d)]
    assert len(uploads) == 3 and _session_files(tmp_path) == []
