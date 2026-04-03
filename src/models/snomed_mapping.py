"""
models/snomed_mapping.py — SNOMED CT Code Cache
================================================

Maps free-text AI-generated diagnosis strings to SNOMED CT codes.

WHY CACHE?
----------
Looking up a SNOMED code requires either a local CSV lookup or an
API call to the SNOMED CT browser. Both are expensive to do on every
case. We cache the result so the same diagnosis string (e.g. "Psoriasis")
only gets looked up once — ever.

This is especially important because the AI generates the same
diagnoses repeatedly across thousands of cases.

SOURCE
------
The old codebase had `snomed_dermatology_subset.csv` and
`snomed_dermatology_subset_with_parent.csv` files. In the new server,
SNOMED lookups will use those files (loaded into memory at startup or
queried from this table for cache hits).

UNIQUE INDEX ON diagnosis_text
-------------------------------
Ensures no duplicate cache entries for the same diagnosis string.
If a new string doesn't match exactly, it gets its own row.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, new_uuid


class SnomedMapping(Base):
    """
    One cached SNOMED CT mapping per unique diagnosis string.
    """

    __tablename__ = "snomed_mappings"

    # ------------------------------------------------------------------ #
    # Primary Key
    # ------------------------------------------------------------------ #
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Lookup Key
    # ------------------------------------------------------------------ #
    diagnosis_text: Mapped[str] = mapped_column(
        String(500),
        unique=True,
        nullable=False,
        index=True,
        comment="Exact AI-generated diagnosis string used as cache key",
    )

    # ------------------------------------------------------------------ #
    # SNOMED Data
    # ------------------------------------------------------------------ #
    snomed_code: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="SNOMED CT concept ID (e.g. 9014002 for Psoriasis)",
    )
    snomed_term: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Official SNOMED CT preferred term",
    )

    # ------------------------------------------------------------------ #
    # Cache Metadata
    # ------------------------------------------------------------------ #
    cached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When this mapping was first cached",
    )

    def __repr__(self) -> str:
        return (
            f"<SnomedMapping "
            f"text={self.diagnosis_text!r} "
            f"code={self.snomed_code!r}>"
        )
