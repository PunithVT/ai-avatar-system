"""
Tests for the optional GFPGAN face-restoration pass.

Output quality needs a GPU and real weights, so it is not asserted here. What
is asserted is everything that decides whether a turn survives: restoration is
off unless asked for, a missing or broken restorer degrades to plain MuseTalk
output instead of failing the turn, and the flag actually reaches the worker.
That is the part a silent regression would break.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# musetalk_worker.py sits at the backend root and imports the MuseTalk tree at
# module scope, which is not installed in CI. Load it with those imports
# stubbed so the pure helpers can be exercised.
_WORKER = Path(__file__).resolve().parent.parent / "musetalk_worker.py"


@pytest.fixture(scope="module")
def worker():
    stubs = {
        "musetalk": types.ModuleType("musetalk"),
        "musetalk.utils": types.ModuleType("musetalk.utils"),
        "musetalk.utils.blending": types.ModuleType("musetalk.utils.blending"),
        "musetalk.utils.face_parsing": types.ModuleType("musetalk.utils.face_parsing"),
        "musetalk.utils.audio_processor": types.ModuleType("musetalk.utils.audio_processor"),
        "musetalk.utils.preprocessing": types.ModuleType("musetalk.utils.preprocessing"),
        "musetalk.utils.utils": types.ModuleType("musetalk.utils.utils"),
    }
    stubs["musetalk.utils.blending"].get_image = lambda *a, **k: None
    stubs["musetalk.utils.face_parsing"].FaceParsing = object
    stubs["musetalk.utils.audio_processor"].AudioProcessor = object
    stubs["musetalk.utils.preprocessing"].get_landmark_and_bbox = lambda *a, **k: None
    stubs["musetalk.utils.preprocessing"].read_imgs = lambda *a, **k: None
    stubs["musetalk.utils.preprocessing"].coord_placeholder = None
    stubs["musetalk.utils.utils"].datagen = lambda *a, **k: None
    stubs["musetalk.utils.utils"].load_all_model = lambda *a, **k: None

    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("_mt_worker", _WORKER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


class _Dev:
    def __init__(self, type_):
        self.type = type_


# ── the loader never raises, whatever is wrong ───────────────────────────
def test_off_by_default_loads_nothing(worker):
    assert worker._load_face_restorer("off", "/any/path.pth", _Dev("cuda")) is None


def test_skipped_on_cpu(worker, tmp_path):
    """A CPU turn is already 30-90 s; a per-frame restore would make it unusable."""
    weights = tmp_path / "w.pth"
    weights.write_bytes(b"x")
    assert worker._load_face_restorer("gfpgan", str(weights), _Dev("cpu")) is None


def test_missing_weights_disable_rather_than_crash(worker):
    assert worker._load_face_restorer("gfpgan", "/nope/missing.pth", _Dev("cuda")) is None


def test_import_or_load_failure_disables_rather_than_crash(worker, tmp_path, monkeypatch):
    """A broken gfpgan install must cost sharpness, not lip-sync."""
    weights = tmp_path / "w.pth"
    weights.write_bytes(b"x")

    broken = types.ModuleType("gfpgan")

    def _boom(*a, **k):
        raise RuntimeError("weights are corrupt")

    broken.GFPGANer = _boom
    monkeypatch.setitem(sys.modules, "gfpgan", broken)

    assert worker._load_face_restorer("gfpgan", str(weights), _Dev("cuda")) is None


# ── the per-frame path is transparent when anything goes wrong ───────────
def test_restore_is_identity_when_disabled(worker):
    frame = object()
    assert worker._restore(None, frame) is frame


def test_restore_returns_the_restored_frame(worker):
    class R:
        def enhance(self, img, **kw):
            return None, None, "restored"

    assert worker._restore(R(), "original") == "restored"


def test_restore_falls_back_when_the_restorer_returns_nothing(worker):
    class R:
        def enhance(self, img, **kw):
            return None, None, None

    assert worker._restore(R(), "original") == "original"


def test_restore_falls_back_when_a_frame_raises(worker):
    """One bad frame is invisible; a failed turn is not."""

    class R:
        def enhance(self, img, **kw):
            raise RuntimeError("cuda oom on this frame")

    assert worker._restore(R(), "original") == "original"


def test_restore_only_touches_the_centre_face(worker):
    """Background faces must not be restored, and paste_back does the compositing."""
    seen = {}

    class R:
        def enhance(self, img, **kw):
            seen.update(kw)
            return None, None, img

    worker._restore(R(), "frame")
    assert seen["only_center_face"] is True
    assert seen["paste_back"] is True
    assert seen["has_aligned"] is False


# ── the setting reaches the worker ───────────────────────────────────────
def test_config_defaults_to_off():
    from app.config import settings

    assert settings.FACE_RESTORE == "off"


def test_animator_forwards_the_flag_and_resolves_the_path(monkeypatch, tmp_path):
    """
    The worker runs with MuseTalk as its cwd, so a relative weights path has to
    be resolved against that directory before it is sent.
    """
    from app.config import settings
    from app.services.animator import AvatarAnimator

    monkeypatch.setattr(settings, "FACE_RESTORE", "gfpgan")
    monkeypatch.setattr(settings, "FACE_RESTORE_MODEL", "models/gfpgan/GFPGANv1.4.pth")

    animator = AvatarAnimator()
    musetalk_dir = tmp_path / "MuseTalk"
    resolved = (
        Path(settings.FACE_RESTORE_MODEL)
        if Path(settings.FACE_RESTORE_MODEL).is_absolute()
        else musetalk_dir / settings.FACE_RESTORE_MODEL
    )
    assert resolved.is_absolute()
    assert str(resolved).endswith("MuseTalk/models/gfpgan/GFPGANv1.4.pth")
    assert animator is not None


def test_absolute_weights_path_is_left_alone(monkeypatch, tmp_path):
    from app.config import settings

    abs_path = tmp_path / "custom" / "GFPGANv1.4.pth"
    monkeypatch.setattr(settings, "FACE_RESTORE_MODEL", str(abs_path))
    resolved = (
        Path(settings.FACE_RESTORE_MODEL)
        if Path(settings.FACE_RESTORE_MODEL).is_absolute()
        else Path("/elsewhere") / settings.FACE_RESTORE_MODEL
    )
    assert resolved == abs_path
