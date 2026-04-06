"""
tests/unit/test_image_quality.py — Image Quality Validation Tests
==================================================================

Tests for src/images/quality.py — validate_image_quality()

APPROACH
--------
- Generate synthetic images with Pillow in-memory (no test files needed)
- Test each failure path independently
- Test the happy path with a realistic synthetic image
"""

import io
import pytest
from PIL import Image, ImageDraw

from src.images.quality import MIN_LAPLACIAN_VARIANCE


# ------------------------------------------------------------------ #
# Helpers — generate synthetic images as bytes
# ------------------------------------------------------------------ #

def _make_image_bytes(
    width: int = 400,
    height: int = 400,
    color: tuple = (120, 80, 60),   # mid-tone brownish skin colour
    fmt: str = "JPEG",
) -> bytes:
    """Create a solid-colour PNG/JPEG image and return its bytes."""
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _make_sharp_image_bytes(width: int = 400, height: int = 400) -> bytes:
    """
    Create an image with high-contrast edges so the Laplacian variance is high.
    Draws a grid of black lines on a white background.
    """
    img = Image.new("RGB", (width, height), color=(200, 200, 200))
    draw = ImageDraw.Draw(img)
    # Draw a dense grid of dark lines — lots of edges = high sharpness score
    for x in range(0, width, 20):
        draw.line([(x, 0), (x, height)], fill=(20, 20, 20), width=2)
    for y in range(0, height, 20):
        draw.line([(0, y), (width, y)], fill=(20, 20, 20), width=2)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ------------------------------------------------------------------ #
# Happy path
# ------------------------------------------------------------------ #

def test_validate_quality_passes_good_image():
    """A sharp, well-lit, correctly-sized image passes all checks."""
    from src.images.quality import validate_image_quality
    raw = _make_sharp_image_bytes(400, 400)
    # Should not raise
    validate_image_quality(raw, filename="lesion.jpg")


# ------------------------------------------------------------------ #
# Corrupt / undecodable
# ------------------------------------------------------------------ #

def test_validate_quality_rejects_corrupt_bytes():
    """Random bytes that aren't a valid image are rejected."""
    from src.images.quality import validate_image_quality
    from src.exceptions import ImageQualityException

    with pytest.raises(ImageQualityException, match="corrupt"):
        validate_image_quality(b"\x00\x01\x02\x03\xff\xfe", filename="bad.jpg")


# ------------------------------------------------------------------ #
# Minimum dimensions
# ------------------------------------------------------------------ #

def test_validate_quality_rejects_tiny_image():
    """Image smaller than 200×200 is rejected."""
    from src.images.quality import validate_image_quality
    from src.exceptions import ImageQualityException

    raw = _make_image_bytes(width=100, height=100, color=(120, 80, 60))
    with pytest.raises(ImageQualityException, match="too small"):
        validate_image_quality(raw, filename="tiny.jpg")


def test_validate_quality_accepts_minimum_size():
    """Exactly 200×200 is accepted (boundary condition)."""
    from src.images.quality import validate_image_quality

    # Need a sharp image at this size — solid colour may be blurry
    img = Image.new("RGB", (200, 200), color=(200, 200, 200))
    draw = ImageDraw.Draw(img)
    for x in range(0, 200, 10):
        draw.line([(x, 0), (x, 200)], fill=(20, 20, 20), width=1)
    for y in range(0, 200, 10):
        draw.line([(0, y), (200, y)], fill=(20, 20, 20), width=1)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")

    validate_image_quality(buf.getvalue(), filename="min_size.jpg")


# ------------------------------------------------------------------ #
# Brightness
# ------------------------------------------------------------------ #

def test_validate_quality_rejects_too_dark():
    """Nearly black image is rejected."""
    from src.images.quality import validate_image_quality
    from src.exceptions import ImageQualityException

    raw = _make_image_bytes(color=(5, 5, 5))   # near-black
    with pytest.raises(ImageQualityException, match="dark"):
        validate_image_quality(raw, filename="dark.jpg")


def test_validate_quality_rejects_overexposed():
    """Nearly white (overexposed) image is rejected."""
    from src.images.quality import validate_image_quality
    from src.exceptions import ImageQualityException

    raw = _make_image_bytes(color=(250, 250, 250))   # near-white
    with pytest.raises(ImageQualityException, match="overexposed"):
        validate_image_quality(raw, filename="overexposed.jpg")


def test_validate_quality_accepts_mid_tone():
    """Mid-tone image is accepted for brightness."""
    from src.images.quality import validate_image_quality

    # Use a sharp mid-tone image
    raw = _make_sharp_image_bytes()
    validate_image_quality(raw, filename="mid_tone.jpg")


# ------------------------------------------------------------------ #
# Blur
# ------------------------------------------------------------------ #

def test_validate_quality_rejects_blurry_solid_color():
    """A solid-colour PNG image (zero edges) scores very low on sharpness."""
    from src.images.quality import validate_image_quality
    from src.exceptions import ImageQualityException

    # Use PNG — JPEG compression introduces DCT artefacts that fake edges.
    # PNG is lossless so a solid-colour image has truly zero internal edges.
    raw = _make_image_bytes(color=(120, 80, 60), fmt="PNG")
    with pytest.raises(ImageQualityException, match="blurry"):
        validate_image_quality(raw, filename="solid.png")


def test_validate_quality_accepts_sharp_image():
    """High-contrast grid image passes sharpness check."""
    from src.images.quality import validate_image_quality

    raw = _make_sharp_image_bytes(400, 400)
    # Should not raise
    validate_image_quality(raw, filename="sharp.jpg")


# ------------------------------------------------------------------ #
# Helper functions
# ------------------------------------------------------------------ #

def test_mean_brightness_black():
    """_mean_brightness returns ~0 for a black image."""
    from src.images.quality import _mean_brightness

    img = Image.new("RGB", (100, 100), color=(0, 0, 0))
    assert _mean_brightness(img) < 5.0


def test_mean_brightness_white():
    """_mean_brightness returns ~255 for a white image."""
    from src.images.quality import _mean_brightness

    img = Image.new("RGB", (100, 100), color=(255, 255, 255))
    assert _mean_brightness(img) > 250.0


def test_laplacian_variance_sharp():
    """_laplacian_variance returns a high value for a high-contrast image."""
    from src.images.quality import _laplacian_variance

    # Checkerboard pattern — maximum edges
    img = Image.new("RGB", (200, 200))
    pixels = []
    for y in range(200):
        for x in range(200):
            pixels.append((0, 0, 0) if (x + y) % 2 == 0 else (255, 255, 255))
    img.putdata(pixels)

    variance = _laplacian_variance(img)
    assert variance > 100.0


def test_laplacian_variance_blurry():
    """_laplacian_variance returns a low value for a solid-colour image."""
    from src.images.quality import _laplacian_variance

    # After border-crop, a truly uniform image has no internal edges → ~0 variance
    img = Image.new("RGB", (200, 200), color=(128, 128, 128))
    variance = _laplacian_variance(img)
    assert variance < MIN_LAPLACIAN_VARIANCE
