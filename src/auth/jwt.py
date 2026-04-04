"""
auth/jwt.py — JWT Token Creation & Decoding
=============================================

JSON Web Tokens (JWT) are the mechanism for stateless authentication.

HOW JWT WORKS IN THIS API
--------------------------
1. Client logs in → server issues:
   - access_token  (short-lived: 60 min)  → sent with every API request
   - refresh_token (long-lived: 30 days)  → used only to get new access tokens

2. Client sends access_token in the Authorization header:
       Authorization: Bearer <access_token>

3. Server decodes the token to identify the user — no DB lookup needed.

4. When the access_token expires, client sends refresh_token to
   POST /api/v1/auth/refresh → gets a new access_token.

JWT STRUCTURE
--------------
A JWT has 3 base64url-encoded parts separated by dots:
    HEADER.PAYLOAD.SIGNATURE

Payload (our "claims"):
    {
        "sub": "user-uuid",          ← subject (who this token belongs to)
        "role": "patient",           ← RBAC role
        "type": "access",            ← "access" or "refresh"
        "exp": 1234567890,           ← expiry (Unix timestamp)
        "iat": 1234567000,           ← issued-at
    }

The signature prevents tampering — if any claim is changed,
the signature verification fails and the token is rejected.

WHY NOT STORE ACCESS TOKENS IN THE DB?
----------------------------------------
Access tokens are short-lived (60 min). Storing them wastes DB space and
adds a DB lookup to every request. We verify them cryptographically.

Only REFRESH tokens are stored (as hashes) because they are long-lived
and need server-side revocation (logout).
"""

from datetime import datetime, timedelta, timezone
from typing import Literal

from jose import JWTError, jwt

from src.config import settings
from src.exceptions import InvalidTokenException


# ------------------------------------------------------------------ #
# Token Types
# ------------------------------------------------------------------ #
TokenType = Literal["access", "refresh"]


# ------------------------------------------------------------------ #
# Token Creation
# ------------------------------------------------------------------ #
def create_access_token(user_id: str, role: str) -> str:
    """
    Create a short-lived JWT access token.

    Expires in settings.ACCESS_TOKEN_EXPIRE_MINUTES (default: 60 min).
    Sent with every API request in the Authorization header.

    Args:
        user_id: The user's UUID (stored as "sub" claim)
        role:    The user's role string ("patient", "doctor", "admin")
    """
    return _create_token(
        user_id=user_id,
        role=role,
        token_type="access",
        expire_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )


def create_refresh_token(user_id: str, role: str) -> str:
    """
    Create a long-lived JWT refresh token.

    Expires in settings.REFRESH_TOKEN_EXPIRE_DAYS (default: 30 days).
    Sent ONLY to POST /api/v1/auth/refresh — not with every request.
    The raw value is stored in the client; a hash is stored in the DB.
    """
    return _create_token(
        user_id=user_id,
        role=role,
        token_type="refresh",
        expire_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )


def _create_token(
    user_id: str,
    role: str,
    token_type: TokenType,
    expire_delta: timedelta,
) -> str:
    """Internal helper — builds and signs a JWT."""
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "type": token_type,
        "iat": now,
        "exp": now + expire_delta,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# ------------------------------------------------------------------ #
# Token Decoding & Validation
# ------------------------------------------------------------------ #
def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.

    Returns the payload dict if valid.
    Raises InvalidTokenException if:
    - Signature is invalid (tampered)
    - Token has expired
    - Token type is not "access"

    Usage in get_current_user dependency:
        payload = decode_access_token(token)
        user_id = payload["sub"]
        role    = payload["role"]
    """
    return _decode_token(token, expected_type="access")


def decode_refresh_token(token: str) -> dict:
    """
    Decode and validate a JWT refresh token.

    Same as decode_access_token but enforces type == "refresh".
    Used in POST /api/v1/auth/refresh.
    """
    return _decode_token(token, expected_type="refresh")


def _decode_token(token: str, expected_type: TokenType) -> dict:
    """Internal helper — decodes and validates a JWT."""
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
    except JWTError:
        raise InvalidTokenException(
            message="Token is invalid, expired, or has been tampered with"
        )

    if payload.get("type") != expected_type:
        raise InvalidTokenException(
            message=f"Expected {expected_type} token, got {payload.get('type')!r}"
        )

    return payload


# ------------------------------------------------------------------ #
# Token Expiry Helper
# ------------------------------------------------------------------ #
def get_refresh_token_expiry() -> datetime:
    """
    Return the expiry datetime for a new refresh token.
    Used when creating a RefreshToken row in the DB.
    """
    return datetime.now(tz=timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
