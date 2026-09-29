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
    """Convert a diagnosis name to a lowercase underscore slug matching the old Streamlit app:
       re.sub(r"[^\\w\\s-]", "", name).strip().replace(" ", "_").lower()
    Preserves hyphens (old app kept them), e.g. "Lichen Planus - Cutaneous" → "lichen_planus_-_cutaneous".
    """
    return re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_").lower()


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

        # Anchor the slug to the round-0 DifferentialDiagnosis (the initial AI result).
        # This prevents the folder path from drifting when _finalize_case or the doctor
        # later updates case.case_title. diagnosis_name is kept as a fallback for the
        # very first call from save_results_task (before the DD row is committed).
        canonical_name = diagnosis_name
        try:
            from src.models.differential_diagnosis import DifferentialDiagnosis as _DD
            _dd_result = await session.execute(
                select(_DD).where(
                    _DD.case_id == case.id,
                    _DD.round_number == 0,
                )
            )
            _dd0 = _dd_result.scalar_one_or_none()
            if _dd0 and _dd0.most_probable_diagnosis:
                canonical_name = _dd0.most_probable_diagnosis
        except Exception:
            pass

        slug = _diagnosis_slug(canonical_name)
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


def build_chat_history_with_qa_txt(
    age_str: str,
    sex_str: str,
    desc_dict: dict,
    ai_msgs_sorted: list,
    pat_answers: dict,
) -> str:
    """
    Full chat_history.txt after Q&A rounds — matches old app periodic_save():
        Age: N
        Sex: X

        Visual language model text:
        {desc_dict!r}

        assistant: {question_text}
        user: {patient_answer}
        ...

    ai_msgs_sorted: AI Message objects sorted by (round_number, question_index).
    pat_answers: dict keyed by (round_number, question_index) → patient answer text.
    """
    content = f"Age: {age_str}\nSex: {sex_str}\n\nVisual language model text: \n{desc_dict!r}\n\n"
    for msg in ai_msgs_sorted:
        try:
            q_data = json.loads(msg.content or "")
            q_text = q_data.get("question", msg.content or "")
        except Exception:
            q_text = msg.content or ""
        rn = getattr(msg, "round_number", 0) or 0
        qi = getattr(msg, "question_index", 0) or 0
        answer = pat_answers.get((rn, qi), "")
        content += f"assistant: {q_text}\n"
        if answer:
            content += f"user: {answer}\n"
    return content


def build_all_differentials_txt(diffs: list[tuple[int, dict]]) -> str:
    """
    Format differential_diagnoses.txt with ALL Q&A rounds — matches old app
    save_differential_diagnoses():
        VISION BASED DIFFERENTIALS:
        {round_0_dict!r}

        DIFFERENTIAL AFTER ITERATION NO. 1:
        {round_1_dict!r}
        ...

    diffs: [(round_number, diag_dict), ...] sorted ascending by round_number.
    """
    sections = []
    for idx, (rn, d) in enumerate(diffs):
        if idx == 0:
            sections.append(f"VISION BASED DIFFERENTIALS: \n{d!r}")
        else:
            sections.append(f"DIFFERENTIAL AFTER ITERATION NO. {rn}: \n{d!r}")
    return "\n\n".join(sections)


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


# ------------------------------------------------------------------ #
# Doctor-consultation helpers
# ------------------------------------------------------------------ #

DOCTOR_INITIATED_FOLDER = "doctor_initiated_cases"


def get_doctor_initiated_prefix(case: Case, diagnosis_name: str) -> str:
    """
    Build the legacy GCS prefix for a doctor-initiated (standalone diagnose) case:
        doctor_initiated_cases/{diagnosis_slug}_{YYYYMMDDHHMMSS}/

    These cases go under a separate root folder from the patient flow
    (master_application_folder/), matching the old Dermchatbot2 behaviour.
    """
    ts = case.created_at or datetime.now(tz=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    slug = _diagnosis_slug(diagnosis_name)
    return f"{DOCTOR_INITIATED_FOLDER}/{slug}_{ts.strftime('%Y%m%d%H%M%S')}"


def build_doctor_agent_conversation_txt(qa_history: list) -> str:
    """
    Build doctor_agent_conversation.txt written into the patient-case Visit folder.

    Matches old app format — one section per Q&A turn:
        \\n\\n=== Conversation ===\\nDoctor: {question}\\nAI Agent: {answer}\\n

    qa_history is a list of dicts with 'question' and 'answer' keys.
    Since we reconstruct from the full cumulative list each time, this
    overwrites (not appends) — the result is identical to the old app's
    append approach.
    """
    if not qa_history:
        return ""
    parts = []
    for item in qa_history:
        if isinstance(item, dict):
            q = item.get("question", "")
            a = item.get("answer", "")
        else:
            q = getattr(item, "question", "")
            a = getattr(item, "answer", "")
        parts.append(f"\n\n=== Conversation ===\nDoctor: {q}\nAI Agent: {a}\n")
    return "".join(parts)


def build_doctor_initiated_chat_history_txt(
    age_str: str, sex_str: str, messages: list[tuple[str, str]]
) -> str:
    """
    Format chat_history.txt for doctor-initiated cases (plain text, NOT Python repr).

    Old app format:
        Age: N
        Sex: X

        Conversation History:
        role: content
    """
    lines = [f"Age: {age_str}", f"Sex: {sex_str}", "", "Conversation History:"]
    for role, content in messages:
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def build_doctor_initiated_presenting_complaints_json(complaint_text: str | None) -> str:
    """
    Format presenting_complaints.json for doctor-initiated cases.
    Old app stored st.session_state.presenting_complaints (a list of strings).
    Pack the case presenting_complaint string as a 1-item list.
    """
    items = [complaint_text] if complaint_text else []
    return json.dumps(items, indent=2)


async def mirror_doctor_agent_conversation(
    session: AsyncSession,
    case: Case,
    qa_history: list,
) -> None:
    """
    Write doctor_agent_conversation.txt into the patient-case Visit folder.
    Called whenever the doctor saves qa_history via PATCH /review.
    No-ops silently if the case has no diagnosis yet (prefix cannot be built).
    """
    try:
        if not case.case_title:
            return
        prefix = await get_legacy_prefix(session, case, case.case_title)
        if not prefix:
            return
        content = build_doctor_agent_conversation_txt(qa_history)
        if content:
            mirror_text(prefix, "doctor_agent_conversation.txt", content)
            logger.info("mirror_doctor_agent_conversation_ok", case_id=case.id)
    except Exception:
        logger.debug("mirror_doctor_agent_conversation_failed", case_id=case.id, exc_info=True)


async def mirror_doctor_initiated_case(
    session: AsyncSession,
    case: Case,
    review: object,
) -> None:
    """
    Full mirror of a doctor-initiated case to doctor_initiated_cases/ after review completion.
    Only runs when the patient has a @placeholder.aiderm.internal email (standalone diagnose flow).

    Files written (JSON format — matching the old Doctor Diagnostic Page):
        clinical_image_N.png, dermoscopy_image_N.png, pathology_image_N.png
        chat_history.txt          (plain text, NOT Python repr)
        differential_diagnoses.txt (JSON, NOT Python repr)
        question_answer.txt        (JSON — doctor Q&A history)
        presenting_complaints.json (JSON list)
    """
    try:
        from src.models.case_image import CaseImage, ImageType
        from src.models.differential_diagnosis import DifferentialDiagnosis
        from src.models.message import Message, MessageRole
        from src.models.patient_profile import PatientProfile
        from src.models.user import User

        # Only mirror placeholder-patient (doctor-standalone) cases
        patient_result = await session.execute(
            select(User).where(User.id == case.patient_id)
        )
        patient = patient_result.scalar_one_or_none()
        if patient is None or "@placeholder.aiderm.internal" not in (patient.email or ""):
            return

        # Resolve diagnosis name from confirmed_diagnosis → case_title → fallback
        diag_name = case.case_title or "unknown_diagnosis"
        if review is not None and getattr(review, "confirmed_diagnosis", None):
            try:
                diag_list = json.loads(review.confirmed_diagnosis)
                if diag_list:
                    diag_name = diag_list[0]
            except Exception:
                pass

        prefix = get_doctor_initiated_prefix(case, diag_name)

        # ── Images (named by type: clinical / dermoscopy / pathology) ── #
        imgs_result = await session.execute(
            select(CaseImage)
            .where(CaseImage.case_id == case.id)
            .order_by(CaseImage.upload_order)
        )
        clinical_n = dermoscopy_n = pathology_n = 1
        for img in imgs_result.scalars():
            try:
                img_bytes = gcs.download_bytes(img.gcs_path)
                t = img.image_type
                if t == ImageType.DERMOSCOPY:
                    fname = f"dermoscopy_image_{dermoscopy_n}.png"
                    dermoscopy_n += 1
                elif t == ImageType.PATHOLOGY:
                    fname = f"pathology_image_{pathology_n}.png"
                    pathology_n += 1
                else:
                    fname = f"clinical_image_{clinical_n}.png"
                    clinical_n += 1
                mirror_bytes(prefix, fname, img_bytes, "image/png")
            except Exception:
                pass

        # ── Age / Sex from PatientProfile ─────────────────────────────── #
        profile_result = await session.execute(
            select(PatientProfile).where(PatientProfile.user_id == case.patient_id)
        )
        profile = profile_result.scalar_one_or_none()
        if profile and profile.date_of_birth:
            today = datetime.now(tz=timezone.utc).date()
            age_str = str((today - profile.date_of_birth).days // 365)
        else:
            age_str = "Unknown"
        sex_str = (profile.gender or "Unknown") if profile else "Unknown"

        # ── chat_history.txt ──────────────────────────────────────────── #
        msgs_result = await session.execute(
            select(Message).where(Message.case_id == case.id).order_by(Message.created_at)
        )
        conv: list[tuple[str, str]] = []
        for msg in msgs_result.scalars():
            role = "assistant" if msg.role == MessageRole.AI else "User"
            conv.append((role, msg.content or ""))
        mirror_text(
            prefix, "chat_history.txt",
            build_doctor_initiated_chat_history_txt(age_str, sex_str, conv),
        )

        # ── differential_diagnoses.txt (JSON) ─────────────────────────── #
        diff_result = await session.execute(
            select(DifferentialDiagnosis)
            .where(DifferentialDiagnosis.case_id == case.id)
            .order_by(DifferentialDiagnosis.round_number.desc())
        )
        latest_diff = diff_result.scalars().first()
        diff_dict: dict = {}
        if latest_diff and latest_diff.diagnosis_json:
            try:
                diff_dict = json.loads(latest_diff.diagnosis_json)
            except Exception:
                pass
        mirror_text(prefix, "differential_diagnoses.txt", json.dumps(diff_dict, indent=2))

        # ── question_answer.txt (doctor Q&A in JSON) ──────────────────── #
        qa_list: list = []
        if review is not None and getattr(review, "qa_history", None):
            try:
                qa_list = json.loads(review.qa_history)
            except Exception:
                pass
        mirror_text(prefix, "question_answer.txt", json.dumps(qa_list, indent=2))

        # ── presenting_complaints.json ─────────────────────────────────── #
        mirror_text(
            prefix,
            "presenting_complaints.json",
            build_doctor_initiated_presenting_complaints_json(case.presenting_complaint),
        )

        logger.info("doctor_initiated_mirror_complete", case_id=case.id, prefix=prefix)
    except Exception:
        logger.debug("doctor_initiated_mirror_failed", case_id=case.id, exc_info=True)
