from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import logging
from typing import List

from app.database import get_db
from app.models import Session, Avatar
from app.schemas import SessionCreate, SessionResponse
from app.websocket import websocket_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/create", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    session_data: SessionCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Create new conversation session
    
    - **avatar_id**: ID of avatar to use
    - **settings**: Optional session settings
    """
    try:
        # Verify avatar exists
        result = await db.execute(
            select(Avatar).where(Avatar.id == session_data.avatar_id)
        )
        avatar = result.scalar_one_or_none()
        
        if not avatar:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Avatar not found"
            )
        
        if avatar.status != "ready":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Avatar is not ready"
            )
        
        # Create session
        # For demo, using default user_id
        user_id = "demo-user"
        
        session = Session(
            user_id=user_id,
            avatar_id=session_data.avatar_id,
            status="active",
            settings=session_data.settings or {}
        )
        
        db.add(session)
        await db.commit()
        await db.refresh(session)
        
        logger.info(f"Session created: {session.id}")
        
        return session
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create session"
        )


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get session by ID"""
    try:
        result = await db.execute(
            select(Session).where(Session.id == session_id)
        )
        session = result.scalar_one_or_none()
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )
        
        return session
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get session: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get session"
        )


@router.get("/", response_model=List[SessionResponse])
async def list_sessions(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db)
):
    """List all sessions"""
    try:
        result = await db.execute(
            select(Session).offset(skip).limit(limit)
        )
        sessions = result.scalars().all()
        return sessions
    
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list sessions"
        )


@router.post("/{session_id}/end", response_model=SessionResponse)
async def end_session(
    session_id: str,
    db: AsyncSession = Depends(get_db)
):
    """End a session"""
    try:
        result = await db.execute(
            select(Session).where(Session.id == session_id)
        )
        session = result.scalar_one_or_none()
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )
        
        from datetime import datetime, timezone
        session.status = "ended"
        session.ended_at = datetime.now(timezone.utc)
        
        await db.commit()
        await db.refresh(session)
        
        # Disconnect WebSocket if active
        await websocket_manager.disconnect(session_id)
        
        logger.info(f"Session ended: {session_id}")
        
        return session
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to end session: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to end session"
        )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Delete session"""
    try:
        result = await db.execute(
            select(Session).where(Session.id == session_id)
        )
        session = result.scalar_one_or_none()
        
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found"
            )
        
        await db.delete(session)
        await db.commit()
        
        logger.info(f"Session deleted: {session_id}")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete session: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete session"
        )
