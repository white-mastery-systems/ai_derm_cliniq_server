"""
tests/unit/test_auth_security.py — Unit Tests for Auth Security Primitives
===========================================================================

These tests cover the pure-Python functions in:
  - src/auth/security.py  (password hashing, token generation, patient code)
  - src/auth/jwt.py       (token creation and decoding)

WHY UNIT TESTS (NOT INTEGRATION)?
------------------------------------
These functions have no database or network dependency. They take inputs
and return outputs — unit testing is the right level:
- Fast (no I/O)
- Focused (one function at a time)
- Deterministic (no external state)

JWT tests use the TEST_SECRET_KEY already set in conftest.py via
os.environ.setdefault, so settings.SECRET_KEY is always available.
"""

import re
import time

import pytest

from src.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    get_refresh_token_expiry,
)
from src.auth.security import (
    generate_patient_code,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    verify_password,
)
from src.exceptions import InvalidTokenException


# ================================================================== #
# Password Hashing — hash_password / verify_password / needs_rehash
# ================================================================== #

class TestPasswordHashing:
    def test_hash_password_returns_string(self):
        h = hash_password("Password1")
        assert isinstance(h, str)

    def test_hash_password_starts_with_bcrypt_prefix(self):
        h = hash_password("Password1")
        assert h.startswith("$2b$")

    def test_hash_password_produces_different_hashes_for_same_input(self):
        """bcrypt uses a random salt — same password yields different hashes."""
        h1 = hash_password("Password1")
        h2 = hash_password("Password1")
        assert h1 != h2

    def test_verify_password_correct(self):
        h = hash_password("MySecret9")
        assert verify_password("MySecret9", h) is True

    def test_verify_password_wrong(self):
        h = hash_password("MySecret9")
        assert verify_password("WrongPass1", h) is False

    def test_verify_password_case_sensitive(self):
        h = hash_password("Password1")
        assert verify_password("password1", h) is False

    def test_needs_rehash_returns_bool(self):
        h = hash_password("Password1")
        result = needs_rehash(h)
        assert isinstance(result, bool)

    def test_needs_rehash_current_hash_returns_false(self):
        """A freshly generated hash should not need rehashing."""
        h = hash_password("Password1")
        assert needs_rehash(h) is False


# ================================================================== #
# Refresh Token Generation & Hashing
# ================================================================== #

class TestRefreshTokenSecurity:
    def test_generate_refresh_token_returns_string(self):
        token = generate_refresh_token()
        assert isinstance(token, str)

    def test_generate_refresh_token_has_sufficient_length(self):
        """secrets.token_urlsafe(32) → 43 chars (base64url of 32 bytes)."""
        token = generate_refresh_token()
        assert len(token) >= 40

    def test_generate_refresh_token_is_unique(self):
        tokens = {generate_refresh_token() for _ in range(20)}
        assert len(tokens) == 20  # all unique

    def test_hash_refresh_token_returns_64_char_hex(self):
        raw = generate_refresh_token()
        h = hash_refresh_token(raw)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_refresh_token_is_deterministic(self):
        """Same input always produces same hash."""
        raw = generate_refresh_token()
        assert hash_refresh_token(raw) == hash_refresh_token(raw)

    def test_hash_refresh_token_different_inputs_different_hashes(self):
        raw1 = generate_refresh_token()
        raw2 = generate_refresh_token()
        assert hash_refresh_token(raw1) != hash_refresh_token(raw2)


# ================================================================== #
# Patient Code Generation
# ================================================================== #

PATIENT_CODE_PATTERN = re.compile(r"^[A-Z]{3}-\d{4}-[A-Z]$")


class TestPatientCode:
    def test_patient_code_matches_format(self):
        code = generate_patient_code()
        assert PATIENT_CODE_PATTERN.match(code), f"Bad format: {code!r}"

    def test_patient_code_correct_length(self):
        code = generate_patient_code()
        # AAA-9999-Z = 3 + 1 + 4 + 1 + 1 = 10 chars
        assert len(code) == 10

    def test_patient_code_uppercase_letters(self):
        code = generate_patient_code()
        parts = code.split("-")
        assert parts[0].isupper()
        assert parts[2].isupper()

    def test_patient_code_numeric_middle(self):
        code = generate_patient_code()
        parts = code.split("-")
        assert parts[1].isdigit()

    def test_patient_code_generates_different_values(self):
        """Codes should be random — 20 calls should not all be identical."""
        codes = {generate_patient_code() for _ in range(20)}
        assert len(codes) > 1


# ================================================================== #
# JWT — Access Token
# ================================================================== #

class TestAccessToken:
    def test_create_access_token_returns_string(self):
        token = create_access_token("user-123", "patient")
        assert isinstance(token, str)

    def test_create_access_token_has_three_parts(self):
        """JWT format: header.payload.signature"""
        token = create_access_token("user-123", "patient")
        parts = token.split(".")
        assert len(parts) == 3

    def test_decode_access_token_returns_correct_sub(self):
        token = create_access_token("user-abc", "patient")
        payload = decode_access_token(token)
        assert payload["sub"] == "user-abc"

    def test_decode_access_token_returns_correct_role(self):
        token = create_access_token("user-abc", "doctor")
        payload = decode_access_token(token)
        assert payload["role"] == "doctor"

    def test_decode_access_token_type_is_access(self):
        token = create_access_token("user-abc", "patient")
        payload = decode_access_token(token)
        assert payload["type"] == "access"

    def test_decode_access_token_has_exp_and_iat(self):
        token = create_access_token("user-abc", "patient")
        payload = decode_access_token(token)
        assert "exp" in payload
        assert "iat" in payload

    def test_decode_access_token_exp_is_in_future(self):
        token = create_access_token("user-abc", "patient")
        payload = decode_access_token(token)
        assert payload["exp"] > time.time()

    def test_decode_access_token_rejects_tampered_token(self):
        token = create_access_token("user-abc", "patient")
        # Corrupt the signature section (last part)
        parts = token.split(".")
        parts[2] = parts[2][:-4] + "XXXX"
        bad_token = ".".join(parts)
        with pytest.raises(InvalidTokenException):
            decode_access_token(bad_token)

    def test_decode_access_token_rejects_refresh_token(self):
        """Access token decoder must reject refresh tokens."""
        refresh = create_refresh_token("user-abc", "patient")
        with pytest.raises(InvalidTokenException):
            decode_access_token(refresh)

    def test_decode_access_token_rejects_garbage(self):
        with pytest.raises(InvalidTokenException):
            decode_access_token("not.a.jwt")

    def test_decode_access_token_rejects_empty_string(self):
        with pytest.raises(InvalidTokenException):
            decode_access_token("")


# ================================================================== #
# JWT — Refresh Token
# ================================================================== #

class TestRefreshToken:
    def test_create_refresh_token_returns_string(self):
        token = create_refresh_token("user-123", "patient")
        assert isinstance(token, str)

    def test_decode_refresh_token_returns_correct_sub(self):
        token = create_refresh_token("user-xyz", "doctor")
        payload = decode_refresh_token(token)
        assert payload["sub"] == "user-xyz"

    def test_decode_refresh_token_type_is_refresh(self):
        token = create_refresh_token("user-xyz", "doctor")
        payload = decode_refresh_token(token)
        assert payload["type"] == "refresh"

    def test_decode_refresh_token_rejects_access_token(self):
        """Refresh token decoder must reject access tokens."""
        access = create_access_token("user-xyz", "doctor")
        with pytest.raises(InvalidTokenException):
            decode_refresh_token(access)

    def test_decode_refresh_token_rejects_tampered(self):
        token = create_refresh_token("user-xyz", "doctor")
        parts = token.split(".")
        parts[1] = parts[1][:-2] + "ZZ"
        with pytest.raises(InvalidTokenException):
            decode_refresh_token(".".join(parts))


# ================================================================== #
# JWT — Expiry Helper
# ================================================================== #

class TestGetRefreshTokenExpiry:
    def test_expiry_is_in_the_future(self):
        from datetime import datetime, timezone
        expiry = get_refresh_token_expiry()
        now = datetime.now(tz=timezone.utc)
        assert expiry > now

    def test_expiry_is_approximately_30_days(self):
        from datetime import datetime, timezone
        expiry = get_refresh_token_expiry()
        now = datetime.now(tz=timezone.utc)
        delta_days = (expiry - now).days
        # Should be 29 or 30 (datetime arithmetic)
        assert 29 <= delta_days <= 30
