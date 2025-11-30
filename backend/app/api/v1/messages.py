from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import logging
from typing import List

from app.database import get_db
from app.models import Message, Session
from app.schemas import MessageCreate, MessageResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/send", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def send_message(
    message_data: MessageCreate,
    db: AsyncSession = Depends(get_db)
):
    """Send a message in a session"""
    try:
        # Verify session exists
        result = await db.execute(
            select(Session).where(Session.id == message_data.session_id)
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
        
        # Create message
        message = Message(
            session_id=message_data.session_id,
            role="user",
            content=message_data.content,
            content_type=message_data.content_type
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
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send message"
        )


@router.get("/{message_id}", response_model=MessageResponse)
async def get_message(
    message_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get message by ID"""
    try:
        result = await db.execute(
            select(Message).where(Message.id == message_id)
        )
        message = result.scalar_one_or_none()
        
        if not message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Message not found"
            )
        
        return message
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get message"
        )


@router.get("/session/{session_id}", response_model=List[MessageResponse])
async def list_session_messages(
    session_id: str,
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db)
):
    """List messages in a session"""
    try:
        result = await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .offset(skip)
            .limit(limit)
            .order_by(Message.created_at)
        )
        messages = result.scalars().all()
        return messages
    
    except Exception as e:
        logger.error(f"Failed to list messages: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list messages"
        )
