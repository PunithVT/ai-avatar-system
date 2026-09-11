from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

# Credential rules, shared by registration and profile update so the two
# entry points cannot drift apart.
#
# Password: bcrypt only reads the first 72 bytes, so anything beyond that is
# security theatre; the floor matters far more than the ceiling. Registration
# previously accepted "" and "a" — and an empty password produced an account
# that could never log in again, because /login rejects empty passwords
# before it ever reaches the hash comparison.
_PASSWORD_MIN = 8
_PASSWORD_MAX = 128

# Username: bounded and restricted to characters that are safe to render and
# to embed in URLs. Registration previously accepted "" and a 50,000-char
# value straight into the column.
_USERNAME_MIN = 3
_USERNAME_MAX = 39
_USERNAME_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$"


def _normalize_email(value: Optional[str]) -> Optional[str]:
    """
    Lower-case the address so it has exactly one representation.

    Domains are case-insensitive and effectively every provider treats the
    local part that way too, so without this "User@example.com" and
    "user@example.com" registered as two separate accounts that a person
    would reasonably believe were the same login.
    """
    return value.lower() if isinstance(value, str) else value


# User Schemas
class UserBase(BaseModel):
    email: EmailStr
    username: str = Field(
        ..., min_length=_USERNAME_MIN, max_length=_USERNAME_MAX, pattern=_USERNAME_PATTERN
    )
    full_name: Optional[str] = Field(default=None, max_length=200)

    _norm_email = field_validator("email", mode="before")(_normalize_email)


class UserCreate(UserBase):
    password: str = Field(..., min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX)


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    username: Optional[str] = Field(
        default=None, min_length=_USERNAME_MIN, max_length=_USERNAME_MAX, pattern=_USERNAME_PATTERN
    )
    full_name: Optional[str] = Field(default=None, max_length=200)
    password: Optional[str] = Field(
        default=None, min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX
    )

    _norm_email = field_validator("email", mode="before")(_normalize_email)


class UserResponse(UserBase):
    id: str
    is_active: bool
    is_guest: bool = False
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


class AvatarRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class AvatarMetadataUpdate(BaseModel):
    """Allowed editable metadata fields for an avatar.

    Restrict to a known allowlist so users cannot stuff arbitrary keys into
    the JSON column (which would otherwise let them shadow internal flags or
    bloat the row).
    """

    system_prompt: Optional[str] = Field(default=None, max_length=8000)
    personality: Optional[str] = Field(default=None, max_length=2000)
    background_color: Optional[str] = Field(default=None, max_length=32)
    animation_style: Optional[str] = Field(default=None, max_length=32)

    model_config = {"extra": "forbid"}


# Token Schema
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: Optional[str] = None
