"""
images/service.py — Image Upload Business Logic
=================================================

VALIDATION GATES (in order)
-----------------------------
1. Case exists and caller has access (patient owns it)
2. Consent given before upload is accepted
3. MIME type is in the allowed list (JPEG, PNG, HEIC, HEIF)
4. File size ≤ MAX_IMAGE_SIZE_MB
5. Case has not reached MAX_IMAGES_PER_CASE

If all gates pass → upload to GCS → insert CaseImage row.

UPLOAD_ORDER
------------
Each new image gets upload_order = current image count.
So if a case has 2 images (orders 0, 1), the new one gets order 2.
Gaps after deletion are not filled — order is for display only.

GCS GRACEFUL DEGRADATION
--------------------------
If GCS credentials are not configured, upload raises StorageException.
This is intentional — the client must know the upload failed.
The CaseImage row is ONLY inserted after successful GCS upload.
No partial state is left in the DB.
"""

import mimetypes
from io import BytesIO

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.exceptions import (
    CaseNotFoundException,
    ForbiddenException,
    ImageNotFoundException,
    InvalidFileTypeException,
    FileTooLargeException,
    TooManyImagesException,
)
from src.images.schemas import ImageListResponse, ImageResponse
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import Case
from src.models.case_image import CaseImage, ImageType
from src.models.user import User, UserRole
from src.storage import gcs

logger = get_logger(__name__)

_ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/heic", "image/heif"}
_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "image/heif": "heif",
}


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _maybe_signed_url(gcs_path: str) -> str | None:
    """
    Return a signed URL for the path, or None if GCS is not configured.
    Never raises — missing GCS just means no URL in dev environments.
    """
    try:
        return gcs.get_signed_url(gcs_path)
    except Exception:
        return None


def _to_image_response(image: CaseImage) -> ImageResponse:
    return ImageResponse(
        id=image.id,
        case_id=image.case_id,
        image_type=image.image_type.value,
        original_filename=image.original_filename,
        mime_type=image.mime_type,
        size_bytes=image.size_bytes,
        upload_order=image.upload_order,
        signed_url=_maybe_signed_url(image.gcs_path),
        created_at=image.created_at,
    )


async def _get_case_or_404(db: AsyncSession, case_id: str, patient: User) -> Case:
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None or case.patient_id != patient.id:
        raise CaseNotFoundException(message=f"No case found with id: {case_id}")
    return case


# ------------------------------------------------------------------ #
# Upload
# ------------------------------------------------------------------ #

async def upload_image(
    db: AsyncSession,
    patient: User,
    case_id: str,
    file: UploadFile,
    image_type: str = "skin",
) -> ImageResponse:
    """
    Validate and upload one image to GCS, then persist metadata.

    Gates:
    1. Case exists + patient owns it
    2. Consent given
    3. MIME type allowed
    4. File size ≤ limit
    5. Image count < max

    Raises:
        CaseNotFoundException     — case not found or not owned by patient
        ForbiddenException        — consent not given
        InvalidFileTypeException  — unsupported MIME type
        FileTooLargeException     — file exceeds MAX_IMAGE_SIZE_MB
        TooManyImagesException    — already at MAX_IMAGES_PER_CASE
        StorageException          — GCS upload failed
    """
    # 1. Case access
    case = await _get_case_or_404(db, case_id, patient)

    # 2. Consent gate
    if not case.consent_given:
        raise ForbiddenException(
            message="Patient must give consent before uploading images"
        )

    # 3. MIME type validation
    content_type = file.content_type or ""
    if content_type not in _ALLOWED_MIME_TYPES:
        # Try to infer from filename
        if file.filename:
            guessed, _ = mimetypes.guess_type(file.filename)
            content_type = guessed or content_type
    if content_type not in _ALLOWED_MIME_TYPES:
        raise InvalidFileTypeException(
            message=f"Unsupported file type: {content_type!r}. "
                    f"Allowed: {', '.join(sorted(_ALLOWED_MIME_TYPES))}"
        )

    # 4. File size
    raw_bytes = await file.read()
    max_bytes = settings.MAX_IMAGE_SIZE_MB * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        raise FileTooLargeException(
            message=f"File size {len(raw_bytes) / 1024 / 1024:.1f} MB "
                    f"exceeds limit of {settings.MAX_IMAGE_SIZE_MB} MB"
        )

    # 5. Image count limit
    count_result = await db.execute(
        select(func.count()).where(CaseImage.case_id == case_id)
    )
    current_count = count_result.scalar_one()
    if current_count >= settings.MAX_IMAGES_PER_CASE:
        raise TooManyImagesException(
            message=f"Case already has {current_count} images "
                    f"(maximum {settings.MAX_IMAGES_PER_CASE})"
        )

    # Resolve image_type enum
    try:
        img_type = ImageType(image_type)
    except ValueError:
        img_type = ImageType.SKIN

    # Build GCS path and upload
    image_id = new_uuid()
    extension = _MIME_TO_EXT.get(content_type, "jpg")
    gcs_path = gcs.build_image_path(case_id, image_id, extension)
    gcs.upload_file(gcs_path, BytesIO(raw_bytes), content_type)

    # Persist metadata only after successful upload
    image = CaseImage(
        id=image_id,
        case_id=case_id,
        gcs_path=gcs_path,
        image_type=img_type,
        original_filename=file.filename,
        mime_type=content_type,
        size_bytes=len(raw_bytes),
        upload_order=current_count,
    )
    db.add(image)
    await db.flush()

    logger.info(
        "image_uploaded",
        image_id=image_id,
        case_id=case_id,
        size_bytes=len(raw_bytes),
    )
    return _to_image_response(image)


# ------------------------------------------------------------------ #
# List
# ------------------------------------------------------------------ #

async def list_images(
    db: AsyncSession,
    user: User,
    case_id: str,
) -> ImageListResponse:
    """
    Return all images for a case, ordered by upload_order.

    Patients see only their own case images.
    Doctors see images for cases assigned to them.
    """
    # Verify the case exists and user has access
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException()
    if user.role == UserRole.PATIENT and case.patient_id != user.id:
        raise CaseNotFoundException()
    if user.role == UserRole.DOCTOR and case.doctor_id != user.id:
        raise CaseNotFoundException()

    images_result = await db.execute(
        select(CaseImage)
        .where(CaseImage.case_id == case_id)
        .order_by(CaseImage.upload_order)
    )
    images = list(images_result.scalars().all())

    return ImageListResponse(
        images=[_to_image_response(img) for img in images],
        total=len(images),
    )


# ------------------------------------------------------------------ #
# Get single
# ------------------------------------------------------------------ #

async def get_image(
    db: AsyncSession,
    user: User,
    case_id: str,
    image_id: str,
) -> ImageResponse:
    """Return a single image with a fresh signed URL."""
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if case is None:
        raise CaseNotFoundException()
    if user.role == UserRole.PATIENT and case.patient_id != user.id:
        raise CaseNotFoundException()
    if user.role == UserRole.DOCTOR and case.doctor_id != user.id:
        raise CaseNotFoundException()

    img_result = await db.execute(
        select(CaseImage).where(
            CaseImage.id == image_id,
            CaseImage.case_id == case_id,
        )
    )
    image = img_result.scalar_one_or_none()
    if image is None:
        raise ImageNotFoundException()

    return _to_image_response(image)


# ------------------------------------------------------------------ #
# Delete
# ------------------------------------------------------------------ #

async def delete_image(
    db: AsyncSession,
    patient: User,
    case_id: str,
    image_id: str,
) -> None:
    """
    Delete an image from both GCS and the DB.

    Only the patient who owns the case can delete images.
    GCS deletion is attempted first; if it fails, the DB row is still removed
    (we don't want orphaned DB rows blocking re-uploads).
    """
    await _get_case_or_404(db, case_id, patient)

    img_result = await db.execute(
        select(CaseImage).where(
            CaseImage.id == image_id,
            CaseImage.case_id == case_id,
        )
    )
    image = img_result.scalar_one_or_none()
    if image is None:
        raise ImageNotFoundException()

    # Delete from GCS (best-effort — don't block DB deletion on GCS failure)
    try:
        gcs.delete_file(image.gcs_path)
    except Exception as exc:
        logger.warning("gcs_delete_failed_continuing", path=image.gcs_path, error=str(exc))

    await db.delete(image)
    logger.info("image_deleted", image_id=image_id, case_id=case_id)
