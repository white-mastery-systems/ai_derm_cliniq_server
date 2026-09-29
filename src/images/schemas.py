"""
images/schemas.py — Image Request & Response Models
=====================================================

IMAGE UPLOAD FLOW
-----------------
1. Client sends multipart/form-data with image file + image_type field
2. Service validates: MIME type, file size, case image count limit, consent
3. Service uploads to GCS, stores metadata in CaseImage row
4. Response returns ImageResponse with a signed URL for immediate display

SIGNED URL IN RESPONSE
-----------------------
Every ImageResponse includes a `signed_url` field — a 30-minute HTTPS URL
pointing directly to the GCS object. The Flutter app uses this URL to render
the image without any additional API call.

When the URL expires, the client calls GET /images to get fresh signed URLs.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ImageResponse(BaseModel):
    """
    Single image metadata returned after upload or in list views.

    signed_url: 30-minute GCS download URL. None if GCS is not configured
                (development mode without credentials).
    """
    id: str
    case_id: str
    image_type: str
    original_filename: str | None = None
    mime_type: str
    size_bytes: int
    upload_order: int
    signed_url: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ImageListResponse(BaseModel):
    """All images for a case, ordered by upload_order."""
    images: list[ImageResponse]
    total: int
