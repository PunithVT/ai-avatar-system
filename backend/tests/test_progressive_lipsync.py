"""
Lip-sync frames reaching the stream as they are generated
(LIPSYNC_PROGRESSIVE), layer by layer:

  the worker      — the real script, with MuseTalk stubbed out, emits each
                    batch of frames ahead of the video file;
  the animator    — hands those batches to a callback, in order, and clears
                    up after itself however the job ends;
  the stream      — a clip built batch by batch keeps every frame over its
                    own samples, and is one clip to everything that counts
                    clips;
  the pipeline    — a sentence begun early is delivered exactly once.

None of it needs a GPU or the MuseTalk models. What that leaves unproven is
the one thing the feature is for — that a real engine on a real GPU makes
frames faster than they are played.
"""

import asyncio
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

import app.websocket as wsmod
from app.config import settings
from app.services.webrtc import AUDIO_FRAME_SAMPLES, AUDIO_RATE, AvatarStream, ClipDropped
from tests.test_animator_protocol import animator  # noqa: F401  (fixture)
from tests.test_turn_pipeline import (  # noqa: F401  (fixtures)
    FakeStream,
    _chunks,
    _go_live,
    _route,
    _settle,
    rig,
    uploads,
)
from tests.test_webrtc_stream import FPS, SIZE, _ffmpeg, _pump, media  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")

WORKER = Path(__file__).resolve().parent.parent / "musetalk_worker.py"
SPF = AUDIO_RATE // FPS  # samples per frame at 25 fps: 1920


# ── the worker ───────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def stub_musetalk(tmp_path_factory):
    """
    A `musetalk` package (and `transformers`) that goes through the motions:
    FAKE_FRAMES frames come out of the "UNet" in batches, frame i a flat
    picture whose value says which frame it is.
    """
    pkg = tmp_path_factory.mktemp("stubs")
    utils = pkg / "musetalk" / "utils"
    utils.mkdir(parents=True)
    (pkg / "musetalk" / "__init__.py").write_text("")
    (utils / "__init__.py").write_text("")
    (utils / "face_parsing.py").write_text("class FaceParsing: pass\n")
    (utils / "audio_processor.py").write_text(
        textwrap.dedent(
            """
            import os
            class AudioProcessor:
                def __init__(self, **k): pass
                def get_audio_feature(self, path): return "features", 0
                def get_whisper_chunk(self, *a, **k):
                    return list(range(int(os.environ["FAKE_FRAMES"])))
            """
        )
    )
    (utils / "blending.py").write_text(
        textwrap.dedent(
            """
            def get_image(ori, res, box, mode="jaw", fp=None):
                out = ori.copy()
                out[:] = int(res.mean())
                return out
            """
        )
    )
    (utils / "preprocessing.py").write_text(
        textwrap.dedent(
            """
            import numpy as np
            coord_placeholder = (0, 0, 0, 0)
            def get_landmark_and_bbox(paths, shift):
                print("reading images...")
                return [(8, 8, 40, 40)], [np.zeros((48, 64, 3), np.uint8)]
            def read_imgs(paths): return [np.zeros((48, 64, 3), np.uint8)]
            """
        )
    )
    (utils / "utils.py").write_text(
        textwrap.dedent(
            """
            import os
            import numpy as np, torch

            class Batch:
                def __init__(self, start, n): self.start, self.n = start, n
                def to(self, **k): return self

            class _Module:
                dtype = torch.float32
                def to(self, *a, **k): return self
                def half(self): return self

            class _UNetModel(_Module):
                def __call__(self, latents, timesteps, encoder_hidden_states=None):
                    class Out: sample = latents
                    return Out

            class _VAE:
                vae = _Module()
                def get_latents_for_unet(self, crop): return "latent"
                def decode_latents(self, batch):
                    bad = int(os.environ.get("FAKE_BAD_FRAME", "-1"))
                    out = []
                    for i in range(batch.start, batch.start + batch.n):
                        # A frame the resize cannot take is skipped, not fatal.
                        out.append("unusable" if i == bad else
                                   np.full((16, 16, 3), 20 + i * 10, np.float32))
                    return out

            class _UNet: model = _UNetModel()
            class _PE(_Module):
                def __call__(self, x): return x

            def load_all_model(**k):
                print("load unet model from", k["unet_model_path"])
                return _VAE(), _UNet(), _PE()

            def datagen(whisper_chunks, vae_encode_latents, batch_size, delay_frame, device):
                n = len(whisper_chunks)
                for start in range(0, n, batch_size):
                    yield "audio", Batch(start, min(batch_size, n - start))
            """
        )
    )
    (pkg / "transformers.py").write_text(
        textwrap.dedent(
            """
            class WhisperModel:
                @classmethod
                def from_pretrained(cls, *a, **k): return cls()
                def to(self, *a, **k): return self
                def eval(self): return self
                def requires_grad_(self, *a): return self
            """
        )
    )
    return pkg


def _run_worker(stub_musetalk, tmp_path, *, frames: int, frames_dir: bool, bad_frame: int = -1):
    audio = tmp_path / "speech.wav"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=300:duration=1", str(audio))
    out = tmp_path / "out.mp4"
    job = {"id": "job1", "image": "avatar.png", "audio": str(audio), "output": str(out)}
    if frames_dir:
        job["frames_dir"] = str(tmp_path / "frames")
    init = {
        "unet_model_path": "u.pth",
        "unet_config": "u.json",
        "whisper_dir": "w",
        "vae_type": "v",
    }
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        input=json.dumps(init) + "\n" + json.dumps(job) + "\n",
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(tmp_path),
        env={
            "PYTHONPATH": str(stub_musetalk),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "FAKE_FRAMES": str(frames),
            "FAKE_BAD_FRAME": str(bad_frame),
        },
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    lines = proc.stdout.splitlines()
    assert lines[0] == "READY"
    return [json.loads(line) for line in lines[1:]], out, tmp_path / "frames"


def _mp4_frame_means(path) -> list[float]:
    import av

    with av.open(str(path)) as container:
        return [float(f.to_ndarray(format="gray").mean()) for f in container.decode(video=0)]


def test_without_a_frames_dir_the_worker_says_and_writes_nothing_extra(stub_musetalk, tmp_path):
    replies, out, frames_dir = _run_worker(stub_musetalk, tmp_path, frames=20, frames_dir=False)
    assert replies == [{"id": "job1", "status": "ok", "output": str(out)}]
    assert len(_mp4_frame_means(out)) == 20
    assert not frames_dir.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.mp4", "speech.wav"]


def test_with_a_frames_dir_each_batch_is_handed_over_before_the_video_exists(
    stub_musetalk, tmp_path
):
    replies, out, frames_dir = _run_worker(stub_musetalk, tmp_path, frames=20, frames_dir=True)

    assert [r["status"] for r in replies] == ["frames", "frames", "frames", "ok"]
    assert [r["seq"] for r in replies[:-1]] == [0, 1, 2]
    assert [r["count"] for r in replies[:-1]] == [8, 8, 4], "one file per UNet batch of 8"
    assert all(r["id"] == "job1" and (r["height"], r["width"]) == (48, 64) for r in replies[:-1])

    assert sorted(p.name for p in frames_dir.iterdir()) == [
        "000000.bgr",
        "000001.bgr",
        "000002.bgr",
    ], "and no half-written .tmp left behind"
    handed_over = np.concatenate(
        [
            np.fromfile(r["path"], np.uint8).reshape(r["count"], r["height"], r["width"], 3)
            for r in replies[:-1]
        ]
    )
    # Frame i is a flat picture of value 20 + 10*i: in order, none missing.
    assert [int(f.mean()) for f in handed_over] == [20 + 10 * i for i in range(20)]
    assert handed_over.min(axis=(1, 2, 3)).tolist() == handed_over.max(axis=(1, 2, 3)).tolist()

    # The video is made from the same frames (compared loosely: it is H.264).
    in_video = _mp4_frame_means(out)
    assert len(in_video) == 20
    assert np.all(np.diff(in_video) > 0), "the video's frames are in the same order"
    assert abs(in_video[-1] - in_video[0] - 190) < 25


def test_a_frame_that_cannot_be_composited_is_skipped_in_both(stub_musetalk, tmp_path):
    """As before the restructure: one unusable frame is dropped, not fatal."""
    replies, out, _ = _run_worker(stub_musetalk, tmp_path, frames=12, frames_dir=True, bad_frame=9)
    assert [r["count"] for r in replies[:-1]] == [8, 3]
    assert replies[-1]["status"] == "ok"
    assert len(_mp4_frame_means(out)) == 11


# ── the animator ─────────────────────────────────────────────────────────
async def _infer(animator, tmp_path, name="a.wav", on_frames=None):  # noqa: F811
    out = tmp_path / (Path(name).stem + ".mp4")
    await animator._worker_infer("img.png", name, str(out), None, on_frames)
    return out


async def test_batches_reach_the_callback_in_order_and_leave_nothing_behind(
    animator,  # noqa: F811
    tmp_path,
):
    got = []

    async def on_frames(batch):
        got.append(batch)

    try:
        out = await _infer(animator, tmp_path, on_frames=on_frames)
        assert out.exists()
        assert [b.shape for b in got] == [(4, 6, 8, 3)] * 3
        assert [b.dtype for b in got] == [np.uint8] * 3
        assert [int(b[i].mean()) for b in got for i in range(4)] == [
            k * 10 + i for k in range(3) for i in range(4)
        ]
        assert not Path(f"{out}.frames").exists(), "the hand-over directory is removed"
    finally:
        await animator.shutdown()


async def test_without_a_callback_the_job_asks_for_no_frames(animator, tmp_path, monkeypatch):  # noqa: F811
    """The worker is told nothing new, so it does nothing new."""
    sent = []
    real = json.dumps

    def spy(obj, *a, **k):
        if isinstance(obj, dict) and "image" in obj:
            sent.append(obj)
        return real(obj, *a, **k)

    monkeypatch.setattr("app.services.animator.json.dumps", spy)
    try:
        out = await _infer(animator, tmp_path)
        assert sent and all("frames_dir" not in job for job in sent)
        assert not Path(f"{out}.frames").exists()
    finally:
        await animator.shutdown()


async def test_a_callback_that_raises_does_not_break_this_job_or_the_next(animator, tmp_path):  # noqa: F811
    calls = []

    async def gives_up(batch):
        calls.append(1)
        raise ClipDropped()

    got = []

    async def takes(batch):
        got.append(batch)

    try:
        out = await _infer(animator, tmp_path, "a.wav", gives_up)
        assert out.exists(), "the job itself still finishes"
        assert len(calls) == 1, "no more frames are offered once the taker has refused"
        assert not Path(f"{out}.frames").exists(), "unread batches are cleared away too"

        await _infer(animator, tmp_path, "b.wav", takes)
        assert len(got) == 3, "and the protocol is still in step for the next job"
    finally:
        await animator.shutdown()


async def test_the_frames_directory_is_removed_when_the_worker_reports_an_error(
    animator,  # noqa: F811
    tmp_path,
):
    got = []

    async def takes(batch):
        got.append(batch)

    try:
        with pytest.raises(RuntimeError, match="out of memory"):
            await _infer(animator, tmp_path, "latefail.wav", takes)
        assert len(got) == 3, "what was made before the failure was still handed over"
        assert not (tmp_path / "latefail.mp4.frames").exists()
    finally:
        await animator.shutdown()


async def test_a_cancelled_job_clears_up_and_its_late_frames_are_not_mistaken(
    animator,  # noqa: F811
    tmp_path,
):
    """Frames of an abandoned job arrive during the next one, like its result."""
    animator._worker_env["FAKE_BATCHES"] = "6"
    animator._worker_env["FAKE_BATCH_SECS"] = "0.15"
    first, second = [], []

    async def take_first(batch):
        first.append(batch)

    async def take_second(batch):
        second.append(batch)

    try:
        task = asyncio.create_task(_infer(animator, tmp_path, "a.wav", take_first))
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        delivered = len(first)
        assert 0 < delivered < 6
        assert not (tmp_path / "a.mp4.frames").exists()

        await _infer(animator, tmp_path, "b.wav", take_second)
        assert len(first) == delivered, "the abandoned job's later frames went to nobody"
        assert len(second) == 6
        assert [int(b[0].mean()) for b in second] == [k * 10 for k in range(6)]
    finally:
        await animator.shutdown()


async def test_a_job_that_times_out_removes_its_frames_directory(animator, tmp_path, monkeypatch):  # noqa: F811
    animator._worker_env["FAKE_BATCHES"] = "40"
    animator._worker_env["FAKE_BATCH_SECS"] = "0.1"
    real_wait_for = asyncio.wait_for

    async def impatient(aw, timeout):
        return await real_wait_for(aw, 0.5)

    monkeypatch.setattr("app.services.animator.asyncio.wait_for", impatient)

    async def takes(batch):
        pass

    try:
        with pytest.raises(RuntimeError, match="timed out"):
            await _infer(animator, tmp_path, "a.wav", takes)
        assert not (tmp_path / "a.mp4.frames").exists()
    finally:
        await animator.shutdown()


async def test_the_rate_frames_are_made_at_is_measured_and_averaged(animator, tmp_path):  # noqa: F811
    animator._worker_env["FAKE_BATCHES"] = "5"
    animator._worker_env["FAKE_BATCH_SECS"] = "0.1"

    async def takes(batch):
        pass

    try:
        assert animator.lipsync_fps is None
        await _infer(animator, tmp_path, "a.wav", takes)
        first = animator.lipsync_fps
        # 20 frames, the last batch announced after ~0.4 s of the worker's pauses.
        assert first is not None and 20 < first < 80, first

        animator._worker_env_fast = True
        animator.lipsync_fps = 1000.0
        await _infer(animator, tmp_path, "b.wav", takes)
        assert 500 < animator.lipsync_fps < 600, "an average: one job does not decide it"
    finally:
        await animator.shutdown()


async def test_animate_reports_which_engine_made_the_clip(animator, tmp_path):  # noqa: F811
    async def still(image, audio, output):
        Path(output).write_text("still")
        return output

    animator._animate_simple = still
    got = []

    async def takes(batch):
        got.append(batch)

    try:
        report: dict = {}
        await animator.animate(
            "img.png", "a.wav", str(tmp_path / "a.mp4"), on_frames=takes, report=report
        )
        assert report == {"engine": "musetalk"} and len(got) == 3

        report = {}
        await animator.animate(
            "img.png", "bad.wav", str(tmp_path / "b.mp4"), on_frames=takes, report=report
        )
        assert report == {"engine": "simple"}, "the still image, after the engine failed"
        assert len(got) == 3, "and no frames for it"
    finally:
        await animator.shutdown()


# ── the stream ───────────────────────────────────────────────────────────
def _tone(path, seconds: float) -> str:
    _ffmpeg(
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-ar", "16000", str(path)
    )
    return str(path)


def _batch(n: int, value: int = 200) -> np.ndarray:
    """n red-ish BGR pictures, not square, so they are fitted like any frame."""
    frames = np.zeros((n, 120, 160, 3), np.uint8)
    frames[..., 2] = value
    return frames


@pytest.fixture
async def stream(media):  # noqa: F811
    s = await AvatarStream.create(media.idle, size=SIZE, fps=FPS)
    released = []
    s.on_clip_released = released.append
    s.released = released
    yield s
    await s.close()


def _drain(stream) -> tuple[int, int]:
    """Take everything queued off both tracks. Returns (frames, samples) taken."""
    frames = samples = 0
    while stream.queued_video_frames:
        stream._take_video()
        frames += 1
    while stream.queued_audio_samples:
        before = stream.queued_audio_samples
        stream._take_audio()
        samples += before - stream.queued_audio_samples
    return frames, samples


async def test_each_batch_is_queued_with_exactly_the_sound_that_belongs_under_it(stream, tmp_path):
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 1.0), "tok")
    assert clip.expected_frames == 25 and clip.duration_ms == 1000
    assert stream.queued_video_frames == 0 and not stream.speaking, "nothing until frames come"

    await clip.push(_batch(8))
    assert (stream.queued_video_frames, stream.queued_audio_samples) == (8, 8 * SPF)
    assert stream.speaking
    await clip.push(_batch(8))
    await clip.push(_batch(9))
    assert (stream.queued_video_frames, stream.queued_audio_samples) == (25, 25 * SPF)
    await clip.finish()
    assert (stream.queued_video_frames, stream.queued_audio_samples) == (25, 25 * SPF)
    assert clip.frames_pushed == 25 and clip.appended and clip.underruns == 0


async def test_the_queued_frames_are_the_pushed_pictures_fitted_like_any_frame(stream, tmp_path):
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 0.2), "tok")
    await clip.push(_batch(5))
    frame = stream._take_video()
    assert frame.shape == (SIZE * 3 // 2, SIZE), "a yuv420p frame of the stream's size"
    assert not np.array_equal(frame, stream._idle)
    luma = frame[:SIZE]
    assert luma[SIZE // 2, SIZE // 2] > 40, "the picture, in the middle"
    assert luma[2, SIZE // 2] <= 20, "letterboxed above: 4:3 into a square"


async def test_sound_left_after_the_last_frame_plays_under_the_idle_picture(stream, tmp_path):
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 1.0), "tok")
    await clip.push(_batch(20))
    await clip.finish()
    assert (stream.queued_video_frames, stream.queued_audio_samples) == (25, 25 * SPF)
    for _ in range(20):
        assert not np.array_equal(stream._take_video(), stream._idle)
    for _ in range(5):
        assert np.array_equal(stream._take_video(), stream._idle)


async def test_frames_beyond_the_sound_are_given_silence(stream, tmp_path):
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 0.4), "tok")
    await clip.push(_batch(15))  # 0.6 s of picture over 0.4 s of sound
    await clip.finish()
    assert (stream.queued_video_frames, stream.queued_audio_samples) == (15, 15 * SPF)
    heard = np.concatenate([stream._take_audio() for _ in range(30)])
    assert np.abs(heard[: 10 * SPF]).mean() > 100
    assert not heard[10 * SPF + 960 :].any(), "silence under the extra frames"


async def test_holding_the_frames_back_queues_the_same_clip_at_the_end(stream, tmp_path, media):  # noqa: F811
    wav = _tone(tmp_path / "s.wav", 1.0)

    async def build(s, buffer_all):
        clip = await s.begin_clip(wav, "tok", buffer_all=buffer_all)
        for n in (8, 8, 4):
            await clip.push(_batch(n))
            if buffer_all:
                assert s.queued_video_frames == 0 and not clip.appended and not s.speaking
        await clip.finish()
        video = [s._take_video().copy() for _ in range(s.queued_video_frames)]
        audio = np.concatenate([s._take_audio() for _ in range(60)])
        return video, audio

    other = await AvatarStream.create(media.idle, size=SIZE, fps=FPS)
    try:
        v1, a1 = await build(stream, False)
        v2, a2 = await build(other, True)
        assert len(v1) == len(v2) == 25
        assert all(np.array_equal(x, y) for x, y in zip(v1, v2))
        assert np.array_equal(a1, a2)
    finally:
        await other.close()


@pytest.mark.parametrize("fps", [18, 30])
async def test_a_frame_rate_that_does_not_divide_the_sample_rate_stays_exact(media, tmp_path, fps):  # noqa: F811
    s = await AvatarStream.create(media.idle, size=SIZE, fps=fps)
    try:
        clip = await s.begin_clip(_tone(tmp_path / "s.wav", 1.0), "tok")
        pushed = 0
        for n in (7, 5, 6):
            await clip.push(_batch(n))
            pushed += n
            assert abs(s.queued_audio_samples - pushed * AUDIO_RATE / fps) < 1
        await clip.finish()
        assert abs(s.queued_audio_samples - s.queued_video_frames * AUDIO_RATE / fps) < 1
        assert s.queued_video_frames == fps
    finally:
        await s.close()


async def test_it_is_one_clip_released_once_and_only_after_finish(stream, tmp_path):
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 0.4), "tok")
    await clip.push(_batch(5))
    _drain(stream)
    assert stream.released == [], "all of it taken, but more is coming"
    assert stream.speaking, "and it is still speech in progress"
    assert stream.queued_clips == ["tok"] or len(stream._clips) == 1

    await clip.push(_batch(5))
    await clip.finish()
    assert stream.released == []
    _drain(stream)
    await asyncio.sleep(0)
    assert stream.released == ["tok"]
    assert not stream.speaking


async def test_finishing_after_everything_played_releases_it_then(stream, tmp_path):
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 0.2), "tok")
    await clip.push(_batch(5))
    _drain(stream)
    assert stream.released == [] and stream.speaking
    await clip.finish()
    await asyncio.sleep(0)
    assert stream.released == ["tok"] and not stream.speaking


async def test_speaking_is_announced_once_however_the_batches_are_spaced(stream, tmp_path):
    said = []
    stream.on_speaking = said.append
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 0.6), "tok")
    for _ in range(3):
        await clip.push(_batch(5))
        _drain(stream)  # the queue runs dry between batches
    await clip.finish()
    _drain(stream)
    await asyncio.sleep(0)
    assert said == [True, False]


async def test_a_flush_ends_the_clip_and_its_token_stays_the_callers(stream, tmp_path):
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 1.0), "tok")
    await clip.push(_batch(8))
    assert stream.flush() is True
    assert stream.queued_video_frames == 0 and not stream.speaking
    assert stream.released == [], "not the stream's to release: it was never finished"
    with pytest.raises(ClipDropped):
        await clip.push(_batch(8))
    with pytest.raises(ClipDropped):
        await clip.finish()
    clip.abort()
    assert stream.queued_video_frames == 0 and stream.released == []


async def test_retire_leaves_a_clip_in_progress_to_its_builder(stream, tmp_path):
    whole = await stream.begin_clip(_tone(tmp_path / "a.wav", 0.2), "finished")
    await whole.push(_batch(5))
    await whole.finish()
    building = await stream.begin_clip(_tone(tmp_path / "b.wav", 0.4), "in progress")
    await building.push(_batch(5))

    assert stream.retire() == ["finished"], "the finished, unplayed one — and only that"
    with pytest.raises(RuntimeError):
        await building.push(_batch(5))
    with pytest.raises(RuntimeError):
        await building.finish()


async def test_an_aborted_clip_is_neither_released_nor_handed_back(stream, tmp_path):
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 1.0), "tok")
    await clip.push(_batch(8))
    clip.abort()
    assert stream.queued_video_frames == 8, "what was queued still plays out"
    _drain(stream)
    await asyncio.sleep(0)
    assert stream.released == [] and not stream.speaking
    assert stream.retire() == []


async def test_a_push_waits_for_room_and_the_wait_can_be_cancelled(media, tmp_path):  # noqa: F811
    s = await AvatarStream.create(media.idle, size=SIZE, fps=FPS, max_queue_seconds=0.3)
    try:
        clip = await s.begin_clip(_tone(tmp_path / "s.wav", 2.0), "tok")
        await clip.push(_batch(10))  # 0.4 s: over the cap
        waiting = asyncio.create_task(clip.push(_batch(5)))
        await asyncio.sleep(0.2)
        assert not waiting.done() and s.queued_video_frames == 10

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert s.queued_video_frames == 10, "nothing of the cancelled batch was queued"

        _drain(s)
        await asyncio.wait_for(clip.push(_batch(5)), 2)
        assert s.queued_video_frames == 5
    finally:
        await s.close()


async def test_running_dry_mid_clip_is_counted_and_does_not_shift_picture_from_sound(
    stream, tmp_path
):
    """
    Pulled in real time, as a sender would: the second batch comes late. The
    tracks idle through the gap and the clip carries on with its frames still
    over their own samples.
    """
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 0.8), "tok")
    await clip.push(_batch(5))
    ends: list[tuple[float, float]] = []

    def timeline_ends() -> tuple[float, float]:
        """Where each queue ends on the shared timeline, in audio samples."""
        video_end = (stream.video.position + len(stream._video)) * SPF
        audio_end = stream.audio.position * AUDIO_FRAME_SAMPLES + stream._audio_samples
        return video_end, audio_end

    async def late():
        await asyncio.sleep(0.6)  # 0.2 s of media, then 0.4 s of nothing
        await clip.push(_batch(15))
        ends.append(timeline_ends())
        await clip.finish()
        ends.append(timeline_ends())

    task = asyncio.create_task(late())
    await _pump(stream, until=lambda: task.done() and not stream.speaking, timeout=6)
    await task
    assert clip.underruns == 1
    # The property that matters: after resuming, the clip's picture and sound
    # END at the same instant on the shared timeline, so each frame is still
    # over its own samples. (Not "as many samples queued as frames x 1920":
    # the two tracks tick at different rates and reach the resume point up to
    # a frame apart, and the stream levels them with a little silence first.
    # Whether any is needed depends on the instant the late batch lands, so a
    # count of what was queued is not stable — and is not the point.)
    for video_end, audio_end in ends:
        assert abs(video_end - audio_end) < 1, (video_end, audio_end)
    # And whatever levelling there was is under one frame, never a growing gap.
    assert 0 <= stream._audio_in - (stream._video_in * SPF) < SPF
    # Everything queued was taken.
    assert stream._video_in == stream._video_out
    assert stream._audio_in == stream._audio_out
    await asyncio.sleep(0)
    assert stream.released == ["tok"]


async def test_a_clip_whose_frames_all_arrived_is_not_charged_with_a_stall(stream, tmp_path):
    """finish() comes after the encode; the queue running dry before it is not a stall."""
    clip = await stream.begin_clip(_tone(tmp_path / "s.wav", 0.2), "tok")
    await clip.push(_batch(5))
    _drain(stream)
    await clip.finish()
    assert clip.underruns == 0


async def test_begin_clip_refuses_a_stream_that_is_finished(stream, tmp_path):
    stream.retire()
    with pytest.raises(RuntimeError):
        await stream.begin_clip(_tone(tmp_path / "s.wav", 0.2), "tok")


# ── the pipeline ─────────────────────────────────────────────────────────
class FakeHandle:
    def __init__(self, stream, token, buffer_all):
        self.stream, self.token, self.buffer_all = stream, token, buffer_all
        self.duration_ms = 1234
        self.frames_pushed = 0
        self.appended = False
        self.underruns = 0
        self.finished = self.aborted = False
        self.pushes: list[int] = []

    async def push(self, frames):
        if self.stream.flushed_since_begin:
            raise ClipDropped()
        if self.stream.retired:
            raise RuntimeError("stream is closed")
        self.pushes.append(len(self.stream.socket.sent))
        self.frames_pushed += len(frames)
        if not self.buffer_all:
            self.appended = True

    async def finish(self):
        if self.stream.flushed_since_begin:
            raise ClipDropped()
        if self.stream.retired:
            raise RuntimeError("stream is closed")
        self.finished = True
        self.appended = True
        self.stream.enqueued.append(str(self.token.path))
        if self.stream.hold:
            self.stream.unplayed.append(self.token)

    def abort(self):
        self.aborted = True


class ProgressiveStream(FakeStream):
    """FakeStream with the batch-by-batch clip."""

    def __init__(self, socket):
        super().__init__(socket)
        self.socket = socket
        self.handles: list[FakeHandle] = []
        self.flushed_since_begin = False
        self.refuse_begin = False

    async def begin_clip(self, audio_path, token=None, *, buffer_all=False):
        assert Path(audio_path).exists()
        if self.retired or self.refuse_begin:
            raise RuntimeError("stream is closed")
        self.flushed_since_begin = False
        handle = FakeHandle(self, token, buffer_all)
        self.handles.append(handle)
        return handle

    def flush(self):
        self.flushed_since_begin = True
        return super().flush()

    async def enqueue_audio(self, source, token=None, **kwargs):
        return await self.enqueue_clip(str(source), token)


@pytest.fixture
def progressive(rig, monkeypatch):  # noqa: F811
    """
    The setting on, a live stream, and a lip-sync engine that delivers two
    batches of frames and then the clip. `engine.fail_after` makes it fall
    back to the still image after that many batches.
    """
    monkeypatch.setattr(settings, "LIPSYNC_PROGRESSIVE", True)
    stream = _go_live(rig, ProgressiveStream(rig.socket))

    class Engine:
        fps = 60.0
        still = False
        fail_after = None
        calls: list[dict] = []
        gate = None

    engine = Engine()
    engine.calls = []

    async def animate(avatar_image_path, audio_path, output_path, cache_key=None, **extra):
        engine.calls.append(extra)
        on_frames, report = extra.get("on_frames"), extra.get("report")
        made = "musetalk"
        if on_frames is not None:
            deliver = True
            for k in range(2):
                if engine.fail_after is not None and k >= engine.fail_after:
                    made = "simple"
                    break
                if deliver:
                    try:
                        await on_frames(np.zeros((4, 8, 8, 3), np.uint8))
                    except Exception:
                        deliver = False  # as the animator does
                if engine.gate is not None:
                    await engine.gate.wait()
            if engine.fail_after == 0:
                made = "simple"
        if report is not None:
            report["engine"] = made
        Path(output_path).write_bytes(b"mp4")
        return output_path

    async def still_image_only():
        return engine.still

    monkeypatch.setattr(wsmod.avatar_animator, "animate", animate)
    monkeypatch.setattr(wsmod.avatar_animator, "still_image_only", still_image_only)
    monkeypatch.setattr(wsmod.avatar_animator, "lipsync_fps", engine.fps, raising=False)
    engine.stream = stream
    engine.set_fps = lambda v: monkeypatch.setattr(wsmod.avatar_animator, "lipsync_fps", v)
    return engine


async def test_with_the_setting_off_the_engine_is_asked_for_nothing_new(rig, monkeypatch, uploads):  # noqa: F811
    monkeypatch.setattr(settings, "LIPSYNC_PROGRESSIVE", False)
    stream = _go_live(rig, ProgressiveStream(rig.socket))
    seen = []
    real = wsmod.avatar_animator.animate

    async def animate(*args, **kwargs):
        seen.append(sorted(kwargs))
        return await real(*args, **kwargs)

    monkeypatch.setattr(wsmod.avatar_animator, "animate", animate)
    await rig.manager._handle_text_input_inner("s1", "question")

    assert seen and all(k == ["audio_path", "avatar_image_path", "output_path"] for k in seen)
    assert stream.handles == [], "no clip was begun early"
    assert [_route(c) for c in _chunks(rig)] == ["webrtc"] * 4
    assert all(
        set(c) == {"type", "chunk_index", "total_chunks", "transport", "text", "duration_ms"}
        for c in _chunks(rig)
    )
    assert uploads == []


async def test_a_fast_engine_puts_frames_on_the_stream_before_the_clip_is_finished(
    rig,
    progressive,
    uploads,  # noqa: F811
):
    progressive.gate = asyncio.Event()
    turn = asyncio.create_task(rig.manager._handle_text_input_inner("s1", "question"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if progressive.stream.handles and progressive.stream.handles[0].frames_pushed:
            break
    first = progressive.stream.handles[0]
    assert first.buffer_all is False, "fast enough to start early"
    assert first.appended and not first.finished, "frames are on the stream; the engine is not done"
    announced = _chunks(rig)
    assert len(announced) == 1 and announced[0]["transport"] == "webrtc"
    assert announced[0]["duration_ms"] == 1234 and announced[0]["chunk_index"] == 0
    assert first.pushes[0] > rig.socket.sent.index(announced[0]), "announced when it began"

    progressive.gate.set()
    await turn
    chunks = _chunks(rig)
    assert [_route(c) for c in chunks] == ["webrtc"] * 4, "one announcement a sentence"
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2, 3]
    assert all(h.finished and not h.aborted for h in progressive.stream.handles)
    assert all("on_frames" in call and "report" in call for call in progressive.calls)
    assert uploads == [] and rig.socket.types()[-1] == "video_chunk_end"


@pytest.mark.parametrize("measured", [None, 20.0, 25.0 * 1.1])
async def test_an_engine_not_known_to_keep_up_has_its_frames_held(rig, progressive, measured):  # noqa: F811
    """Unknown, slower than playback, or faster by less than the margin."""
    progressive.set_fps(measured)
    await rig.manager._handle_text_input_inner("s1", "question")
    assert all(h.buffer_all for h in progressive.stream.handles)
    assert [_route(c) for c in _chunks(rig)] == ["webrtc"] * 4


async def test_a_fast_enough_engine_is_one_past_the_margin(rig, progressive):  # noqa: F811
    progressive.set_fps(settings.AVATAR_FPS * wsmod.PROGRESSIVE_FPS_MARGIN)
    await rig.manager._handle_text_input_inner("s1", "question")
    assert not any(h.buffer_all for h in progressive.stream.handles)


async def test_the_still_image_engine_is_left_to_the_audio_only_path(rig, progressive):  # noqa: F811
    progressive.still = True
    await rig.manager._handle_text_input_inner("s1", "question")
    assert progressive.stream.handles == [] and progressive.calls == []
    assert [_route(c) for c in _chunks(rig)] == ["webrtc"] * 4


async def test_an_engine_that_falls_back_to_the_still_delivers_the_sentence_once(
    rig,
    progressive,
    uploads,  # noqa: F811
):
    """No frames ever came: the clip the engine wrote goes on the existing way."""
    progressive.fail_after = 0
    stream = progressive.stream
    await rig.manager._handle_text_input_inner("s1", "question")

    chunks = _chunks(rig)
    assert [_route(c) for c in chunks] == ["webrtc"] * 4, "announced once each, not twice"
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2, 3]
    assert all(h.aborted and not h.finished for h in stream.handles)
    assert len(stream.enqueued) == 4, "each put on the stream whole, once"
    assert uploads == []


async def test_a_job_that_fails_after_frames_were_heard_is_not_sent_again(
    rig,
    progressive,
    uploads,  # noqa: F811
):
    progressive.fail_after = 1
    stream = progressive.stream
    await rig.manager._handle_text_input_inner("s1", "question")

    chunks = _chunks(rig)
    assert [_route(c) for c in chunks] == ["webrtc"] * 4
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2, 3], "it still counts as a sentence"
    assert all(h.aborted for h in stream.handles)
    assert stream.enqueued == [] and uploads == [], "by no other route"
    assert rig.socket.sent[-1] == {"type": "video_chunk_end", "sent_chunks": 4}


async def test_held_frames_of_a_job_that_then_failed_were_never_heard_so_it_is_delivered(
    rig,
    progressive,
    uploads,  # noqa: F811
):
    progressive.set_fps(None)  # held until complete
    progressive.fail_after = 1
    stream = progressive.stream
    await rig.manager._handle_text_input_inner("s1", "question")
    assert [_route(c) for c in _chunks(rig)] == ["webrtc"] * 4
    assert len(stream.enqueued) == 4 and uploads == []


async def test_a_stream_that_dies_mid_sentence_gets_it_by_url_once(
    rig, progressive, uploads, tmp_path
):  # noqa: F811
    stream = progressive.stream
    progressive.gate = asyncio.Event()
    turn = asyncio.create_task(rig.manager._handle_text_input_inner("s1", "question"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if stream.handles and stream.handles[0].frames_pushed:
            break
    await rig.manager._stream_down("s1", stream)  # the peer connection failed
    progressive.gate.set()
    await turn
    await _settle(rig.manager)

    chunks = _chunks(rig)
    assert _route(chunks[0]) == "webrtc" and chunks[0]["chunk_index"] == 0
    again = [c for c in chunks if c["chunk_index"] == 0 and c.get("video_url")]
    assert len(again) == 1 and again[0]["resend"] is True, "the same sentence, once, by URL"
    rest = [c for c in chunks if c["chunk_index"] > 0]
    assert [_route(c) for c in rest] == ["url"] * 3, "and the rest of the turn follows by URL"
    assert [c["chunk_index"] for c in rest] == [1, 2, 3]
    assert len(uploads) == 4
    assert list((tmp_path / "avatar-session-s1").iterdir()) == []


async def test_a_stream_that_will_not_begin_the_clip_changes_nothing(rig, progressive, uploads):  # noqa: F811
    progressive.stream.refuse_begin = True
    await rig.manager._handle_text_input_inner("s1", "question")
    chunks = _chunks(rig)
    assert [_route(c) for c in chunks] == ["webrtc"] * 4
    assert not any(c.get("resend") for c in chunks)
    assert len(progressive.stream.enqueued) == 4 and uploads == []
    assert all("on_frames" not in call for call in progressive.calls)


async def test_an_interrupt_mid_sentence_leaves_no_files_and_sends_it_nowhere(
    rig,
    progressive,
    uploads,
    tmp_path,  # noqa: F811
):
    stream = progressive.stream
    progressive.gate = asyncio.Event()
    rig.manager._spawn_turn("s1", rig.manager._handle_text_input_inner("s1", "question"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if stream.handles and stream.handles[0].frames_pushed:
            break
    turn = rig.manager._active_turns["s1"]
    await rig.manager.interrupt_active_turn("s1")
    await asyncio.wait({turn}, timeout=3)
    await _settle(rig.manager)

    assert turn.cancelled()
    assert stream.handles[0].aborted and not stream.handles[0].finished
    assert len(_chunks(rig)) == 1 and uploads == []
    assert list((tmp_path / "avatar-session-s1").iterdir()) == []


def test_health_reports_the_setting_and_the_measured_rate():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as tc:
        body = tc.get("/health").json()
    assert body["lipsync_progressive"] is False
    assert "lipsync_fps" in body and body["lipsync_fps"] is None
