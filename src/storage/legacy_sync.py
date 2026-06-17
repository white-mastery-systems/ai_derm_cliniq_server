"""
storage/legacy_sync.py — Legacy GCS Mirror
===========================================

After AI analysis completes, mirrors case files into the EXACT folder structure
that the legacy Streamlit app (Dermchatbot2) used:

    master_application_folder/{patient_ts}/complaint_{n}_{diagnosis}/Visit_{ts}/

Where:
    patient_ts  = PatientProfile.patient_code (e.g. QTS-1807-F) for self cases,
                  or Dependent.created_at as YYYYMMDDHHMMSSmmm for dependent cases
    n           = ordinal among cases for this person only (self or dependent counted separately)
    diagnosis   = top diagnosis slug (lowercase, special chars → underscores)
    ts          = case.created_at as Visit_YYYYMMDDHHMMSS (14 digits, no separator)

Files written per case (matching old app file names exactly):
    uploaded_image_1.{ext}, uploaded_image_2.{ext}, ...  ← skin images
    chat_history.txt          ← "Age: N\\nSex: X\\n\\nVisual language model text:\\n{dict_repr}"
    differential_diagnoses.txt ← "VISION BASED DIFFERENTIALS:\\n{dict_repr}"
    question_answer.txt        ← Python list-of-tuples as string
    doubts.txt                 ← "DOUBTS AFTER ITERATION NO. N:\\n{dict_repr}" (one section per round)
    study_metadata.json        ← {"study_arm", "assigned_llm", "total_questions_asked", ...}
    report.pdf                 ← generated clinical report

Images are mirrored from their primary GCS paths AFTER analysis, because the diagnosis
name (required for the complaint folder) is only known once analysis completes.

Every function is best-effort — exceptions are silently swallowed.
A mirror failure NEVER blocks the primary flow.
"""

import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.case import Case
from src.models.dependent import Dependent
from src.models.patient_profile import PatientProfile
from src.storage import gcs

logger = logging.getLogger(__name__)

MASTER_FOLDER = "master_application_folder"


# ------------------------------------------------------------------ #
# Path helpers
# ------------------------------------------------------------------ #

def _ts17(dt: datetime) -> str:
    """Format a datetime as a 17-digit string: YYYYMMDDHHMMSSmmm."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S") + f"{dt.microsecond // 1000:03d}"


def _visit_folder(dt: datetime) -> str:
    """Format a datetime as 'Visit_YYYYMMDDHHMMSS' (14-digit, no separator)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return "Visit_" + dt.strftime("%Y%m%d%H%M%S")


def _diagnosis_slug(name: str) -> str:
    """Convert a diagnosis name to a lowercase underscore slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


# ------------------------------------------------------------------ #
# Prefix builder
# ------------------------------------------------------------------ #

async def get_legacy_prefix(
    session: AsyncSession,
    case: Case,
    diagnosis_name: str,
) -> str | None:
    """
    Build the legacy GCS path prefix for a given case.

    For self cases:      master_application_folder/{patient_code}/complaint_{n}_{slug}/Visit_{ts}
    For dependent cases: master_application_folder/{dep_ts}/complaint_{n}_{slug}/Visit_{ts}

    Returns None if the prefix cannot be computed. Never raises.
    """
    try:
        if case.dependent_id:
            dep_result = await session.execute(
                select(Dependent).where(Dependent.id == case.dependent_id)
            )
            dep = dep_result.scalar_one_or_none()
            if dep is None:
                return None
            patient_folder = _ts17(dep.created_at)
            count_result = await session.execute(
                select(func.count(Case.id)).where(
                    Case.dependent_id == case.dependent_id,
                    Case.created_at <= case.created_at,
                )
            )
        else:
            profile_result = await session.execute(
                select(PatientProfile).where(PatientProfile.user_id == case.patient_id)
            )
            profile = profile_result.scalar_one_or_none()
            if profile is None:
                return None
            patient_folder = profile.patient_code
            count_result = await session.execute(
                select(func.count(Case.id)).where(
                    Case.patient_id == case.patient_id,
                    Case.dependent_id.is_(None),
                    Case.created_at <= case.created_at,
                )
            )

        complaint_n = count_result.scalar() or 1
        slug = _diagnosis_slug(diagnosis_name)
        ts = case.created_at or datetime.now(tz=timezone.utc)

        return (
            f"{MASTER_FOLDER}/{patient_folder}"
            f"/complaint_{complaint_n}_{slug}"
            f"/{_visit_folder(ts)}"
        )
    except Exception:
        logger.debug("legacy_prefix_failed", exc_info=True)
        return None


# ------------------------------------------------------------------ #
# File content builders
# ------------------------------------------------------------------ #

def build_chat_history_txt(age_str: str, sex_str: str, desc_dict: dict) -> str:
    """
    Format chat_history.txt matching the old Streamlit output:
        Age: 28
        Sex: Male

        Visual language model text:
        {'type_of_lesion': ..., ...}
    """
    return f"Age: {age_str}\nSex: {sex_str}\n\nVisual language model text: \n{desc_dict!r}"


def build_differential_txt(diag_dict: dict) -> str:
    """
    Format differential_diagnoses.txt matching the old Streamlit output:
        VISION BASED DIFFERENTIALS:
        {'most_probable_diagnosis': ..., ...}
    """
    return f"VISION BASED DIFFERENTIALS: \n{diag_dict!r}"


def build_doubts_txt(rounds_data: list[tuple[int, dict]]) -> str:
    """
    Format doubts.txt — one section per Q&A round:
        DOUBTS AFTER ITERATION NO. 0:
        {'doubt_present': 'yes', 'doubt': [...]}

        DOUBTS AFTER ITERATION NO. 1:
        ...
    """
    sections = []
    for round_n, doubts_dict in rounds_data:
        sections.append(f"DOUBTS AFTER ITERATION NO. {round_n}: \n{doubts_dict!r}")
    return "\n\n".join(sections)


def build_question_answer_txt(qa_pairs: list[tuple]) -> str:
    """
    Format question_answer.txt as a Python list of 3-tuples per question:
        [('assistant', '...'), ('answer_list', [...]), ('User', '...'), ...]
    """
    return str(qa_pairs)


def build_snomed_txt(snomed_entries: list[dict]) -> str:
    """
    Format snomed_diagnosis.txt:
        Diagnosis: Circinate Balanitis
        SNOMED diagnosis: Circinate balanitis of Reiter's disease (disorder)

    """
    parts = []
    for entry in snomed_entries:
        diag = entry.get("diagnosis", "Unknown")
        snomed = entry.get("snomed_term") or "None"
        parts.append(f"Diagnosis: {diag}\nSNOMED diagnosis: {snomed}\n\n")
    return "".join(parts)


def build_study_metadata(
    prefix: str,
    llm_provider: str,
    total_questions: int,
    n_differentials: int,
    timestamp: str,
) -> dict:
    """Build study_metadata.json dict."""
    return {
        "study_arm": "prospective",
        "assigned_llm": llm_provider,
        "total_questions_asked": total_questions,
        "n_differential_snapshots": n_differentials,
        "visit_folder": prefix,
        "timestamp": timestamp,
    }


# ------------------------------------------------------------------ #
# Upload helpers
# ------------------------------------------------------------------ #

def mirror_bytes(legacy_prefix: str, filename: str, data: bytes, content_type: str) -> None:
    """Upload bytes to the legacy GCS path. Silent on failure."""
    try:
        gcs.upload_file(f"{legacy_prefix}/{filename}", data, content_type)
    except Exception:
        pass


def mirror_json(legacy_prefix: str, filename: str, data: dict) -> None:
    """Serialize a dict to JSON and upload to the legacy GCS path. Silent on failure."""
    try:
        mirror_bytes(
            legacy_prefix, filename,
            json.dumps(data, indent=2, default=str).encode("utf-8"),
            "application/json",
        )
    except Exception:
        pass


def mirror_text(legacy_prefix: str, filename: str, text: str) -> None:
    """Upload a plain text file to the legacy GCS path. Silent on failure."""
    try:
        mirror_bytes(legacy_prefix, filename, text.encode("utf-8"), "text/plain")
    except Exception:
        pass
