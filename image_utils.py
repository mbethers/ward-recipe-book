"""Normalizes uploaded photos into JPEG bytes.

Claude's vision input only accepts jpeg/png/gif/webp, so every image is
re-encoded to JPEG here before it's sent to the model or stored. PDFs pass
through untouched (Claude accepts PDFs natively).

Note: HEIC/HEIF (the iPhone default photo format) is not supported here —
reading it requires a native library (pillow-heif) that fails to build on
Render's hosting environment. Most phone browsers auto-convert photos to
JPEG on upload, but a submitter who hits this should be told to switch their
camera format to "Most Compatible" (Settings > Camera > Formats on iPhone)
or re-export the photo as JPEG before uploading.
"""
import io

from PIL import Image, ImageOps

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
PDF_EXTENSIONS = {".pdf"}


class UnsupportedImageError(Exception):
    """Raised when an uploaded file can't be read as an image (e.g. unconverted HEIC)."""


def is_pdf(filename: str) -> bool:
    return _ext(filename) in PDF_EXTENSIONS


def is_image(filename: str) -> bool:
    return _ext(filename) in IMAGE_EXTENSIONS


def _ext(filename: str) -> str:
    import os

    return os.path.splitext(filename or "")[1].lower()


def normalize_image(file_bytes: bytes, max_dim: int = 1600, quality: int = 85) -> bytes:
    """Re-encode any supported image format to a rotation-corrected JPEG."""
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img.load()
    except Exception as exc:
        raise UnsupportedImageError(
            "We couldn't read that photo. If it's from an iPhone in HEIC format, "
            "please re-save or export it as a JPEG and try uploading again."
        ) from exc
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
