"""Time-based one-time passwords (RFC 6238) — the 6-digit codes authenticator apps show.

Standard library only. Compatible with Google Authenticator, Microsoft Authenticator, Authy,
1Password, Bitwarden, etc. (SHA-1, 6 digits, 30-second step — the interoperable defaults)."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import List, Optional
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
RECOVERY_CODE_COUNT = 10


def generate_secret() -> str:
    """160 random bits, base32 (what authenticator apps expect), no padding."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = DIGITS) -> str:
    padded = secret_b32.upper() + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def totp_at(secret_b32: str, at: Optional[float] = None, digits: int = DIGITS) -> str:
    return _hotp(secret_b32, int((time.time() if at is None else at) // STEP_SECONDS), digits)


def verify_totp(secret_b32: str, code: str, now: Optional[float] = None, window: int = 1,
                last_used_step: Optional[int] = None) -> Optional[int]:
    """Returns the time-step the code matched, or None. `window` allows that many steps either side
    (clock drift). A step at or before `last_used_step` is refused: a code works ONCE, so someone who
    watched it being typed can't replay it."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == DIGITS):
        return None
    current = int((time.time() if now is None else now) // STEP_SECONDS)
    matched = None
    for step in range(current - window, current + window + 1):          # no early exit: constant-ish time
        if hmac.compare_digest(_hotp(secret_b32, step), code) and matched is None:
            matched = step
    if matched is None or (last_used_step is not None and matched <= last_used_step):
        return None
    return matched


def provisioning_uri(secret_b32: str, account: str, issuer: str) -> str:
    """The otpauth:// link a QR code (or a tap on a phone) hands to an authenticator app."""
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> List[str]:
    """One-time codes for a lost phone: 64 random bits each, shown as xxxx-xxxx-xxxx-xxxx."""
    out = []
    for _ in range(count):
        raw = secrets.token_hex(8)
        out.append("-".join(raw[i:i + 4] for i in range(0, 16, 4)))
    return out


def normalize_recovery_code(code: str) -> str:
    return (code or "").strip().lower().replace(" ", "").replace("-", "")


def looks_like_recovery_code(code: str) -> bool:
    n = normalize_recovery_code(code)
    return len(n) == 16 and all(c in "0123456789abcdef" for c in n)
