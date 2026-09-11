import logging
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.users import require_current_user
from app.config import settings
from app.database import get_db
from app.models import Avatar, User
from app.schemas import AvatarMetadataUpdate, AvatarRename, AvatarResponse
from app.services.avatar_processor import avatar_processor
from app.services.storage import storage_service

logger = logging.getLogger(__name__)
router = APIRouter()
TMPDIR = Path(tempfile.gettempdir())


def _validate_uuid(avatar_id: str) -> None:
    """
    Reject anything that isn't a UUID to keep S3 keys + filesystem paths safe.

    Returns 404 (not 400) for malformed IDs: a non-UUID can never name an
    existing avatar, so "not found" is the correct REST semantics and it
    avoids leaking the fact that IDs are UUIDs to a probing client.
    """
    try:
        uuid.UUID(avatar_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Avatar not found")


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """
    Read an upload in chunks, aborting as soon as it exceeds `limit`.

    `await file.read()` with a size check afterwards buffers the entire body
    first, so a client could force the server to hold an arbitrarily large
    payload in memory before we ever rejected it. Reading incrementally caps
    peak memory at `limit` + one chunk regardless of what the client sends.
    """
    chunk_size = 64 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File must be under {limit // (1024 * 1024)} MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_extension(filename: Optional[str]) -> str:
    """
    Check the filename's extension against the configured allowlist and
    return the suffix to use for the temp file.

    Content-Type is client-supplied and trivially spoofed, so it is only a
    first filter; the authoritative check is that PIL can actually decode
    the bytes (see the UnidentifiedImageError branch below).
    """
    allowed = {e.lower().lstrip(".") for e in settings.ALLOWED_EXTENSIONS}
    suffix = Path(filename or "avatar.jpg").suffix.lower()
    if suffix.lstrip(".") not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(allowed))}",
        )
    return suffix


@router.post("/upload", response_model=AvatarResponse, status_code=status.HTTP_201_CREATED)
async def upload_avatar(
    name: str = Form(..., min_length=1, max_length=settings.MAX_AVATAR_NAME_LEN),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    """Upload and process an avatar image."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image (JPG, PNG, WEBP)")

    suffix = _validate_extension(file.filename)
    file_data = await _read_capped(file, settings.MAX_UPLOAD_SIZE)

    avatar_id = str(uuid.uuid4())
    temp_orig = TMPDIR / f"{avatar_id}_original{suffix}"
    temp_processed = TMPDIR / f"{avatar_id}_processed.jpg"
    metadata: dict = {}

    try:
        temp_orig.write_bytes(file_data)

        _, metadata = await avatar_processor.process_image(str(temp_orig), str(temp_processed))

        image_key = f"avatars/{avatar_id}/image.jpg"
        image_url = await storage_service.upload_file(
            temp_processed.read_bytes(), image_key, content_type="image/jpeg"
        )

        # Resolve the thumbnail path defensively: an empty/missing value would
        # make Path("") == Path(".") (the cwd), so guard against it explicitly
        # and fall back to the processed image when there's no real thumbnail.
        thumb_value = metadata.get("thumbnail_path") or ""
        thumb_file = Path(thumb_value) if thumb_value else None
        thumb_key = f"avatars/{avatar_id}/thumbnail.jpg"
        thumb_bytes = (
            thumb_file.read_bytes()
            if thumb_file and thumb_file.is_file()
            else temp_processed.read_bytes()
        )
        thumbnail_url = await storage_service.upload_file(
            thumb_bytes, thumb_key, content_type="image/jpeg"
        )

    except HTTPException:
        raise
    except Exception as e:
        from PIL import UnidentifiedImageError

        if isinstance(e, UnidentifiedImageError):
            # Client sent something that isn't a decodable image — their
            # fault, not ours.
            raise HTTPException(
                status_code=400, detail="File is not a valid image (JPG, PNG, WEBP)"
            )
        logger.error(f"Avatar processing error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process avatar")
    finally:
        temp_orig.unlink(missing_ok=True)
        temp_processed.unlink(missing_ok=True)
        # is_file() guards against the Path("") == "." footgun — never unlink
        # a directory.
        thumb_value = metadata.get("thumbnail_path") or ""
        if thumb_value:
            thumb_file = Path(thumb_value)
            if thumb_file.is_file():
                thumb_file.unlink(missing_ok=True)

    avatar = Avatar(
        id=avatar_id,
        user_id=current_user.id,
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

    logger.info(f"Avatar created: {avatar_id} for user {current_user.id}")
    return avatar


@router.get("/", response_model=List[AvatarResponse])
async def list_avatars(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    """List avatars belonging to the current user."""
    uid = current_user.id
    result = await db.execute(
        select(Avatar)
        .where(Avatar.user_id == uid)
        .offset(skip)
        .limit(limit)
        .order_by(Avatar.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{avatar_id}", response_model=AvatarResponse)
async def get_avatar(
    avatar_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    _validate_uuid(avatar_id)
    result = await db.execute(select(Avatar).where(Avatar.id == avatar_id))
    avatar = result.scalar_one_or_none()
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if avatar.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorised to access this avatar")
    return avatar


@router.put("/{avatar_id}/voice", response_model=AvatarResponse)
async def set_avatar_voice(
    avatar_id: str,
    voice_id: Optional[str] = Query(
        default=None,
        description="Voice profile ID to assign. Omit or pass an empty string to unassign.",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    """Assign (or clear) a voice profile on an avatar."""
    _validate_uuid(avatar_id)
    result = await db.execute(select(Avatar).where(Avatar.id == avatar_id))
    avatar = result.scalar_one_or_none()
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if avatar.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorised to modify this avatar")

    try:
        normalized = (voice_id or "").strip() or None
        avatar.voice_id = normalized
        await db.commit()
        await db.refresh(avatar)
        logger.info(f"Avatar {avatar_id} voice set to: {normalized!r}")
        return avatar
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to set voice for avatar {avatar_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update avatar voice")


@router.patch("/{avatar_id}/metadata", response_model=AvatarResponse)
async def update_avatar_metadata(
    avatar_id: str,
    payload: AvatarMetadataUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    """Merge an allowlist of metadata fields into avatar_metadata."""
    _validate_uuid(avatar_id)
    result = await db.execute(select(Avatar).where(Avatar.id == avatar_id))
    avatar = result.scalar_one_or_none()
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if avatar.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorised to modify this avatar")

    existing: dict = avatar.avatar_metadata or {}
    if isinstance(existing, str):
        import json as _json

        try:
            existing = _json.loads(existing)
        except Exception:
            existing = {}

    # Only merge fields the caller actually set (exclude_unset=True keeps the
    # PATCH semantics — omitted fields are left untouched, not nulled out).
    update_data = payload.model_dump(exclude_unset=True)
    try:
        # Assign a NEW dict — mutating the ORM-held dict in place and
        # re-assigning the same object defeats SQLAlchemy's change detection
        # (old is new → no UPDATE emitted), so the metadata silently never
        # persisted even though the endpoint returned 200.
        avatar.avatar_metadata = {**existing, **update_data}
        await db.commit()
        await db.refresh(avatar)
        logger.info(f"Avatar {avatar_id} metadata updated: {list(update_data.keys())}")
        return avatar
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update metadata for avatar {avatar_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update avatar metadata")


@router.patch("/{avatar_id}/name", response_model=AvatarResponse)
async def rename_avatar(
    avatar_id: str,
    payload: AvatarRename,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    """Rename an avatar."""
    _validate_uuid(avatar_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    result = await db.execute(select(Avatar).where(Avatar.id == avatar_id))
    avatar = result.scalar_one_or_none()
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if avatar.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorised to modify this avatar")
    try:
        avatar.name = name
        await db.commit()
        await db.refresh(avatar)
        logger.info(f"Avatar {avatar_id} renamed to: {name!r}")
        return avatar
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to rename avatar {avatar_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to rename avatar")


@router.delete("/{avatar_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_avatar(
    avatar_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    _validate_uuid(avatar_id)
    result = await db.execute(select(Avatar).where(Avatar.id == avatar_id))
    avatar = result.scalar_one_or_none()
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if avatar.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorised to delete this avatar")

    # Delete the DB row first (sessions/messages/conversations cascade), THEN
    # the stored files — if the DB delete fails we haven't orphaned the row by
    # removing its image out from under it.
    try:
        await db.delete(avatar)
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to delete avatar {avatar_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete avatar")

    try:
        await storage_service.delete_file(avatar.s3_key)
        await storage_service.delete_file(avatar.s3_key.replace("image.jpg", "thumbnail.jpg"))
    except Exception as e:
        # Row is gone; leftover files are harmless and reaped by the cleanup task.
        logger.warning(f"Could not delete stored files for avatar {avatar_id}: {e}")

    logger.info(f"Avatar deleted: {avatar_id}")
