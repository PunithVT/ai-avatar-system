from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import logging

from app.database import get_db
from app.models import Session, Avatar, User
from app.schemas import SessionCreate, SessionResponse
from app.websocket import websocket_manager
from app.api.v1.users import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


def _user_id(current_user: Optional[User]) -> str:
    return current_user.id if current_user else "demo-user"


@router.post("/create", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    session_data: SessionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Create a new conversation session for the current user."""
    try:
        result = await db.execute(select(Avatar).where(Avatar.id == session_data.avatar_id))
        avatar = result.scalar_one_or_none()

        if not avatar:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Avatar not found")

        if avatar.status != "ready":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Avatar is not ready")

        # Ensure user owns this avatar (or is demo)
        uid = _user_id(current_user)
        if avatar.user_id != uid:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised to use this avatar")

        session = Session(
            user_id=uid,
            avatar_id=session_data.avatar_id,
            status="active",
            settings=session_data.settings or {},
        )

        db.add(session)
        await db.commit()
        await db.refresh(session)

        logger.info(f"Session created: {session.id} (user={uid})")
        return session

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create session")


@router.get("/", response_model=List[SessionResponse])
async def list_sessions(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """List sessions belonging to the current user."""
    try:
        result = await db.execute(
            select(Session)
            .where(Session.user_id == _user_id(current_user))
            .offset(skip)
            .limit(limit)
            .order_by(Session.created_at.desc())
        )
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list sessions")


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get session by ID (must belong to current user)."""
    try:
        result = await db.execute(select(Session).where(Session.id == session_id))
        session = result.scalar_one_or_none()

        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if session.user_id != _user_id(current_user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised to access this session")

        return session
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get session: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get session")


@router.post("/{session_id}/end", response_model=SessionResponse)
async def end_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """End an active session."""
    try:
        result = await db.execute(select(Session).where(Session.id == session_id))
        session = result.scalar_one_or_none()

        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if session.user_id != _user_id(current_user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised to end this session")

        session.status = "ended"
        session.ended_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(session)

        await websocket_manager.disconnect(session_id)

        logger.info(f"Session ended: {session_id}")
        return session

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to end session: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to end session")


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Delete a session (must belong to current user)."""
    try:
        result = await db.execute(select(Session).where(Session.id == session_id))
        session = result.scalar_one_or_none()

        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if session.user_id != _user_id(current_user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised to delete this session")

        await db.delete(session)
        await db.commit()
        logger.info(f"Session deleted: {session_id}")

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to delete session: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete session")
