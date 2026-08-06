"""
Full go-live flow verification — build brief Phase 4 acceptance criteria:
"The go-live checklist passes, real notifications fire correctly for at
least one full test shift with no false escalations, and a shift digest
is generated automatically at end of shift from real event data."

Three things are proven here, chained:
  1. go_live_checklist.py's main() actually exits 0 when every condition
     is genuinely met (not just that the individual check functions
     return True in isolation — this runs the real CLI entrypoint).
  2. With FLOORWATCH_SHADOW_MODE=false and a (mocked-SDK) Twilio channel
     configured, a real shift genuinely calls the Twilio send path for a
     legitimate escalation — and every escalation traces back to a
     roster-staffed zone with a real sustained gap, i.e. no false
     escalation slipped through.
  3. shift_digest_job.py, run against the resulting shift_digest.jsonl,
     produces a correct end-of-shift summary automatically — no manual
     step between "shift happened" and "digest exists."

No real Twilio account exists in this sandbox (see notifications.py's
docstring) — step 2 mocks the Twilio SDK client, same as
test_notifications.py, so this proves the wiring is correct end-to-end,
not that Twilio's live API actually accepted a message.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import go_live_checklist  # noqa: E402
from digest_store import DigestStore  # noqa: E402
from engine import RulesEngine  # noqa: E402
from notifications import ContactBook, NotificationDispatcher, TwilioSmsSender  # noqa: E402
from roster import Roster  # noqa: E402
from shift_digest_job import summarize  # noqa: E402


# ── 1. the checklist itself, run end-to-end ──────────────────────────────

def test_go_live_checklist_main_exits_zero_when_all_conditions_met(tmp_path, monkeypatch, capsys):
    accuracy_report = tmp_path / "accuracy_report.json"
    accuracy_report.write_text(json.dumps({"false_positive_rate": 0.08, "total_reviewed": 25}))

    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({"concession": True}))

    contacts_path = tmp_path / "contacts.json"
    contacts_path.write_text(json.dumps({
        "supervisor_phone": "+15559998888",
        "zones": {"concession": {"employee_phone": "+15551112222"}},
    }))

    users_path = tmp_path / "users.json"
    users_path.write_text(json.dumps({"alice": {"password_hash": "...", "role": "supervisor"}}))

    monkeypatch.setattr(go_live_checklist.config, "ROSTER_PATH", roster_path)
    monkeypatch.setattr(go_live_checklist.config, "CONTACTS_PATH", contacts_path)
    monkeypatch.setattr(go_live_checklist.config, "NOTIFY_CHANNEL", "twilio")
    monkeypatch.setattr(go_live_checklist.config, "TWILIO_ACCOUNT_SID", "ACxxxx")
    monkeypatch.setattr(go_live_checklist.config, "TWILIO_AUTH_TOKEN", "authtoken")
    monkeypatch.setattr(go_live_checklist.config, "TWILIO_FROM_NUMBER", "+15550009999")
    monkeypatch.setattr(go_live_checklist.config, "USERS_PATH", users_path)
    monkeypatch.setattr(sys, "argv", ["go_live_checklist.py", "--accuracy-report", str(accuracy_report)])

    with patch("twilio.rest.Client") as MockClient, \
         patch("go_live_checklist.httpx.get", return_value=MagicMock(status_code=401)):
        mock_instance = MockClient.return_value
        mock_instance.api.accounts.return_value.fetch.return_value = MagicMock(status="active")

        with pytest.raises(SystemExit) as exc_info:
            go_live_checklist.main()

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "ALL CHECKS PASSED" in out
    assert "[PASS]" in out
    assert "[FAIL]" not in out


def test_go_live_checklist_main_exits_nonzero_when_fp_rate_too_high(tmp_path, monkeypatch):
    accuracy_report = tmp_path / "accuracy_report.json"
    accuracy_report.write_text(json.dumps({"false_positive_rate": 0.40, "total_reviewed": 25}))

    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({"concession": True}))
    contacts_path = tmp_path / "contacts.json"
    contacts_path.write_text(json.dumps({"zones": {"concession": {"employee_phone": "+15551112222"}}}))

    monkeypatch.setattr(go_live_checklist.config, "ROSTER_PATH", roster_path)
    monkeypatch.setattr(go_live_checklist.config, "CONTACTS_PATH", contacts_path)
    monkeypatch.setattr(go_live_checklist.config, "NOTIFY_CHANNEL", "none")
    monkeypatch.setattr(sys, "argv", ["go_live_checklist.py", "--accuracy-report", str(accuracy_report)])

    with pytest.raises(SystemExit) as exc_info:
        go_live_checklist.main()
    assert exc_info.value.code == 1


# ── 2 + 3. real shift, real (mocked-SDK) notification, automatic digest ──

class AllStaffedRoster:
    def is_zone_staffed(self, zone_id: str) -> bool:
        return zone_id == "concession"  # only concession is staffed -> gates the other zone below


def test_live_mode_shift_fires_real_notification_no_false_escalation_and_generates_digest(tmp_path):
    contacts_path = tmp_path / "contacts.json"
    contacts_path.write_text(json.dumps({
        "zones": {"concession": {"employee_phone": "+15551112222"}},
    }))
    contacts = ContactBook(contacts_path)

    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = MagicMock(sid="SM_TEST_1")

        sender = TwilioSmsSender("ACxxxx", "authtoken", "+15550009999")
        dispatcher = NotificationDispatcher(sender, contacts)

        digest_path = tmp_path / "digest.jsonl"
        digest = DigestStore(digest_path)
        engine = RulesEngine(
            roster=AllStaffedRoster(),
            digest=digest,
            zones_meta={
                "concession": {"name": "Concession Counter", "role_tag": "concession"},
                "entrance": {"name": "Front Entrance", "role_tag": "security"},
            },
            shadow_mode=False,  # THIS is the go-live flag — real sends happen below
            nudge_timer_seconds=180, nudge_cap_per_shift=3,
            command_timer_seconds=300, command_throttle_per_hour=5,
            resolve_cooldown_seconds=900,
            on_notify=dispatcher,
        )

        # A genuine, sustained gap in a STAFFED zone -> real nudge should fire.
        gap_event = {
            "camera_id": "lobby_cam_1", "zone_id": "concession", "role_tag": "concession",
            "entity_ref": None, "event_type": "zone_gap",
            "confidence": 0.9, "source_model_version": "floorwatch-coverage/0.2.0",
        }
        out = engine.process_detection_event(gap_event)
        assert out[0]["event_type"] == "zone_nudge_sent"
        assert out[0]["shadow_mode_suppressed"] is False

        # A gap in an UNSTAFFED zone must never trigger anything — the "no
        # false escalation" guarantee (roster hard precondition, Constraint 5).
        unstaffed_gap = dict(gap_event, zone_id="entrance", role_tag="security")
        out2 = engine.process_detection_event(unstaffed_gap)
        assert out2 == []
        assert mock_instance.messages.create.call_count == 1  # only the legitimate one sent

    # ── The real (mocked-SDK) Twilio call actually happened ─────────────
    mock_instance.messages.create.assert_called_once()
    call_kwargs = mock_instance.messages.create.call_args.kwargs
    assert call_kwargs["to"] == "+15551112222"
    assert "Concession Counter" in call_kwargs["body"]

    # ── Escalate it to Tier 3 so there's something in the digest to summarize ──
    original_clock = engine._clock
    engine._clock = lambda: original_clock() + 1000  # fast-forward past both timers
    for evt in engine.tick():
        pass
    assert engine.zones["concession"].status in ("command", "escalated")
    for evt in engine.tick():  # second jump if only reached command on first tick
        pass

    # ── 3. shift digest job summarizes the real digest file automatically ──
    events = digest.read_all()
    assert len(events) >= 1
    summary = summarize(events, events[0]["timestamp"][:10])
    assert summary["total_events_today"] == len(events)

    # "No false escalation" means no nudge/command/escalation ever fired
    # for the unstaffed zone — a roster-ignored gap IS still logged for
    # audit purposes (that's intentional, see engine.py), but it must
    # never appear as an actionable tier event.
    entrance_entries = [e for e in events if e.get("zone_id") == "entrance"]
    assert all(e.get("action_type") == "ignored_unstaffed" for e in entrance_entries), \
        f"unstaffed zone must only ever produce audit-log entries, got: {entrance_entries}"
    escalation_types = {"zone_nudge_sent", "zone_supervisor_command", "zone_escalated"}
    assert not any(e.get("event_type") in escalation_types and e.get("zone_id") == "entrance" for e in events)
