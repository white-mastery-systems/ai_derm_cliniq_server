"""
auth/security.py — Password Hashing & Token Hashing
======================================================

TWO HASHING CONCERNS IN AUTH
------------------------------
1. Password hashing   → bcrypt (via passlib)
2. Refresh token hashing → SHA-256 (via hashlib)

WHY DIFFERENT ALGORITHMS?
--------------------------
bcrypt for passwords:
  - Intentionally slow (work factor ~12 rounds ≈ 250ms per hash)
  - Built-in random salt per hash → same password produces different hashes
  - Purpose: make brute-force and rainbow-table attacks computationally expensive

SHA-256 for refresh tokens:
  - Intentionally fast (microseconds)
  - Tokens are already cryptographically random (32 bytes from secrets.token_urlsafe)
  - We need fast lookups by hash: given token → find row in refresh_tokens table
  - bcrypt would be too slow and is designed for low-entropy inputs like passwords

PASSLIB CONTEXT
---------------
CryptContext is passlib's multi-scheme manager.
- schemes=["bcrypt"] → only bcrypt is accepted
- deprecated="auto" → if we add a new scheme later (e.g. argon2), older
  hashes are flagged as needing re-hash on next login (backward compatible)
"""

import hashlib
import secrets

from passlib.context import CryptContext

# ------------------------------------------------------------------ #
# Password Hashing (bcrypt)
# ------------------------------------------------------------------ #
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """
    Hash a plain-text password using bcrypt.

    Returns a 60-character bcrypt hash string that includes:
    - Algorithm identifier ($2b$)
    - Work factor ($12$)
    - Salt (22 chars)
    - Hash (31 chars)

    Example:
        "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"
    """
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain-text password against a bcrypt hash.

    Returns True if they match, False otherwise.
    Uses constant-time comparison internally to prevent timing attacks.

    Usage in login:
        if not verify_password(request.password, user.password_hash):
            raise InvalidCredentialsException()
    """
    return _pwd_context.verify(plain_password, hashed_password)


def needs_rehash(hashed_password: str) -> bool:
    """
    Check if a stored hash needs to be upgraded.

    Returns True if the hash was created with a deprecated algorithm
    or a lower work factor than current settings.

    Use this in the login flow to transparently upgrade old hashes:
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(plain_password)
    """
    return _pwd_context.needs_update(hashed_password)


# ------------------------------------------------------------------ #
# Refresh Token Generation & Hashing (SHA-256)
# ------------------------------------------------------------------ #
def generate_refresh_token() -> str:
    """
    Generate a cryptographically secure URL-safe refresh token.

    Returns a 43-character URL-safe base64 string (256 bits of entropy).
    This is the RAW token sent to the client — never store this in the DB.

    Example: "V3rY-L0ng-S3cur3-T0k3n-V4lu3-H3r3-Ex4mpl3"
    """
    return secrets.token_urlsafe(32)


def hash_refresh_token(raw_token: str) -> str:
    """
    Hash a refresh token using SHA-256 for DB storage.

    Returns a 64-character lowercase hex string.
    Only this hash is stored in refresh_tokens.token_hash.

    Usage:
        raw = generate_refresh_token()    # send to client
        h   = hash_refresh_token(raw)     # store in DB
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


# ------------------------------------------------------------------ #
# Patient Code Generation
# ------------------------------------------------------------------ #
import random
import string


def generate_patient_code() -> str:
    """
    Generate a human-readable patient identifier shown on the patient
    Profile screen. Format: AAA-9999-Z (e.g. "ABC-5482-S")

    Used by doctors to manually look up a patient on their Home screen.
    Uniqueness is enforced by the DB unique constraint on patient_code.
    The auth service retries if a collision occurs (extremely rare).

    Format breakdown:
        3 uppercase letters  →  "ABC"
        dash
        4 digits             →  "5482"
        dash
        1 uppercase letter   →  "S"
    """
    letters = random.choices(string.ascii_uppercase, k=3)
    digits = random.choices(string.digits, k=4)
    suffix = random.choices(string.ascii_uppercase, k=1)
    return f"{''.join(letters)}-{''.join(digits)}-{''.join(suffix)}"
