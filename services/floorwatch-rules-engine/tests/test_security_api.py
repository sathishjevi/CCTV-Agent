"""Security alerts end to end: presence readings arrive on the Redis stream exactly as the coverage skill
publishes them, the rules engine judges them, and the dashboard/app see alerts, notifications and audit trail."""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

fakeredis = pytest.importorskip("fakeredis")
redis = pytest.importorskip("redis")
pytest.importorskip("fastapi")

from test_integration import fake_redis_url  # noqa: E402,F401
from test_employee_dashboard_api import _add_employee, _set_password_and_login  # noqa: E402

NIGHT = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc).timestamp()      # 02:00, inside "closed"
DAYTIME = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def sec(fake_redis_url, monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "REDIS_URL", fake_redis_url)
    monkeypatch.setattr(config, "DIGEST_PATH", tmp_path / "digest.jsonl")
    monkeypatch.setattr(config, "TICK_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(config, "USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr(config, "EVENT_HISTORY_PATH", tmp_path / "event_history.jsonl")
    monkeypatch.setattr(config, "EMPLOYEE_DIRECTORY_PATH", tmp_path / "employee_directory.json")
    monkeypatch.setattr(config, "TASK_STORE_PATH", tmp_path / "tasks.json")
    monkeypatch.setattr(config, "PRIVACY_REQUESTS_PATH", tmp_path / "privacy_requests.json")
    monkeypatch.setattr(config, "MFA_STORE_PATH", tmp_path / "mfa.json")
    monkeypatch.setattr(config, "ZONE_DIRECTORY_PATH", tmp_path / "zone_directory.json")     # never the real file
    monkeypatch.setattr(config, "ZONES_META_PATH", tmp_path / "no_zones_meta.json")
    monkeypatch.setattr(config, "AUTH_SECRET", "test-fixture-secret-needs-32-bytes-minimum")
    monkeypatch.setattr(config, "SHADOW_MODE", config.SHADOW_MODE)
    monkeypatch.setattr(config, "OPEN_HOURS", "daily 09:00-23:00")
    monkeypatch.setattr(config, "TIMEZONE", "UTC")

    sys.modules.pop("main", None)
    sys.modules.pop("engine", None)
    sys.modules.pop("effort_engine", None)
    sys.modules.pop("security_engine", None)
    import main as main_module

    main_module.users.create_user("test-supervisor", "test-password-123", role="supervisor")
    main_module.users.create_user("test-admin", "correct-horse-battery-9", role="admin", must_change_password=False)
    main_module.users.create_user("test-viewer", "correct-horse-battery-8", role="viewer", must_change_password=False)
    main_module.security_engine._clock = lambda: NIGHT

    from fastapi.testclient import TestClient
    publisher = redis.Redis.from_url(fake_redis_url, decode_responses=True)

    def publish(zone_id, action="arrived", etype="zone_person_present", camera="cam1"):
        publisher.xadd(config.REDIS_STREAM, {"data": json.dumps({
            "camera_id": camera, "zone_id": zone_id, "role_tag": "security", "event_type": etype,
            "action_type": action, "confidence": 0.9, "source_model_version": "floorwatch-coverage/0.3.0",
            "timestamp": datetime.now(timezone.utc).isoformat()})})

    def wait_for(fn, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            value = fn()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError("timed out waiting for the condition")

    with TestClient(main_module.app) as client:
        def login(user, password):
            r = client.post("/api/login", json={"username": user, "password": password})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['token']}"}

        sup = login("test-supervisor", "test-password-123")
        client.headers.update(sup)
        yield SimpleNamespace(
            client=client, main=main_module, publish=publish, wait_for=wait_for, config=config, sup=sup,
            admin=login("test-admin", "correct-horse-battery-9"), viewer=login("test-viewer", "correct-horse-battery-8"),
            tmp_path=tmp_path, redis_url=fake_redis_url)


def add_zones(sec):
    for body in (
        {"zone_id": "back_door", "name": "Back Door", "role_tag": "security", "camera_id": "cam1", "entry_point": "door"},
        {"zone_id": "stockroom", "name": "Stockroom", "role_tag": "janitor", "camera_id": "cam3", "access": "closed_hours"},
        {"zone_id": "safe", "name": "Cash Office", "role_tag": "security", "camera_id": "cam4", "access": "restricted"},
        {"zone_id": "lobby", "name": "Lobby", "role_tag": "usher", "camera_id": "cam5"},
    ):
        r = sec.client.post("/api/admin/zones", json=body)
        assert r.status_code == 200, r.text


def status(sec):
    r = sec.client.get("/api/security/status")
    assert r.status_code == 200, r.text
    return r.json()


def alerts(sec):
    return status(sec)["alerts"]


# ── zone settings ────────────────────────────────────────────────────────────

def test_zones_carry_their_security_settings_and_old_zones_default_to_no_rule(sec):
    add_zones(sec)
    zones = {z["zone_id"]: z for z in sec.client.get("/api/admin/zones").json()["zones"]}
    assert (zones["back_door"]["access"], zones["back_door"]["entry_point"]) == ("open", "door")
    assert (zones["stockroom"]["access"], zones["stockroom"]["entry_point"]) == ("closed_hours", "")
    assert (zones["safe"]["access"], zones["lobby"]["access"]) == ("restricted", "open")


def test_a_zone_can_be_changed_and_the_change_is_audited_in_history(sec):
    add_zones(sec)
    r = sec.client.post("/api/admin/zones/lobby/set-security", json={"access": "restricted", "entry_point": "window"})
    assert r.status_code == 200 and r.json() == {"ok": True, "access": "restricted", "entry_point": "window"}
    lobby = next(z for z in sec.client.get("/api/admin/zones").json()["zones"] if z["zone_id"] == "lobby")
    assert (lobby["access"], lobby["entry_point"]) == ("restricted", "window")
    history = sec.wait_for(lambda: sec.client.get("/api/history?event_type=security_mode_changed").json())
    assert any("Lobby" in e["message"] and "test-supervisor" in e["message"] for e in history)


def test_zone_security_input_is_validated(sec):
    add_zones(sec)
    assert sec.client.post("/api/admin/zones/lobby/set-security", json={}).status_code == 400
    assert sec.client.post("/api/admin/zones/lobby/set-security", json={"access": "whenever"}).status_code == 422
    assert sec.client.post("/api/admin/zones/lobby/set-security", json={"entry_point": "roof"}).status_code == 422
    assert sec.client.post("/api/admin/zones/lobby/set-security", json={"access": "open", "extra": 1}).status_code == 422
    assert sec.client.post("/api/admin/zones/ghost/set-security", json={"access": "open"}).status_code == 404


def test_changing_zone_security_needs_supervisor_not_viewer(sec):
    add_zones(sec)
    r = sec.client.post("/api/admin/zones/lobby/set-security", json={"access": "restricted"}, headers=sec.viewer)
    assert r.status_code == 403


# ── presence -> alert -> dashboard ───────────────────────────────────────────

def test_a_person_at_the_door_after_hours_raises_a_critical_alert_that_reaches_the_dashboard(sec):
    add_zones(sec)
    with sec.client.websocket_connect(f"/events?token={sec.sup['Authorization'].split()[1]}") as ws:
        sec.publish("back_door")
        evt = ws.receive_json()
    assert evt["event_type"] == "security_alert" and evt["alert_kind"] == "break_in_door"
    assert evt["severity"] == "critical" and evt["zone_name"] == "Back Door"
    assert "_notify" not in evt                                    # internal marker never leaves the server
    (alert,) = sec.wait_for(lambda: alerts(sec))
    assert alert["alert_id"] == evt["alert_id"] and alert["status"] == "open"


def test_the_same_person_at_the_door_in_the_daytime_raises_nothing(sec):
    add_zones(sec)
    sec.main.security_engine._clock = lambda: DAYTIME
    sec.publish("back_door")
    sec.publish("lobby")
    time.sleep(0.6)
    assert alerts(sec) == []


def test_a_restricted_room_alerts_even_in_the_daytime(sec):
    add_zones(sec)
    sec.main.security_engine._clock = lambda: DAYTIME
    sec.publish("safe")
    (alert,) = sec.wait_for(lambda: alerts(sec))
    assert alert["kind"] == "restricted_zone_entry"


def test_presence_readings_are_not_kept_in_history_but_alerts_are(sec):
    add_zones(sec)
    sec.publish("back_door")
    sec.wait_for(lambda: alerts(sec))
    kinds = {e["event_type"] for e in sec.client.get("/api/history?limit=100").json()}
    assert "security_alert" in kinds
    assert "zone_person_present" not in kinds and "zone_person_cleared" not in kinds


def test_a_zone_edited_on_another_replica_is_picked_up(sec):
    add_zones(sec)
    sec.main.zone_directory.set_security("lobby", "restricted", None)       # as if another replica did it
    sec.publish("lobby")
    (alert,) = sec.wait_for(lambda: alerts(sec))
    assert alert["zone_id"] == "lobby" and alert["kind"] == "restricted_zone_entry"


def test_end_to_end_with_the_real_coverage_skill(sec, tmp_path):
    """Camera detections in -> the REAL coverage skill (a subprocess) -> Redis -> the app -> an alert out."""
    pytest.importorskip("supervision")
    add_zones(sec)
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir()
    (zones_dir / "cam1.json").write_text(json.dumps({"camera_id": "cam1", "zones": [
        {"zone_id": "back_door", "role_tag": "security", "polygon": [[100, 100], [400, 100], [400, 500], [100, 500]]}]}))
    script = Path(__file__).resolve().parents[3] / "skills" / "detection" / "floorwatch-coverage" / "scripts" / "main.py"
    proc = subprocess.Popen(
        [sys.executable, str(script), "--zones-dir", str(zones_dir), "--redis-url", sec.redis_url,
         "--presence-arrive-seconds", "1", "--presence-clear-seconds", "2", "--debounce-seconds", "60"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        assert json.loads(proc.stdout.readline())["presence_events"] is True

        def frame(second, person):
            objects = [{"class": "person", "confidence": 0.93, "bbox": [150, 200, 250, 400]}] if person else []
            proc.stdin.write(json.dumps({"event": "detections", "camera_id": "cam1",
                                         "timestamp": f"2026-11-03T02:00:{second:02d}Z", "objects": objects}) + "\n")
            proc.stdin.flush()

        for second in range(0, 3):                      # someone stands at the back door at 02:00
            frame(second, True)
        (alert,) = sec.wait_for(lambda: alerts(sec))
        assert alert["kind"] == "break_in_door" and alert["zone_id"] == "back_door" and alert["still_present"] is True

        for second in range(3, 8):                      # ...and walks away
            frame(second, False)
        sec.wait_for(lambda: alerts(sec) and alerts(sec)[0]["still_present"] is False)
        assert alerts(sec)[0]["status"] == "open"        # leaving does not close it: a human must
    finally:
        proc.stdin.write(json.dumps({"command": "stop"}) + "\n")
        proc.stdin.flush()
        proc.terminate()


def test_bad_presence_events_are_dropped_not_fatal(sec):
    add_zones(sec)
    import redis as _redis
    r = _redis.Redis.from_url(sec.redis_url, decode_responses=True)
    r.xadd(sec.config.REDIS_STREAM, {"data": json.dumps({"event_type": "zone_person_present", "zone_id": 5})})
    r.xadd(sec.config.REDIS_STREAM, {"data": "not json"})
    sec.publish("back_door")
    assert sec.wait_for(lambda: alerts(sec))


# ── acting on alerts ─────────────────────────────────────────────────────────

def test_acknowledge_then_resolve(sec):
    add_zones(sec)
    sec.publish("back_door")
    (alert,) = sec.wait_for(lambda: alerts(sec))
    r = sec.client.post(f"/api/security/alerts/{alert['alert_id']}/acknowledge")
    assert r.status_code == 200 and "Acknowledged by test-supervisor" in r.json()["message"]
    assert alerts(sec)[0]["status"] == "acknowledged"
    r = sec.client.post(f"/api/security/alerts/{alert['alert_id']}/resolve", json={"outcome": "handled", "note": "Called police"})
    assert r.status_code == 200 and r.json()["resolved_by"] == "test-supervisor"
    assert alerts(sec) == []
    resolved = status(sec)["recently_resolved"][0]
    assert resolved["outcome"] == "handled" and resolved["note"] == "Called police"
    history = sec.client.get("/api/history?event_type=security_alert_resolved").json()
    assert history and history[0]["resolved_by"] == "test-supervisor"


def test_resolve_needs_a_valid_outcome_and_a_real_open_alert(sec):
    add_zones(sec)
    sec.publish("back_door")
    (alert,) = sec.wait_for(lambda: alerts(sec))
    assert sec.client.post(f"/api/security/alerts/{alert['alert_id']}/resolve", json={"outcome": "meh"}).status_code == 422
    assert sec.client.post(f"/api/security/alerts/{alert['alert_id']}/resolve", json={}).status_code == 422
    assert sec.client.post("/api/security/alerts/not-an-alert/resolve", json={"outcome": "handled"}).status_code == 404
    assert sec.client.post("/api/security/alerts/not-an-alert/acknowledge").status_code == 404


def test_viewers_can_neither_see_nor_act(sec):
    add_zones(sec)
    assert sec.client.get("/api/security/status", headers=sec.viewer).status_code == 403
    assert sec.client.post("/api/security/disarm", json={"minutes": 30}, headers=sec.viewer).status_code == 403
    assert sec.client.post("/api/security/alerts/x/acknowledge", headers=sec.viewer).status_code == 403
    assert sec.client.get("/api/security/status", headers={"Authorization": ""}).status_code in (401, 403)


def test_disarm_and_re_arm(sec):
    add_zones(sec)
    r = sec.client.post("/api/security/disarm", json={"minutes": 60, "reason": "cleaning crew"})
    assert r.status_code == 200 and "disarmed by test-supervisor" in r.json()["message"]
    s = sec.wait_for(lambda: (lambda st: st if st["phase"] == "disarmed" else None)(status(sec)))
    assert s["disarmed_by"] == "test-supervisor" and s["armed"] is False
    sec.publish("back_door")
    time.sleep(0.6)
    assert alerts(sec) == []
    assert sec.client.post("/api/security/arm").status_code == 200
    assert sec.client.post("/api/security/arm").status_code == 409            # nothing left to undo
    sec.publish("back_door", action="cleared", etype="zone_person_cleared")
    sec.publish("back_door")
    sec.wait_for(lambda: alerts(sec))


def test_disarm_input_is_validated(sec):
    for body in ({}, {"minutes": 0}, {"minutes": -5}, {"minutes": 99999}, {"minutes": "soon"}, {"minutes": 30, "x": 1}):
        assert sec.client.post("/api/security/disarm", json=body).status_code == 422, body


def test_status_reports_the_schedule_and_shadow_mode(sec):
    s = status(sec)
    assert s["enabled"] and s["schedule"]["configured"] and s["schedule"]["timezone"] == "UTC"
    assert s["schedule_error"] is None and "shadow_mode" in s


# ── notifications ────────────────────────────────────────────────────────────

def _capture_sends(sec, monkeypatch):
    sent = []
    monkeypatch.setattr(sec.main, "_send_task_notification",
                        lambda number, message, task_id=None: sent.append((number, message)) or {"sent": True, "channel": "fcm"})
    return sent


def test_alerts_message_supervisors_and_admins_only_in_plain_ascii_under_160_chars(sec, monkeypatch):
    add_zones(sec)
    _add_employee(sec.client, "300", "supervisor", "+15559000300")
    _add_employee(sec.client, "301", "admin", "+15559000301")
    _add_employee(sec.client, "302", "secondary_admin", "+15559000302")
    _add_employee(sec.client, "100", "employee", "+15559000100")
    sent = _capture_sends(sec, monkeypatch)
    sec.publish("back_door")
    sec.wait_for(lambda: sent)
    time.sleep(0.3)
    assert sorted(n for n, _ in sent) == ["300", "301", "302"]            # the line employee is not told
    for _, text in sent:
        assert text.startswith("Floorwatch ALERT:") and "break-in" in text and "Back Door" in text
        assert len(text) <= 160 and text.isascii()


def test_a_long_or_unusual_zone_name_still_fits_one_sms(sec, monkeypatch):
    sec.client.post("/api/admin/zones", json={
        "zone_id": "odd", "name": "Café — " + "x" * 100, "role_tag": "security", "entry_point": "door"})
    _add_employee(sec.client, "300", "supervisor", "+15559000300")
    sent = _capture_sends(sec, monkeypatch)
    sec.publish("odd")
    sec.wait_for(lambda: sent)
    assert len(sent[0][1]) <= 160 and sent[0][1].isascii()


def test_disarming_tells_the_team_too(sec, monkeypatch):
    _add_employee(sec.client, "300", "supervisor", "+15559000300")
    sent = _capture_sends(sec, monkeypatch)
    sec.client.post("/api/security/disarm", json={"minutes": 30, "reason": "delivery"})
    sec.wait_for(lambda: sent)
    assert "disarmed by test-supervisor" in sent[0][1]


def test_an_alert_nobody_could_be_told_about_is_still_raised_and_logged_as_an_error(sec, monkeypatch):
    add_zones(sec)                                             # no staff exist, so there is no one to message
    errors = []
    monkeypatch.setattr(sec.main, "log", lambda msg, *a, level="info", **k: errors.append(msg) if level == "error" else None)
    sec.publish("back_door")
    sec.wait_for(lambda: any("NOBODY to notify" in m for m in errors))
    assert alerts(sec)[0]["kind"] == "break_in_door"           # visible on the dashboard regardless


def test_in_shadow_mode_the_alert_is_visible_but_flagged_as_not_delivered(sec):
    add_zones(sec)
    sec.config.SHADOW_MODE = True
    with sec.client.websocket_connect(f"/events?token={sec.sup['Authorization'].split()[1]}") as ws:
        sec.publish("back_door")
        assert ws.receive_json()["shadow_mode_suppressed"] is True


# ── the mobile app's own endpoints ───────────────────────────────────────────

def test_the_app_can_see_and_act_on_alerts_as_a_supervisor_but_not_as_a_plain_employee(sec):
    add_zones(sec)
    _add_employee(sec.client, "300", "supervisor", "+15559000300")
    _add_employee(sec.client, "100", "employee", "+15559000100")
    sup = _set_password_and_login(sec.client, "300", "+15559000300")
    emp = _set_password_and_login(sec.client, "100", "+15559000100")
    sup_h = {"Authorization": f"Bearer {sup['token']}"}
    emp_h = {"Authorization": f"Bearer {emp['token']}"}
    sec.publish("back_door")
    sec.wait_for(lambda: alerts(sec))
    assert sec.client.get("/api/employee/dashboard/security/status", headers=emp_h).status_code == 403
    st = sec.client.get("/api/employee/dashboard/security/status", headers=sup_h)
    assert st.status_code == 200 and st.json()["alerts"][0]["kind"] == "break_in_door"
    alert_id = st.json()["alerts"][0]["alert_id"]
    r = sec.client.post(f"/api/employee/dashboard/security/alerts/{alert_id}/acknowledge", headers=sup_h)
    assert r.status_code == 200 and "employee:300" in r.json()["message"]
    r = sec.client.post(f"/api/employee/dashboard/security/alerts/{alert_id}/resolve", headers=sup_h,
                        json={"outcome": "false_alarm"})
    assert r.status_code == 200 and r.json()["resolved_by"] == "employee:300"
    assert sec.client.post("/api/employee/dashboard/security/disarm", json={"minutes": 30}, headers=sup_h).status_code == 200
    assert sec.client.post("/api/employee/dashboard/security/arm", headers=sup_h).status_code == 200
    assert sec.client.post("/api/employee/dashboard/security/disarm", json={"minutes": 30}, headers=emp_h).status_code == 403


def test_only_admins_and_secondary_admins_can_change_zone_security_from_the_app(sec):
    add_zones(sec)
    _add_employee(sec.client, "300", "supervisor", "+15559000300")
    _add_employee(sec.client, "302", "secondary_admin", "+15559000302")
    sup = {"Authorization": f"Bearer {_set_password_and_login(sec.client, '300', '+15559000300')['token']}"}
    mgr = {"Authorization": f"Bearer {_set_password_and_login(sec.client, '302', '+15559000302')['token']}"}
    url = "/api/employee/dashboard/zones/lobby/set-security"
    assert sec.client.post(url, json={"access": "restricted"}, headers=sup).status_code == 403
    assert sec.client.post(url, json={"access": "restricted"}, headers=mgr).status_code == 200
    zones = {z["zone_id"]: z for z in sec.client.get("/api/employee/dashboard/zones", headers=mgr).json()["zones"]}
    assert zones["lobby"]["access"] == "restricted"


# ── configuration problems must be loud, not silent ──────────────────────────

def test_an_unparseable_opening_hours_setting_does_not_stop_the_service_but_is_reported(fake_redis_url, monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "REDIS_URL", fake_redis_url)
    for name, fname in (("DIGEST_PATH", "d.jsonl"), ("USERS_PATH", "u.json"), ("EVENT_HISTORY_PATH", "h.jsonl"),
                        ("EMPLOYEE_DIRECTORY_PATH", "e.json"), ("TASK_STORE_PATH", "t.json"),
                        ("PRIVACY_REQUESTS_PATH", "p.json"), ("MFA_STORE_PATH", "m.json"),
                        ("ZONE_DIRECTORY_PATH", "z.json"), ("ZONES_META_PATH", "zm.json")):
        monkeypatch.setattr(config, name, tmp_path / fname)
    monkeypatch.setattr(config, "AUTH_SECRET", "test-fixture-secret-needs-32-bytes-minimum")
    monkeypatch.setattr(config, "OPEN_HOURS", "mon-fri nine to five")
    for m in ("main", "engine", "effort_engine", "security_engine"):
        sys.modules.pop(m, None)
    import main as main_module
    assert main_module.security_schedule_error and "nine to five" in main_module.security_schedule_error
    assert main_module.security_schedule.configured is False         # falls back to "never closed"
    main_module.users.create_user("adm", "correct-horse-battery-9", role="admin", must_change_password=False)
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        token = client.post("/api/login", json={"username": "adm", "password": "correct-horse-battery-9"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        d = client.get("/api/admin/diagnostics", headers=h).json()["security_alerts"]
        assert d["schedule_configured"] is False and "nine to five" in d["schedule_error"]
        assert client.get("/api/security/status", headers=h).json()["schedule_error"]


def test_diagnostics_summarise_the_security_setup(sec):
    add_zones(sec)
    _add_employee(sec.client, "300", "supervisor", "+15559000300")
    d = sec.client.get("/api/admin/diagnostics", headers=sec.admin).json()["security_alerts"]
    assert d["enabled"] and d["schedule_configured"] and d["schedule_error"] is None
    assert d["zones_with_rules"] == 2 and d["entry_point_zones"] == 1
    assert d["recipients"] == 1 and "supervisor" in d["alert_roles"]
    assert d["recipients_reachable"] == 0               # right role, but no channel/phone-or-app set up: not reachable


def test_diagnostics_count_who_can_actually_be_reached(sec):
    sec.client.post("/api/admin/employees", json={"employee_number": "300", "name": "A", "role": "supervisor",
                                                  "department": "ops", "phone": "+15559000300", "channel": "sms"})
    sec.client.post("/api/admin/employees", json={"employee_number": "301", "name": "B", "role": "admin",
                                                  "department": "ops", "phone": "+15559000301"})   # no channel yet
    d = sec.client.get("/api/admin/diagnostics", headers=sec.admin).json()["security_alerts"]
    assert d["recipients"] == 2 and d["recipients_reachable"] == 1


# ── the privacy notice tells staff about it ──────────────────────────────────

def test_the_privacy_notice_discloses_security_alerts_when_they_are_on_and_not_when_off(sec, monkeypatch):
    on = sec.client.get("/api/privacy-notice", headers={"Authorization": ""}).json()
    assert "Security alerts" in on["text"] and "does not identify anyone" in on["text"]
    monkeypatch.setattr(sec.config, "SECURITY_ALERTS_ENABLED", False)
    off = sec.client.get("/api/privacy-notice", headers={"Authorization": ""}).json()
    assert "Security alerts" not in off["text"]
    assert on["version"] != off["version"]          # a material change makes everyone read it again
