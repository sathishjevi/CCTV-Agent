"""One-time-passcode store for employee mobile-app login (Phase 1 of the
mobile app — see dazzling-hopping-comet.md). An employee never has a
password; they prove phone ownership by receiving a 6-digit code over
whichever SMS_PROVIDER is already configured and typing it back.

Same Redis-with-local-dict-fallback shape as floorwatch_auth.py's
RevocationStore, for the same reason: real deployments share one Redis
instance across replicas (an OTP requested against one rules-engine
replica must be verifiable against whichever replica handles the
verify-otp call), while tests and single-process runs don't need a real
Redis just to exercise this logic.
"""

import secrets
import time
from typing import Optional


class OtpStore:
    _KEY_PREFIX = "floorwatch:otp:"
    TTL_SECONDS = 5 * 60
    CODE_LENGTH = 6
    # One-time use, and short — resistant to guessing within the TTL
    # window without needing a separate attempt-count lockout; the
    # request-otp endpoint's own rate limiting (RateLimiter, same as
    # login) is the actual brute-force defense, matching this codebase's
    # existing division of concerns (see main.py's login rate limiters).

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._local: dict[str, tuple[str, float]] = {}  # phone -> (code, expires_at_monotonic)

    @staticmethod
    def generate_code() -> str:
        return "".join(secrets.choice("0123456789") for _ in range(OtpStore.CODE_LENGTH))

    async def issue(self, phone: str) -> str:
        """Generates and stores a fresh code for `phone`, overwriting any
        still-pending one (a re-request invalidates the previous code
        rather than leaving two valid codes outstanding)."""
        code = self.generate_code()
        if self._redis is not None:
            await self._redis.set(f"{self._KEY_PREFIX}{phone}", code, ex=self.TTL_SECONDS)
        else:
            self._local[phone] = (code, time.monotonic() + self.TTL_SECONDS)
        return code

    async def verify(self, phone: str, code: str) -> bool:
        """One-time use: a correct verify consumes the code immediately,
        so replaying the same code twice fails on the second attempt."""
        if self._redis is not None:
            key = f"{self._KEY_PREFIX}{phone}"
            stored = await self._redis.get(key)
            if stored is None or not secrets.compare_digest(stored, code):
                return False
            await self._redis.delete(key)
            return True
        entry = self._local.get(phone)
        if entry is None:
            return False
        stored_code, expires_at = entry
        if time.monotonic() > expires_at or not secrets.compare_digest(stored_code, code):
            return False
        del self._local[phone]
        return True
