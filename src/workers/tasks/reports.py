"""
workers/tasks/reports.py — PDF Report Generation Celery Task
=============================================================

Single task: generate_report_task(case_id)

Generates a clinical PDF report after the doctor completes their review.
The PDF is uploaded to GCS and a CaseReport row is created.

TASK FLOW
---------
1. Load case + all related data (visual description, differential,
   messages, doctor review, patient profile) via eager loading
2. Call Gemini: generate_final_summary() using confirmed diagnosis
3. Render HTML report from inline Jinja2 template
4. Convert HTML → PDF using WeasyPrint
5. Upload PDF to GCS: cases/{case_id}/reports/report_doctor.pdf
6. Insert CaseReport row → signals report is ready

GUARDS (checked in service before enqueueing)
---------------------------------------------
- Doctor must be assigned to the case
- DoctorReview must have review_status=COMPLETED
- CaseReport must not already exist (409 if it does)

FAILURE HANDLING
----------------
If any step fails (AI, PDF, GCS), the task logs the error and returns
without saving a CaseReport. The service layer can retry by calling
POST /report again. No case status is changed on failure (unlike the AI
analysis chain which sets ai_status=FAILED) — report generation is
best-effort and retryable.
"""

import asyncio
import json
from datetime import date, datetime, timezone

from celery.exceptions import SoftTimeLimitExceeded
from jinja2 import Template
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from src.ai.llm_router import call_llm, extract_json
from src.ai.prompts.doctor_review_prompts import DoctorReviewPrompts
from src.config import settings
from src.exceptions import AIProviderException
from src.logger import get_logger
from src.models.base import new_uuid
from src.models.case import Case
from src.models.case_report import CaseReport, ReportType
from src.models.message import MessageRole
from src.models.user import User
from src.storage import gcs
from src.workers.celery_app import celery_app

logger = get_logger(__name__)


# ------------------------------------------------------------------ #
# DB + async helpers (same pattern as analysis.py)
# ------------------------------------------------------------------ #

def _make_engine():
    return create_async_engine(settings.DATABASE_URL, poolclass=NullPool)


def _run_async(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ #
# Report HTML template
# ------------------------------------------------------------------ #

_REPORT_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  body { font-family: Arial, sans-serif; font-size: 11pt; color: #1a1a1a; margin: 40px; }
  h1   { font-size: 20pt; color: #1a3a5c; border-bottom: 2px solid #1a3a5c; padding-bottom: 6px; }
  h2   { font-size: 14pt; color: #1a3a5c; margin-top: 28px; border-bottom: 1px solid #cce0f0; padding-bottom: 4px; }
  h3   { font-size: 11pt; color: #2c5f8a; margin-top: 16px; }
  .meta  { color: #555; font-size: 10pt; margin-bottom: 20px; }
  .label { font-weight: bold; color: #333; min-width: 160px; display: inline-block; }
  .row   { margin: 6px 0; }
  .section-box { background: #f5f9fd; border-left: 4px solid #2c7be5; padding: 12px 16px; margin: 12px 0; }
  .diagnosis-badge {
    display: inline-block; padding: 4px 12px; border-radius: 4px;
    background: #1a3a5c; color: #fff; font-weight: bold;
  }
  .differential-item { margin: 6px 0; padding: 6px 12px; background: #eef4fb; border-radius: 3px; }
  .confidence-high   { color: #1e7e34; font-weight: bold; }
  .confidence-medium { color: #e07b00; font-weight: bold; }
  .confidence-low    { color: #c0392b; font-weight: bold; }
  .footer { margin-top: 50px; font-size: 9pt; color: #888; border-top: 1px solid #ddd; padding-top: 10px; }
  table  { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 10pt; }
  th     { background: #1a3a5c; color: #fff; padding: 6px 10px; text-align: left; }
  td     { padding: 5px 10px; border-bottom: 1px solid #e0e0e0; }
  tr:nth-child(even) td { background: #f5f9fd; }
</style>
</head>
<body>

<h1>AiDerm Cliniq — Clinical Case Report</h1>
<p class="meta">
  Generated: {{ generated_at }}<br>
  Case ID: {{ case_id }}<br>
  Report Type: Doctor
</p>

<!-- ─── PATIENT INFORMATION ─── -->
<h2>Patient Information</h2>
<div class="section-box">
  <div class="row"><span class="label">Name:</span> {{ patient_name }}</div>
  {% if age %}<div class="row"><span class="label">Age:</span> {{ age }}</div>{% endif %}
  {% if sex  %}<div class="row"><span class="label">Sex:</span> {{ sex }}</div>{% endif %}
  <div class="row"><span class="label">Consultation Type:</span> {{ consultation_type }}</div>
  {% if presenting_complaint %}
  <div class="row"><span class="label">Presenting Complaint:</span> {{ presenting_complaint }}</div>
  {% endif %}
</div>

<!-- ─── CONFIRMED DIAGNOSIS ─── -->
<h2>Confirmed Diagnosis</h2>
{% if confirmed_diagnosis %}
<p><span class="diagnosis-badge">{{ confirmed_diagnosis }}</span></p>
{% else %}
<p><em>Not recorded.</em></p>
{% endif %}

<!-- ─── CLINICAL SUMMARY ─── -->
{% if summary %}
<h2>Clinical Summary</h2>
<div class="section-box">
  {% if summary.presenting_complaint %}
  <h3>Presenting Complaint</h3>
  <p>{{ summary.presenting_complaint }}</p>
  {% endif %}
  {% if summary.clinical_dialogue_summary %}
  <h3>Clinical Dialogue Summary</h3>
  <p>{{ summary.clinical_dialogue_summary }}</p>
  {% endif %}
  {% if summary.key_supporting_features %}
  <h3>Key Supporting Features</h3>
  <p>{{ summary.key_supporting_features }}</p>
  {% endif %}
</div>
{% endif %}

<!-- ─── DOCTOR'S NOTES ─── -->
{% if review_notes %}
<h2>Doctor's Notes</h2>
<div class="section-box">{{ review_notes }}</div>
{% endif %}

<!-- ─── VISUAL DESCRIPTION ─── -->
{% if overall_description %}
<h2>AI Visual Description</h2>
<div class="section-box">{{ overall_description }}</div>
{% endif %}

<!-- ─── AI DIFFERENTIAL DIAGNOSIS ─── -->
{% if final_diff %}
<h2>AI Differential Diagnosis</h2>
{% set diag = final_diff %}
{% if diag.most_probable %}
<p><span class="label">Most Probable:</span> {{ diag.most_probable }}
  {% if diag.confidence %}
    — <span class="confidence-{{ diag.confidence | lower }}">Confidence: {{ diag.confidence }}</span>
  {% endif %}
</p>
{% endif %}
{% if diag.differentials %}
<h3>Differential List</h3>
{% for d in diag.differentials %}
<div class="differential-item">
  <strong>{{ d.diagnosis }}</strong>
  {% if d.likelihood %} — {{ d.likelihood }}{% endif %}
  {% if d.key_supporting_features %}
    <br><span style="color:#555">{{ d.key_supporting_features }}</span>
  {% endif %}
</div>
{% endfor %}
{% endif %}
{% endif %}

<!-- ─── TREATMENT PLAN ─── -->
{% if treatment_plan %}
<h2>Treatment Plan</h2>
{% if treatment_plan.treatment_plan %}
{% set tp = treatment_plan.treatment_plan %}
{% if tp.medications %}
<h3>Medications</h3>
<div class="section-box">{{ tp.medications }}</div>
{% endif %}
{% if tp.lifestyle_modifications %}
<h3>Lifestyle Modifications</h3>
<div class="section-box">{{ tp.lifestyle_modifications }}</div>
{% endif %}
{% if tp.dietary_recommendations %}
<h3>Dietary Recommendations</h3>
<div class="section-box">{{ tp.dietary_recommendations }}</div>
{% endif %}
{% endif %}
{% if treatment_plan.prescription %}
<h3>Prescription</h3>
<table>
  <thead><tr><th>Drug</th><th>Dosage</th><th>Frequency</th><th>Duration</th></tr></thead>
  <tbody>
  {% for rx in treatment_plan.prescription %}
  <tr>
    <td>{{ rx.drug }}</td>
    <td>{{ rx.dosage }}</td>
    <td>{{ rx.frequency }}</td>
    <td>{{ rx.duration }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}
{% endif %}

<!-- ─── FOOTER ─── -->
<div class="footer">
  This report was generated by AiDerm Cliniq. AI-generated content is for
  clinical decision support only and does not replace physician judgement.
  Final diagnosis and treatment decisions remain the responsibility of the
  treating clinician.
</div>

</body>
</html>
"""


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _get_patient_age_sex(patient_profile) -> tuple[str, str]:
    """Extract age and sex from PatientProfile (may be None)."""
    age = "Unknown"
    sex = "Unknown"
    if patient_profile:
        if patient_profile.date_of_birth:
            age = str((date.today() - patient_profile.date_of_birth).days // 365)
        if patient_profile.gender:
            sex = patient_profile.gender
    return age, sex


def _build_history_for_report(messages) -> str:
    """
    Build Q&A history string for AI prompt.
    Same logic as _build_conversation_history in questions.py.
    Kept local to avoid inter-task imports.
    """
    from collections import defaultdict
    pairs = defaultdict(dict)

    for msg in messages:
        key = (msg.round_number, msg.question_index)
        if msg.role == MessageRole.AI:
            try:
                data = json.loads(msg.content)
                pairs[key]["question"] = data.get("question", msg.content)
            except (json.JSONDecodeError, TypeError):
                pairs[key]["question"] = msg.content
        elif msg.role == MessageRole.PATIENT:
            pairs[key]["answer"] = msg.content

    lines = []
    for i, (_key, pair) in enumerate(sorted(pairs.items()), start=1):
        if "question" in pair:
            lines.append(f"Q{i}: {pair['question']}")
        if "answer" in pair:
            lines.append(f"A{i}: {pair['answer']}")

    return "\n".join(lines)


def _parse_differential(diag_json_str: str | None) -> dict:
    """Parse diagnosis_json into display-friendly dict."""
    if not diag_json_str:
        return {}
    try:
        data = json.loads(diag_json_str)
    except (json.JSONDecodeError, TypeError):
        return {}

    most = data.get("most_probable_diagnosis", {})
    differentials_raw = data.get("differential_diagnoses", [])

    return {
        "most_probable": most.get("diagnosis") if isinstance(most, dict) else None,
        "confidence": data.get("confidence in answer") or data.get("confidence"),
        "differentials": differentials_raw if isinstance(differentials_raw, list) else [],
    }


def _render_html(context: dict) -> str:
    """Render the report HTML from the inline Jinja2 template."""
    template = Template(_REPORT_HTML)
    return template.render(**context)


def _html_to_pdf(html: str) -> bytes:
    """
    Convert HTML string to PDF bytes using xhtml2pdf (pure Python, Windows-compatible).
    WeasyPrint requires GTK/Cairo system libraries unavailable on Windows.
    """
    import io
    from xhtml2pdf import pisa
    buffer = io.BytesIO()
    result = pisa.CreatePDF(html, dest=buffer)
    if result.err:
        raise RuntimeError(f"xhtml2pdf conversion failed with {result.err} errors")
    return buffer.getvalue()


# ------------------------------------------------------------------ #
# Celery Task
# ------------------------------------------------------------------ #

@celery_app.task(
    bind=True,
    name="src.workers.tasks.reports.generate_report_task",
    time_limit=300,
    soft_time_limit=270,
    max_retries=1,
    default_retry_delay=30,
)
def generate_report_task(self, case_id: str) -> None:
    """
    Generate a PDF clinical report for the given case.

    This task is triggered by POST /cases/{case_id}/report.
    On success, inserts a CaseReport row — GET /report then returns 200.
    On failure, logs the error and returns — no status is changed.
    """
    logger.info("generate_report_task_start", case_id=case_id)

    async def _run():
        engine = _make_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        # ── 1. Load case with all related data ──────────────────────── #
        async with factory() as session:
            result = await session.execute(
                select(Case)
                .where(Case.id == case_id)
                .options(
                    selectinload(Case.patient).selectinload(User.patient_profile),
                    selectinload(Case.doctor),
                    selectinload(Case.visual_descriptions),
                    selectinload(Case.differential_diagnoses),
                    selectinload(Case.messages),
                    selectinload(Case.doctor_review),
                )
            )
            case = result.scalar_one_or_none()

        if case is None:
            logger.error("generate_report_task_case_not_found", case_id=case_id)
            await engine.dispose()
            return

        patient_profile = getattr(case.patient, "patient_profile", None)
        age, sex = _get_patient_age_sex(patient_profile)

        # Latest visual description (highest round_number)
        visual = (
            max(case.visual_descriptions, key=lambda v: v.round_number)
            if case.visual_descriptions else None
        )

        # Final differential (is_final=True, or latest)
        final_dd = next(
            (d for d in case.differential_diagnoses if d.is_final), None
        ) or (
            max(case.differential_diagnoses, key=lambda d: d.round_number)
            if case.differential_diagnoses else None
        )

        review = case.doctor_review
        history = _build_history_for_report(case.messages)

        # ── 2. Call Gemini for final clinical summary ────────────────── #
        summary_data = {}
        if review and review.confirmed_diagnosis:
            try:
                _cd = review.confirmed_diagnosis
                try:
                    _cd_list = json.loads(_cd)
                    final_diagnosis_str = ", ".join(_cd_list) if _cd_list else _cd
                except (ValueError, TypeError):
                    final_diagnosis_str = _cd
                _ci = "None recorded"
                if review.clinical_indicators:
                    try:
                        _ci_list = json.loads(review.clinical_indicators)
                        _ci = ", ".join(_ci_list) if _ci_list else "None recorded"
                    except (ValueError, TypeError):
                        _ci = review.clinical_indicators
                prompt = DoctorReviewPrompts.generate_final_summary().format(
                    conversation=history or "No Q&A recorded.",
                    visual_description=visual.overall_description if visual else "Not available.",
                    final_diagnosis=final_diagnosis_str,
                    clinical_indicators=_ci,
                    age=age,
                    sex=sex,
                )
                response_text = call_llm(prompt)
                parsed = extract_json(response_text)
                summary_data = parsed.get("summary", {})
            except (AIProviderException, Exception) as exc:
                # Non-fatal — report is still generated without AI summary
                logger.warning(
                    "generate_report_task_summary_failed",
                    case_id=case_id,
                    error=str(exc),
                )

        # ── 3. Parse treatment plan ──────────────────────────────────── #
        treatment_plan = None
        if review and review.treatment_plan_json:
            try:
                treatment_plan = json.loads(review.treatment_plan_json)
            except (json.JSONDecodeError, TypeError):
                pass

        # ── 4. Render HTML ───────────────────────────────────────────── #
        context = {
            "case_id": case_id,
            "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "patient_name": case.patient.full_name,
            "age": age,
            "sex": sex,
            "consultation_type": case.consultation_type.value.replace("_", " ").title(),
            "presenting_complaint": case.presenting_complaint,
            "confirmed_diagnosis": final_diagnosis_str if (review and review.confirmed_diagnosis) else None,
            "review_notes": review.review_notes if review else None,
            "overall_description": visual.overall_description if visual else None,
            "final_diff": _parse_differential(final_dd.diagnosis_json if final_dd else None),
            "summary": summary_data,
            "treatment_plan": treatment_plan,
        }
        html_content = _render_html(context)

        # ── 5. Convert HTML → PDF ────────────────────────────────────── #
        try:
            pdf_bytes = _html_to_pdf(html_content)
        except Exception as exc:
            logger.error(
                "generate_report_task_pdf_failed",
                case_id=case_id,
                error=str(exc),
            )
            await engine.dispose()
            return

        # ── 6. Upload to GCS ─────────────────────────────────────────── #
        gcs_path = f"cases/{case_id}/reports/report_doctor.pdf"
        try:
            gcs.upload_file(gcs_path, pdf_bytes, content_type="application/pdf")
        except Exception as exc:
            logger.error(
                "generate_report_task_upload_failed",
                case_id=case_id,
                gcs_path=gcs_path,
                error=str(exc),
            )
            await engine.dispose()
            return

        # ── 7. Save CaseReport row ───────────────────────────────────── #
        async with factory() as session:
            report = CaseReport(
                id=new_uuid(),
                case_id=case_id,
                gcs_path=gcs_path,
                report_type=ReportType.DOCTOR,
                generated_at=datetime.now(tz=timezone.utc),
                download_count=0,
            )
            session.add(report)
            await session.commit()

        logger.info("generate_report_task_ok", case_id=case_id, gcs_path=gcs_path)
        await engine.dispose()

    try:
        _run_async(_run())
    except SoftTimeLimitExceeded:
        logger.error("generate_report_task_timeout", case_id=case_id)
    except Exception as exc:
        logger.error("generate_report_task_error", case_id=case_id, error=str(exc))
        raise self.retry(exc=exc)
