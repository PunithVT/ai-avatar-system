"""
Tests for the optional LiveAvatar engine.

This integration cannot be executed here — it needs 48 GB+ of VRAM — so
nothing below claims the engine works. What is tested is everything that can
be wrong without a GPU, and everything that protects the default path:
the argument contract, that MuseTalk stays default, that a missing install or
missing CUDA degrades instead of failing a turn, and that a run which exits 0
without producing a file is still treated as a failure.
"""

from pathlib import Path

import pytest

from app.config import settings
from app.services.animator import AvatarAnimator


@pytest.fixture
def animator(monkeypatch, tmp_path):
    a = AvatarAnimator()
    a._liveavatar_dir = tmp_path / "LiveAvatar"
    return a


def _argv(animator, tmp_path):
    return animator._liveavatar_argv(
        tmp_path / "LiveAvatar", "/img/a.jpg", "/aud/a.wav", "/out/v.mp4"
    )


# ── the default must not move ────────────────────────────────────────────
def test_musetalk_is_still_the_default():
    """Adding an engine must not change which one runs for everyone else."""
    assert settings.AVATAR_ENGINE == "musetalk"


# ── argument contract ────────────────────────────────────────────────────
def test_save_file_is_passed(animator, tmp_path):
    """
    Without --save_file the script invents a timestamped name under ./output/,
    which would break animate()'s contract of returning the path it was given.
    """
    argv = _argv(animator, tmp_path)
    assert "--save_file" in argv
    assert argv[argv.index("--save_file") + 1] == str(Path("/out/v.mp4").resolve())


def test_image_and_audio_are_absolute(animator, tmp_path):
    """The subprocess runs with cwd=LiveAvatar, so relative paths would break."""
    argv = _argv(animator, tmp_path)
    for flag in ("--image", "--audio"):
        value = argv[argv.index(flag) + 1]
        assert Path(value).is_absolute(), f"{flag} must be absolute, got {value}"


def test_single_gpu_flags_match_upstream_script(animator, tmp_path):
    """Mirrors infinite_inference_single_gpu.sh."""
    argv = _argv(animator, tmp_path)
    assert argv[0] == "torchrun"
    assert "--nproc_per_node=1" in argv
    assert "--single_gpu" in argv
    assert "minimal_inference/s2v_streaming_interact.py" in argv
    assert argv[argv.index("--task") + 1] == "s2v-14B"
    assert "--load_lora" in argv
    assert argv[argv.index("--sample_solver") + 1] == "euler"


def test_fp8_is_toggleable(animator, tmp_path, monkeypatch):
    """FP8 is the difference between fitting on 48 GB and needing 80 GB."""
    monkeypatch.setattr(settings, "LIVEAVATAR_FP8", True)
    assert "--fp8" in _argv(animator, tmp_path)
    monkeypatch.setattr(settings, "LIVEAVATAR_FP8", False)
    assert "--fp8" not in _argv(animator, tmp_path)


def test_sampling_settings_are_honoured(animator, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LIVEAVATAR_SAMPLE_STEPS", 7)
    monkeypatch.setattr(settings, "LIVEAVATAR_INFER_FRAMES", 96)
    argv = _argv(animator, tmp_path)
    assert argv[argv.index("--sample_steps") + 1] == "7"
    assert argv[argv.index("--infer_frames") + 1] == "96"


# ── degradation, not failure ─────────────────────────────────────────────
async def test_missing_install_falls_back_to_simple(monkeypatch):
    monkeypatch.setattr(settings, "AVATAR_ENGINE", "liveavatar")
    a = AvatarAnimator()
    monkeypatch.setattr(a, "_find_dir", lambda *args, **kw: None)
    await a.initialize()
    assert a.engine == "simple"


async def test_cpu_host_falls_back_to_simple(monkeypatch, tmp_path):
    """48 GB of VRAM is not optional; a CPU box must not try."""
    monkeypatch.setattr(settings, "AVATAR_ENGINE", "liveavatar")
    a = AvatarAnimator()
    monkeypatch.setattr(a, "_find_dir", lambda *args, **kw: tmp_path)
    monkeypatch.setattr(a, "device", "cpu")
    await a.initialize()
    assert a.engine == "simple"


async def test_engine_failure_falls_back_rather_than_raising(monkeypatch, tmp_path):
    """A broken optional engine must cost quality, not the turn."""
    monkeypatch.setattr(settings, "AVATAR_ENGINE", "liveavatar")
    a = AvatarAnimator()
    a._initialised = True
    a.engine = "liveavatar"

    async def _boom(*args, **kw):
        raise RuntimeError("LiveAvatar exited 1")

    called = {}

    async def _simple(avatar, audio, out):
        called["simple"] = True
        return out

    monkeypatch.setattr(a, "_animate_liveavatar", _boom)
    monkeypatch.setattr(a, "_animate_simple", _simple)

    out = str(tmp_path / "v.mp4")
    assert await a.animate("/img.jpg", "/a.wav", out) == out
    assert called.get("simple") is True


async def test_exit_zero_without_output_is_still_an_error(monkeypatch, tmp_path):
    """
    The script picks its own filename if --save_file is not honoured, so a
    clean exit proves nothing. A missing file must fail loudly here rather
    than surfacing later as an unplayable chunk.
    """
    a = AvatarAnimator()
    a._liveavatar_dir = tmp_path
    (tmp_path / "minimal_inference").mkdir(parents=True, exist_ok=True)

    class _Proc:
        returncode = 0

        async def wait(self):
            return 0

    async def _fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

    with pytest.raises(RuntimeError, match="produced no file"):
        await a._animate_liveavatar("/img.jpg", "/a.wav", str(tmp_path / "never_written.mp4"))
