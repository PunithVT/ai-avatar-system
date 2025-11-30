from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import logging
from pathlib import Path
import uuid
from typing import List

from app.database import get_db
from app.models import User, Avatar
from app.schemas import AvatarCreate, AvatarResponse
from app.services.storage import storage_service
from app.services.avatar_processor import avatar_processor

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/upload", response_model=AvatarResponse, status_code=status.HTTP_201_CREATED)
async def upload_avatar(
    name: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Upload and process avatar image
    
    - **name**: Avatar name
    - **file**: Image file (JPG, PNG, WEBP)
    """
    try:
        # Validate file type
        if not file.content_type.startswith('image/'):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File must be an image"
            )
        
        # Read file
        file_data = await file.read()
        
        # Generate unique ID
        avatar_id = str(uuid.uuid4())
        
        # Save temporarily
        temp_path = f"/tmp/{avatar_id}_original{Path(file.filename).suffix}"
        with open(temp_path, 'wb') as f:
            f.write(file_data)
        
        # Process image
        processed_path = f"/tmp/{avatar_id}_processed.jpg"
        processed_path, metadata = await avatar_processor.process_image(
            temp_path,
            processed_path
        )
        
        # Upload to S3
        with open(processed_path, 'rb') as f:
            processed_data = f.read()
        
        s3_key = f"avatars/{avatar_id}/image.jpg"
        image_url = await storage_service.upload_file(
            processed_data,
            s3_key,
            content_type="image/jpeg"
        )
        
        # Upload thumbnail
        thumbnail_path = metadata['thumbnail_path']
        with open(thumbnail_path, 'rb') as f:
            thumbnail_data = f.read()
        
        thumb_s3_key = f"avatars/{avatar_id}/thumbnail.jpg"
        thumbnail_url = await storage_service.upload_file(
            thumbnail_data,
            thumb_s3_key,
            content_type="image/jpeg"
        )
        
        # Clean up temporary files
        Path(temp_path).unlink()
        Path(processed_path).unlink()
        Path(thumbnail_path).unlink()
        
        # Create avatar record
        # For demo, using a default user_id
        user_id = "demo-user"
        
        avatar = Avatar(
            id=avatar_id,
            user_id=user_id,
            name=name,
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            s3_key=s3_key,
            status="ready",
            metadata=metadata
        )
        
        db.add(avatar)
        await db.commit()
        await db.refresh(avatar)
        
        logger.info(f"Avatar created successfully: {avatar_id}")
        
        return avatar
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload avatar: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload avatar: {str(e)}"
        )


@router.get("/{avatar_id}", response_model=AvatarResponse)
async def get_avatar(
    avatar_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get avatar by ID"""
    try:
        result = await db.execute(
            select(Avatar).where(Avatar.id == avatar_id)
        )
        avatar = result.scalar_one_or_none()
        
        if not avatar:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Avatar not found"
            )
        
        return avatar
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get avatar: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get avatar"
        )


@router.get("/", response_model=List[AvatarResponse])
async def list_avatars(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db)
):
    """List all avatars"""
    try:
        result = await db.execute(
            select(Avatar).offset(skip).limit(limit)
        )
        avatars = result.scalars().all()
        return avatars
    
    except Exception as e:
        logger.error(f"Failed to list avatars: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list avatars"
        )


@router.delete("/{avatar_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_avatar(
    avatar_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Delete avatar"""
    try:
        result = await db.execute(
            select(Avatar).where(Avatar.id == avatar_id)
        )
        avatar = result.scalar_one_or_none()
        
        if not avatar:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Avatar not found"
            )
        
        # Delete from S3
        await storage_service.delete_file(avatar.s3_key)
        if avatar.thumbnail_url:
            thumb_key = avatar.s3_key.replace('image.jpg', 'thumbnail.jpg')
            await storage_service.delete_file(thumb_key)
        
        # Delete from database
        await db.delete(avatar)
        await db.commit()
        
        logger.info(f"Avatar deleted: {avatar_id}")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete avatar: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete avatar"
        )
