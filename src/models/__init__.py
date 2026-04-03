"""
models/__init__.py — Model Registry
=====================================

Importing all models here serves two purposes:

1. ALEMBIC DISCOVERY
   alembic/env.py imports Base from this package.
   SQLAlchemy's metadata only knows about tables that have been
   imported at least once. By importing all models here, we guarantee
   that every table is registered in Base.metadata before Alembic
   generates its migration.

   Without this, running `alembic revision --autogenerate` would
   produce an empty migration — it wouldn't "see" any tables.

2. CONVENIENCE IMPORTS
   Other modules can do:
       from src.models import User, Case, CaseImage
   instead of:
       from src.models.user import User
       from src.models.case import Case
       from src.models.case_image import CaseImage
"""

from src.models.base import Base, TimestampMixin, new_uuid
from src.models.case import AiStatus, Case, ClinicalStatus, ConsultationType
from src.models.case_image import CaseImage, ImageType
from src.models.case_report import CaseReport, ReportType
from src.models.differential_diagnosis import DifferentialDiagnosis
from src.models.doctor_profile import DoctorProfile
from src.models.doctor_review import DoctorReview, ReviewStatus
from src.models.message import Message, MessageRole
from src.models.patient_profile import PatientProfile
from src.models.qr_token import QRToken
from src.models.refresh_token import RefreshToken
from src.models.snomed_mapping import SnomedMapping
from src.models.user import User, UserRole
from src.models.visual_description import VisualDescription

__all__ = [
    # Base
    "Base",
    "TimestampMixin",
    "new_uuid",
    # User & Profiles
    "User",
    "UserRole",
    "PatientProfile",
    "DoctorProfile",
    # Cases
    "Case",
    "AiStatus",
    "ClinicalStatus",
    "ConsultationType",
    # Case sub-entities
    "CaseImage",
    "ImageType",
    "VisualDescription",
    "DifferentialDiagnosis",
    "Message",
    "MessageRole",
    # Doctor
    "DoctorReview",
    "ReviewStatus",
    # Reports & Tokens
    "CaseReport",
    "ReportType",
    "SnomedMapping",
    "QRToken",
    "RefreshToken",
]
