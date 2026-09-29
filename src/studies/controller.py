from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import require_doctor
from src.database.core import get_async_session
from src.models.user import User
from src.studies import service
from src.studies.schemas import (
    FeatureResponse,
    NextSpecimenCodeResponse,
    StudyResponse,
    SubmitStudyResponse,
)

router = APIRouter()


@router.get(
    "",
    response_model=list[StudyResponse],
    summary="List active studies — doctor home screen card",
)
async def list_studies(
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> list[StudyResponse]:
    return await service.list_active_studies(db)


@router.get(
    "/{study_id}/specimens/next-code",
    response_model=NextSpecimenCodeResponse,
    summary="Get next specimen code for the labelling screen",
)
async def next_specimen_code(
    study_id: str,
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> NextSpecimenCodeResponse:
    return await service.get_next_specimen_code(db, study_id)


@router.get(
    "/{study_id}/features",
    response_model=list[FeatureResponse],
    summary="List trichoscopic features with reference image URLs",
)
async def list_features(
    study_id: str,
    doctor: User = Depends(require_doctor),
) -> list[FeatureResponse]:
    return await service.list_features(study_id)


@router.post(
    "/{study_id}/submit",
    response_model=SubmitStudyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit trichoscopic labelling for a study",
)
async def submit_labelling(
    study_id: str,
    file: UploadFile = File(..., description="Dermoscopic image (JPEG / PNG / WebP)"),
    features: str = Form(
        ...,
        description='JSON object with all 8 trichoscopic features, each "yes"/"no"/"uncertain"',
    ),
    stability: str = Form(..., description='"unstable" | "stable" | "regrowing"'),
    technical_quality: str = Form(..., description='"good" | "acceptable" | "poor"'),
    specimen_code: str | None = Form(default=None, description="Code from /specimens/next-code, e.g. AA-2941-B"),
    doctor: User = Depends(require_doctor),
    db: AsyncSession = Depends(get_async_session),
) -> SubmitStudyResponse:
    return await service.submit_labelling(
        db, doctor, study_id, file, features, stability, technical_quality, specimen_code
    )
