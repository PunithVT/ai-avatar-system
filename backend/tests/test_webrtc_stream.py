"""
The WebRTC media transport: the continuous audio/video stream that replaces
per-sentence clips when MEDIA_TRANSPORT=webrtc.

Three layers, cheapest first: the decoder (a clip becomes equal lengths of
audio and video), the tracks driven by hand with no peer connection, and a
real loopback in which a second aiortc peer in this process plays the browser
and receives decoded media over ICE on localhost.

Clips are generated with ffmpeg rather than checked in, so what is decoded is
a real H.264/AAC MP4 of the kind the animator writes.
"""

import asyncio
import shutil
import socket
import subprocess
import threading
import time

import numpy as np
import pytest

import app.services.webrtc as rtc
from app.config import settings
from app.services.webrtc import (
    AUDIO_FRAME_SAMPLES,
    AUDIO_RATE,
    AvatarStream,
    ClipDropped,
    decode_clip,
    load_idle_frame,
)

pytestmark = [
    pytest.mark.skipif(not rtc.WEBRTC_AVAILABLE, reason="aiortc is not installed"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"),
]

SIZE = 256  # smaller than production's 512: same code paths, a quarter the pixels
FPS = 25
SAMPLES_PER_FRAME = AUDIO_RATE // FPS  # 1920


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True, timeout=60)


def _clip(path, *, colour="red", size="320x240", fps=25, video=1.0, audio=1.0) -> str:
    """An MP4 of a flat colour and a 440 Hz tone; either half may be left out."""
    args: list[str] = []
    if video:
        args += ["-f", "lavfi", "-i", f"color=c={colour}:s={size}:r={fps}:d={video}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={audio}"]
    if video:
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac"]
    _ffmpeg(*args, str(path))
    return str(path)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("rtc")
    # Portrait, so fitting it into the square has to pad the sides.
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=blue:s=300x400:d=1", "-frames:v", "1", str(d / "idle.png")
    )

    class Media:
        idle = str(d / "idle.png")
        # 4:3 at a different size from the stream: scaled AND letterboxed.
        clip = _clip(d / "clip.mp4")
        short = _clip(d / "short.mp4", video=0.4, audio=0.4)
        fps30 = _clip(d / "fps30.mp4", fps=30)
        silent = _clip(d / "silent.mp4", audio=0)
        long_audio = _clip(d / "long_audio.mp4", video=0.6, audio=1.0)
        audio_only = _clip(d / "audio_only.m4a", video=0, audio=0.5)
        garbage = str(d / "garbage.mp4")

    (d / "garbage.mp4").write_bytes(b"this is not an mp4" * 64)
    return Media


@pytest.fixture
async def stream(media):
    s = await AvatarStream.create(media.idle, size=SIZE, fps=FPS)
    yield s
    await s.close()


def _rgb(frame) -> np.ndarray:
    """Mean R, G, B of the middle of a frame (clear of any letterbox bars)."""
    img = frame.to_ndarray(format="rgb24")
    h, w = img.shape[:2]
    return img[h // 3 : 2 * h // 3, w // 3 : 2 * w // 3].reshape(-1, 3).mean(axis=0)


def _is_blue(frame) -> bool:
    r, _, b = _rgb(frame)
    return b > r + 60


def _is_red(frame) -> bool:
    r, _, b = _rgb(frame)
    return r > b + 60


def _level(frame) -> float:
    return float(np.abs(frame.to_ndarray().astype(np.int32)).mean())


async def _pump(stream, *, until, timeout=5.0):
    """
    Pull both tracks in real time, as aiortc's senders would, until `until()`
    is true. Returns (video frames, audio frames).
    """
    video, audio = [], []

    async def pull(track, out):
        while not until():
            out.append(await track.recv())

    await asyncio.wait_for(
        asyncio.gather(pull(stream.video, video), pull(stream.audio, audio)), timeout
    )
    return video, audio


# ── decoding ─────────────────────────────────────────────────────────────


def test_the_idle_image_is_fitted_into_the_square_without_distortion(media):
    idle = load_idle_frame(media.idle, SIZE)
    assert idle.shape == (SIZE * 3 // 2, SIZE)
    luma = idle[:SIZE]
    # 300x400 in a 256 square is 192 wide: black bars either side of it.
    assert luma[:, :24].max() <= 20 and luma[:, -24:].max() <= 20
    assert luma[:, SIZE // 2].min() > 20, "the picture itself must reach top to bottom"


@pytest.mark.parametrize("name", ["clip", "short", "fps30", "silent", "long_audio", "audio_only"])
def test_every_clip_decodes_to_exactly_equal_audio_and_video(media, name):
    idle = load_idle_frame(media.idle, SIZE)
    frames, samples = decode_clip(getattr(media, name), SIZE, FPS, idle)
    assert frames and all(f.shape == (SIZE * 3 // 2, SIZE) for f in frames)
    assert samples.dtype == np.int16
    assert len(samples) == len(frames) * SAMPLES_PER_FRAME


def test_a_clip_at_another_frame_rate_keeps_its_real_time_length(media):
    """30 frames at 30 fps is one second, so 25 frames on a 25 fps stream."""
    idle = load_idle_frame(media.idle, SIZE)
    frames, samples = decode_clip(media.fps30, SIZE, FPS, idle)
    assert len(frames) == 25
    assert len(samples) == AUDIO_RATE
    # And the other way: a 25 fps clip on a 50 fps stream doubles its frames.
    frames, samples = decode_clip(media.clip, SIZE, 50, idle)
    assert len(frames) == 50 and len(samples) == AUDIO_RATE


def test_missing_halves_are_filled_in(media):
    idle = load_idle_frame(media.idle, SIZE)

    frames, samples = decode_clip(media.silent, SIZE, FPS, idle)
    assert len(frames) == 25 and not samples.any(), "no audio stream means silence"

    frames, samples = decode_clip(media.audio_only, SIZE, FPS, idle)
    assert len(frames) == 13, "0.5 s of audio needs 13 frames at 25 fps to cover it"
    assert all(f is idle for f in frames), "no video stream means the still face"
    assert np.abs(samples[:20000]).mean() > 500


def test_audio_longer_than_the_video_is_trimmed_to_it(media):
    idle = load_idle_frame(media.idle, SIZE)
    frames, samples = decode_clip(media.long_audio, SIZE, FPS, idle)
    assert len(frames) == 15
    assert len(samples) == 15 * SAMPLES_PER_FRAME
    assert np.abs(samples[-SAMPLES_PER_FRAME:]).mean() > 500, "trimmed, not padded"


def test_a_file_that_is_not_media_raises(media):
    idle = load_idle_frame(media.idle, SIZE)
    with pytest.raises(Exception):
        decode_clip(media.garbage, SIZE, FPS, idle)


def test_an_over_long_clip_is_refused_rather_than_decoded_into_memory(media, monkeypatch):
    idle = load_idle_frame(media.idle, SIZE)
    fitted: list = []
    real = rtc._fit_square
    monkeypatch.setattr(rtc, "_fit_square", lambda *a: fitted.append(1) or real(*a))
    with pytest.raises(ValueError, match="longer than"):
        decode_clip(media.clip, SIZE, FPS, idle, max_seconds=0.5)
    assert fitted == [], "refused from the file's header, before a single frame is decoded"


def test_a_clip_that_hides_its_length_is_still_refused(media, monkeypatch):
    """The in-loop backstop, for containers that do not state a duration."""
    import av

    idle = load_idle_frame(media.idle, SIZE)
    real_open = av.open

    class NoDuration:
        def __init__(self, container):
            self._c = container

        duration = None

        def __getattr__(self, name):
            return getattr(self._c, name)

        def __enter__(self):
            self._c.__enter__()
            return self

        def __exit__(self, *exc):
            return self._c.__exit__(*exc)

    monkeypatch.setattr(rtc.av, "open", lambda path: NoDuration(real_open(path)))
    with pytest.raises(ValueError, match="longer than"):
        decode_clip(media.clip, SIZE, FPS, idle, max_seconds=0.5)


def test_the_clip_cap_sits_just_above_the_longest_sentence():
    """
    A clip is held in memory whole, so this is a per-session memory bound
    (~10 MB a second at 512x512/25). Sentences are cut at 200 characters,
    about 15 s of speech; raising the cap far past that buys nothing.
    """
    assert 15 < rtc.MAX_CLIP_SECONDS <= 20


# ── the tracks, with no peer connection ──────────────────────────────────


async def test_an_empty_stream_carries_the_still_face_and_silence(stream):
    v1, a1 = await stream.video.recv(), await stream.audio.recv()
    v2, a2 = await stream.video.recv(), await stream.audio.recv()

    assert (v1.width, v1.height, v1.format.name) == (SIZE, SIZE, "yuv420p")
    assert _is_blue(v1)
    assert v1.time_base.denominator == 90000 and v2.pts - v1.pts == 90000 // FPS
    assert v1 is not v2, "the encoder writes to the frame it is given; never share one"

    assert (a1.samples, a1.sample_rate, a1.format.name, a1.layout.name) == (
        AUDIO_FRAME_SAMPLES,
        AUDIO_RATE,
        "s16",
        "mono",
    )
    assert _level(a1) == 0
    assert a2.pts - a1.pts == AUDIO_FRAME_SAMPLES
    assert not stream.speaking


async def test_frames_are_paced_in_real_time_without_drift(stream):
    await stream.video.recv()
    started = time.monotonic()
    for _ in range(10):
        await stream.video.recv()
    elapsed = time.monotonic() - started
    assert 0.36 <= elapsed <= 0.50, f"10 frames at 25 fps took {elapsed:.3f}s"


@pytest.mark.parametrize("kind", ["video", "audio"])
async def test_a_late_recv_catches_up_to_the_grid_instead_of_shifting_it(stream, kind):
    """
    What "without drift" means: every deadline comes from the epoch and the
    frame index. A track that slept one period after each wake-up instead
    would pass the test above (ten frames still take 0.4 s) while losing
    every stall, and every millisecond of timer overshoot, for good — and
    the two tracks would lose them separately.

    So: stall, then check the frames after it are handed out at once until
    the track is back where it would have been.
    """
    track = getattr(stream, kind)
    period = float(track._period)
    step = 90000 // FPS if kind == "video" else AUDIO_FRAME_SAMPLES
    n = round(0.4 / period)  # 10 video frames, 20 audio frames

    first = await track.recv()
    t0, p0 = time.monotonic(), track.position
    await asyncio.sleep(0.2)  # the loop was busy: half of those frames are overdue

    frames, took = [], []
    for _ in range(n):
        before = time.monotonic()
        frames.append(await track.recv())
        took.append(time.monotonic() - before)
    elapsed = time.monotonic() - t0

    # On the grid the nth frame after `first` is due n periods after it,
    # stall or no stall. Sleeping a period per frame would end at 0.6 s.
    assert n * period - 0.03 <= elapsed <= n * period + 0.06, f"ended at {elapsed:.3f}s"
    overdue = round(0.2 / period) - 1
    assert max(took[:overdue]) < 0.01, "overdue frames must not be waited for"
    assert track.position == p0 + n
    pts = [first.pts] + [f.pts for f in frames]
    assert [b - a for a, b in zip(pts, pts[1:])] == [step] * n, "pts stay index-based"


async def test_an_idle_stream_drops_to_a_low_frame_rate_and_back(stream, media, monkeypatch):
    """
    A still picture at 25 fps is 25 encodes a second for nothing. After a
    second of it only every fifth slot carries a frame — on the same grid —
    and a clip gets full rate back from its very first frame.
    """
    tick = 90000 // FPS
    step = FPS // rtc.IDLE_FPS  # every 5th slot

    frames = [await stream.video.recv()]
    started = time.monotonic()
    while time.monotonic() - started < 2.0:
        frames.append(await stream.video.recv())
    pts = [f.pts for f in frames]
    gaps = [b - a for a, b in zip(pts, pts[1:])]

    grace = FPS  # one second at full rate first
    assert gaps[: grace - 1] == [tick] * (grace - 1)
    resting = gaps[grace + 1 :]
    assert resting and set(resting) == {step * tick}, f"expected 5 fps, got gaps {resting}"
    assert all(p % (step * tick) == 0 for p in pts[grace + 2 :]), "same grid, fewer slots"
    assert 4 <= len(resting) <= 6, "about five frames in the second second"
    assert all(_is_blue(f) for f in frames[-3:])
    # Real time is unchanged: 2 s of wall clock is 2 s of timestamps.
    assert abs((pts[-1] - pts[0]) / 90000 - 2.0) < 0.25

    # A clip arrives while the track is resting between two idle frames.
    await stream.audio.recv()
    queued_at = time.monotonic()
    await stream.enqueue_clip(media.short)
    decoded_at = time.monotonic()
    first = await stream.video.recv()
    assert _is_red(first), "the next frame out is the clip's first, not another still"
    assert time.monotonic() - decoded_at < 1.5 / FPS + 0.02, (
        f"it waited for an idle interval ({time.monotonic() - queued_at:.3f}s)"
    )
    assert first.pts % tick == 0 and first.pts > pts[-1]

    clip = [first] + [await stream.video.recv() for _ in range(9)]
    assert all(map(_is_red, clip))
    assert [b.pts - a.pts for a, b in zip(clip, clip[1:])] == [tick] * 9, "full rate while speaking"

    # And the pause after a sentence stays at full rate (the grace period):
    # replies do not flicker between rates in the gaps between sentences.
    after = [await stream.video.recv() for _ in range(5)]
    assert all(map(_is_blue, after))
    assert [b.pts - a.pts for a, b in zip([clip[-1]] + after, after)] == [tick] * 5


async def test_audio_keeps_its_full_rate_while_the_picture_rests(stream):
    """Only the video is thinned: audio frames must stay contiguous."""
    for _ in range(FPS + 10):  # past the grace period
        await stream.video.recv()
    a = [await stream.audio.recv() for _ in range(5)]
    assert [y.pts - x.pts for x, y in zip(a, a[1:])] == [AUDIO_FRAME_SAMPLES] * 4


async def test_a_clip_plays_then_the_stream_returns_to_idle(stream, media):
    """Idle -> clip -> idle, with the speaking signal fired once each way."""
    events: list[bool] = []
    stream.on_speaking = events.append

    before_v, before_a = await _pump(stream, until=lambda: stream.video.position is not None)
    duration_ms = await stream.enqueue_clip(media.short)
    assert duration_ms == 400
    assert stream.speaking and events == [True]

    during_v, during_a = await _pump(stream, until=lambda: not stream.speaking)
    resumed = stream.video.position
    after_v, after_a = await _pump(stream, until=lambda: stream.video.position > resumed + 1)

    assert events == [True, False], "one True and one False per burst of speech"

    red = [f for f in during_v if _is_red(f)]
    assert len(red) == 10, "all ten frames of the 0.4 s clip, and nothing else"
    assert all(_is_blue(f) for f in before_v + after_v)
    # Letterboxed, not stretched: 320x240 in a square leaves bars top and bottom.
    assert red[0].to_ndarray(format="rgb24")[:16].mean() < 30

    loud = [f for f in during_a if _level(f) > 500]
    assert 18 <= len(loud) <= 20, f"0.4 s of tone is 20 audio frames, got {len(loud)}"
    assert all(_level(f) == 0 for f in before_a + after_a)

    for frames in (before_v + during_v + after_v, before_a + during_a + after_a):
        pts = [f.pts for f in frames]
        assert all(b > a for a, b in zip(pts, pts[1:])), "pts must never repeat or go back"


async def test_audio_and_video_start_a_clip_on_the_same_instant(stream, media):
    """
    The tracks tick at different rates, so after a pause they are rarely at
    the same point. Queueing must level them before the clip, or its sound
    leads or trails its picture by up to a frame for the whole burst.
    """
    await stream.video.recv()
    for _ in range(3):  # leave audio a frame and a half ahead of video
        await stream.audio.recv()
    await stream.enqueue_clip(media.short)

    video_end = (stream.video.position + stream.queued_video_frames) * SAMPLES_PER_FRAME
    audio_end = stream.audio.position * AUDIO_FRAME_SAMPLES + stream.queued_audio_samples
    assert video_end == audio_end
    assert stream.queued_video_frames == 10 + 1, "one idle frame to wait for the audio"


async def test_twenty_clips_accumulate_no_drift(stream, media, monkeypatch):
    """A 30 ms mismatch per clip would be 0.6 s of lip-sync error by the end."""
    stream._max_queue_seconds = 1e9
    await stream.video.recv()
    await stream.audio.recv()
    for i in range(20):
        # Clips whose own audio is longer, shorter and absent, interleaved.
        await stream.enqueue_clip((media.short, media.long_audio, media.silent)[i % 3])

    video_end = (stream.video.position + stream.queued_video_frames) * SAMPLES_PER_FRAME
    audio_end = stream.audio.position * AUDIO_FRAME_SAMPLES + stream.queued_audio_samples
    assert video_end == audio_end, "audio and video must end on the same sample"
    assert stream.queued_video_frames >= 7 * 10 + 7 * 15 + 6 * 25


@pytest.mark.parametrize("fps", [18, 27, 45, 7])
async def test_a_frame_rate_that_does_not_divide_48000_adds_no_padding_mid_burst(media, fps):
    """
    At 18 fps a frame is 2666.67 samples. Each clip's audio is a whole number
    of samples, so every clip used to leave a fraction behind; a few clips
    in, the levelling took the sum for a real offset and put a still frame
    and most of a frame of silence in the middle of the reply.
    """
    idle = load_idle_frame(media.idle, SIZE)
    stream = AvatarStream(idle, size=SIZE, fps=fps)
    try:
        await stream.video.recv()
        await stream.audio.recv()
        per_frame = rtc.fractions.Fraction(AUDIO_RATE, fps)
        rng = np.random.default_rng(7)

        def ends():
            video = (stream.video.position + stream.queued_video_frames) * per_frame
            audio = stream.audio.position * AUDIO_FRAME_SAMPLES + stream.queued_audio_samples
            return video, audio

        # The first clip is levelled against wherever the tracks have got to;
        # everything after it is mid-burst.
        stream._append([idle] * fps, np.zeros(round(fps * per_frame), np.int16))
        frames_before, samples_before = stream.queued_video_frames, stream.queued_audio_samples
        total_frames = 0
        for _ in range(40):
            n = int(rng.integers(fps, 5 * fps))  # 1-5 s
            own = round(n * per_frame)  # what decode_clip hands over
            before = stream.queued_audio_samples
            stream._append([idle] * n, np.ones(own, np.int16))
            total_frames += n
            assert abs(stream.queued_audio_samples - before - own) <= 1, "a sample at most"
            video, audio = ends()
            assert abs(video - audio) <= rtc.fractions.Fraction(1, 2), "the remainder is carried"

        assert stream.queued_video_frames == frames_before + total_frames, "no still frames added"
        # The audio is as long as the VIDEO it belongs to, to the sample —
        # which is not the sum of the clips' own rounded lengths.
        added = stream.queued_audio_samples - samples_before
        assert abs(added - total_frames * per_frame) <= 1
    finally:
        await stream.close()


# ── which clips have played ──────────────────────────────────────────────


async def test_the_stream_accounts_for_every_clip_exactly_once(stream, media):
    """
    A played to the end, B part-way through, C untouched, and the stream
    goes down: A was released when it finished, B and C are handed back —
    B whole — and neither is played from the queue as well.
    """
    released: list = []
    stream.on_clip_released = released.append
    await stream.video.recv()
    await stream.audio.recv()
    for token in "ABC":
        assert await stream.enqueue_clip(media.short, token) == 400
    assert stream.queued_clips == ["A", "B", "C"] and released == []

    await _pump(stream, until=lambda: stream.queued_video_frames <= 15 and released == ["A"])
    assert stream.queued_clips == ["B", "C"]
    assert stream.queued_video_frames >= 11, "B is part-way through, not finished"

    assert stream.retire() == ["B", "C"]
    assert released == ["A"], "handing a clip back is not releasing it"
    assert stream.queued_video_frames == 0 and stream.queued_audio_samples == 0
    assert not stream.speaking and stream.queued_clips == []
    assert _is_blue(await stream.video.recv()), "nothing handed back is still played"

    assert stream.retire() == [], "and nothing is handed back twice"
    with pytest.raises(RuntimeError):
        await stream.enqueue_clip(media.short, "D")
    assert stream.flush() is False and released == ["A"]
    await stream.close()
    assert stream.retire() == []


async def test_a_clip_is_released_only_when_both_its_halves_have_played(stream, media):
    released: list = []
    stream.on_clip_released = released.append
    await stream.video.recv()
    await stream.audio.recv()
    await stream.enqueue_clip(media.short, "A")
    while stream.queued_video_frames:
        await stream.video.recv()
    assert released == [], "its audio is still to come"
    while stream.queued_audio_samples:
        await stream.audio.recv()
    assert released == ["A"]


async def test_flushed_clips_are_released_not_kept_for_recovery(stream, media):
    """The user interrupted them: their files go, and they are never re-sent."""
    released: list = []
    stream.on_clip_released = released.append
    for token in "AB":
        await stream.enqueue_clip(media.short, token)
    assert stream.flush() is True
    assert released == ["A", "B"]
    assert stream.retire() == []


async def test_close_alone_hands_nothing_back(media):
    stream = await AvatarStream.create(media.idle, size=SIZE, fps=FPS)
    released: list = []
    stream.on_clip_released = released.append
    await stream.enqueue_clip(media.short, "A")
    await stream.close()
    assert stream.retire() == [] and released == []


async def test_wait_drained_returns_when_the_queued_speech_has_played(stream, media):
    assert await stream.wait_drained() is True, "nothing queued: no wait"
    await stream.video.recv()
    await stream.audio.recv()
    await stream.enqueue_clip(media.short, "A")
    waiter = asyncio.create_task(stream.wait_drained())
    await asyncio.sleep(0.1)
    assert not waiter.done()
    await _pump(stream, until=waiter.done)
    assert await waiter is True and not stream.speaking

    # Nobody pulling: it gives up once the queue should have emptied.
    await stream.enqueue_clip(media.short, "B")
    started = time.monotonic()
    assert await stream.wait_drained(margin=0.1) is False
    assert 0.4 <= time.monotonic() - started < 1.0
    # A flush ends the wait too.
    waiter = asyncio.create_task(stream.wait_drained())
    await asyncio.sleep(0.05)
    stream.flush()
    assert await asyncio.wait_for(waiter, 1) is True


# ── a stream that is no longer being sent ────────────────────────────────


async def test_a_stopped_track_fails_the_stream_at_once(stream, media):
    """
    aiortc's sender stops the track it reads when its loop dies, and leaves
    the peer connection "connected". Clips used to be accepted and announced
    as streamed regardless, until the queue filled and the turn hung.
    """
    states: list[str] = []
    stream.on_state = states.append
    await stream.enqueue_clip(media.short, "A")

    stream.video.stop()
    assert states == ["failed"], "the owner is told, so the client can be put back on clips"
    assert not stream.connected
    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc:
        await stream.enqueue_clip(media.short, "B")
    assert not isinstance(exc.value, ClipDropped), "a failure, so the clip falls back"
    assert time.monotonic() - started < 0.1, "refused before decoding, not after a wait"
    assert stream.retire() == ["A"], "what it held is still there to be recovered"


async def test_connected_means_both_tracks_are_live_not_just_the_peer_connection(stream):
    """Checked directly, not only through the track's own "ended" signal."""
    from types import SimpleNamespace

    async def close():
        pass

    stream._pc = SimpleNamespace(connectionState="connected", close=close)
    assert stream.connected
    stream.audio.remove_all_listeners("ended")
    stream.audio.stop()
    assert not stream.connected


async def test_a_caller_waiting_for_room_is_released_when_a_track_stops(media):
    stream = await AvatarStream.create(media.idle, size=SIZE, fps=FPS, max_queue_seconds=0.5)
    try:
        await stream.enqueue_clip(media.clip, "A")
        waiting = asyncio.create_task(stream.enqueue_clip(media.short, "B"))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        stream.audio.stop()
        with pytest.raises(RuntimeError, match="no longer being sent"):
            await asyncio.wait_for(waiting, 1)
        assert stream.queued_clips == ["A"]
    finally:
        await stream.close()


async def test_a_queue_that_does_not_drain_fails_instead_of_hanging(media, monkeypatch):
    """
    The wait for room is for a known time — the queue empties in real time —
    so one that overruns means nobody is pulling the tracks. The turn must
    get an error it can fall back on, not wait for ever.
    """
    monkeypatch.setattr(rtc, "ROOM_WAIT_MARGIN_SECONDS", 0.3)
    stream = await AvatarStream.create(media.idle, size=SIZE, fps=FPS, max_queue_seconds=0.5)
    states: list[str] = []
    stream.on_state = states.append
    try:
        await stream.enqueue_clip(media.clip, "A")  # 1 s queued, 0.5 s over the cap
        started = time.monotonic()
        with pytest.raises(RuntimeError) as exc:
            await asyncio.wait_for(stream.enqueue_clip(media.short, "B"), 5)
        elapsed = time.monotonic() - started
        assert not isinstance(exc.value, ClipDropped)
        assert 0.7 <= elapsed < 1.5, f"waited {elapsed:.2f}s for 0.5 s of room plus 0.3 s margin"
        assert states == ["failed"] and not stream.connected
        assert stream.queued_clips == ["A"], "B was never queued; A is still recoverable"
        with pytest.raises(RuntimeError):
            await stream.enqueue_clip(media.short, "C")
    finally:
        await stream.close()


async def test_a_slow_but_draining_queue_is_not_mistaken_for_a_dead_one(media, monkeypatch):
    monkeypatch.setattr(rtc, "ROOM_WAIT_MARGIN_SECONDS", 0.3)
    stream = await AvatarStream.create(media.idle, size=SIZE, fps=FPS, max_queue_seconds=0.5)
    states: list[str] = []
    stream.on_state = states.append
    try:
        await stream.enqueue_clip(media.clip, "A")
        waiting = asyncio.create_task(stream.enqueue_clip(media.short, "B"))
        await _pump(stream, until=waiting.done, timeout=5)
        assert await waiting == 400 and states == []
    finally:
        await stream.close()


async def test_flush_returns_to_idle_at_once(stream, media):
    events: list[bool] = []
    stream.on_speaking = events.append
    await stream.enqueue_clip(media.clip)
    assert _is_red(await stream.video.recv())

    assert stream.flush() is True
    assert stream.queued_video_frames == 0 and stream.queued_audio_samples == 0
    assert _is_blue(await stream.video.recv()), "the very next frame is the still face"
    assert _level(await stream.audio.recv()) == 0
    assert events == [True, False]
    assert stream.flush() is False, "nothing to cut off the second time"
    assert events == [True, False]


async def test_a_clip_still_decoding_when_the_stream_is_flushed_is_never_queued(
    stream, media, monkeypatch
):
    """
    Barge-in while the next sentence is in the decoder thread. The thread
    cannot be stopped, so its result must be thrown away when it lands.
    """
    decoding, release = threading.Event(), threading.Event()
    real = rtc.decode_clip

    def slow(*args, **kwargs):
        decoding.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(rtc, "decode_clip", slow)
    task = asyncio.create_task(stream.enqueue_clip(media.short))
    await asyncio.to_thread(decoding.wait, 5)

    stream.flush()
    release.set()
    with pytest.raises(ClipDropped):
        await task
    assert stream.queued_video_frames == 0 and stream.queued_audio_samples == 0
    assert not stream.speaking


async def test_a_full_queue_makes_the_caller_wait_and_the_wait_can_be_cancelled(media):
    stream = await AvatarStream.create(media.idle, size=SIZE, fps=FPS, max_queue_seconds=0.5)
    try:
        await stream.enqueue_clip(media.clip)  # 1 s queued: over the cap
        blocked = asyncio.create_task(stream.enqueue_clip(media.short))
        await asyncio.sleep(0.2)
        assert not blocked.done(), "enqueue must wait while the queue is over its cap"
        assert stream.queued_video_frames == 25, "and must not have queued anything"

        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        assert stream.queued_video_frames == 25

        # Draining below the cap lets the next one through.
        waiting = asyncio.create_task(stream.enqueue_clip(media.short))
        await _pump(stream, until=waiting.done, timeout=5)
        assert await waiting == 400
        assert 10 <= stream.queued_video_frames < 25
    finally:
        await stream.close()


async def test_a_flush_releases_a_waiting_caller_without_queueing_its_clip(media):
    stream = await AvatarStream.create(media.idle, size=SIZE, fps=FPS, max_queue_seconds=0.5)
    try:
        await stream.enqueue_clip(media.clip)
        blocked = asyncio.create_task(stream.enqueue_clip(media.short))
        await asyncio.sleep(0.05)
        stream.flush()
        with pytest.raises(ClipDropped):
            await asyncio.wait_for(blocked, 2)
        assert stream.queued_video_frames == 0
    finally:
        await stream.close()


async def test_an_undecodable_clip_raises_and_queues_nothing(stream, media):
    with pytest.raises(Exception) as exc:
        await stream.enqueue_clip(media.garbage)
    assert not isinstance(exc.value, ClipDropped), "a bad file is a failure, not an interruption"
    assert stream.queued_video_frames == 0 and not stream.speaking


async def test_an_async_speaking_callback_is_scheduled_not_awaited(stream, media):
    seen: list[bool] = []

    async def on_speaking(active):
        await asyncio.sleep(0)
        seen.append(active)

    stream.on_speaking = on_speaking
    await stream.enqueue_clip(media.short)
    stream.flush()
    assert seen == [], "recv and flush must not wait on the owner's callback"
    await asyncio.sleep(0.05)
    assert seen == [True, False]


async def test_close_is_idempotent_and_final(media):
    stream = await AvatarStream.create(media.idle, size=SIZE, fps=FPS)
    await stream.enqueue_clip(media.short)
    await stream.close()
    await stream.close()
    assert stream.closed and not stream.connected
    assert stream.queued_video_frames == 0
    with pytest.raises(RuntimeError):
        await stream.enqueue_clip(media.short)
    with pytest.raises(rtc.MediaStreamError):
        await stream.video.recv()
    with pytest.raises(RuntimeError):
        await stream.handle_offer("v=0")


# ── configuration ────────────────────────────────────────────────────────


def test_ice_servers_are_parsed_with_credentials_only_where_given(monkeypatch):
    monkeypatch.setattr(
        settings,
        "WEBRTC_ICE_SERVERS",
        '[{"urls": "stun:stun.example.com:3478"},'
        ' {"urls": ["turn:turn.example.com:3478"], "username": "u", "credential": "p"}]',
    )
    assert rtc.ice_servers() == [
        {"urls": ["stun:stun.example.com:3478"]},
        {"urls": ["turn:turn.example.com:3478"], "username": "u", "credential": "p"},
    ]


@pytest.mark.parametrize("bad", ["not json", '{"urls": "stun:x"}', '[{"url": "stun:x"}]', "[1]"])
def test_malformed_ice_servers_mean_no_servers_not_a_crash(monkeypatch, bad):
    monkeypatch.setattr(settings, "WEBRTC_ICE_SERVERS", bad)
    assert rtc.ice_servers() == []
    assert rtc.media_descriptor()["ice_servers"] == []


def test_webrtc_is_offered_only_when_configured_and_importable(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "clips")
    assert rtc.media_descriptor()["webrtc"] is False, "off unless asked for"
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    assert rtc.media_descriptor()["webrtc"] is True
    monkeypatch.setattr(rtc, "WEBRTC_AVAILABLE", False)
    assert rtc.media_descriptor()["webrtc"] is False


# ── a real peer connection, over ICE on localhost ────────────────────────


def _udp_loopback_works() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a:
            a.bind(("127.0.0.1", 0))
            a.settimeout(1)
            a.sendto(b"x", a.getsockname())
            return a.recv(1) == b"x"
    except OSError:
        return False


# Skipped only where the sandbox forbids UDP altogether; on a developer
# machine or a CI runner these run.
needs_udp = pytest.mark.skipif(not _udp_loopback_works(), reason="no UDP loopback here")


class Browser:
    """The receiving end: what a browser does with `ready.media`, in aiortc."""

    def __init__(self):
        from aiortc import RTCConfiguration, RTCPeerConnection

        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.video: list = []
        self.audio: list = []
        self._tasks: list[asyncio.Task] = []
        self.pc.addTransceiver("audio", direction="recvonly")
        self.pc.addTransceiver("video", direction="recvonly")

        @self.pc.on("track")
        def on_track(track):
            self._tasks.append(asyncio.create_task(self._read(track)))

    async def _read(self, track):
        out = self.video if track.kind == "video" else self.audio
        try:
            while True:
                out.append(await track.recv())
        except Exception:
            pass  # the track ended with the connection

    async def offer(self) -> str:
        # aiortc gathers inside setLocalDescription, so this is already the
        # full, non-trickled SDP the wire contract asks the browser for.
        await self.pc.setLocalDescription(await self.pc.createOffer())
        return self.pc.localDescription.sdp

    async def accept(self, answer_sdp: str) -> None:
        from aiortc import RTCSessionDescription

        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

    async def wait_for(self, what: str, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out waiting for {what}")

    def mark(self) -> tuple[int, int]:
        return len(self.video), len(self.audio)

    async def close(self):
        for t in self._tasks:
            t.cancel()
        await self.pc.close()


@needs_udp
async def test_a_browser_receives_idle_then_the_clip_then_idle_again(media, monkeypatch):
    monkeypatch.setattr(settings, "WEBRTC_ICE_SERVERS", "[]")  # host candidates only
    states: list[str] = []
    speaking: list[bool] = []
    stream = await AvatarStream.create(media.idle, size=SIZE, fps=FPS)
    stream.on_state = states.append
    stream.on_speaking = speaking.append
    browser = Browser()
    try:
        answer = await stream.handle_offer(await browser.offer())
        assert "a=candidate" in answer, "the answer must carry its candidates: there is no trickle"
        assert answer.count("a=sendonly") == 2, "one outgoing audio and one outgoing video track"
        await browser.accept(answer)

        await browser.wait_for("the connection", lambda: stream.connected)
        assert "connected" in states

        # 1. Nothing queued: the still face, in silence.
        await browser.wait_for("idle video", lambda: len(browser.video) >= 5)
        await browser.wait_for("idle audio", lambda: len(browser.audio) >= 10)
        idle = browser.video[-1]
        assert (idle.width, idle.height) == (SIZE, SIZE)
        assert _is_blue(idle), _rgb(idle)
        assert max(_level(f) for f in browser.audio[-5:]) < 50

        # 2. A clip: its picture and its sound both arrive.
        v0, a0 = browser.mark()
        await stream.enqueue_clip(media.clip)
        await stream.enqueue_clip(media.clip)
        await browser.wait_for("the clip's picture", lambda: any(map(_is_red, browser.video[v0:])))
        await browser.wait_for(
            "the clip's sound", lambda: any(_level(f) > 500 for f in browser.audio[a0:])
        )
        assert speaking == [True]

        # 3. Interrupted mid-clip: back to the still face without waiting
        # out the second of media that was still queued.
        assert stream.queued_seconds > 0.5
        stream.flush()
        v1, a1 = browser.mark()
        await browser.wait_for(
            "idle after the flush",
            lambda: (
                len(browser.video) > v1 + 3
                and all(map(_is_blue, browser.video[-3:]))
                and len(browser.audio) > a1 + 10
                and max(_level(f) for f in browser.audio[-5:]) < 50
            ),
            timeout=1.5,
        )
        assert speaking == [True, False]
    finally:
        await browser.close()
        await stream.close()


@needs_udp
async def test_a_connected_stream_whose_track_has_stopped_does_not_report_connected(
    media, monkeypatch
):
    monkeypatch.setattr(settings, "WEBRTC_ICE_SERVERS", "[]")
    states: list[str] = []
    stream = await AvatarStream.create(media.idle, size=SIZE, fps=FPS)
    stream.on_state = states.append
    browser = Browser()
    try:
        await browser.accept(await stream.handle_offer(await browser.offer()))
        await browser.wait_for("the connection", lambda: stream.connected)
        await browser.wait_for("media", lambda: len(browser.video) >= 3)
        await stream.enqueue_clip(media.clip, "A")

        stream.video.stop()  # what RTCRtpSender does when its loop dies
        assert stream._pc.connectionState == "connected", "aiortc itself notices nothing"
        assert not stream.connected
        assert states[-1] == "failed"
        with pytest.raises(RuntimeError):
            await stream.enqueue_clip(media.short, "B")
        assert stream.retire() == ["A"]
    finally:
        await browser.close()
        await stream.close()


@needs_udp
async def test_a_resting_stream_still_reaches_the_browser_and_wakes_for_a_clip(media, monkeypatch):
    """The low idle rate through a real encoder, packetiser and decoder."""
    monkeypatch.setattr(settings, "WEBRTC_ICE_SERVERS", "[]")
    stream = await AvatarStream.create(media.idle, size=SIZE, fps=FPS)
    browser = Browser()
    try:
        await browser.accept(await stream.handle_offer(await browser.offer()))
        await browser.wait_for("the connection", lambda: stream.connected)
        await browser.wait_for("resting", lambda: stream.video._idle_slots > 2 * FPS)

        v0, _ = browser.mark()
        await asyncio.sleep(1.0)
        got = len(browser.video) - v0
        assert 3 <= got <= 8, f"about 5 frames a second while idle, got {got}"
        assert _is_blue(browser.video[-1])

        v1, _ = browser.mark()
        await stream.enqueue_clip(media.clip)
        await browser.wait_for(
            "the clip at full rate",
            lambda: sum(map(_is_red, browser.video[v1:])) >= 20,
            timeout=3,
        )
    finally:
        await browser.close()
        await stream.close()


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self, code=1000):
        pass

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == kind]


async def test_an_offer_aiortc_rejects_is_refused_not_raised(media, monkeypatch):
    """A real avatar image and an SDP that parses to nothing usable."""
    from app.websocket import ConnectionManager

    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    monkeypatch.setattr(settings, "WEBRTC_ICE_SERVERS", "[]")
    m = ConnectionManager()
    ws = FakeWS()
    m.active_connections["s1"] = ws
    m._send_locks["s1"] = asyncio.Lock()
    m.session_data["s1"] = {"messages": [], "avatar_image_local": media.idle, "user_id": "u1"}

    for sdp in (
        "v=0\r\n",
        "garbage",
        "v=0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n",
    ):
        await m.handle_webrtc_offer("s1", sdp)
        while m._background:
            await asyncio.gather(*list(m._background), return_exceptions=True)
    assert [x["type"] for x in ws.sent] == ["webrtc_error"] * 3
    assert "s1" not in m._streams, "a stream that never negotiated must not be left registered"


@needs_udp
async def test_the_manager_negotiates_announces_and_tears_down(media, monkeypatch, tmp_path):
    """The signalling path end to end: offer in, answer out, transport announced."""
    import app.websocket as wsmod
    from app.websocket import ConnectionManager

    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    monkeypatch.setattr(settings, "WEBRTC_ICE_SERVERS", "[]")
    monkeypatch.setattr(settings, "AVATAR_RESOLUTION", SIZE)
    monkeypatch.setattr(wsmod, "TMPDIR", tmp_path)

    m = ConnectionManager()
    ws = FakeWS()
    m.active_connections["s1"] = ws
    m._send_locks["s1"] = asyncio.Lock()
    m.session_data["s1"] = {"messages": [], "avatar_image_local": media.idle, "user_id": "u1"}

    browser = Browser()
    try:
        await m.handle_webrtc_offer("s1", await browser.offer())
        await browser.wait_for("the answer", lambda: ws.of("webrtc_answer"))
        assert not ws.of("media_transport"), "nothing is announced before the media path is up"
        assert m._live_stream("s1") is None, "and until then clips are delivered the old way"
        await browser.accept(ws.of("webrtc_answer")[0]["sdp"])

        await browser.wait_for("the announcement", lambda: ws.of("media_transport"))
        assert ws.of("media_transport") == [{"type": "media_transport", "transport": "webrtc"}]
        stream = m._live_stream("s1")
        assert stream is not None

        # Speech on the stream is reported, and a stop silences it first.
        await stream.enqueue_clip(media.clip)
        await browser.wait_for("speaking", lambda: ws.of("speaking"))
        assert await m.interrupt_active_turn("s1") is True
        assert stream.queued_video_frames == 0
        await browser.wait_for("speaking off", lambda: len(ws.of("speaking")) == 2)
        assert [s["active"] for s in ws.of("speaking")] == [True, False]
        assert ws.of("interrupted")

        # A second offer replaces the first connection and says so.
        replacement = Browser()
        try:
            await m.handle_webrtc_offer("s1", await replacement.offer())
            assert stream.closed
            assert ws.of("media_transport")[-1]["transport"] == "clips"
            await browser.wait_for("the second answer", lambda: len(ws.of("webrtc_answer")) == 2)
            newer = m._streams["s1"]
            assert newer is not stream

            await m.disconnect("s1")
            assert newer.closed and "s1" not in m._streams
        finally:
            await replacement.close()
    finally:
        await browser.close()
        for t in list(m._background):
            t.cancel()
