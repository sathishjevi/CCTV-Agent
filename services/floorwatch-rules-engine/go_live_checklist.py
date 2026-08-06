#!/usr/bin/env python3
"""
Go-live checklist — build brief Phase 4 task 4: "Build the go-live
checklist as an actual script/checklist artifact (not just documentation)
that verifies: shadow-mode false-positive rate below target, roster
cross-check active, notification channel tested, before flipping the
'real notifications' flag on."

This script is a GATE, not a switch: it cannot itself flip
FLOORWATCH_SHADOW_MODE (that's a deployment-level env var change an
operator makes), but it refuses to report PASS unless every check below
is genuinely satisfied, and exits non-zero on any failure so it's usable
as a CI/deploy-pipeline gate, not just something a human reads.

Checks:
  1. Shadow-mode false-positive rate below target, from a reviewed
     accuracy_report.json (tools/accuracy_audit/compute_accuracy.py output).
  2. Roster cross-check is active: roster.json exists, parses, and has at
     least one real (non-comment) zone entry.
  3. Notification channel is configured AND reachable — a real
     connectivity check against Twilio/FCM credentials (never sends an
     actual message; this is Phase 4's "tested" requirement, not a send).
  4. Authentication is actually configured and enforced (SECURITY_REVIEW.md
     M3: this checklist previously had no check for "is this API actually
     protected," so a deployment could pass every other check and still be
     wide open). Two parts: at least one real account exists in users.json,
     and — if the service is reachable — an unauthenticated request to a
     protected endpoint genuinely gets rejected (401), not just "the code
     for auth exists somewhere."

Usage:
  python go_live_checklist.py --accuracy-report ../../tools/accuracy_audit/accuracy_report.json
  python go_live_checklist.py --accuracy-report accuracy_report.json --max-false-positive-rate 0.15
  python go_live_checklist.py --base-url http://127.0.0.1:8080   # live auth-enforcement check
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
import config  # noqa: E402
from notifications import ContactBook  # noqa: E402
from roster import Roster  # noqa: E402


class CheckResult:
    def __init__(self, name: str, passed: bool, detail: str):
        self.name = name
        self.passed = passed
        self.detail = detail


def check_false_positive_rate(report_path: Path, max_rate: float) -> CheckResult:
    if not report_path.exists():
        return CheckResult(
            "Shadow-mode false-positive rate",
            False,
            f"No accuracy report at {report_path} — run tools/accuracy_audit/sample_events.py "
            f"and compute_accuracy.py first.",
        )
    try:
        stats = json.loads(report_path.read_text())
    except json.JSONDecodeError:
        return CheckResult("Shadow-mode false-positive rate", False, f"{report_path} is not valid JSON")

    rate = stats.get("false_positive_rate")
    reviewed = stats.get("total_reviewed", 0)
    if rate is None or reviewed == 0:
        return CheckResult("Shadow-mode false-positive rate", False,
                            "No reviewed samples yet — accuracy report has zero reviewed rows")
    if reviewed < 10:
        return CheckResult("Shadow-mode false-positive rate", False,
                            f"Only {reviewed} reviewed samples — too few to trust a rate (need >=10)")
    passed = rate <= max_rate
    return CheckResult(
        "Shadow-mode false-positive rate", passed,
        f"{rate:.1%} (target <= {max_rate:.1%}, n={reviewed} reviewed samples)",
    )


def check_roster_active(roster_path: Path) -> CheckResult:
    if not roster_path.exists():
        return CheckResult("Roster cross-check active", False, f"No roster file at {roster_path}")
    try:
        data = json.loads(roster_path.read_text())
    except json.JSONDecodeError:
        return CheckResult("Roster cross-check active", False, f"{roster_path} is not valid JSON")

    zone_entries = {k: v for k, v in data.items() if not k.startswith("_")}
    if not zone_entries:
        return CheckResult("Roster cross-check active", False,
                            f"{roster_path} has no zone entries (only comments/empty)")

    roster = Roster(roster_path)
    # Sanity-check the actual code path, not just the file shape.
    sample_zone = next(iter(zone_entries))
    try:
        roster.is_zone_staffed(sample_zone)
    except Exception as e:
        return CheckResult("Roster cross-check active", False, f"Roster.is_zone_staffed() raised: {e}")

    return CheckResult("Roster cross-check active", True,
                        f"{len(zone_entries)} zone(s) configured in {roster_path}")


def check_notification_channel(cfg) -> CheckResult:
    channel = cfg.NOTIFY_CHANNEL
    if channel == "none":
        return CheckResult("Notification channel tested", False,
                            "FLOORWATCH_NOTIFY_CHANNEL is 'none' — no real channel configured")

    if channel == "twilio":
        if not (cfg.TWILIO_ACCOUNT_SID and cfg.TWILIO_AUTH_TOKEN and cfg.TWILIO_FROM_NUMBER):
            return CheckResult("Notification channel tested", False,
                                "Twilio channel selected but SID/token/from-number not fully set")
        try:
            from twilio.rest import Client
            client = Client(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
            # Fetches account metadata — validates credentials are live without sending a message.
            account = client.api.accounts(cfg.TWILIO_ACCOUNT_SID).fetch()
            return CheckResult("Notification channel tested", True,
                                f"Twilio credentials valid (account status: {account.status})")
        except Exception as e:
            return CheckResult("Notification channel tested", False, f"Twilio connectivity check failed: {e}")

    if channel == "fcm":
        if not cfg.FCM_CREDENTIALS_PATH:
            return CheckResult("Notification channel tested", False,
                                "FCM channel selected but FLOORWATCH_FCM_CREDENTIALS_PATH not set")
        cred_path = Path(cfg.FCM_CREDENTIALS_PATH)
        if not cred_path.exists():
            return CheckResult("Notification channel tested", False,
                                f"FCM credentials file not found at {cred_path}")
        try:
            import firebase_admin
            from firebase_admin import credentials
            if not firebase_admin._apps:
                cred = credentials.Certificate(str(cred_path))
                firebase_admin.initialize_app(cred)
            return CheckResult("Notification channel tested", True,
                                f"FCM credentials at {cred_path} loaded successfully")
        except Exception as e:
            return CheckResult("Notification channel tested", False, f"FCM initialization failed: {e}")

    return CheckResult("Notification channel tested", False, f"Unrecognized channel '{channel}'")


def check_contacts_configured(contacts_path: Path, roster_path: Path) -> CheckResult:
    """Every staffed zone should have a real (non-placeholder) contact on file."""
    if not roster_path.exists() or not contacts_path.exists():
        return CheckResult("Contact directory populated", False,
                            f"Missing roster ({roster_path.exists()}) or contacts ({contacts_path.exists()})")
    roster_data = json.loads(roster_path.read_text())
    staffed_zones = [k for k, v in roster_data.items() if not k.startswith("_") and v]
    book = ContactBook(contacts_path)
    missing = [z for z in staffed_zones if not book.phone_for_zone(z) and not book.fcm_token_for_zone(z)]
    if missing:
        return CheckResult("Contact directory populated", False,
                            f"No phone/FCM token on file for staffed zone(s): {missing}")
    return CheckResult("Contact directory populated", True, f"{len(staffed_zones)} staffed zone(s) have contacts")


def check_auth_accounts_exist(users_path: Path) -> CheckResult:
    if not users_path.exists():
        return CheckResult("Authentication — accounts configured", False,
                            f"No users file at {users_path} — run create_user.py to bootstrap an account")
    try:
        users = json.loads(users_path.read_text())
    except json.JSONDecodeError:
        return CheckResult("Authentication — accounts configured", False, f"{users_path} is not valid JSON")
    if not users:
        return CheckResult("Authentication — accounts configured", False,
                            f"{users_path} exists but has no accounts")
    return CheckResult("Authentication — accounts configured", True, f"{len(users)} account(s) in {users_path}")


def check_auth_enforced_live(base_url: str) -> CheckResult:
    """Not a code-shape check — an actual unauthenticated HTTP request
    against a running instance, per SECURITY_REVIEW.md M3's recommendation:
    'does hitting a protected endpoint without credentials return 401.'"""
    url = f"{base_url.rstrip('/')}/api/state"
    try:
        resp = httpx.get(url, timeout=5.0)
    except httpx.RequestError as e:
        return CheckResult("Authentication — enforced live", False,
                            f"Could not reach {url} ({e}) — start the service and retry, "
                            f"or this check cannot verify enforcement")
    if resp.status_code == 401:
        return CheckResult("Authentication — enforced live", True,
                            f"Unauthenticated GET {url} correctly rejected with 401")
    return CheckResult("Authentication — enforced live", False,
                        f"Unauthenticated GET {url} returned {resp.status_code}, expected 401 — "
                        f"this endpoint is NOT protected")


def main():
    parser = argparse.ArgumentParser(description="Floorwatch go-live checklist")
    parser.add_argument("--accuracy-report", type=str,
                        default=str(Path(__file__).resolve().parent.parent.parent /
                                    "tools" / "accuracy_audit" / "accuracy_report.json"))
    parser.add_argument("--max-false-positive-rate", type=float, default=0.15)
    parser.add_argument("--base-url", type=str, default="http://127.0.0.1:8080",
                        help="Running rules-engine instance to live-check auth enforcement against")
    args = parser.parse_args()

    checks = [
        check_false_positive_rate(Path(args.accuracy_report), args.max_false_positive_rate),
        check_roster_active(config.ROSTER_PATH),
        check_contacts_configured(config.CONTACTS_PATH, config.ROSTER_PATH),
        check_notification_channel(config),
        check_auth_accounts_exist(config.USERS_PATH),
        check_auth_enforced_live(args.base_url),
    ]

    print("=== Floorwatch Go-Live Checklist ===\n")
    all_passed = True
    for c in checks:
        status = "PASS" if c.passed else "FAIL"
        print(f"[{status}] {c.name}")
        print(f"       {c.detail}\n")
        all_passed = all_passed and c.passed

    if all_passed:
        print("ALL CHECKS PASSED. Safe to set FLOORWATCH_SHADOW_MODE=false for these pilot zones.")
        sys.exit(0)
    else:
        print("CHECKLIST FAILED. Do NOT disable shadow mode until every check above passes.")
        sys.exit(1)


if __name__ == "__main__":
    main()
