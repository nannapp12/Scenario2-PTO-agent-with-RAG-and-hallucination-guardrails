"""Field-level encryption (AES-256-GCM) and masking for employee PII.

PostgreSQL already encrypts its storage at rest; this adds a second layer so that
names and emails are unreadable to anyone with database access but not the key
(DBAs, backups, a leaked dump). The 32-byte key lives in Key Vault (`pii-encryption-key`).

Ciphertext layout: version(1) | key_id(4) | nonce(12) | AES-GCM ciphertext+tag.
The associated data binds each value to its row and column, so a ciphertext copied
to another employee or field fails to decrypt. Rotation: deploy the new key as
PII_ENCRYPTION_KEY and the old one as PII_ENCRYPTION_KEY_PREVIOUS; the next
employee sync re-encrypts every row with the new key.
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"


class PiiCipher:
    def __init__(self, key_b64: str, previous_key_b64: str = ""):
        self._keys: dict[bytes, AESGCM] = {}
        self._current = self._add(key_b64)
        if previous_key_b64:
            self._add(previous_key_b64)

    def _add(self, key_b64: str) -> bytes:
        key = base64.b64decode(key_b64, validate=True)
        if len(key) != 32:
            raise ValueError("PII encryption key must be 32 bytes (base64-encoded).")
        key_id = hashlib.sha256(key).digest()[:4]
        self._keys[key_id] = AESGCM(key)
        return key_id

    def encrypt(self, plaintext: str | None, aad: str) -> bytes | None:
        if plaintext is None:
            return None
        nonce = os.urandom(12)
        ct = self._keys[self._current].encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
        return _VERSION + self._current + nonce + ct

    def decrypt(self, blob: bytes | None, aad: str) -> str | None:
        if blob is None:
            return None
        blob = bytes(blob)
        if blob[:1] != _VERSION:
            raise ValueError("Unknown PII ciphertext version.")
        key = self._keys.get(blob[1:5])
        if key is None:
            raise ValueError("PII ciphertext was encrypted with an unknown key.")
        return key.decrypt(blob[5:17], blob[17:], aad.encode("utf-8")).decode("utf-8")


def aad(employee_id: str, field: str) -> str:
    return f"{employee_id}|{field}"


def mask_name(name: str | None) -> str | None:
    """'Jane Doe' -> 'J*** D***'"""
    if not name:
        return None
    return " ".join(f"{part[0]}***" for part in name.split())


def mask_email(email: str | None) -> str | None:
    """'jane.doe@contoso.com' -> 'j***@contoso.com'"""
    if not email or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    return f"{local[:1]}***@{domain}"
