import base64
import io
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import UploadFile
from jinja2 import Template
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.exceptions import BadRequestException, NotFoundException
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.study import Study, StudySubmission
from src.models.user import User
from src.storage import gcs
from src.studies.schemas import (
    FeatureResponse,
    NextSpecimenCodeResponse,
    StudyResponse,
    SubmitStudyResponse,
)

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

FEATURE_DEFINITIONS = [
    {"key": "black_dots",                                    "label": "Black Dots"},
    {"key": "broken_hairs",                                  "label": "Broken Hairs"},
    {"key": "exclamation_mark_hairs",                        "label": "Exclamation Mark Hairs"},
    {"key": "yellow_dots",                                   "label": "Yellow Dots"},
    {"key": "pohl_pinkus_constrictions",                     "label": "Pohl Pinkus Constrictions"},
    {"key": "proximal_tapered_hairs",                        "label": "Proximal Tapered Hairs"},
    {"key": "short_vellous_hairs_or_upright_regrowing_hairs","label": "Short Vellus / Upright Regrowing Hairs"},
    {"key": "pigtail_hairs",                                 "label": "Pigtail Hairs"},
]

TRICHOSCOPIC_FEATURES = [f["key"] for f in FEATURE_DEFINITIONS]
FEATURE_VALUES    = {"yes", "no", "uncertain"}
VALID_STABILITY   = {"unstable", "stable", "regrowing"}
VALID_QUALITY     = {"good", "acceptable", "poor"}
_ALLOWED_MIME     = {"image/jpeg", "image/png", "image/webp"}
_MIME_TO_EXT      = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

_GCS_REF_PREFIX = "studies/reference_images"


# ------------------------------------------------------------------ #
# GCS path helpers
# ------------------------------------------------------------------ #

def _submission_image_path(study_id: str, submission_id: str, ext: str) -> str:
    return f"studies/{study_id}/submissions/{submission_id}.{ext}"


def _submission_report_path(study_id: str, submission_id: str) -> str:
    return f"studies/{study_id}/submissions/{submission_id}_report.pdf"


def _ref_image_path(feature_key: str) -> str:
    return f"{_GCS_REF_PREFIX}/{feature_key}.png"


# ------------------------------------------------------------------ #
# PDF template
# ------------------------------------------------------------------ #

_REPORT_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  body  { font-family: Arial, sans-serif; font-size: 11pt; color: #1a1a1a; margin: 40px; }
  h1    { font-size: 18pt; color: #1a3a5c; border-bottom: 2px solid #1a3a5c; padding-bottom: 6px; }
  h2    { font-size: 13pt; color: #1a3a5c; margin-top: 24px; border-bottom: 1px solid #cce0f0; padding-bottom: 4px; }
  .meta { color: #555; font-size: 10pt; margin-bottom: 20px; }
  .box  { background: #f5f9fd; border-left: 4px solid #2c7be5; padding: 10px 14px; margin: 10px 0; }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 10pt; }
  th    { background: #1a3a5c; color: #fff; padding: 6px 10px; text-align: left; }
  td    { padding: 5px 10px; border-bottom: 1px solid #e0e0e0; }
  tr:nth-child(even) td { background: #f5f9fd; }
  .badge-yes       { color: #1e7e34; font-weight: bold; }
  .badge-no        { color: #c0392b; }
  .badge-uncertain { color: #e07b00; }
  .stability       { display: inline-block; padding: 3px 10px; border-radius: 4px;
                     background: #1a3a5c; color: #fff; font-weight: bold; text-transform: capitalize; }
  .footer { margin-top: 40px; font-size: 9pt; color: #888; border-top: 1px solid #ddd; padding-top: 8px; }
  img.specimen { max-width: 280px; max-height: 220px; border: 1px solid #ccc; margin: 10px 0; }
</style>
</head>
<body>

<h1>Trichoscopic Stability Model — Labelling Report</h1>
<p class="meta">
  Study: {{ study_title }}<br>
  Specimen: {{ specimen_code }}<br>
  Doctor: {{ doctor_name }}<br>
  Submitted: {{ submitted_at }}
</p>

{% if image_b64 %}
<h2>Dermoscopic Image</h2>
<img class="specimen" src="data:image/jpeg;base64,{{ image_b64 }}" alt="Specimen image" />
{% endif %}

<h2>Trichoscopic Features</h2>
<table>
  <tr><th>Feature</th><th>Selection</th></tr>
  {% for f in features %}
  <tr>
    <td>{{ f.label }}</td>
    <td>
      {% if f.value == 'yes' %}<span class="badge-yes">Yes</span>
      {% elif f.value == 'no' %}<span class="badge-no">No</span>
      {% else %}<span class="badge-uncertain">Uncertain</span>{% endif %}
    </td>
  </tr>
  {% endfor %}
</table>

<h2>Lesion Stability Assessment</h2>
<div class="box">
  <span class="stability">{{ stability }}</span>
</div>

<h2>Technical Quality</h2>
<div class="box" style="text-transform: capitalize;">{{ technical_quality }}</div>

<p class="footer">AiDerm Cliniq — Trichoscopic Stability Model Study &bull; Generated by AI Derm Cliniq</p>
</body>
</html>
"""


def _render_pdf(
    study_title: str,
    specimen_code: str | None,
    doctor_name: str,
    image_bytes: bytes,
    features_parsed: dict[str, str],
    stability: str,
    technical_quality: str,
) -> bytes:
    image_b64 = base64.b64encode(image_bytes).decode("ascii") if image_bytes else ""

    feature_rows = [
        {"label": f["label"], "value": features_parsed.get(f["key"], "uncertain")}
        for f in FEATURE_DEFINITIONS
    ]

    html = Template(_REPORT_HTML).render(
        study_title=study_title,
        specimen_code=specimen_code or "N/A",
        doctor_name=doctor_name,
        submitted_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        image_b64=image_b64,
        features=feature_rows,
        stability=stability,
        technical_quality=technical_quality,
    )

    from xhtml2pdf import pisa
    buffer = io.BytesIO()
    result = pisa.CreatePDF(html, dest=buffer)
    if result.err:
        raise RuntimeError(f"xhtml2pdf failed with {result.err} errors")
    return buffer.getvalue()


# ------------------------------------------------------------------ #
# Service functions
# ------------------------------------------------------------------ #

async def list_active_studies(db: AsyncSession) -> list[StudyResponse]:
    result = await db.execute(select(Study).where(Study.is_active == True))
    studies = result.scalars().all()

    responses = []
    for study in studies:
        count_result = await db.execute(
            select(func.count()).where(StudySubmission.study_id == study.id)
        )
        count = count_result.scalar_one()
        responses.append(
            StudyResponse(
                id=study.id,
                title=study.title,
                description=study.description,
                is_active=study.is_active,
                submission_count=count,
            )
        )
    return responses


async def get_next_specimen_code(
    db: AsyncSession, study_id: str
) -> NextSpecimenCodeResponse:
    result = await db.execute(
        select(Study).where(Study.id == study_id, Study.is_active == True)
    )
    if result.scalar_one_or_none() is None:
        raise NotFoundException(message=f"Study '{study_id}' not found or inactive")

    count_result = await db.execute(
        select(func.count()).where(StudySubmission.study_id == study_id)
    )
    next_num = count_result.scalar_one() + 1
    code = f"AA-{next_num:04d}-B"
    return NextSpecimenCodeResponse(specimen_code=code)


async def list_features(study_id: str) -> list[FeatureResponse]:
    responses = []
    for f in FEATURE_DEFINITIONS:
        ref_url = None
        gcs_path = _ref_image_path(f["key"])
        try:
            ref_url = gcs.get_signed_url(gcs_path, expiry_minutes=120)
        except Exception:
            pass
        responses.append(
            FeatureResponse(key=f["key"], label=f["label"], reference_image_url=ref_url)
        )
    return responses


async def submit_labelling(
    db: AsyncSession,
    doctor: User,
    study_id: str,
    file: UploadFile,
    features_json: str,
    stability: str,
    technical_quality: str,
    specimen_code: str | None,
) -> SubmitStudyResponse:
    result = await db.execute(
        select(Study).where(Study.id == study_id, Study.is_active == True)
    )
    study = result.scalar_one_or_none()
    if study is None:
        raise NotFoundException(message=f"Study '{study_id}' not found or inactive")

    if stability not in VALID_STABILITY:
        raise BadRequestException(
            message=f"Invalid stability. Must be one of: {', '.join(sorted(VALID_STABILITY))}"
        )

    if technical_quality not in VALID_QUALITY:
        raise BadRequestException(
            message=f"Invalid technical_quality. Must be one of: {', '.join(sorted(VALID_QUALITY))}"
        )

    try:
        features: dict[str, Any] = json.loads(features_json)
    except (json.JSONDecodeError, ValueError):
        raise BadRequestException(message="features must be a valid JSON string")

    missing = [f for f in TRICHOSCOPIC_FEATURES if f not in features]
    if missing:
        raise BadRequestException(message=f"Missing features: {', '.join(missing)}")

    invalid_vals = [
        f"{k}={v!r}"
        for k, v in features.items()
        if k in TRICHOSCOPIC_FEATURES and v not in FEATURE_VALUES
    ]
    if invalid_vals:
        raise BadRequestException(
            message=f"Invalid feature values (must be yes/no/uncertain): {', '.join(invalid_vals)}"
        )

    content_type = file.content_type or ""
    if content_type not in _ALLOWED_MIME:
        raise BadRequestException(message="Image must be JPEG, PNG, or WebP")

    raw_bytes = await file.read()
    if not raw_bytes:
        raise BadRequestException(message="Uploaded file is empty")

    # ── Upload image to GCS ──
    submission_id = new_uuid()
    ext = _MIME_TO_EXT[content_type]
    img_gcs_path = _submission_image_path(study_id, submission_id, ext)
    gcs.upload_file(img_gcs_path, io.BytesIO(raw_bytes), content_type)

    # ── Generate PDF ──
    report_url = None
    try:
        pdf_bytes = _render_pdf(
            study_title=study.title,
            specimen_code=specimen_code,
            doctor_name=doctor.full_name or doctor.email,
            image_bytes=raw_bytes,
            features_parsed=features,
            stability=stability,
            technical_quality=technical_quality,
        )
        pdf_gcs_path = _submission_report_path(study_id, submission_id)
        gcs.upload_file(pdf_gcs_path, io.BytesIO(pdf_bytes), "application/pdf")
        report_url = gcs.get_signed_url(pdf_gcs_path, expiry_minutes=30)
    except Exception as exc:
        logger.warning(
            "study_submission_pdf_failed",
            study_id=study_id,
            submission_id=submission_id,
            error=str(exc),
        )

    # ── Persist submission ──
    submission = StudySubmission(
        id=submission_id,
        study_id=study_id,
        doctor_id=doctor.id,
        gcs_image_path=img_gcs_path,
        specimen_code=specimen_code,
        features=json.dumps(features),
        stability=stability,
        technical_quality=technical_quality,
    )
    db.add(submission)
    await db.flush()

    logger.info(
        "study_submission_saved",
        study_id=study_id,
        submission_id=submission_id,
        specimen_code=specimen_code,
        doctor_id=doctor.id,
    )
    return SubmitStudyResponse(
        submission_id=submission_id,
        specimen_code=specimen_code,
        report_url=report_url,
    )
