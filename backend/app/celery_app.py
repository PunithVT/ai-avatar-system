from celery import Celery
from celery.schedules import crontab
from pathlib import Path
import logging
import os
import time

from app.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "avatar_system",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND
)

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,  # 30 minutes
    task_soft_time_limit=25 * 60,  # 25 minutes
    beat_schedule={
        'cleanup-old-files-daily': {
            'task': 'cleanup_old_files',
            'schedule': crontab(hour=3, minute=0),  # Run daily at 3 AM
        },
    },
)


@celery_app.task(name="process_avatar", bind=True, max_retries=3)
def process_avatar_task(self, avatar_id: str, image_path: str):
    """Background task to process avatar image"""
    try:
        logger.info(f"Processing avatar {avatar_id} from {image_path}")

        from app.services.avatar_processor import avatar_processor
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        processed_path = f"/tmp/avatars/{avatar_id}_processed.jpg"
        result_path, metadata = loop.run_until_complete(
            avatar_processor.process_image(image_path, processed_path)
        )
        loop.close()

        logger.info(f"Avatar {avatar_id} processed successfully: {result_path}")
        return {
            "avatar_id": avatar_id,
            "processed_path": result_path,
            "metadata": metadata,
            "status": "ready"
        }

    except Exception as e:
        logger.error(f"Failed to process avatar {avatar_id}: {e}")
        raise self.retry(exc=e, countdown=60 * (self.request.retries + 1))


@celery_app.task(name="generate_video", bind=True, max_retries=2)
def generate_video_task(self, session_id: str, text: str, avatar_image_path: str):
    """Background task to generate avatar video"""
    try:
        logger.info(f"Generating video for session {session_id}")

        from app.services.tts import tts_service
        from app.services.animator import avatar_animator
        import asyncio
        import tempfile

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Generate speech audio
        audio_path = tempfile.mktemp(suffix='.wav')
        loop.run_until_complete(tts_service.synthesize(text, audio_path))

        # Generate animation
        video_path = tempfile.mktemp(suffix='.mp4')
        loop.run_until_complete(
            avatar_animator.animate(avatar_image_path, audio_path, video_path)
        )

        loop.close()

        # Clean up audio temp file
        Path(audio_path).unlink(missing_ok=True)

        logger.info(f"Video generated for session {session_id}: {video_path}")
        return {
            "session_id": session_id,
            "video_path": video_path,
            "status": "completed"
        }

    except Exception as e:
        logger.error(f"Failed to generate video for session {session_id}: {e}")
        raise self.retry(exc=e, countdown=30 * (self.request.retries + 1))


@celery_app.task(name="cleanup_old_files")
def cleanup_old_files_task():
    """Background task to cleanup old temporary files older than 24 hours"""
    try:
        cleanup_dirs = ["/tmp/avatars", "/tmp/videos", "/tmp/audio"]
        max_age_seconds = 24 * 60 * 60  # 24 hours
        now = time.time()
        total_cleaned = 0

        for dir_path in cleanup_dirs:
            directory = Path(dir_path)
            if not directory.exists():
                continue

            for file_path in directory.iterdir():
                if file_path.is_file():
                    file_age = now - file_path.stat().st_mtime
                    if file_age > max_age_seconds:
                        file_path.unlink()
                        total_cleaned += 1
                        logger.debug(f"Cleaned up: {file_path}")

        logger.info(f"Cleanup completed: {total_cleaned} files removed")
        return {"cleaned_files": total_cleaned}

    except Exception as e:
        logger.error(f"Cleanup task failed: {e}")
        raise
