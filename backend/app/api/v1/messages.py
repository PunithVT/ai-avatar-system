from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import logging

from app.database import get_db
from app.models import Message, Session, User
from app.schemas import MessageCreate, MessageResponse
from app.api.v1.users import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


def _user_id(current_user: Optional[User]) -> str:
    return current_user.id if current_user else "demo-user"


async def _get_owned_session(session_id: str, uid: str, db: AsyncSession) -> Session:
    """Fetch session and verify ownership."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if session.user_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised to access this session")
    return session


@router.post("/send", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def send_message(
    message_data: MessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Send a message in a session (REST fallback; prefer WebSocket for real-time)."""
    try:
        session = await _get_owned_session(message_data.session_id, _user_id(current_user), db)

        if session.status != "active":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Session is not active")

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
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to send message")


@router.get("/session/{session_id}", response_model=List[MessageResponse])
async def list_session_messages(
    session_id: str,
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """List messages in a session (must own the session)."""
    try:
        await _get_owned_session(session_id, _user_id(current_user), db)

        result = await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .offset(skip)
            .limit(min(limit, 500))
            .order_by(Message.created_at)
        )
        return result.scalars().all()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list messages: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list messages")


@router.get("/{message_id}", response_model=MessageResponse)
async def get_message(
    message_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get a message by ID (must own the parent session)."""
    try:
        result = await db.execute(select(Message).where(Message.id == message_id))
        message = result.scalar_one_or_none()
        if not message:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

        # Verify ownership via parent session
        await _get_owned_session(message.session_id, _user_id(current_user), db)
        return message

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get message: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get message")
