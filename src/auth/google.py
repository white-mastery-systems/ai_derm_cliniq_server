"""
auth/google.py — Google ID Token Verification
===============================================

Flutter uses the Google Sign-In SDK which returns an ID token.
We verify this token server-side using Google's public keys.

WHY SERVER-SIDE VERIFICATION?
-------------------------------
The ID token from Flutter could be tampered with if only verified client-side.
Server-side verification uses Google's public JWKS endpoint to validate:
1. The token was actually issued by Google (signature check)
2. The token was issued for OUR app (audience check = GOOGLE_CLIENT_ID)
3. The token has not expired

HOW IT WORKS
------------
1. Flutter → Google Sign-In SDK → gets id_token
2. Flutter → POST /api/v1/auth/google  {id_token: "..."}
3. This module calls Google's token info endpoint to verify
4. Returns the extracted user info (email, name, google_id)

TOKEN INFO ENDPOINT (simple approach)
---------------------------------------
Instead of full JWKS verification (which requires cryptography libraries),
we use Google's tokeninfo endpoint:
    GET https://oauth2.googleapis.com/tokeninfo?id_token=TOKEN

Google verifies the token and returns the claims.
This is simpler and fully reliable — Google does the crypto.

Trade-off: one extra HTTP call per login. Acceptable for auth frequency.
For high-scale systems, use google-auth library with local JWKS verification.
"""

import httpx

from src.config import settings
from src.exceptions import UnauthorizedException
from src.logger import get_logger

logger = get_logger(__name__)

GOOGLE_TOKEN_INFO_URL = "https://oauth2.googleapis.com/tokeninfo"


class GoogleUserInfo:
    """Extracted fields from a verified Google ID token."""

    def __init__(self, google_id: str, email: str, full_name: str) -> None:
        self.google_id = google_id
        self.email = email
        self.full_name = full_name


async def verify_google_id_token(id_token: str) -> GoogleUserInfo:
    """
    Verify a Google ID token and extract user info.

    Raises UnauthorizedException if:
    - The token is invalid or expired
    - The token was not issued for our app (wrong audience)
    - Google's endpoint is unreachable

    Returns GoogleUserInfo with google_id, email, full_name.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(
                GOOGLE_TOKEN_INFO_URL,
                params={"id_token": id_token},
            )
        except httpx.RequestError as e:
            logger.error("google_token_info_request_failed", error=str(e))
            raise UnauthorizedException(
                message="Could not reach Google authentication service"
            )

    if response.status_code != 200:
        logger.warning(
            "google_token_invalid",
            status_code=response.status_code,
            body=response.text[:200],
        )
        raise UnauthorizedException(message="Invalid or expired Google token")

    claims = response.json()

    # Verify the token was issued for our app
    # GOOGLE_CLIENT_ID may be empty in development — skip audience check
    if settings.GOOGLE_CLIENT_ID:
        audience = claims.get("aud", "")
        if audience != settings.GOOGLE_CLIENT_ID:
            logger.warning(
                "google_token_wrong_audience",
                expected=settings.GOOGLE_CLIENT_ID,
                got=audience,
            )
            raise UnauthorizedException(
                message="Google token was not issued for this application"
            )

    google_id = claims.get("sub")
    email = claims.get("email")
    full_name = claims.get("name", email or "Google User")

    if not google_id or not email:
        raise UnauthorizedException(
            message="Google token missing required fields (sub, email)"
        )

    return GoogleUserInfo(
        google_id=google_id,
        email=email,
        full_name=full_name,
    )
