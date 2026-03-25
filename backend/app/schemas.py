from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Dict, Any
from datetime import datetime


# User Schemas
class UserBase(BaseModel):
    email: EmailStr
    username: str
    full_name: Optional[str] = None


class UserCreate(UserBase):
    password: str


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    full_name: Optional[str] = None
    password: Optional[str] = None


class UserResponse(UserBase):
    id: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# Avatar Schemas
class AvatarBase(BaseModel):
    name: str


class AvatarResponse(AvatarBase):
    id: str
    user_id: str
    image_url: str
    thumbnail_url: Optional[str] = None
    status: str
    voice_id: Optional[str] = None
    avatar_metadata: Optional[Dict[str, Any]] = Field(None, alias="avatar_metadata")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


# Session Schemas
class SessionCreate(BaseModel):
    avatar_id: str
    settings: Optional[Dict[str, Any]] = None


class SessionResponse(BaseModel):
    id: str
    user_id: str
    avatar_id: str
    status: str
    settings: Optional[Dict[str, Any]] = None
    started_at: datetime
    ended_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# Message Schemas
class MessageBase(BaseModel):
    content: str
    content_type: str = "text"


class MessageCreate(MessageBase):
    session_id: str


class MessageResponse(MessageBase):
    id: str
    session_id: str
    role: str
    audio_url: Optional[str] = None
    video_url: Optional[str] = None
    message_metadata: Optional[Dict[str, Any]] = Field(None, alias="message_metadata")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


# Conversation Schemas
class ConversationResponse(BaseModel):
    id: str
    session_id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    message_count: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# Token Schema
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: Optional[str] = None
