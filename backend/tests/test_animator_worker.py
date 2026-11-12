"""
Regression test for issue #5 — missing musetalk_worker.py.

musetalk_worker.py is our custom persistent-worker driver, not part of the
upstream MuseTalk repo that setup_musetalk.sh clones. It's gitignored under
models/, so a fresh clone never had it and MuseTalk silently fell back to the
static-image engine for every user. We now ship a tracked copy at
backend/musetalk_worker.py and resolve to it when the clone lacks the script.
"""

from pathlib import Path

from app.services.animator import AvatarAnimator


def test_tracked_worker_script_is_shipped():
    """The tracked worker must exist in the repo (outside gitignored models/)."""
    tracked = Path(__file__).resolve().parent.parent / "musetalk_worker.py"
    assert tracked.is_file(), "backend/musetalk_worker.py is missing — MuseTalk will not start"


def test_resolve_worker_prefers_the_tracked_copy(tmp_path):
    """
    The tracked backend copy wins even when the clone has its own.

    The clone's copy is a snapshot from whenever setup_musetalk.sh last ran,
    in a gitignored directory — preferring it meant a checkout set up earlier
    kept running an old worker that no later fix ever reached.
    """
    animator = AvatarAnimator()
    tracked = Path(__file__).resolve().parent.parent / "musetalk_worker.py"

    clone = tmp_path / "MuseTalk"
    (clone / "scripts").mkdir(parents=True)
    (clone / "scripts" / "musetalk_worker.py").write_text("# stale snapshot")
    assert animator._resolve_worker_script(clone) == tracked

    empty_clone = tmp_path / "EmptyClone"
    (empty_clone / "scripts").mkdir(parents=True)
    assert animator._resolve_worker_script(empty_clone) == tracked
