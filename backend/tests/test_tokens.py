"""
Tests for app.tokens — the single place JWTs are minted and verified.

These pin the security contract rather than the implementation, so they stay
meaningful if the backing library changes again. The migration from
python-jose to PyJWT was validated by differentially testing both libraries
against every case below; these tests keep that behaviour from regressing.
"""

import datetime as dt

import jwt as pyjwt
import pytest

from app.config import settings
from app.tokens import create_access_token, decode_token, subject_from_token


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _sign(claims: dict, key: str | None = None, algorithm: str = "HS256") -> str:
    """Mint a token directly, bypassing create_access_token, to forge claims."""
    return pyjwt.encode(claims, key or settings.JWT_SECRET_KEY, algorithm=algorithm)


# ── happy path ───────────────────────────────────────────────────────────
def test_round_trip_preserves_subject():
    token = create_access_token({"sub": "user-1"})
    assert subject_from_token(token) == "user-1"
    assert decode_token(token)["sub"] == "user-1"


def test_issued_token_carries_an_expiry():
    """A token without exp never expires — every token we mint must have one."""
    claims = decode_token(create_access_token({"sub": "user-1"}))
    assert "exp" in claims
    assert claims["exp"] > _now().timestamp()


def test_custom_expiry_is_honoured():
    token = create_access_token({"sub": "u"}, expires_delta=dt.timedelta(seconds=30))
    delta = decode_token(token)["exp"] - _now().timestamp()
    assert 0 < delta <= 31


# ── rejection: these are the security contract ───────────────────────────
def test_expired_token_rejected():
    token = _sign({"sub": "u", "exp": _now() - dt.timedelta(hours=1)})
    assert decode_token(token) is None
    assert subject_from_token(token) is None


def test_token_signed_with_another_key_rejected():
    token = _sign({"sub": "u", "exp": _now() + dt.timedelta(hours=1)}, key="x" * 40)
    assert decode_token(token) is None


def test_algorithm_confusion_rejected():
    """A token signed with a different algorithm must not be accepted."""
    other = "HS512" if settings.JWT_ALGORITHM != "HS512" else "HS384"
    token = _sign({"sub": "u", "exp": _now() + dt.timedelta(hours=1)}, algorithm=other)
    assert decode_token(token) is None


def test_alg_none_rejected():
    """The classic JWT bypass: an unsigned token claiming alg=none."""
    token = pyjwt.encode(
        {"sub": "u", "exp": _now() + dt.timedelta(hours=1)}, key=None, algorithm=None
    )
    assert decode_token(token) is None


def test_token_without_exp_rejected():
    """
    Both PyJWT and python-jose accept an exp-less token by default, which
    would make it a permanent credential. app.tokens requires the claim.
    """
    assert decode_token(_sign({"sub": "u"})) is None


def test_token_without_sub_rejected():
    assert decode_token(_sign({"exp": _now() + dt.timedelta(hours=1)})) is None


@pytest.mark.parametrize("bad_sub", [123, None, ["u"], {"id": "u"}, True])
def test_non_string_subject_never_returned(bad_sub):
    """
    `sub` flows into database queries and ownership comparisons, so a
    non-string value must never be handed back as if it were a user id.
    """
    token = _sign({"sub": bad_sub, "exp": _now() + dt.timedelta(hours=1)})
    assert subject_from_token(token) is None


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c", "....", "Bearer x"])
def test_malformed_tokens_rejected(garbage):
    assert decode_token(garbage) is None
    assert subject_from_token(garbage) is None


def test_tampered_payload_rejected():
    """Flipping a claim invalidates the signature."""
    import base64
    import json

    token = create_access_token({"sub": "user-1"})
    header, payload, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["sub"] = "user-2"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    assert decode_token(f"{header}.{forged}.{sig}") is None
