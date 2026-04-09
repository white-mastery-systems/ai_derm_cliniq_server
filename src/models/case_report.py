"""
models/case_report.py — Generated Case Report
===============================================

Stores metadata about the PDF report generated at the end of a case.
The actual PDF file is stored in GCS.

TWO REPORT TYPES (from Doctor Cases & Reports screen)
------------------------------------------------------
The Figma Cases & Reports screen has two tabs: Cases | Reports.
The Reports tab shows "Diagnosis Reports". The doctor generates
reports that both the doctor and patient can access.

- "doctor"  : Full clinical report with technical terms, treatment plan
- "patient" : Simplified patient-friendly version of the same case

DOWNLOAD TRACKING
-----------------
download_count tracks how many times the report has been downloaded.
Useful for analytics and confirming the patient received their report.
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, new_uuid


class ReportType(str, enum.Enum):
    DOCTOR = "doctor"
    PATIENT = "patient"


class CaseReport(Base):
    """
    Metadata for one generated PDF report.
    1-to-1 with Case (one final report per case).
    """

    __tablename__ = "case_reports"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Foreign Key — 1-to-1 with Case
    # ------------------------------------------------------------------ #
    case_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cases.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # ------------------------------------------------------------------ #
    # Storage
    # ------------------------------------------------------------------ #
    gcs_path: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
        comment="GCS path: cases/{case_id}/reports/report_{type}.pdf",
    )
    report_type: Mapped[ReportType] = mapped_column(
        Enum(ReportType, name="report_type_enum", create_type=True, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=ReportType.DOCTOR,
        comment="doctor | patient",
    )

    # ------------------------------------------------------------------ #
    # Timestamps & Tracking
    # ------------------------------------------------------------------ #
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the PDF was generated",
    )
    download_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Number of times this report has been downloaded",
    )

    # ------------------------------------------------------------------ #
    # Relationship
    # ------------------------------------------------------------------ #
    case: Mapped["Case"] = relationship(  # noqa: F821
        "Case",
        back_populates="report",
    )

    def __repr__(self) -> str:
        return f"<CaseReport case={self.case_id!r} type={self.report_type}>"
