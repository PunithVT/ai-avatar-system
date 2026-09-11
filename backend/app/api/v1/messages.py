import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.users import require_current_user
from app.database import get_db
from app.models import Message, Session, User
from app.schemas import MessageCreate, MessageResponse

logger = logging.getLogger(__name__)
router = APIRouter()


async def _get_owned_session(session_id: str, uid: str, db: AsyncSession) -> Session:
    """Fetch session and verify ownership."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if session.user_id != uid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised to access this session"
        )
    return session


@router.post("/send", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def send_message(
    message_data: MessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    """Send a message in a session (REST fallback; prefer WebSocket for real-time)."""
    try:
        session = await _get_owned_session(message_data.session_id, current_user.id, db)

        if session.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Session is not active"
            )

        message = Message(
            session_id=message_data.session_id,
            role="user",
            content=message_data.content,
            content_type=message_data.content_type,
        )
        db.add(message)
        await db.commit()
        await db.refresh(message)

        logger.info(f"Message created: {message.id}")
        return message

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to send message"
        )


@router.get("/session/{session_id}", response_model=List[MessageResponse])
async def list_session_messages(
    session_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    """List messages in a session (must own the session)."""
    try:
        await _get_owned_session(session_id, current_user.id, db)

        result = await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .offset(skip)
            .limit(limit)
            .order_by(Message.created_at)
        )
        return result.scalars().all()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list messages"
        )


@router.get("/{message_id}", response_model=MessageResponse)
async def get_message(
    message_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    """Get a message by ID (must own the parent session)."""
    try:
        result = await db.execute(select(Message).where(Message.id == message_id))
        message = result.scalar_one_or_none()
        if not message:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

        # Verify ownership via parent session
        await _get_owned_session(message.session_id, current_user.id, db)
        return message

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get message"
        )


class MessageEditPayload(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)


@router.patch("/{message_id}", response_model=MessageResponse)
async def edit_message(
    message_id: str,
    payload: MessageEditPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    """Edit a message's content (must own the parent session)."""
    try:
        result = await db.execute(select(Message).where(Message.id == message_id))
        message = result.scalar_one_or_none()
        if not message:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

        await _get_owned_session(message.session_id, current_user.id, db)
        message.content = payload.content.strip()
        await db.commit()
        await db.refresh(message)
        return message
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to edit message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to edit message"
        )


@router.delete("/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    message_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    """Delete a message (must own the parent session)."""
    try:
        result = await db.execute(select(Message).where(Message.id == message_id))
        message = result.scalar_one_or_none()
        if not message:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

        await _get_owned_session(message.session_id, current_user.id, db)
        await db.delete(message)
        await db.commit()
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to delete message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete message"
        )
