from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import logging
from typing import List, Optional

from pydantic import BaseModel, Field

from app.database import get_db
from app.models import Conversation, Message, Session, User
from app.schemas import ConversationResponse
from app.api.v1.users import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


def _user_id(current_user: Optional[User]) -> str:
    return current_user.id if current_user else "demo-user"


async def _get_owned_session(session_id: str, uid: str, db: AsyncSession) -> Session:
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if session.user_id != uid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised")
    return session


@router.get("/", response_model=List[ConversationResponse])
async def list_conversations(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """List conversations for the current user (joined via session)."""
    try:
        uid = _user_id(current_user)
        result = await db.execute(
            select(Conversation)
            .join(Session, Conversation.session_id == Session.id)
            .where(Session.user_id == uid)
            .order_by(Conversation.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to list conversations: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list conversations",
        )


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get conversation by ID (must own parent session)."""
    try:
        result = await db.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
        conversation = result.scalar_one_or_none()
        if not conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

        await _get_owned_session(conversation.session_id, _user_id(current_user), db)
        return conversation
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get conversation",
        )


@router.get("/session/{session_id}", response_model=List[ConversationResponse])
async def list_session_conversations(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """List conversations for a session (must own it)."""
    try:
        await _get_owned_session(session_id, _user_id(current_user), db)
        result = await db.execute(
            select(Conversation)
            .where(Conversation.session_id == session_id)
            .order_by(Conversation.created_at.desc())
        )
        return result.scalars().all()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list session conversations: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list session conversations",
        )


@router.post(
    "/session/{session_id}",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Create a new conversation for a session."""
    try:
        session = await _get_owned_session(session_id, _user_id(current_user), db)
        if session.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Session is not active",
            )

        conversation = Conversation(
            session_id=session_id,
            title="New Conversation",
            message_count=0,
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        logger.info(f"Conversation created: {conversation.id}")
        return conversation
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to create conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create conversation",
        )


class ConversationRenamePayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


@router.patch("/{conversation_id}/rename", response_model=ConversationResponse)
async def rename_conversation(
    conversation_id: str,
    payload: ConversationRenamePayload,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Rename a conversation."""
    try:
        result = await db.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
        conversation = result.scalar_one_or_none()
        if not conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

        await _get_owned_session(conversation.session_id, _user_id(current_user), db)
        conversation.title = payload.title.strip()
        await db.commit()
        await db.refresh(conversation)
        return conversation
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to rename conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to rename conversation",
        )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Delete a conversation (must own parent session)."""
    try:
        result = await db.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
        conversation = result.scalar_one_or_none()
        if not conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

        await _get_owned_session(conversation.session_id, _user_id(current_user), db)
        await db.delete(conversation)
        await db.commit()
        logger.info(f"Conversation deleted: {conversation_id}")
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to delete conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete conversation",
        )
