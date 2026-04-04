"""
storage/gcs.py — Google Cloud Storage Client
==============================================

Wraps the google-cloud-storage SDK into three operations:
1. upload_file   — stream bytes into a GCS object
2. delete_file   — remove an object by path
3. get_signed_url — generate a time-limited download URL

WHY SIGNED URLs?
-----------------
Skin images are private medical data. We never make the GCS bucket public.
Instead, we generate signed URLs valid for 30 minutes. The Flutter client
uses these to display images. After expiry the URL is dead — if the attacker
captured it, it's useless.

WHY A MODULE-LEVEL SINGLETON?
-------------------------------
Creating a GCS client is not free (reads credentials, opens connections).
We build it once at first use (_client()) and reuse across requests.
This is safe because google-cloud-storage clients are thread-safe.

GRACEFUL DEGRADATION IN DEVELOPMENT
--------------------------------------
If GOOGLE_APPLICATION_CREDENTIALS is empty, GCS is not configured.
Calling any GCS function raises StorageException with a clear message.
This lets the server start and run non-image routes in dev without GCS.
"""

from datetime import timedelta
from functools import lru_cache
from typing import BinaryIO

from src.config import settings
from src.exceptions import StorageException
from src.logger import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def _client():
    """
    Build and cache the GCS client.

    Raises StorageException if credentials are not configured.
    lru_cache(maxsize=1) ensures we build the client only once.
    """
    if not settings.GOOGLE_APPLICATION_CREDENTIALS:
        raise StorageException(
            message="GCS is not configured. Set GOOGLE_APPLICATION_CREDENTIALS in .env"
        )
    try:
        from google.cloud import storage as gcs
        return gcs.Client.from_service_account_json(
            settings.GOOGLE_APPLICATION_CREDENTIALS
        )
    except Exception as exc:
        logger.error("gcs_client_init_failed", error=str(exc))
        raise StorageException(message=f"Failed to initialise GCS client: {exc}") from exc


def _bucket():
    """Return the configured GCS bucket handle."""
    return _client().bucket(settings.GCS_BUCKET_NAME)


# ------------------------------------------------------------------ #
# Public API
# ------------------------------------------------------------------ #

def upload_file(
    gcs_path: str,
    data: bytes | BinaryIO,
    content_type: str,
) -> str:
    """
    Upload bytes or a file-like object to GCS.

    Parameters
    ----------
    gcs_path     : Full object path inside the bucket,
                   e.g. "cases/abc-123/images/img-456.jpg"
    data         : Raw bytes or file-like object (UploadFile.file)
    content_type : MIME type stored on the object, e.g. "image/jpeg"

    Returns
    -------
    str  — the gcs_path (same as input, useful for chaining)

    Raises
    ------
    StorageException — credentials missing or upload failed
    """
    try:
        blob = _bucket().blob(gcs_path)
        if isinstance(data, (bytes, bytearray)):
            blob.upload_from_string(data, content_type=content_type)
        else:
            blob.upload_from_file(data, content_type=content_type, rewind=True)
        logger.info("gcs_upload_ok", path=gcs_path, content_type=content_type)
        return gcs_path
    except StorageException:
        raise
    except Exception as exc:
        logger.error("gcs_upload_failed", path=gcs_path, error=str(exc))
        raise StorageException(message=f"Upload failed: {exc}") from exc


def delete_file(gcs_path: str) -> None:
    """
    Delete an object from GCS.

    Silently succeeds if the object does not exist (idempotent).

    Raises
    ------
    StorageException — credentials missing or deletion failed unexpectedly
    """
    try:
        blob = _bucket().blob(gcs_path)
        blob.delete(if_generation_match=None)
        logger.info("gcs_delete_ok", path=gcs_path)
    except StorageException:
        raise
    except Exception as exc:
        # NotFound is acceptable — object may have been deleted already
        if "404" in str(exc) or "NotFound" in type(exc).__name__:
            logger.warning("gcs_delete_not_found", path=gcs_path)
            return
        logger.error("gcs_delete_failed", path=gcs_path, error=str(exc))
        raise StorageException(message=f"Delete failed: {exc}") from exc


def get_signed_url(gcs_path: str, expiry_minutes: int = 30) -> str:
    """
    Generate a signed HTTPS URL to download the object.

    The URL is valid for `expiry_minutes` minutes and requires no auth header.
    The Flutter app uses this URL directly in an <Image> widget.

    Parameters
    ----------
    gcs_path       : Full GCS object path
    expiry_minutes : How long the URL is valid (default 30 min)

    Returns
    -------
    str — HTTPS signed URL

    Raises
    ------
    StorageException — credentials missing or signing failed
    """
    try:
        blob = _bucket().blob(gcs_path)
        url = blob.generate_signed_url(
            expiration=timedelta(minutes=expiry_minutes),
            method="GET",
            version="v4",
        )
        return url
    except StorageException:
        raise
    except Exception as exc:
        logger.error("gcs_signed_url_failed", path=gcs_path, error=str(exc))
        raise StorageException(message=f"Failed to generate signed URL: {exc}") from exc


def download_bytes(gcs_path: str) -> bytes:
    """
    Download an object from GCS and return its raw bytes.

    Used by Celery tasks to fetch image data for Gemini analysis.

    Parameters
    ----------
    gcs_path : Full GCS object path (same value stored in CaseImage.gcs_path)

    Returns
    -------
    bytes — raw file contents

    Raises
    ------
    StorageException — credentials missing or download failed
    """
    try:
        blob = _bucket().blob(gcs_path)
        data = blob.download_as_bytes()
        logger.info("gcs_download_ok", path=gcs_path, size_bytes=len(data))
        return data
    except StorageException:
        raise
    except Exception as exc:
        logger.error("gcs_download_failed", path=gcs_path, error=str(exc))
        raise StorageException(message=f"Download failed: {exc}") from exc


def build_image_path(case_id: str, image_id: str, extension: str) -> str:
    """
    Build the canonical GCS path for a case image.

    Format: cases/{case_id}/images/{image_id}.{ext}
    Example: cases/abc-123/images/img-456.jpg

    This is the path stored in CaseImage.gcs_path.
    """
    ext = extension.lstrip(".")
    return f"cases/{case_id}/images/{image_id}.{ext}"
