"""
tests/unit/test_exceptions.py — Unit Tests for Custom Exceptions
=================================================================

WHAT WE'RE TESTING
-------------------
1. Each exception class has the correct status_code and error_code.
2. Custom messages can be passed to exceptions.
3. The JSON error response has the expected structure.
4. The global exception handler produces the correct HTTP response.

LESSON: Test exception classes to ensure the error format Flutter
receives is always predictable. If the format changes, these tests
break — warning you before Flutter clients break.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.exceptions import (
    AIServiceException,
    AppException,
    BadRequestException,
    CaseNotFoundException,
    EmailAlreadyRegisteredException,
    ForbiddenException,
    InvalidCredentialsException,
    InvalidTokenException,
    QRTokenExpiredException,
    StorageException,
    UnauthorizedException,
    UserNotFoundException,
    register_exception_handlers,
)


class TestExceptionAttributes:
    """Test that each exception has the correct class-level attributes."""

    def test_app_exception_defaults(self):
        exc = AppException()
        assert exc.status_code == 500
        assert exc.error_code == "INTERNAL_ERROR"

    def test_bad_request(self):
        exc = BadRequestException()
        assert exc.status_code == 400
        assert exc.error_code == "BAD_REQUEST"

    def test_unauthorized(self):
        exc = UnauthorizedException()
        assert exc.status_code == 401
        assert exc.error_code == "UNAUTHORIZED"

    def test_invalid_token(self):
        exc = InvalidTokenException()
        assert exc.status_code == 401
        assert exc.error_code == "INVALID_TOKEN"

    def test_invalid_credentials(self):
        exc = InvalidCredentialsException()
        assert exc.status_code == 401
        assert exc.error_code == "INVALID_CREDENTIALS"

    def test_forbidden(self):
        exc = ForbiddenException()
        assert exc.status_code == 403
        assert exc.error_code == "FORBIDDEN"

    def test_user_not_found(self):
        exc = UserNotFoundException()
        assert exc.status_code == 404
        assert exc.error_code == "USER_NOT_FOUND"

    def test_case_not_found(self):
        exc = CaseNotFoundException()
        assert exc.status_code == 404
        assert exc.error_code == "CASE_NOT_FOUND"

    def test_email_conflict(self):
        exc = EmailAlreadyRegisteredException()
        assert exc.status_code == 409
        assert exc.error_code == "EMAIL_ALREADY_REGISTERED"

    def test_qr_expired(self):
        exc = QRTokenExpiredException()
        assert exc.status_code == 410
        assert exc.error_code == "QR_EXPIRED"

    def test_ai_service_unavailable(self):
        exc = AIServiceException()
        assert exc.status_code == 503
        assert exc.error_code == "AI_SERVICE_UNAVAILABLE"

    def test_storage_error(self):
        exc = StorageException()
        assert exc.status_code == 503
        assert exc.error_code == "STORAGE_ERROR"


class TestCustomMessages:
    """Test that custom messages override the default."""

    def test_custom_message(self):
        exc = CaseNotFoundException(message="No case found with id: abc-123")
        assert exc.message == "No case found with id: abc-123"
        assert str(exc) == "No case found with id: abc-123"

    def test_custom_details(self):
        exc = BadRequestException(
            message="Bad file type",
            details={"received": "application/pdf", "allowed": ["image/jpeg", "image/png"]},
        )
        assert exc.details["received"] == "application/pdf"
        assert "image/jpeg" in exc.details["allowed"]

    def test_empty_details_defaults_to_empty_dict(self):
        exc = AppException()
        assert exc.details == {}


class TestExceptionHandlerIntegration:
    """
    Test the global exception handler produces correct HTTP responses.
    We create a tiny FastAPI app with one test route that raises
    our custom exceptions, then assert the HTTP response.
    """

    @pytest.fixture
    def test_app(self):
        """Tiny FastAPI app with routes that raise our exceptions."""
        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/raise-404")
        async def raise_404():
            raise CaseNotFoundException(message="Case abc-123 not found")

        @app.get("/raise-401")
        async def raise_401():
            raise InvalidTokenException()

        @app.get("/raise-503")
        async def raise_503():
            raise AIServiceException()

        @app.get("/raise-validation")
        async def raise_validation(name: str):  # `name` is required query param
            return {"name": name}

        return app

    @pytest.fixture
    def client(self, test_app):
        return TestClient(test_app, raise_server_exceptions=False)

    def test_404_response_structure(self, client):
        """
        404 response must have the standard error envelope.
        Flutter parses this exact structure.
        """
        response = client.get("/raise-404")
        assert response.status_code == 404

        body = response.json()
        assert "error" in body
        assert body["error"]["code"] == "CASE_NOT_FOUND"
        assert body["error"]["status"] == 404
        assert body["error"]["message"] == "Case abc-123 not found"
        assert "details" in body["error"]

    def test_401_response_structure(self, client):
        response = client.get("/raise-401")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_TOKEN"

    def test_503_response_structure(self, client):
        response = client.get("/raise-503")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AI_SERVICE_UNAVAILABLE"

    def test_422_validation_error_structure(self, client):
        """
        Missing required query param triggers Pydantic validation error.
        Our handler should return 422 with field-level details.
        """
        response = client.get("/raise-validation")  # Missing ?name=
        assert response.status_code == 422

        body = response.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "fields" in body["error"]["details"]
        # At least one field error should reference 'name'
        field_names = [f["field"] for f in body["error"]["details"]["fields"]]
        assert any("name" in f for f in field_names)
