"""Two-step verification (TOTP) — the logic, separate from the web routes.

Who is covered: any login can enrol (dashboard accounts and phone-app staff
alike). `FLOORWATCH_MFA_REQUIRED_ROLES` lists roles that MUST have it (e.g.
"admin,secondary_admin"); someone in such a role who hasn't enrolled can't sign
in until they do, and is walked through enrolment as part of the sign-in.

Guarantees, each covered by tests:
  * a code works once (replays are refused, even two at the same instant);
  * guessing is throttled per account AND per address, and failures raise
    security events (and, in volume, an alert);
  * the authenticator secret is encrypted at rest; recovery codes are stored
    only as keyed hashes and each works once;
  * "disable" needs the password AND a current code, so a stolen session alone
    can't turn it off;
  * an admin can reset a lost authenticator, which also signs that person out."""

import asyncio
from typing import List, Optional

import floorwatch_totp as totp
from floorwatch_crypto import FieldCipher
from floorwatch_rate_limit import RateLimiter

RESULT_OK = "ok"
RESULT_OK_RECOVERY = "ok_recovery"
RESULT_INVALID = "invalid"
RESULT_RATE_LIMITED = "rate_limited"


class MfaService:
    def __init__(self, store, cipher: FieldCipher, issuer: str, required_roles: set, security_monitor, log):
        self.store = store
        self.cipher = cipher
        self.issuer = issuer
        self.required_roles = {r.strip() for r in required_roles if r.strip()}
        self.monitor = security_monitor
        self.log = log
        self._by_subject = RateLimiter(5, 300.0)     # guesses at one account's code
        self._by_ip = RateLimiter(20, 300.0)         # guesses from one address (across accounts)

    # ── naming ──
    @staticmethod
    def subject(kind: str, ident: str) -> str:
        return f"{kind}:{ident}"

    def required_for(self, role: str) -> bool:
        return role in self.required_roles

    # ── state ──
    async def is_enabled(self, kind: str, ident: str) -> bool:
        record = await asyncio.to_thread(self.store.get, self.subject(kind, ident))
        return bool(record and record["enabled"])

    async def status(self, kind: str, ident: str) -> dict:
        record = await asyncio.to_thread(self.store.get, self.subject(kind, ident))
        enabled = bool(record and record["enabled"])
        return {"enabled": enabled, "recovery_codes_remaining": len(record["recovery_hashes"]) if enabled else 0}

    # ── verifying a code at sign-in (or before a sensitive change) ──
    async def verify_code(self, kind: str, ident: str, code: str, ip: str) -> str:
        sub = self.subject(kind, ident)
        if not self._by_subject.allow(sub) or not self._by_ip.allow(ip):
            self.monitor.record("mfa_failed", keys=[f"ip:{ip}", sub], reason="rate_limited", ip=ip)
            return RESULT_RATE_LIMITED
        record = await asyncio.to_thread(self.store.get, sub)
        ok = None
        if record and record["enabled"]:
            if totp.looks_like_recovery_code(code):
                digest = self.cipher.keyed_hash("recovery", totp.normalize_recovery_code(code))
                if await asyncio.to_thread(self.store.consume_recovery, sub, digest):
                    ok = RESULT_OK_RECOVERY
                    self.monitor.record("mfa_recovery_used", subject=sub, ip=ip)
            else:
                try:
                    secret = self.cipher.decrypt(record["secret_enc"])
                except ValueError:
                    self.log(f"two-step secret for {sub} cannot be decrypted (encryption key changed?) — "
                             f"an admin must reset it", level="error")
                    secret = None
                if secret:
                    step = totp.verify_totp(secret, code, last_used_step=record["last_step"])
                    if step is not None and await asyncio.to_thread(self.store.accept_step, sub, step):
                        ok = RESULT_OK
        if ok:
            return ok
        self.monitor.record("mfa_failed", keys=[f"ip:{ip}", sub], reason="bad_code", ip=ip)
        return RESULT_INVALID

    # ── enrolment ──
    async def begin_enrollment(self, kind: str, ident: str, account_label: str) -> Optional[dict]:
        """None if they already have it enabled."""
        secret = totp.generate_secret()
        if not await asyncio.to_thread(self.store.begin_enrollment, self.subject(kind, ident), self.cipher.encrypt(secret)):
            return None
        return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, account_label, self.issuer)}

    async def finish_enrollment(self, kind: str, ident: str, code: str, ip: str) -> Optional[List[str]]:
        """Recovery codes (shown ONCE) if the code proves the authenticator works, else None."""
        sub = self.subject(kind, ident)
        if not self._by_subject.allow(sub) or not self._by_ip.allow(ip):
            return None
        record = await asyncio.to_thread(self.store.get, sub)
        if not record or record["enabled"]:
            return None
        try:
            secret = self.cipher.decrypt(record["secret_enc"])
        except ValueError:
            return None
        step = totp.verify_totp(secret, code)
        if step is None:
            self.monitor.record("mfa_failed", keys=[f"ip:{ip}", sub], reason="bad_enrolment_code", ip=ip)
            return None
        codes = totp.generate_recovery_codes()
        hashes = [self.cipher.keyed_hash("recovery", totp.normalize_recovery_code(c)) for c in codes]
        if not await asyncio.to_thread(self.store.enable, sub, hashes, step):
            return None
        self.monitor.record("mfa_enrolled", subject=sub)
        return codes

    async def regenerate_recovery_codes(self, kind: str, ident: str) -> Optional[List[str]]:
        sub = self.subject(kind, ident)
        codes = totp.generate_recovery_codes()
        hashes = [self.cipher.keyed_hash("recovery", totp.normalize_recovery_code(c)) for c in codes]
        return codes if await asyncio.to_thread(self.store.replace_recovery, sub, hashes) else None

    async def disable(self, kind: str, ident: str, by: str) -> bool:
        removed = await asyncio.to_thread(self.store.disable, self.subject(kind, ident))
        if removed:
            self.monitor.record("mfa_disabled", subject=self.subject(kind, ident), by=by)
        return removed
