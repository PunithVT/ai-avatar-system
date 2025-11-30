from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Boolean, JSON, Float
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import uuid


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
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    avatars = relationship("Avatar", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")


class Avatar(Base):
    __tablename__ = "avatars"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    image_url = Column(String, nullable=False)
    thumbnail_url = Column(String, nullable=True)
    s3_key = Column(String, nullable=False)
    status = Column(String, default="processing")  # processing, ready, failed
    metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    user = relationship("User", back_populates="avatars")
    sessions = relationship("Session", back_populates="avatar")


class Session(Base):
    __tablename__ = "sessions"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    avatar_id = Column(String, ForeignKey("avatars.id"), nullable=False)
    status = Column(String, default="active")  # active, paused, ended
    settings = Column(JSON, nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    ended_at = Column(DateTime(timezone=True), nullable=True)
    
    # Relationships
    user = relationship("User", back_populates="sessions")
    avatar = relationship("Avatar", back_populates="sessions")
    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False)
    role = Column(String, nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    content_type = Column(String, default="text")  # text, audio, video
    audio_url = Column(String, nullable=True)
    video_url = Column(String, nullable=True)
    metadata = Column(JSON, nullable=True)
    tokens = Column(Integer, nullable=True)
    latency = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    session = relationship("Session", back_populates="messages")


class Conversation(Base):
    __tablename__ = "conversations"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False)
    title = Column(String, nullable=True)
    summary = Column(Text, nullable=True)
    message_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class AvatarCache(Base):
    __tablename__ = "avatar_cache"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    avatar_id = Column(String, ForeignKey("avatars.id"), nullable=False)
    text_hash = Column(String, nullable=False, index=True)
    video_url = Column(String, nullable=False)
    s3_key = Column(String, nullable=False)
    duration = Column(Float, nullable=False)
    size = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    accessed_at = Column(DateTime(timezone=True), server_default=func.now())
    access_count = Column(Integer, default=0)
