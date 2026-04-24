from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class Study(TimestampMixin, Base):
    __tablename__ = "studies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    submissions: Mapped[list["StudySubmission"]] = relationship(
        "StudySubmission", back_populates="study", lazy="select"
    )


class StudySubmission(TimestampMixin, Base):
    __tablename__ = "study_submissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    study_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("studies.id", ondelete="CASCADE"), nullable=False
    )
    doctor_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    gcs_image_path: Mapped[str] = mapped_column(String(500), nullable=False)
    features: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment='JSON: {"black_dots":"yes","broken_hairs":"no",...}',
    )
    specimen_code: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Human-readable label e.g. AA-2941-B"
    )
    stability: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="unstable | stable | regrowing"
    )
    technical_quality: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="good | acceptable | poor"
    )

    study: Mapped["Study"] = relationship("Study", back_populates="submissions")
