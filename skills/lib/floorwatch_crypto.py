"""Application-layer encryption for sensitive fields (currently: two-step-verification secrets).

A database dump or a leaked backup must not, by itself, be enough to impersonate
an admin. Authenticator secrets are therefore stored ENCRYPTED, with a key that
lives in the service's environment (a Railway variable) — never next to the
data. Anyone who gets only the database sees ciphertext.

  FLOORWATCH_DATA_ENCRYPTION_KEY   one key, or several comma-separated for ROTATION:
                                   "new-key,old-key" — the FIRST encrypts, ALL decrypt.
                                   Each is either a Fernet key or any long random string
                                   (stretched to one). Generate one with:
                                   python -c "import secrets; print(secrets.token_urlsafe(48))"

If it is not set, a key is derived from the auth-signing secret as a fallback
(clearly reported by diagnostics) — workable, but rotating the auth secret (the
"log everyone out" lever) would then ALSO make every stored authenticator secret
unreadable. A dedicated key avoids coupling the two."""

import base64
import hashlib
import hmac
from typing import List, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

PREFIX = "enc1:"


def _to_fernet_key(raw: str) -> bytes:
    raw = raw.strip()
    try:
        if len(base64.urlsafe_b64decode(raw.encode())) == 32:
            return raw.encode()                     # already a Fernet key
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(b"floorwatch-data-encryption-v1|" + raw.encode()).digest())


class FieldCipher:
    def __init__(self, keys: Optional[str], fallback_secret: str):
        self.dedicated = bool(keys and keys.strip())
        material: List[str] = [k for k in (keys or "").split(",") if k.strip()] if self.dedicated else [fallback_secret]
        if not material or not all(material):
            raise ValueError("no encryption key available")
        self._primary_material = material[0]
        self._fernet = MultiFernet([Fernet(_to_fernet_key(k)) for k in material])

    def encrypt(self, plaintext: str) -> str:
        return PREFIX + self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, stored: str) -> str:
        """Raises ValueError if the value isn't ours or no configured key can open it."""
        if not isinstance(stored, str) or not stored.startswith(PREFIX):
            raise ValueError("not an encrypted value")
        try:
            return self._fernet.decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise ValueError("cannot decrypt: wrong or rotated-away key")

    def keyed_hash(self, purpose: str, value: str) -> str:
        """HMAC-SHA256 with a key derived from the PRIMARY key and a purpose label. For values that must be
        compared but never recovered (recovery codes): useless to someone holding only the database."""
        key = hashlib.sha256(b"floorwatch-keyed-hash-v1|" + purpose.encode() + b"|" + self._primary_material.encode()).digest()
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
