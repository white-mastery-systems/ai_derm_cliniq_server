"""
exceptions.py — Custom Exception Classes & Global Error Handlers
=================================================================

WHY CUSTOM EXCEPTIONS?
-----------------------
Instead of raising generic HTTPException(status_code=404) everywhere,
we define specific exception classes like CaseNotFoundException.

Benefits:
- Readable: `raise CaseNotFoundException(case_id)` is self-documenting.
- Consistent: Every 404 has the same JSON shape.
- Testable: Tests can assert `with pytest.raises(CaseNotFoundException)`.
- Maintainable: Change the 404 format in one place, not 50.

ERROR RESPONSE FORMAT (Flutter will parse this):
-------------------------------------------------
{
    "error": {
        "code": "CASE_NOT_FOUND",
        "message": "No case found with id: abc-123",
        "status": 404,
        "details": {}       ← optional extra info
    }
}

HOW FASTAPI HANDLES EXCEPTIONS
--------------------------------
FastAPI has an `exception_handler` decorator. When our route raises
an `AppException`, our handler catches it and converts it to an
HTTP response with the right status code and JSON body.

We also handle:
- Pydantic ValidationError  → 422 with field-level error details
- Generic Exception         → 500 (never leak tracebacks to clients)
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


# ------------------------------------------------------------------ #
# Base Exception
# ------------------------------------------------------------------ #
class AppException(Exception):
    """
    Base class for all application-specific exceptions.

    Every custom exception inherits from this.
    The global handler catches AppException and returns a JSON response.
    """

    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.details = details or {}
        super().__init__(self.message)


# ------------------------------------------------------------------ #
# 400 Bad Request
# ------------------------------------------------------------------ #
class BadRequestException(AppException):
    status_code = 400
    error_code = "BAD_REQUEST"
    message = "Bad request"


class InvalidFileTypeException(AppException):
    status_code = 400
    error_code = "INVALID_FILE_TYPE"
    message = "Unsupported file type"


class FileTooLargeException(AppException):
    status_code = 400
    error_code = "FILE_TOO_LARGE"
    message = "File exceeds maximum allowed size"


class TooManyImagesException(AppException):
    status_code = 400
    error_code = "TOO_MANY_IMAGES"
    message = "Case already has the maximum number of images"


class ImageQualityException(AppException):
    status_code = 400
    error_code = "IMAGE_QUALITY_FAILED"
    message = "Image did not pass quality checks"


# ------------------------------------------------------------------ #
# 401 Unauthorized
# ------------------------------------------------------------------ #
class UnauthorizedException(AppException):
    status_code = 401
    error_code = "UNAUTHORIZED"
    message = "Authentication required"


class InvalidTokenException(AppException):
    status_code = 401
    error_code = "INVALID_TOKEN"
    message = "Token is invalid or expired"


class InvalidCredentialsException(AppException):
    status_code = 401
    error_code = "INVALID_CREDENTIALS"
    message = "Incorrect email or password"


# ------------------------------------------------------------------ #
# 403 Forbidden
# ------------------------------------------------------------------ #
class ForbiddenException(AppException):
    status_code = 403
    error_code = "FORBIDDEN"
    message = "You do not have permission to perform this action"


class InsufficientRoleException(AppException):
    status_code = 403
    error_code = "INSUFFICIENT_ROLE"
    message = "Your role does not allow this action"


# ------------------------------------------------------------------ #
# 404 Not Found
# ------------------------------------------------------------------ #
class NotFoundException(AppException):
    status_code = 404
    error_code = "NOT_FOUND"
    message = "Resource not found"


class UserNotFoundException(NotFoundException):
    error_code = "USER_NOT_FOUND"
    message = "User not found"


class CaseNotFoundException(NotFoundException):
    error_code = "CASE_NOT_FOUND"
    message = "Case not found"


class ImageNotFoundException(NotFoundException):
    error_code = "IMAGE_NOT_FOUND"
    message = "Image not found"


class ReportNotFoundException(NotFoundException):
    error_code = "REPORT_NOT_FOUND"
    message = "Case report not found"


# ------------------------------------------------------------------ #
# 409 Conflict
# ------------------------------------------------------------------ #
class ConflictException(AppException):
    status_code = 409
    error_code = "CONFLICT"
    message = "Resource already exists"


class EmailAlreadyRegisteredException(ConflictException):
    error_code = "EMAIL_ALREADY_REGISTERED"
    message = "An account with this email already exists"


# ------------------------------------------------------------------ #
# 410 Gone
# ------------------------------------------------------------------ #
class QRTokenExpiredException(AppException):
    status_code = 410
    error_code = "QR_EXPIRED"
    message = "This QR code has expired or already been used"


# ------------------------------------------------------------------ #
# 422 Unprocessable Entity — handled by Pydantic, see handler below
# ------------------------------------------------------------------ #

# ------------------------------------------------------------------ #
# 503 Service Unavailable
# ------------------------------------------------------------------ #
class AIServiceException(AppException):
    status_code = 503
    error_code = "AI_SERVICE_UNAVAILABLE"
    message = "The AI service is temporarily unavailable"


class AIProviderException(AppException):
    status_code = 503
    error_code = "AI_PROVIDER_ERROR"
    message = "The AI provider returned an error or is not configured"


class StorageException(AppException):
    status_code = 503
    error_code = "STORAGE_ERROR"
    message = "File storage service error"


# ------------------------------------------------------------------ #
# Global Exception Handlers — register these on the FastAPI app
# ------------------------------------------------------------------ #
def _error_response(
    status_code: int,
    error_code: str,
    message: str,
    details: dict | None = None,
) -> JSONResponse:
    """Build the standard error JSON response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": error_code,
                "message": message,
                "status": status_code,
                "details": details or {},
            }
        },
    )


async def app_exception_handler(_request: Request, exc: AppException) -> JSONResponse:
    """Handles all our custom AppException subclasses."""
    return _error_response(
        status_code=exc.status_code,
        error_code=exc.error_code,
        message=exc.message,
        details=exc.details,
    )


async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Handles Pydantic validation errors (422).
    Extracts field-level details so the Flutter app can highlight
    which form field has an error.

    Example response:
    {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "status": 422,
            "details": {
                "fields": [
                    {"field": "email", "message": "value is not a valid email address"}
                ]
            }
        }
    }
    """
    field_errors = []
    for error in exc.errors():
        field_errors.append(
            {
                "field": " → ".join(str(loc) for loc in error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
        )
    return _error_response(
        status_code=422,
        error_code="VALIDATION_ERROR",
        message="Request validation failed",
        details={"fields": field_errors},
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all for unhandled exceptions.
    NEVER expose internal error details to the client in production.
    """
    from src.config import settings
    from src.logger import get_logger

    logger = get_logger(__name__)
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        exc_info=exc,
    )

    # In development, include the error message to help debugging
    message = str(exc) if settings.is_development else "An unexpected error occurred"
    return _error_response(
        status_code=500,
        error_code="INTERNAL_ERROR",
        message=message,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register all exception handlers on the FastAPI app instance.
    Call this in main.py during app creation.
    """
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)