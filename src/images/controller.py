"""
images/controller.py — Image HTTP Endpoints
============================================

All routes prefixed /api/v1/cases/{case_id}/images (set in src/api.py).

ROUTES
------
POST   /                → Patient: upload one image to GCS
GET    /                → Patient/Doctor: list all images for a case
GET    /{image_id}      → Patient/Doctor: get single image with fresh signed URL
DELETE /{image_id}      → Patient: delete image from GCS + DB

UPLOAD FORMAT
-------------
multipart/form-data with two fields:
  - file       : the image file (JPEG, PNG, HEIC, HEIF)
  - image_type : optional string, default "skin"

WHY PATIENTS-ONLY FOR UPLOAD/DELETE
-------------------------------------
Only patients own cases and have consent to upload their own skin images.
Doctors read images to review the case — they never upload or delete.
"""

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_patient
from src.database.core import get_async_session
from src.images import service
from src.images.schemas import ImageListResponse, ImageResponse
from src.models.user import User

router = APIRouter()


@router.post(
    "",
    response_model=ImageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an image for a case",
)
async def upload_image(
    case_id: str,
    file: UploadFile = File(...),
    image_type: str = Form(default="skin"),
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> ImageResponse:
    return await service.upload_image(db, patient, case_id, file, image_type)


@router.get(
    "",
    response_model=ImageListResponse,
    status_code=status.HTTP_200_OK,
    summary="List all images for a case",
)
async def list_images(
    case_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> ImageListResponse:
    return await service.list_images(db, user, case_id)


@router.get(
    "/{image_id}",
    response_model=ImageResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a single image with a fresh signed URL",
)
async def get_image(
    case_id: str,
    image_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> ImageResponse:
    return await service.get_image(db, user, case_id, image_id)


@router.delete(
    "/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an image from GCS and the database",
)
async def delete_image(
    case_id: str,
    image_id: str,
    patient: User = Depends(require_patient),
    db: AsyncSession = Depends(get_async_session),
) -> None:
    await service.delete_image(db, patient, case_id, image_id)
