"""Sends the admin a notification email whenever something new lands in the
review queue (a recipe or a dish photo). Uses Resend's API.

Never lets an email failure break the submission flow that triggered it -
callers should catch RuntimeError and just log it.
"""
import os

import requests

RESEND_API_URL = "https://api.resend.com/emails"


def _api_key() -> str:
    return os.environ.get("RESEND_API_KEY", "")


def _from_address() -> str:
    return os.environ.get("RESEND_FROM_EMAIL", "")


def _admin_email() -> str:
    return os.environ.get("ADMIN_NOTIFY_EMAIL", "")


def notifications_configured() -> bool:
    return bool(_api_key() and _from_address() and _admin_email())


def send_admin_notification(subject: str, body_text: str) -> None:
    """Emails the admin. Raises RuntimeError if not configured or the send fails.
    Callers should treat this as best-effort and not let it break the request."""
    if not notifications_configured():
        raise RuntimeError(
            "Email notifications aren't configured "
            "(RESEND_API_KEY / RESEND_FROM_EMAIL / ADMIN_NOTIFY_EMAIL missing)."
        )

    resp = requests.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "from": _from_address(),
            "to": [_admin_email()],
            "subject": subject,
            "text": body_text,
        },
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Resend send failed ({resp.status_code}): {resp.text[:300]}")
