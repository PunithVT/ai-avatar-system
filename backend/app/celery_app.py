import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from celery import Celery
from celery.schedules import crontab

from app.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "avatar_system", broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,  # 30 minutes
    task_soft_time_limit=25 * 60,  # 25 minutes
    beat_schedule={
        "cleanup-old-files-daily": {
            "task": "cleanup_old_files",
            "schedule": crontab(hour=3, minute=0),  # Run daily at 3 AM
        },
        "cleanup-guest-accounts-daily": {
            "task": "cleanup_guest_accounts",
            "schedule": crontab(hour=3, minute=30),  # after the file sweep
        },
    },
)


@celery_app.task(name="process_avatar", bind=True, max_retries=3)
def process_avatar_task(self, avatar_id: str, image_path: str):
    """Background task to process avatar image"""
    try:
        logger.info(f"Processing avatar {avatar_id} from {image_path}")

        import asyncio

        from app.services.avatar_processor import avatar_processor

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Honor the actual system temp dir instead of hardcoding /tmp.
        processed_dir = Path(tempfile.gettempdir()) / "avatars"
        processed_dir.mkdir(parents=True, exist_ok=True)
        processed_path = str(processed_dir / f"{avatar_id}_processed.jpg")
        result_path, metadata = loop.run_until_complete(
            avatar_processor.process_image(image_path, processed_path)
        )
        loop.close()

        logger.info(f"Avatar {avatar_id} processed successfully: {result_path}")
        return {
            "avatar_id": avatar_id,
            "processed_path": result_path,
            "metadata": metadata,
            "status": "ready",
        }

    except Exception as e:
        logger.error(f"Failed to process avatar {avatar_id}: {e}")
        raise self.retry(exc=e, countdown=60 * (self.request.retries + 1))


@celery_app.task(name="generate_video", bind=True, max_retries=2)
def generate_video_task(self, session_id: str, text: str, avatar_image_path: str):
    """Background task to generate avatar video"""
    try:
        logger.info(f"Generating video for session {session_id}")

        import asyncio

        from app.services.animator import avatar_animator
        from app.services.tts import tts_service

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # mkstemp creates the file atomically with owner-only perms (0600) —
        # mktemp only returned a name, leaving a TOCTOU race and world-readable
        # output. We close the fds immediately; the services write to the paths.
        afd, audio_path = tempfile.mkstemp(suffix=".wav")
        vfd, video_path = tempfile.mkstemp(suffix=".mp4")
        os.close(afd)
        os.close(vfd)

        loop.run_until_complete(tts_service.synthesize(text, audio_path))
        loop.run_until_complete(avatar_animator.animate(avatar_image_path, audio_path, video_path))

        loop.close()

        # Clean up audio temp file
        Path(audio_path).unlink(missing_ok=True)

        logger.info(f"Video generated for session {session_id}: {video_path}")
        return {"session_id": session_id, "video_path": video_path, "status": "completed"}

    except Exception as e:
        logger.error(f"Failed to generate video for session {session_id}: {e}")
        raise self.retry(exc=e, countdown=30 * (self.request.retries + 1))


def _sweep_local_videos(now: float) -> int:
    """
    Delete generated clips older than MEDIA_RETENTION_HOURS from local
    storage, and prune session directories left empty. Returns files removed.
    """
    if not settings.USE_LOCAL_STORAGE or settings.MEDIA_RETENTION_HOURS <= 0:
        return 0
    videos = Path(settings.LOCAL_STORAGE_PATH) / "videos"
    if not videos.is_dir():
        return 0
    max_age = settings.MEDIA_RETENTION_HOURS * 3600
    removed = 0
    for session_dir in videos.iterdir():
        if not session_dir.is_dir():
            continue
        for clip in session_dir.iterdir():
            try:
                if clip.is_file() and now - clip.stat().st_mtime > max_age:
                    clip.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        try:
            session_dir.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return removed


@celery_app.task(name="cleanup_old_files")
def cleanup_old_files_task():
    """Reap temp media older than 24h that a dropped/abandoned session left behind.

    The WebSocket pipeline writes per-turn audio/video into per-session dirs
    named ``avatar-session-<id>/`` under the system temp dir, and caches avatar
    images under ``avatars/`` there too — NOT the hardcoded ``/tmp/{avatars,
    videos,audio}`` this task used to scan. On a host where ``gettempdir()``
    isn't ``/tmp`` (or just because those dirs never existed) the old globs
    matched nothing and the real temp media leaked forever. Sweep the actual
    locations, oldest-first, and prune now-empty session dirs.
    """
    import shutil

    tmpdir = Path(tempfile.gettempdir())
    max_age_seconds = 24 * 60 * 60  # 24 hours
    now = time.time()
    total_cleaned = 0
    dirs_removed = 0

    try:
        # Per-session working dirs (input audio + per-chunk wav/mp4).
        for session_dir in tmpdir.glob("avatar-session-*"):
            if not session_dir.is_dir():
                continue
            # Snapshot the dir's mtime BEFORE deleting anything — unlinking a
            # child bumps the parent's mtime to now, which would otherwise make
            # a long-dead dir look freshly active and never get pruned.
            dir_stale = now - session_dir.stat().st_mtime > max_age_seconds
            for file_path in session_dir.rglob("*"):
                if file_path.is_file() and now - file_path.stat().st_mtime > max_age_seconds:
                    file_path.unlink(missing_ok=True)
                    total_cleaned += 1
            # Drop the session dir once it's empty and was itself stale — its
            # owning websocket is long gone.
            if dir_stale and not any(session_dir.iterdir()):
                shutil.rmtree(session_dir, ignore_errors=True)
                dirs_removed += 1

        # Cached avatar images + any legacy flat dirs that might still exist.
        for sub in ("avatars", "videos", "audio"):
            directory = tmpdir / sub
            if not directory.exists():
                continue
            for file_path in directory.iterdir():
                if file_path.is_file() and now - file_path.stat().st_mtime > max_age_seconds:
                    file_path.unlink(missing_ok=True)
                    total_cleaned += 1

        # Generated clips in local storage. These are not temp files — they
        # are what the browser plays — but nothing ever removed them.
        total_cleaned += _sweep_local_videos(now)

        logger.info(
            f"Cleanup completed: {total_cleaned} file(s), {dirs_removed} session dir(s) removed"
        )
        return {"cleaned_files": total_cleaned, "dirs_removed": dirs_removed}

    except Exception as e:
        logger.error(f"Cleanup task failed: {e}")
        raise


def _purge_voice_profiles(user_ids: set) -> int:
    """
    Delete the voice recordings — a person's voice — owned by `user_ids`, and
    drop them from the index. Returns the number removed.

    The index is a JSON file the API process also rewrites, under a lock this
    process cannot share. The window is small (this runs once a day, and the
    write is temp-file-then-rename so the file is never half-written), but it
    exists: a clone finishing in the same instant could be lost from the
    index. The recordings are deleted FIRST, so the failure mode of that race
    is a stale index entry pointing at nothing — never a recording left behind
    for an account that no longer exists.
    """
    import json

    index_path = Path("voice_profiles") / "index.json"
    if not user_ids or not index_path.exists():
        return 0
    try:
        entries = json.loads(index_path.read_text())
    except Exception:
        return 0
    doomed = [e for e in entries if e.get("user_id") in user_ids]
    if not doomed:
        return 0
    for entry in doomed:
        wav = entry.get("wav_path")
        if wav:
            Path(wav).unlink(missing_ok=True)
    keep = [e for e in entries if e.get("user_id") not in user_ids]
    tmp = index_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep, indent=2, default=str))
    tmp.replace(index_path)
    return len(doomed)


@celery_app.task(name="cleanup_guest_accounts")
def cleanup_guest_accounts_task():
    """
    Delete anonymous "Continue as Guest" accounts that have gone idle.

    Guests get a real user row so that per-user ownership checks isolate
    their data, which means the table would otherwise grow by one row per
    visitor forever. A guest is reapable once BOTH its account and its most
    recent session are older than GUEST_RETENTION_HOURS — checking the
    session too keeps us from deleting someone who is mid-conversation on a
    long-lived account.

    Avatars, sessions, messages and conversations all cascade from the user
    row (ondelete="CASCADE" plus the ORM relationships), so removing the
    user removes everything it owned in one statement.
    """
    import asyncio

    from sqlalchemy import delete, func, select

    from app.database import AsyncSessionLocal
    from app.models import Avatar, User
    from app.models import Session as SessionModel

    if not settings.GUEST_ACCOUNTS_ENABLED:
        logger.info("Guest accounts disabled — retention sweep skipped")
        return {"deleted_guests": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.GUEST_RETENTION_HOURS)

    async def _sweep() -> int:
        async with AsyncSessionLocal() as db:
            # Most recent activity per guest, treating "no sessions at all"
            # as the account's own creation time.
            last_activity = (
                select(
                    User.id.label("uid"),
                    func.coalesce(
                        func.max(func.coalesce(SessionModel.ended_at, SessionModel.started_at)),
                        User.created_at,
                    ).label("seen"),
                )
                .outerjoin(SessionModel, SessionModel.user_id == User.id)
                .where(User.is_guest.is_(True))
                .group_by(User.id, User.created_at)
                .subquery()
            )
            stale = select(last_activity.c.uid).where(last_activity.c.seen < cutoff)
            ids = (await db.execute(stale)).scalars().all()
            if not ids:
                return 0

            # Storage objects do not cascade with the rows. Collect what these
            # guests own BEFORE the delete, then remove it after the commit —
            # otherwise every guest's avatar image and generated clips stayed
            # on disk (and stayed fetchable) forever.
            avatar_keys = (
                (await db.execute(select(Avatar.s3_key).where(Avatar.user_id.in_(ids))))
                .scalars()
                .all()
            )
            session_ids = (
                (await db.execute(select(SessionModel.id).where(SessionModel.user_id.in_(ids))))
                .scalars()
                .all()
            )

            await db.execute(delete(User).where(User.id.in_(ids)))
            await db.commit()

            from app.services.storage import storage_service

            for key in avatar_keys:
                if not key:
                    continue
                try:
                    # avatars/<id>/image.jpg → avatars/<id>/
                    await storage_service.delete_prefix(key.rsplit("/", 1)[0] + "/")
                except Exception as e:
                    logger.warning(f"Could not delete avatar files {key}: {e}")
            for sid in session_ids:
                try:
                    await storage_service.delete_prefix(f"videos/{sid}/")
                except Exception as e:
                    logger.warning(f"Could not delete media for session {sid}: {e}")
            try:
                _purge_voice_profiles(set(ids))
            except Exception as e:
                logger.warning(f"Could not delete voice recordings for reaped guests: {e}")
            return len(ids)

    async def _run() -> int:
        try:
            return await _sweep()
        finally:
            # The engine is module-level but each task runs its own event
            # loop. Connections left in the pool would be bound to a loop
            # that is about to close, and the next task in this worker
            # process would be handed one of them.
            from app.database import engine

            await engine.dispose()

    deleted = asyncio.run(_run())

    logger.info(f"Guest retention sweep removed {deleted} account(s)")
    return {"deleted_guests": deleted}
