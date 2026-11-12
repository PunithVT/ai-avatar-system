"""
The parent <-> MuseTalk worker line protocol.

None of this needs a GPU: the protocol is exercised against a stand-in worker
that behaves the way the real one does at the edges that broke —

  * upstream MuseTalk prints to stdout before the worker is ready and again on
    every job, which used to be read as protocol lines;
  * a caller cancelled mid-job (the user interrupts the avatar) leaves a result
    in the pipe that the next job must not mistake for its own;
  * a caller cancelled during the minutes-long model load must not orphan the
    process or cause a second one to be spawned.
"""

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

from app.services.animator import AvatarAnimator

_FAKE_WORKER = textwrap.dedent(
    """
    import json, os, sys, time
    init = json.loads(sys.stdin.readline())
    if os.environ.get("FAKE_FAIL"):
        print("Traceback: CUDA out of memory", file=sys.stderr, flush=True)
        sys.exit(1)
    time.sleep(float(os.environ.get("FAKE_LOAD_SECS", "0")))
    with open(os.environ["FAKE_STARTS"], "a") as f:
        f.write("started\\n")
    if os.environ.get("FAKE_NOISY"):
        print("load unet model from ./models/musetalkV15/unet.pth", flush=True)
    print("READY", flush=True)
    for raw in sys.stdin:
        job = json.loads(raw)
        if os.environ.get("FAKE_NOISY"):
            print("reading images...", flush=True)
        if "slow" in job["audio"]:
            # A long job that, like the real worker, checks between steps
            # whether it has been told to stop.
            cancelled = False
            for _ in range(int(float(os.environ.get("FAKE_SLOW_SECS", "0.6")) / 0.05)):
                if os.path.exists(job.get("cancel_file", "")):
                    cancelled = True
                    break
                time.sleep(0.05)
            if cancelled:
                with open(os.environ["FAKE_STARTS"], "a") as f:
                    f.write("cancelled\\n")
                print(json.dumps({"id": job["id"], "status": "cancelled"}), flush=True)
                continue
        if "bad" in job["audio"]:
            print(json.dumps({"id": job["id"], "status": "error", "msg": "No face detected"}), flush=True)
            continue
        with open(job["output"], "w") as f:
            f.write(job["audio"])
        print(json.dumps({"id": job["id"], "status": "ok", "output": job["output"]}), flush=True)
    """
)


@pytest.fixture
def animator(tmp_path, monkeypatch):
    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE_WORKER)
    starts = tmp_path / "starts.log"
    starts.touch()

    a = AvatarAnimator()
    a.engine = "musetalk"
    a.device = "cuda"  # short timeouts; nothing here touches a GPU
    a._initialised = True
    a._musetalk_dir = tmp_path
    a._worker_env = {"PATH": "/usr/bin:/bin", "FAKE_STARTS": str(starts)}
    monkeypatch.setattr(a, "_resolve_worker_script", lambda _dir: script)
    a.starts = starts
    return a


def _starts(a) -> int:
    return a.starts.read_text().splitlines().count("started")


@pytest.mark.asyncio
async def test_output_before_ready_and_per_job_is_not_read_as_protocol(animator, tmp_path):
    """The exact failure: upstream prints a line before READY, and one per job."""
    animator._worker_env["FAKE_NOISY"] = "1"
    try:
        out = tmp_path / "a.mp4"
        await animator._worker_infer("img.png", "a.wav", str(out), None)
        assert out.read_text().endswith("a.wav")

        out2 = tmp_path / "b.mp4"
        await animator._worker_infer("img.png", "b.wav", str(out2), None)
        assert out2.read_text().endswith("b.wav")
        assert _starts(animator) == 1, "the worker must persist between jobs"
    finally:
        await animator.shutdown()


@pytest.mark.asyncio
async def test_cancelled_job_does_not_desync_the_next_one(animator, tmp_path):
    """
    Interrupt a job mid-flight, then run another. The abandoned job's result
    arrives first in the pipe; the second job must wait for its own.
    """
    try:
        slow_out = tmp_path / "slow.mp4"
        task = asyncio.create_task(
            animator._worker_infer("img.png", "slow.wav", str(slow_out), None)
        )
        await asyncio.sleep(0.25)  # worker has the job and is "computing"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        out = tmp_path / "next.mp4"
        await animator._worker_infer("img.png", "next.wav", str(out), None)
        assert out.read_text().endswith("next.wav")
        assert _starts(animator) == 1, "an interruption must not cost a model reload"
    finally:
        await animator.shutdown()


@pytest.mark.asyncio
async def test_an_abandoned_job_is_stopped_so_the_next_one_does_not_wait_for_it(animator, tmp_path):
    """
    Found by running the real worker: an interrupted job kept computing to
    the end, and the job the user was actually waiting for sat behind it —
    long enough, on a slow machine, to hit the inference timeout.
    """
    import time

    animator._worker_env["FAKE_SLOW_SECS"] = "30"  # far longer than this test may take
    try:
        task = asyncio.create_task(
            animator._worker_infer("img.png", "slow.wav", str(tmp_path / "slow.mp4"), None)
        )
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        t0 = time.monotonic()
        out = tmp_path / "next.mp4"
        await animator._worker_infer("img.png", "next.wav", str(out), None)
        assert time.monotonic() - t0 < 5, "the next job waited for the abandoned one"
        assert out.exists()
        assert "cancelled" in animator.starts.read_text()
        assert _starts(animator) == 1
    finally:
        await animator.shutdown()


@pytest.mark.asyncio
async def test_worker_error_is_raised_and_worker_survives(animator, tmp_path):
    try:
        with pytest.raises(RuntimeError, match="No face detected"):
            await animator._worker_infer("img.png", "bad.wav", str(tmp_path / "x.mp4"), None)
        out = tmp_path / "ok.mp4"
        await animator._worker_infer("img.png", "ok.wav", str(out), None)
        assert out.exists()
        assert _starts(animator) == 1
    finally:
        await animator.shutdown()


@pytest.mark.asyncio
async def test_caller_cancelled_during_load_does_not_orphan_or_respawn(animator, tmp_path):
    """
    The first caller gives up while models are loading. The start must carry
    on for the next caller rather than being abandoned and duplicated.
    """
    animator._worker_env["FAKE_LOAD_SECS"] = "0.5"
    try:
        first = asyncio.create_task(
            animator._worker_infer("img.png", "a.wav", str(tmp_path / "a.mp4"), None)
        )
        await asyncio.sleep(0.15)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        out = tmp_path / "b.mp4"
        await animator._worker_infer("img.png", "b.wav", str(out), None)
        assert out.exists()
        assert _starts(animator) == 1, "exactly one worker process must have been started"
    finally:
        await animator.shutdown()


@pytest.mark.asyncio
async def test_failed_start_is_not_retried_for_every_sentence(animator, tmp_path):
    animator._worker_env["FAKE_FAIL"] = "1"
    try:
        with pytest.raises(RuntimeError, match="exited while loading"):
            await animator._worker_infer("img.png", "a.wav", str(tmp_path / "a.mp4"), None)
        assert "failed" in animator.worker_state()

        # Inside the cool-off the next sentence fails immediately, without
        # spawning (and waiting on) another worker.
        spawned = []
        original = animator._spawn_worker

        async def counting_spawn():
            spawned.append(1)
            return await original()

        animator._spawn_worker = counting_spawn
        with pytest.raises(RuntimeError, match="unavailable"):
            await animator._worker_infer("img.png", "b.wav", str(tmp_path / "b.mp4"), None)
        assert spawned == []

        # After the cool-off it is allowed to try again.
        animator._worker_failed_at -= animator._WORKER_RETRY_COOLDOWN_SECS + 1
        with pytest.raises(RuntimeError, match="exited while loading"):
            await animator._worker_infer("img.png", "c.wav", str(tmp_path / "c.mp4"), None)
        assert spawned == [1]
    finally:
        await animator.shutdown()


@pytest.mark.asyncio
async def test_animate_falls_back_to_simple_when_the_worker_cannot_start(animator, tmp_path):
    animator._worker_env["FAKE_FAIL"] = "1"
    called = {}

    async def fake_simple(image, audio, output):
        called["simple"] = True
        Path(output).write_text("still")
        return output

    animator._animate_simple = fake_simple
    try:
        out = tmp_path / "v.mp4"
        await animator.animate("img.png", "a.wav", str(out))
        assert called == {"simple": True}
    finally:
        await animator.shutdown()


def test_the_tracked_worker_keeps_library_output_off_the_protocol_channel(tmp_path):
    """
    Run the REAL worker script far enough to prove the redirect: with the
    MuseTalk imports stubbed to print on import and inside load_all_model, the
    only thing on the process's stdout must be the READY line.
    """
    import subprocess

    pkg = tmp_path / "stubs"
    (pkg / "musetalk" / "utils").mkdir(parents=True)
    (pkg / "musetalk" / "__init__.py").write_text("print('stub import noise')\n")
    (pkg / "musetalk" / "utils" / "__init__.py").write_text("")
    (pkg / "musetalk" / "utils" / "audio_processor.py").write_text(
        "class AudioProcessor:\n    def __init__(self, **k): pass\n"
    )
    (pkg / "musetalk" / "utils" / "blending.py").write_text("def get_image(*a, **k): pass\n")
    (pkg / "musetalk" / "utils" / "face_parsing.py").write_text("class FaceParsing: pass\n")
    (pkg / "musetalk" / "utils" / "preprocessing.py").write_text(
        "coord_placeholder = (0, 0, 0, 0)\n"
        "def get_landmark_and_bbox(*a, **k): pass\n"
        "def read_imgs(*a, **k): pass\n"
    )
    (pkg / "musetalk" / "utils" / "utils.py").write_text(
        textwrap.dedent(
            """
            import os, torch
            def datagen(*a, **k): return []
            class _M:
                dtype = torch.float32
                def to(self, *a, **k): return self
                def half(self): return self
            class _Wrap:
                def __init__(self): self.vae = _M(); self.model = _M()
            class _PE(_M): pass
            def load_all_model(**k):
                print("load unet model from", k["unet_model_path"])
                os.system("echo subprocess noise")
                w = _Wrap()
                return w, w, _PE()
            """
        )
    )
    # transformers.WhisperModel.from_pretrained would need real weights.
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

    worker = Path(__file__).resolve().parent.parent / "musetalk_worker.py"
    init = (
        '{"unet_model_path": "u.pth", "unet_config": "u.json", '
        '"whisper_dir": "w", "vae_type": "v"}\n'
    )
    proc = subprocess.run(
        [sys.executable, str(worker)],
        input=init,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": str(pkg), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout == "READY\n", f"protocol channel was polluted: {proc.stdout!r}"
    assert "load unet model from" in proc.stderr
    assert "subprocess noise" in proc.stderr
