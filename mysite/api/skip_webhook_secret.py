"""
Skip Webhook Secret Module

Mengabaikan verifikasi signature webhook jika environment variable
SKIP_WEBHOOK_SECRET disetel ke 'true'. Berguna untuk development/testing.
"""

import os

def is_skip_webhook_secret() -> bool:
    """Cek apakah verifikasi signature webhook harus dilewati."""
    return os.getenv('SKIP_WEBHOOK_SECRET', 'false').lower() == 'true'


def verify_webhook_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """
    Verifikasi signature HMAC-SHA256, dengan opsi skip.

    Args:
        payload: Raw request body
        signature_header: Isi header X-OpenClaw-Signature
        secret: Webhook secret dari database

    Returns:
        True jika valid atau skip diaktifkan, False jika tidak valid.
    """
    # Jika skip diaktifkan, selalu kembalikan True
    if is_skip_webhook_secret():
        return True

    # Jika secret tidak dikonfigurasi, verifikasi tidak diperlukan
    if not secret:
        return True

    # Jika ada secret tapi tidak ada signature header, tolak
    if not signature_header:
        return False

    import hmac
    import hashlib

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)