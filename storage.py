"""Uploads dish photos and source recipe images to Supabase Storage.

Uses the raw Storage REST API (no supabase-py dependency) with the service-role
key, which must stay server-side only. The bucket itself is public-read so the
returned URLs can be used directly in <img>/<iframe> tags.
"""
import mimetypes
import os
import uuid

import requests

def _supabase_url() -> str:
    return os.environ.get("SUPABASE_URL", "").rstrip("/")


def _supabase_service_key() -> str:
    return os.environ.get("SUPABASE_SERVICE_KEY", "")


def _bucket() -> str:
    return os.environ.get("SUPABASE_BUCKET", "recipe-photos")


def storage_configured() -> bool:
    return bool(_supabase_url() and _supabase_service_key())


def upload_bytes(data: bytes, filename: str, content_type: str, folder: str) -> str:
    """Upload raw bytes to Supabase Storage. Returns the public URL.

    Raises RuntimeError if storage isn't configured or the upload fails.
    """
    supabase_url = _supabase_url()
    service_key = _supabase_service_key()
    bucket = _bucket()
    if not (supabase_url and service_key):
        raise RuntimeError(
            "Supabase Storage isn't configured (SUPABASE_URL / SUPABASE_SERVICE_KEY missing)."
        )

    ext = os.path.splitext(filename or "")[1].lower()
    if not content_type:
        content_type = mimetypes.guess_type(filename or "")[0] or "application/octet-stream"

    key = f"{folder}/{uuid.uuid4().hex}{ext}"
    url = f"{supabase_url}/storage/v1/object/{bucket}/{key}"

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": content_type,
        },
        data=data,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Supabase upload failed ({resp.status_code}): {resp.text[:300]}")

    return f"{supabase_url}/storage/v1/object/public/{bucket}/{key}"
