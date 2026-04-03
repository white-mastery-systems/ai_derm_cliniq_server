"""
AI Prompt templates for AiDerm Cliniq.

Modules:
    image_analysis_prompts     — Prompts for image inspection, visual description, and image-based diagnosis
    patient_consultation_prompts — Prompts for patient Q&A, complaints, question generation, and case summary
    doctor_review_prompts      — Prompts for doctor-facing analysis, doubts, final summary, and treatment plan
"""

from .image_analysis_prompts import ImageAnalysisPrompts
from .patient_consultation_prompts import PatientConsultationPrompts
from .doctor_review_prompts import DoctorReviewPrompts

__all__ = [
    "ImageAnalysisPrompts",
    "PatientConsultationPrompts",
    "DoctorReviewPrompts",
]
