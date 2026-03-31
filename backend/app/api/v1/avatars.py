import tempfile
import uuid
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, UploadFile, File, Form, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, Avatar
from app.schemas import AvatarResponse
from app.services.storage import storage_service
from app.services.avatar_processor import avatar_processor
from app.api.v1.users import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()
TMPDIR = Path(tempfile.gettempdir())


def _user_id(current_user: Optional[User]) -> str:
    return current_user.id if current_user else "demo-user"


@router.post("/upload", response_model=AvatarResponse, status_code=status.HTTP_201_CREATED)
async def upload_avatar(
    name: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Upload and process an avatar image."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image (JPG, PNG, WEBP)")

    file_data: bytes = await file.read()  # type: ignore[assignment]
    if len(file_data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File must be under 10 MB")

    avatar_id = str(uuid.uuid4())
    suffix = Path(file.filename or "avatar.jpg").suffix or ".jpg"
    temp_orig = TMPDIR / f"{avatar_id}_original{suffix}"
    temp_processed = TMPDIR / f"{avatar_id}_processed.jpg"
    metadata: dict = {}

    try:
        temp_orig.write_bytes(file_data)

        _, metadata = await avatar_processor.process_image(
            str(temp_orig), str(temp_processed)
        )

        image_key = f"avatars/{avatar_id}/image.jpg"
        image_url = await storage_service.upload_file(
            temp_processed.read_bytes(), image_key, content_type="image/jpeg"
        )

        thumb_path = Path(metadata.get("thumbnail_path", ""))
        thumb_key = f"avatars/{avatar_id}/thumbnail.jpg"
        thumbnail_url = await storage_service.upload_file(
            thumb_path.read_bytes() if thumb_path.exists() else temp_processed.read_bytes(),
            thumb_key,
            content_type="image/jpeg",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Avatar processing error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process avatar: {e}")
    finally:
        temp_orig.unlink(missing_ok=True)
        temp_processed.unlink(missing_ok=True)
        thumb_path = Path(metadata.get("thumbnail_path", ""))
        if thumb_path.exists():
            thumb_path.unlink(missing_ok=True)

    avatar = Avatar(
        id=avatar_id,
        user_id=_user_id(current_user),
        name=name,
        image_url=image_url,
        thumbnail_url=thumbnail_url,
        s3_key=image_key,
        status="ready",
        avatar_metadata=metadata,
    )
    db.add(avatar)
    await db.commit()
    await db.refresh(avatar)

    logger.info(f"Avatar created: {avatar_id} for user {_user_id(current_user)}")
    return avatar


@router.get("/", response_model=List[AvatarResponse])
async def list_avatars(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """List avatars belonging to the current user."""
    uid = _user_id(current_user)
    result = await db.execute(
        select(Avatar)
        .where(Avatar.user_id == uid)
        .offset(skip)
        .limit(min(limit, 200))
        .order_by(Avatar.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{avatar_id}", response_model=AvatarResponse)
async def get_avatar(
    avatar_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    result = await db.execute(select(Avatar).where(Avatar.id == avatar_id))
    avatar = result.scalar_one_or_none()
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if avatar.user_id != _user_id(current_user):
        raise HTTPException(status_code=403, detail="Not authorised to access this avatar")
    return avatar


@router.put("/{avatar_id}/voice", response_model=AvatarResponse)
async def set_avatar_voice(
    avatar_id: str,
    voice_id: str = Query(..., description="Voice profile ID to assign"),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Assign a voice profile to an avatar (persisted for all future sessions)."""
    result = await db.execute(select(Avatar).where(Avatar.id == avatar_id))
    avatar = result.scalar_one_or_none()
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if avatar.user_id != _user_id(current_user):
        raise HTTPException(status_code=403, detail="Not authorised to modify this avatar")

    avatar.voice_id = voice_id if voice_id else None
    await db.commit()
    await db.refresh(avatar)
    logger.info(f"Avatar {avatar_id} voice set to: {voice_id!r}")
    return avatar


@router.delete("/{avatar_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_avatar(
    avatar_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    result = await db.execute(select(Avatar).where(Avatar.id == avatar_id))
    avatar = result.scalar_one_or_none()
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if avatar.user_id != _user_id(current_user):
        raise HTTPException(status_code=403, detail="Not authorised to delete this avatar")

    await storage_service.delete_file(avatar.s3_key)
    await storage_service.delete_file(avatar.s3_key.replace("image.jpg", "thumbnail.jpg"))

    await db.delete(avatar)
    await db.commit()
    logger.info(f"Avatar deleted: {avatar_id}")
