"""Normalizes uploaded photos (including iPhone HEIC) into JPEG bytes.

Claude's vision input only accepts jpeg/png/gif/webp, and iPhones default to
HEIC, so every image is re-encoded to JPEG here before it's sent to the model
or stored. PDFs pass through untouched (Claude accepts PDFs natively).
"""
import io

from PIL import Image, ImageOps
import pillow_heif

pillow_heif.register_heif_opener()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
PDF_EXTENSIONS = {".pdf"}


def is_pdf(filename: str) -> bool:
    return _ext(filename) in PDF_EXTENSIONS


def is_image(filename: str) -> bool:
    return _ext(filename) in IMAGE_EXTENSIONS


def _ext(filename: str) -> str:
    import os

    return os.path.splitext(filename or "")[1].lower()


def normalize_image(file_bytes: bytes, max_dim: int = 1600, quality: int = 85) -> bytes:
    """Re-encode any supported image format to a rotation-corrected JPEG."""
    img = Image.open(io.BytesIO(file_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
