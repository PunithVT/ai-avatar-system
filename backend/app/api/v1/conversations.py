from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import logging
from typing import List

from app.database import get_db
from app.models import Conversation, Message, Session
from app.schemas import ConversationResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_model=List[ConversationResponse])
async def list_conversations(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db)
):
    """List all conversations"""
    try:
        result = await db.execute(
            select(Conversation)
            .order_by(Conversation.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        conversations = result.scalars().all()
        return conversations

    except Exception as e:
        logger.error(f"Failed to list conversations: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list conversations"
        )


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get conversation by ID"""
    try:
        result = await db.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
        conversation = result.scalar_one_or_none()

        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found"
            )

        return conversation

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get conversation"
        )


@router.get("/session/{session_id}", response_model=List[ConversationResponse])
async def list_session_conversations(
    session_id: str,
    db: AsyncSession = Depends(get_db)
):
    """List conversations for a session"""
    try:
        # Verify session exists
        result = await db.execute(
            select(Session).where(Session.id == session_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )

        result = await db.execute(
            select(Conversation)
            .where(Conversation.session_id == session_id)
            .order_by(Conversation.created_at.desc())
        )
        conversations = result.scalars().all()
        return conversations

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list session conversations: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list session conversations"
        )


@router.post("/session/{session_id}", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    session_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Create a new conversation for a session"""
    try:
        # Verify session exists and is active
        result = await db.execute(
            select(Session).where(Session.id == session_id)
        )
        session = result.scalar_one_or_none()

        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )

        if session.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Session is not active"
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
        logger.error(f"Failed to create conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create conversation"
        )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Delete a conversation"""
    try:
        result = await db.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
        conversation = result.scalar_one_or_none()

        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found"
            )

        await db.delete(conversation)
        await db.commit()

        logger.info(f"Conversation deleted: {conversation_id}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete conversation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete conversation"
        )
