import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


def generate_uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=generate_uuid)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    # Anonymous "Continue as Guest" account. Indexed because the retention
    # sweep selects on it, and it is the flag the UI uses to prompt for a
    # real sign-up before the account is reaped.
    is_guest = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    avatars = relationship("Avatar", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")


class Avatar(Base):
    __tablename__ = "avatars"

    id = Column(String, primary_key=True, default=generate_uuid)
    # index=True is required for the hot "list my avatars" query — without it
    # PostgreSQL falls back to a sequential scan once the table grows past a
    # few thousand rows, turning a 5 ms lookup into a 500 ms one.
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    image_url = Column(String, nullable=False)
    thumbnail_url = Column(String, nullable=True)
    s3_key = Column(String, nullable=False)
    status = Column(String, default="processing")  # processing, ready, failed
    voice_id = Column(
        String, nullable=True, index=True
    )  # so voice-deletion clears references quickly
    avatar_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="avatars")
    # Deleting an avatar deletes its sessions (and, transitively, their
    # messages/conversations). Without the cascade, deleting an avatar that
    # had ever been chatted with raised a NOT NULL/FK violation → HTTP 500.
    sessions = relationship("Session", back_populates="avatar", cascade="all, delete-orphan")

    __table_args__ = (
        # `ORDER BY created_at DESC LIMIT N` is the list-avatars query —
        # the composite covers both predicate columns for a single index scan.
        Index("ix_avatars_user_created", "user_id", "created_at"),
    )


class Session(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    avatar_id = Column(
        String, ForeignKey("avatars.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status = Column(String, default="active", index=True)  # active/paused/ended filters by this
    settings = Column(JSON, nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    ended_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="sessions")
    avatar = relationship("Avatar", back_populates="sessions")
    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")
    # Conversations hang off sessions too — without this cascade, deleting a
    # session that had been auto-titled (i.e. any session with at least one
    # turn) raised an FK violation → HTTP 500 from the delete endpoint.
    conversations = relationship(
        "Conversation", back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # HistoryPanel's "list my sessions ordered by recency" query.
        Index("ix_sessions_user_started", "user_id", "started_at"),
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(String, primary_key=True, default=generate_uuid)
    session_id = Column(
        String, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(String, nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    content_type = Column(String, default="text")  # text, audio, video
    audio_url = Column(String, nullable=True)
    video_url = Column(String, nullable=True)
    message_metadata = Column(JSON, nullable=True)
    tokens = Column(Integer, nullable=True)
    latency = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    session = relationship("Session", back_populates="messages")

    __table_args__ = (
        # Covers the chat-history + WS rehydration query
        # `WHERE session_id=? ORDER BY created_at`. The DESC variant uses the
        # same index because Postgres can scan it backwards.
        Index("ix_messages_session_created", "session_id", "created_at"),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String, primary_key=True, default=generate_uuid)
    session_id = Column(
        String, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String, nullable=True)
    summary = Column(Text, nullable=True)
    message_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    session = relationship("Session", back_populates="conversations")
