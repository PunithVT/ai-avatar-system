"""
Issue and verify the JWTs used for API and WebSocket authentication.

Every token in the system is minted and checked here. Before this module the
same decode was written out at three call sites (the API dependency, the rate
limiter, and the WebSocket handshake) and had already drifted — one
type-checked the `sub` claim, the others did not. Verification logic that is
copied is verification logic that eventually disagrees with itself, so it
lives in one place.

Backed by PyJWT rather than python-jose: python-jose pins `pyasn1<0.5.0` and
pulls in `ecdsa`, both of which carry advisories with no fixed release
reachable behind that pin. PyJWT leans on `cryptography` instead and drops
both transitive dependencies. Accept/reject behaviour was differentially
tested against python-jose first — identical on valid, expired, wrong-key,
algorithm-confusion, `alg: none`, malformed and empty input.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from jwt import PyJWTError

from app.config import settings

logger = logging.getLogger(__name__)

# Claims every token we issue carries, enforced at decode time.
#
# `exp` matters most: a token without it never expires, so requiring it means
# a malformed or hand-rolled token can't become a permanent credential. Both
# PyJWT and python-jose accept an exp-less token by default — this closes that.
_REQUIRED_CLAIMS = ["exp", "sub"]


def create_access_token(data: dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """Sign a JWT carrying `data`, expiring after JWT_EXPIRATION_HOURS."""
    to_encode = dict(data)
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(hours=settings.JWT_EXPIRATION_HOURS)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict[str, Any]]:
    """
    Return the verified claims, or None if the token is unusable for any
    reason — bad signature, expired, wrong algorithm, malformed, or missing a
    required claim.

    Callers get None rather than an exception because at every call site an
    invalid token and an absent one mean the same thing: not authenticated.
    Pinning `algorithms` is what stops an attacker re-signing with `alg: none`
    or downgrading to an algorithm we didn't intend to accept.
    """
    if not token:
        return None
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": _REQUIRED_CLAIMS},
        )
    except PyJWTError:
        # Deliberately not logged at warning level: an expired token is a
        # routine event on any long-lived browser session, and logging every
        # one would bury real signals.
        return None


def subject_from_token(token: str) -> Optional[str]:
    """
    Return the `sub` claim (the user id) of a valid token, else None.

    The isinstance check is load-bearing: `sub` reaches database queries and
    ownership comparisons, so a token carrying a non-string `sub` must not be
    handed onward as if it were a user id.
    """
    payload = decode_token(token)
    if payload is None:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None
