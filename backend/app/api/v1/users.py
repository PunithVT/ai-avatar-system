import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import Token, UserCreate, UserResponse, UserUpdate

logger = logging.getLogger(__name__)

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/users/login", auto_error=False)

# bcrypt only inspects the first 72 bytes of input and bcrypt>=4.1 raises
# (rather than silently truncating) on longer input. We use the `bcrypt`
# library directly because passlib 1.7.4 (last released 2020) is incompatible
# with bcrypt 5.x — it can't read the version and mis-handles the backend.
_BCRYPT_MAX_BYTES = 72


def _truncate_password(password: str) -> bytes:
    """Encode to UTF-8 and cap at bcrypt's 72-byte limit (boundary-safe)."""
    encoded = password.encode("utf-8")
    if len(encoded) <= _BCRYPT_MAX_BYTES:
        return encoded
    # Truncate without splitting a multi-byte character mid-sequence.
    return encoded[:_BCRYPT_MAX_BYTES].decode("utf-8", "ignore").encode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if not plain_password or not hashed_password:
        return False
    try:
        return bcrypt.checkpw(_truncate_password(plain_password), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash (e.g. the empty-hash demo user) — treat as no match.
        return False


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(_truncate_password(password), bcrypt.gensalt()).decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(hours=settings.JWT_EXPIRATION_HOURS)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _set_auth_cookie(response: Response, token: str) -> None:
    """Store the JWT in an httpOnly cookie so JS can't read it (XSS-safe)."""
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=token,
        max_age=settings.JWT_EXPIRATION_HOURS * 3600,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        domain=settings.AUTH_COOKIE_DOMAIN,
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.AUTH_COOKIE_NAME,
        domain=settings.AUTH_COOKIE_DOMAIN,
        path="/",
    )


async def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """
    Resolve the current user from (in priority order):
      1. the `Authorization: Bearer` header (API clients, cross-origin)
      2. the httpOnly auth cookie (browser sessions — XSS-safe)
    Returns None when neither yields a valid, known user.
    """
    if token is None:
        token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if token is None:
        return None
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            return None
    except JWTError:
        return None

    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def require_current_user(
    user: Optional[User] = Depends(get_current_user),
) -> User:
    """Require an authenticated user"""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return user


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register_user(user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    """Register a new user"""
    try:
        # Check if email already exists
        result = await db.execute(select(User).where(User.email == user_data.email))
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
            )

        # Check if username already exists
        result = await db.execute(select(User).where(User.username == user_data.username))
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Username already taken"
            )

        user = User(
            email=user_data.email,
            username=user_data.username,
            full_name=user_data.full_name,
            hashed_password=get_password_hash(user_data.password),
        )

        db.add(user)
        await db.commit()
        await db.refresh(user)

        logger.info(f"User registered: {user.id}")
        return user

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to register user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to register user"
        )


@router.post("/login", response_model=Token)
async def login(
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticate and issue a JWT. The token is BOTH returned in the body
    (for API clients) AND set as an httpOnly cookie (for browser sessions —
    not readable by JS, so XSS can't steal it).
    """
    try:
        # Registration lower-cases the address before storing it, so the
        # lookup has to match — otherwise typing your email back with the
        # capitalisation you signed up with would fail to find the account.
        email = form_data.username.strip().lower()
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        # Reject empty passwords explicitly — the demo user is seeded with an
        # empty hash and would otherwise become a passwordless backdoor.
        if (
            not form_data.password
            or not user
            or not user.hashed_password
            or not verify_password(form_data.password, user.hashed_password)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")

        access_token = create_access_token(data={"sub": user.id})
        _set_auth_cookie(response, access_token)
        return Token(access_token=access_token, token_type="bearer")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Login failed"
        )


@router.post("/guest", response_model=Token, status_code=status.HTTP_201_CREATED)
async def create_guest(response: Response, db: AsyncSession = Depends(get_db)):
    """
    Mint a throwaway account for "Continue as Guest" and return its JWT.

    Guests get a real (if anonymous) user row rather than sharing one global
    account. That is what makes the UI's promise true: every ownership check
    in the API compares against this row, so one guest can never see
    another's avatars, sessions or transcripts.

    The row carries an unusable password hash, so the account can only ever
    be reached through the token issued here — there is no password to guess
    and no way to log into it later. Guests are reaped by the
    `cleanup_guest_accounts` task once idle past GUEST_RETENTION_HOURS.
    """
    if not settings.GUEST_ACCOUNTS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Guest access is disabled on this deployment",
        )

    # Collision-proof identifiers: the `guest-` prefix is what the reaper and
    # the UI both key off, and the uuid4 suffix keeps email/username unique.
    marker = uuid.uuid4().hex
    user = User(
        id=f"guest-{marker}",
        email=f"guest-{marker}@guests.example.com",
        username=f"guest-{marker[:12]}",
        full_name="Guest",
        # "*" is not a valid bcrypt hash, so verify_password can never match
        # it — the account is unreachable via /login by construction.
        hashed_password="*",
        is_guest=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    access_token = create_access_token(data={"sub": user.id})
    _set_auth_cookie(response, access_token)
    logger.info(f"Guest account created: {user.id}")
    return Token(access_token=access_token, token_type="bearer")


@router.post("/logout")
async def logout(response: Response):
    """Clear the auth cookie. Stateless JWTs can't be server-revoked, so this
    just removes the browser's cookie; bearer-token clients simply drop theirs."""
    _clear_auth_cookie(response)
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(user: User = Depends(require_current_user)):
    """Get current user profile"""
    return user


@router.put("/me", response_model=UserResponse)
async def update_current_user(
    update_data: UserUpdate,
    user: User = Depends(require_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update current user profile"""
    try:
        if update_data.email is not None:
            result = await db.execute(
                select(User).where(User.email == update_data.email, User.id != user.id)
            )
            if result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
                )
            user.email = update_data.email

        if update_data.username is not None:
            result = await db.execute(
                select(User).where(User.username == update_data.username, User.id != user.id)
            )
            if result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Username already taken"
                )
            user.username = update_data.username

        if update_data.full_name is not None:
            user.full_name = update_data.full_name

        if update_data.password is not None:
            user.hashed_password = get_password_hash(update_data.password)

        await db.commit()
        await db.refresh(user)

        logger.info(f"User updated: {user.id}")
        return user

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to update user"
        )


@router.get("/", response_model=List[UserResponse])
async def list_users(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(require_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all users — superuser only"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    try:
        result = await db.execute(select(User).offset(skip).limit(min(limit, 200)))
        return result.scalars().all()

    except Exception as e:
        logger.error(f"Failed to list users: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list users"
        )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    current_user: User = Depends(require_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get user by ID — own profile or superuser only"""
    if str(current_user.id) != user_id and not current_user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    try:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        return user

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get user"
        )
