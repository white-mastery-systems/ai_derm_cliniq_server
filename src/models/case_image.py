"""
models/case_image.py — Case Image Metadata
===========================================

Stores metadata about uploaded images. The actual binary file lives
in Google Cloud Storage (GCS) — we only store the GCS path here.

WHY NOT STORE THE FILE IN THE DB?
-----------------------------------
Storing binary files in PostgreSQL (BYTEA columns) is an anti-pattern:
- DB backups become huge
- DB is not optimised for large binary reads
- No CDN, no streaming, no resumable downloads

Instead: file → GCS bucket, metadata → this table.
The GCS path can be used to generate a signed URL for download.

IMAGE TYPES
-----------
- skin        : The primary clinical skin photograph
- prescription: Optional prescription image sent for OCR
- dermoscopy  : Dermoscopic image (if available)

UPLOAD ORDER
------------
Patients may upload multiple images (up to MAX_IMAGES_PER_CASE = 10).
upload_order preserves the sequence for display and AI processing.
"""

import enum

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin, new_uuid


class ImageType(str, enum.Enum):
    SKIN = "skin"
    PRESCRIPTION = "prescription"
    DERMOSCOPY = "dermoscopy"


class CaseImage(TimestampMixin, Base):
    """
    Metadata for one uploaded image associated with a case.

    The actual file is stored in GCS at `gcs_path`.
    Use the storage service to generate a signed download URL.
    """

    __tablename__ = "case_images"

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
    # Storage
    # ------------------------------------------------------------------ #
    gcs_path: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
        comment="Full GCS object path: cases/{case_id}/images/{image_id}.jpg",
    )
    image_type: Mapped[ImageType] = mapped_column(
        Enum(ImageType, name="image_type_enum", create_type=True, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=ImageType.SKIN,
        comment="skin | prescription | dermoscopy",
    )

    # ------------------------------------------------------------------ #
    # File Metadata
    # ------------------------------------------------------------------ #
    original_filename: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Original filename from the patient's device",
    )
    mime_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="e.g. image/jpeg, image/png",
    )
    size_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="File size in bytes — used for storage quota checks",
    )
    upload_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="Position in the image set (0-indexed). Preserves display order.",
    )

    # ------------------------------------------------------------------ #
    # Relationship
    # ------------------------------------------------------------------ #
    case: Mapped["Case"] = relationship(  # noqa: F821
        "Case",
        back_populates="images",
    )

    def __repr__(self) -> str:
        return f"<CaseImage id={self.id!r} type={self.image_type} case={self.case_id!r}>"
