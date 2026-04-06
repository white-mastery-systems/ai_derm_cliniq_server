"""
images/quality.py — Pre-Upload Image Quality Validation
=========================================================

Validates raw image bytes before accepting an upload.
Called from images/service.py immediately after reading the file bytes.

CHECKS (in order)
------------------
1. Decodable       — Pillow can open the image (not corrupt, not a fake)
2. Min dimensions  — at least 200 × 200 pixels (too small = useless for AI)
3. Not too small   — same as above (separate readable check)
4. Brightness      — mean pixel brightness between 20–235 (not pitch black
                     or completely washed out)
5. Blur            — Laplacian variance ≥ MIN_LAPLACIAN_VARIANCE
                     (measures edge sharpness; a low value = blurry image)

WHY THESE CHECKS?
------------------
From the old Dermchatbot2 `image_plausibility.py`:
- The original Gemini inspection gate was used as a second-pass AI check.
  These Pillow checks are the fast first pass — they catch obvious failures
  (black/blank images, tiny icons, heavily blurred uploads) BEFORE spending
  an API call and Celery task on them.
- The Gemini `inspect_images_task` remains as a second gate for semantic
  validation (is this actually a skin image?).

THRESHOLDS
----------
All thresholds are conservative — we prefer false negatives (accepting a
slightly blurry image) over false positives (rejecting a valid clinical
photo taken in imperfect conditions).

PILLOW IMPORT
-------------
Pillow is already in requirements.txt (pillow==11.0.0).
We convert to RGB for brightness — handles RGBA, HEIC (after conversion),
and palette-mode images uniformly.
"""

from __future__ import annotations

from io import BytesIO

from src.exceptions import ImageQualityException
from src.logger import get_logger

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# Thresholds
# ------------------------------------------------------------------ #
MIN_WIDTH = 200           # pixels
MIN_HEIGHT = 200          # pixels
MIN_BRIGHTNESS = 20       # 0–255 mean pixel value (avoid pitch-black images)
MAX_BRIGHTNESS = 235      # 0–255 mean pixel value (avoid completely overexposed)
MIN_LAPLACIAN_VARIANCE = 50.0   # sharpness floor (lower = more blurry allowed)


# ------------------------------------------------------------------ #
# Public interface
# ------------------------------------------------------------------ #

def validate_image_quality(raw_bytes: bytes, filename: str = "") -> None:
    """
    Run all quality checks on raw image bytes.

    Raises ImageQualityException with a human-readable reason if any
    check fails. Returns None if all checks pass.

    Parameters
    ----------
    raw_bytes : bytes   — raw file bytes from the upload
    filename  : str     — original filename (for logging only)

    Raises
    ------
    ImageQualityException — image failed one or more quality checks
    """
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        # Pillow not installed — skip quality checks (dev environment)
        logger.warning("pillow_not_installed_skipping_quality_checks")
        return

    # ── 1. Decode ────────────────────────────────────────────────── #
    try:
        img = Image.open(BytesIO(raw_bytes))
        img.verify()          # detect truncated / corrupt files
        img = Image.open(BytesIO(raw_bytes))   # re-open after verify (verify closes)
        img.load()            # force decode (HEIC lazy-loads)
    except Exception as exc:
        logger.warning("image_quality_decode_failed", filename=filename, error=str(exc))
        raise ImageQualityException(
            message="Image file is corrupt or cannot be decoded. Please upload a valid photo."
        ) from exc

    width, height = img.size

    # ── 2. Minimum dimensions ────────────────────────────────────── #
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        logger.warning(
            "image_quality_too_small",
            filename=filename,
            width=width,
            height=height,
        )
        raise ImageQualityException(
            message=f"Image is too small ({width}×{height}px). "
                    f"Please upload a photo of at least {MIN_WIDTH}×{MIN_HEIGHT} pixels."
        )

    # Convert to RGB for numeric analysis (handles RGBA, palette, greyscale)
    rgb = img.convert("RGB")

    # ── 3. Brightness ────────────────────────────────────────────── #
    mean_brightness = _mean_brightness(rgb)
    if mean_brightness < MIN_BRIGHTNESS:
        logger.warning(
            "image_quality_too_dark",
            filename=filename,
            brightness=mean_brightness,
        )
        raise ImageQualityException(
            message="Image is too dark. Please take the photo in better lighting conditions."
        )
    if mean_brightness > MAX_BRIGHTNESS:
        logger.warning(
            "image_quality_overexposed",
            filename=filename,
            brightness=mean_brightness,
        )
        raise ImageQualityException(
            message="Image is overexposed. Please avoid strong direct lighting or flash."
        )

    # ── 4. Blur (Laplacian variance) ─────────────────────────────── #
    laplacian_variance = _laplacian_variance(rgb)
    if laplacian_variance < MIN_LAPLACIAN_VARIANCE:
        logger.warning(
            "image_quality_too_blurry",
            filename=filename,
            laplacian_variance=laplacian_variance,
        )
        raise ImageQualityException(
            message="Image appears to be blurry. Please hold the camera steady and retake the photo."
        )

    logger.info(
        "image_quality_passed",
        filename=filename,
        width=width,
        height=height,
        brightness=round(mean_brightness, 1),
        laplacian_variance=round(laplacian_variance, 1),
    )


# ------------------------------------------------------------------ #
# Internal helpers
# ------------------------------------------------------------------ #

def _mean_brightness(rgb_image) -> float:
    """
    Calculate mean pixel brightness across R, G, B channels.

    Uses Pillow's fast getdata() instead of numpy for minimal dependencies.
    Returns a float in [0, 255].
    """
    pixels = list(rgb_image.getdata())
    total = sum(r + g + b for r, g, b in pixels)
    return total / (len(pixels) * 3)


def _laplacian_variance(rgb_image) -> float:
    """
    Estimate image sharpness using Laplacian variance.

    The Laplacian filter highlights edges — a sharp image has high
    variance in its edge map; a blurry image has low variance.

    Uses Pillow's FIND_EDGES filter as a Laplacian approximation.
    Returns a float ≥ 0 (higher = sharper).

    BORDER CROP
    -----------
    Pillow's FIND_EDGES creates artificially strong responses at image
    boundaries even for uniform images (the boundary is treated as an
    edge against the implicit zero-padding). We crop 5 pixels on each
    side before computing variance to remove this artefact, making the
    score reflect only genuine image content.
    """
    from PIL import ImageFilter
    greyscale = rgb_image.convert("L")

    # Apply edge filter first, then crop the border from the result.
    # Cropping before the filter just moves the boundary problem — the
    # filter's 3×3 kernel still treats the new edge as a hard boundary.
    # Cropping AFTER the filter removes the artefact rows/columns entirely.
    edges = greyscale.filter(ImageFilter.FIND_EDGES)
    w, h = edges.size
    border = 5
    if w > 2 * border and h > 2 * border:
        edges = edges.crop((border, border, w - border, h - border))

    edge_pixels = list(edges.getdata())

    if not edge_pixels:
        return 0.0

    n = len(edge_pixels)
    mean = sum(edge_pixels) / n
    variance = sum((p - mean) ** 2 for p in edge_pixels) / n
    return variance
