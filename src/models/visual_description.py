"""
models/visual_description.py — AI Visual Description
======================================================

Stores the AI-generated structured description of the skin lesion,
produced by the `ImageAnalysisPrompts.get_description()` prompt.

ONE PER ANALYSIS ROUND
------------------------
The visual description can be regenerated after each Q&A round
(using get_description_with_context() which incorporates conversation).
We store every version with a round_number so the doctor can compare
how the description evolved as more patient context was gathered.

STORAGE STRATEGY
-----------------
description_json  — the raw JSON dict from the LLM (full structure)
overall_description — the plain-text summary extracted from the JSON
                      (used for quick display without parsing)

Storing both means the Flutter app can show a summary instantly
without parsing the full JSON, while the backend can access all
structured fields (type_of_lesion, border, etc.) when needed.
"""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class VisualDescription(TimestampMixin, Base):
    """
    One AI-generated lesion description per analysis round.
    """

    __tablename__ = "visual_descriptions"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Foreign Key
    # ------------------------------------------------------------------ #
    case_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ------------------------------------------------------------------ #
    # Round Tracking
    # ------------------------------------------------------------------ #
    round_number: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="0 = initial (no conversation context). "
                "Increments after each Q&A round.",
    )

    # ------------------------------------------------------------------ #
    # AI Output
    # ------------------------------------------------------------------ #
    description_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Full JSON from LLM: type_of_lesion, site, count, border, etc.",
    )
    overall_description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Plain-text summary extracted from description_json.overall_description",
    )

    # ------------------------------------------------------------------ #
    # Relationship
    # ------------------------------------------------------------------ #
    case: Mapped["Case"] = relationship(  # noqa: F821
        "Case",
        back_populates="visual_descriptions",
    )

    def __repr__(self) -> str:
        return f"<VisualDescription case={self.case_id!r} round={self.round_number}>"
