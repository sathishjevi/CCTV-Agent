"""The database layer against a REAL PostgreSQL — the pool, timeouts, slow-query
logging, schema audit log, TLS reporting, and every store's actual SQL.

Everything else in the suite runs the stores on their local-file fallbacks, so
SQL like `clear_fcm_token_from_others` was never executed by any test. These
run only when FLOORWATCH_TEST_POSTGRES_DSN points at a throwaway server
(CI starts one as a service container; locally, any scratch Postgres works —
each test gets its own freshly created database and drops it afterwards).
Skipped otherwise."""

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

ADMIN_DSN = os.environ.get("FLOORWATCH_TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason="FLOORWATCH_TEST_POSTGRES_DSN not set")

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

import floorwatch_auth as auth  # noqa: E402
import floorwatch_db  # noqa: E402


def _dsn_for(dbname: str) -> str:
    from urllib.parse import urlparse, urlunparse
    u = urlparse(ADMIN_DSN)
    return urlunparse(u._replace(path="/" + dbname))


@pytest.fixture
def dsn():
    """A brand-new empty database for each test, dropped afterwards."""
    name = "fw_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    yield _dsn_for(name)
    floorwatch_db.close_all()
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(autouse=True)
def _reset_db_module(monkeypatch):
    yield
    floorwatch_db.close_all()


# ── pool, timeouts, logging ─────────────────────────────────────────────────

def test_connections_are_pooled_and_reused(dsn):
    with floorwatch_db.connection(dsn) as c1:
        pid1 = c1.execute("SELECT pg_backend_pid()").fetchone()[0]
    with floorwatch_db.connection(dsn) as c2:
        pid2 = c2.execute("SELECT pg_backend_pid()").fetchone()[0]
    assert pid1 == pid2                       # the same backend: reused, not reopened per call
    assert floorwatch_db.get_pool(dsn) is floorwatch_db.get_pool(dsn)


def test_the_pool_never_opens_more_connections_than_its_cap(dsn, monkeypatch):
    import threading
    monkeypatch.setattr(floorwatch_db, "POOL_MAX", 3)
    monkeypatch.setattr(floorwatch_db, "POOL_MIN", 1)
    pids, lock = set(), threading.Lock()

    def work():
        with floorwatch_db.connection(dsn) as conn:
            with lock:
                pids.add(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            conn.execute("SELECT pg_sleep(0.15)")

    threads = [threading.Thread(target=work) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert 1 <= len(pids) <= 3


def test_a_runaway_statement_is_cancelled_by_the_statement_timeout(dsn, monkeypatch):
    monkeypatch.setattr(floorwatch_db, "STATEMENT_TIMEOUT_MS", 300)
    with floorwatch_db.connection(dsn) as conn:
        started = time.time()
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("SELECT pg_sleep(5)")
        assert time.time() - started < 3


def test_new_connections_carry_the_application_name(dsn):
    with floorwatch_db.connection(dsn) as conn:
        assert conn.execute("SHOW application_name").fetchone()[0] == "floorwatch"


def test_slow_queries_are_logged_without_their_parameters(dsn, monkeypatch, capsys):
    monkeypatch.setattr(floorwatch_db, "SLOW_QUERY_MS", 50)
    with floorwatch_db.connection(dsn) as conn:
        conn.execute("SELECT pg_sleep(%s), %s", (0.15, "SECRET-PARAMETER-VALUE"))
    err = capsys.readouterr().err
    assert "slow query" in err and "pg_sleep" in err
    assert "SECRET-PARAMETER-VALUE" not in err


def test_fast_queries_are_not_logged(dsn, capsys):
    with floorwatch_db.connection(dsn) as conn:
        conn.execute("SELECT 1")
    assert "slow query" not in capsys.readouterr().err


def test_literals_in_logged_statements_are_masked():
    assert floorwatch_db.describe_statement("SELECT * FROM t WHERE phone = '+15551234567' AND id = 42") == \
        "SELECT * FROM t WHERE phone = ? AND id = ?"


def test_the_connection_string_is_never_exposed_by_safe_target():
    shown = floorwatch_db.safe_target("postgresql://app:hunter2-password@db.internal:5432/floorwatch")
    assert shown == "db.internal:5432/floorwatch" and "hunter2" not in shown and "app:" not in shown


def test_tls_status_reports_what_the_server_says(dsn):
    status = floorwatch_db.tls_status(dsn)
    assert status["configured"] is True and "server_version" in status
    assert status["tls"] in (True, False)            # the scratch server may or may not have TLS


def test_requiring_tls_refuses_a_plaintext_connection(dsn, monkeypatch):
    if floorwatch_db.tls_status(dsn).get("tls"):
        pytest.skip("this scratch server offers TLS, so there is no plaintext connection to refuse")
    floorwatch_db.close_all()
    monkeypatch.setattr(floorwatch_db, "REQUIRE_TLS", True)
    with pytest.raises(Exception):
        floorwatch_db.get_pool(dsn)
    assert dsn not in floorwatch_db._pools            # a failed pool is not cached


def test_an_unreachable_database_fails_fast_and_stores_fall_back_to_local_files(tmp_path):
    bad = "postgresql://nobody:nothing@127.0.0.1:1/none"
    started = time.time()
    store = auth.build_user_store(bad, tmp_path / "users.json")
    assert isinstance(store, auth.UserStore)           # fell back, exactly as before
    assert time.time() - started < 30


# ── schema audit log ────────────────────────────────────────────────────────

def test_schema_changes_are_recorded_once_and_a_restart_is_a_no_op(dsn, capsys):
    from employee_directory import PostgresEmployeeDirectory
    PostgresEmployeeDirectory(dsn)
    with floorwatch_db.connection(dsn) as conn:
        first = conn.execute("SELECT count(*) FROM floorwatch_schema_log WHERE store='employees'").fetchone()[0]
    assert first >= 5
    assert "schema change applied for 'employees'" in capsys.readouterr().err

    PostgresEmployeeDirectory(dsn)                      # "restart"
    with floorwatch_db.connection(dsn) as conn:
        second = conn.execute("SELECT count(*) FROM floorwatch_schema_log WHERE store='employees'").fetchone()[0]
    assert second == first
    assert "schema change applied" not in capsys.readouterr().err


# ── employees ───────────────────────────────────────────────────────────────

def _employees(dsn):
    from employee_directory import PostgresEmployeeDirectory
    return PostgresEmployeeDirectory(dsn)


def test_employee_round_trip_and_phone_lookup(dsn):
    d = _employees(dsn)
    d.add("101", "Alex Chen", "employee", "ops", "+15559000101")
    got = d.get("101")
    assert got["name"] == "Alex Chen" and got["active"] is True
    assert d.get_by_phone("+15559000101")["employee_number"] == "101"
    d.set_active("101", False)
    assert d.get_by_phone("+15559000101") is None        # inactive employees can't be found for login


def test_a_push_token_belongs_to_one_employee_at_a_time_on_real_sql(dsn):
    d = _employees(dsn)
    d.add("101", "A", "employee", "ops", "+15559000101")
    d.add("102", "B", "employee", "ops", "+15559000102")
    d.set_fcm_token("101", "shared-phone")
    d.set_fcm_token("102", "shared-phone")
    assert d.clear_fcm_token_from_others("shared-phone", "102") == 1
    assert d.get("101")["fcm_token"] is None
    assert d.get("102")["fcm_token"] == "shared-phone"
    assert d.clear_fcm_token_from_others("shared-phone", "102") == 0     # idempotent


def test_clearing_a_token_with_none_works(dsn):
    d = _employees(dsn)
    d.add("101", "A", "employee", "ops", "+15559000101")
    d.set_fcm_token("101", "tok")
    d.set_fcm_token("101", None)
    assert d.get("101")["fcm_token"] is None


def test_employee_password_hash_round_trip(dsn):
    d = _employees(dsn)
    d.add("101", "A", "employee", "ops", "+15559000101")
    d.set_password_hash("101", auth.hash_password("correct horse battery"))
    stored = d.get("101")["password_hash"]
    assert auth.verify_password("correct horse battery", stored)


# ── dashboard accounts ──────────────────────────────────────────────────────

def test_user_store_login_and_legacy_hash_upgrade_on_real_sql(dsn):
    store = auth.PostgresUserStore(dsn)
    store.create_user("alice", "first-password-12", role="supervisor", must_change_password=False)
    assert store.authenticate("alice", "first-password-12") == {"role": "supervisor", "must_change_password": False}
    assert store.authenticate("alice", "wrong") is None
    assert store.authenticate("nobody", "whatever") is None

    legacy = auth._legacy_pbkdf2_hash("first-password-12")
    with floorwatch_db.connection(dsn) as conn:
        conn.execute("UPDATE floorwatch_users SET password_hash=%s WHERE username='alice'", (legacy,))
    assert store.authenticate("alice", "first-password-12") is not None
    with floorwatch_db.connection(dsn) as conn:
        upgraded = conn.execute("SELECT password_hash FROM floorwatch_users WHERE username='alice'").fetchone()[0]
    assert upgraded.startswith("scrypt$")
    assert store.authenticate("alice", "first-password-12") is not None


def test_user_deactivation_blocks_login_and_is_timestamped(dsn):
    store = auth.PostgresUserStore(dsn)
    store.create_user("bob", "second-password-12", role="viewer", must_change_password=False)
    store.set_active("bob", False)
    assert store.authenticate("bob", "second-password-12") is None
    assert [u for u in store.list_users() if u["username"] == "bob"][0]["deactivated_at"] is not None
    store.set_active("bob", True)
    assert store.authenticate("bob", "second-password-12") is not None


# ── zones, history, tasks ───────────────────────────────────────────────────

def test_zone_directory_on_real_sql(dsn):
    from zone_directory import PostgresZoneDirectory
    z = PostgresZoneDirectory(dsn)
    z.add("lobby", "Main Lobby", "usher", "cam1", created_by="t")
    assert z.get("lobby")["name"] == "Main Lobby"
    z.set_staffed("lobby", False)
    assert z.get("lobby")["staffed"] is False
    z.set_active("lobby", False)
    assert z.list_all(active_only=True) == [] and len(z.list_all()) == 1


def test_event_history_on_real_sql_record_query_and_purge(dsn):
    from event_history import PostgresEventHistoryStore
    h = PostgresEventHistoryStore(dsn)
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    h.record({"event_id": "e1", "timestamp": old, "event_type": "zone_gap", "zone_id": "lobby"})
    h.record({"event_id": "e2", "timestamp": new, "event_type": "task_assigned", "zone_id": "lobby", "task_id": "t1"})
    h.record({"event_id": "e2", "timestamp": new, "event_type": "task_assigned", "zone_id": "lobby", "task_id": "t1"})  # dedupe
    assert len(h.query()) == 2
    assert [e["event_id"] for e in h.query(task_id="t1")] == ["e2"]
    assert [e["event_id"] for e in h.query(event_type="zone_gap")] == ["e1"]
    result = h.purge_older_than(90)
    assert result["purged"] == 1 and result["kept"] == 1
    assert [e["event_id"] for e in h.query()] == ["e2"]


def test_task_store_on_real_sql(dsn):
    from task_store import PostgresTaskStore
    t = PostgresTaskStore(dsn)
    now = datetime.now(timezone.utc).isoformat()
    rec = {"task_id": "t1", "task_name": "Sweep", "task_type": "clean", "zone_id": "lobby", "camera_id": None,
           "assigned_minutes": 30.0, "active_seconds": 0.0, "status": "open", "workflow_status": "notified",
           "assigned_to": "101", "assigned_by": "user:x", "nudged": False, "status_nudge_sent": False,
           "reopened_for_review": False, "resolution_type": None, "started_at": now, "updated_at": now}
    t.upsert(rec)
    t.upsert({**rec, "workflow_status": "in_progress"})
    assert t.get("t1")["workflow_status"] == "in_progress"
    assert [r["task_id"] for r in t.list_open()] == ["t1"]
    t.upsert({**rec, "status": "resolved"})
    assert t.list_open() == []


# ── privacy features on real SQL ────────────────────────────────────────────

def test_employee_deactivation_is_timestamped_and_reactivation_clears_it(dsn):
    d = _employees(dsn)
    d.add("101", "A", "employee", "ops", "+15559000101")
    assert d.get_privacy_state("101")["deactivated_at"] is None
    d.set_active("101", False)
    first = d.get_privacy_state("101")["deactivated_at"]
    assert first is not None
    d.set_active("101", False)                                        # deactivating again keeps the ORIGINAL moment
    assert d.get_privacy_state("101")["deactivated_at"] == first
    d.set_active("101", True)
    assert d.get_privacy_state("101")["deactivated_at"] is None


def test_notice_acknowledgment_is_stored_per_employee(dsn):
    d = _employees(dsn)
    d.add("101", "A", "employee", "ops", "+15559000101")
    d.add("102", "B", "employee", "ops", "+15559000102")
    assert d.record_privacy_notice_acceptance("101", "v1") is True
    assert d.get_privacy_state("101")["privacy_notice_version"] == "v1"
    assert d.get_privacy_state("102")["privacy_notice_version"] is None
    assert d.record_privacy_notice_acceptance("nobody", "v1") is False


def test_anonymize_erases_the_person_and_nothing_else_on_real_sql(dsn):
    d = _employees(dsn)
    d.add("101", "Alex Chen", "supervisor", "ops", "+15559000101", account_username="alex", channel="fcm",
          fcm_token="tok", is_primary_contact=True, password_hash=auth.hash_password("correct horse battery"))
    d.add("102", "Bo Diaz", "employee", "ops", "+15559000102")
    assert d.anonymize("101") is True
    gone = d.get("101")
    assert gone["name"] == "Former employee" and gone["phone"] == "erased:101"
    assert gone["fcm_token"] is None and gone["password_hash"] is None and gone["channel"] is None
    assert gone["account_username"] is None and gone["is_primary_contact"] is False and gone["active"] is False
    assert gone["department"] == "ops" and gone["role"] == "supervisor"          # non-identifying facts kept
    assert d.get_privacy_state("101")["anonymized_at"] is not None
    assert d.get("102")["name"] == "Bo Diaz"                                     # nobody else touched
    assert d.get_by_phone("+15559000101") is None
    assert d.anonymize("nobody") is False


def test_the_retention_query_finds_only_long_deactivated_unerased_staff(dsn):
    d = _employees(dsn)
    for n in ("1", "2", "3", "4"):
        d.add(n, f"P{n}", "employee", "ops", f"+1555900000{n}")
    for n in ("1", "2", "4"):
        d.set_active(n, False)
    with floorwatch_db.connection(dsn) as conn:
        conn.execute("UPDATE floorwatch_employees SET deactivated_at = now() - interval '400 days' "
                     "WHERE employee_number IN ('1', '4')")
    d.anonymize("4")                                                            # already erased -> not listed again
    assert d.list_deactivated_for_longer_than(365) == ["1"]                     # 2 is recent, 3 is active, 4 is done


def test_the_migration_starts_the_retention_clock_for_people_deactivated_earlier(dsn):
    d = _employees(dsn)
    d.add("1", "Old", "employee", "ops", "+15559000001")
    with floorwatch_db.connection(dsn) as conn:
        conn.execute("UPDATE floorwatch_employees SET active=false, deactivated_at=NULL WHERE employee_number='1'")
    _employees(dsn)                                                              # a restart re-runs the migration
    assert d.get_privacy_state("1")["deactivated_at"] is not None


def test_reactivation_of_an_erased_person_is_refused_by_the_api_not_the_store(dsn):
    # the store has no opinion; main.py checks anonymized_at first (see test_privacy.py) — this just proves the
    # state it checks is readable on real SQL
    d = _employees(dsn)
    d.add("1", "X", "employee", "ops", "+15559000001")
    d.anonymize("1")
    assert d.get_privacy_state("1")["anonymized_at"] is not None


def test_privacy_request_register_on_real_sql(dsn):
    from privacy_requests import PostgresPrivacyRequests
    r = PostgresPrivacyRequests(dsn)
    a = r.create("101", "erasure", "please", due_days=30)
    b = r.create("102", "access", None, due_days=-1)                              # already late
    assert a["status"] == "open" and a["overdue"] is False and b["overdue"] is True
    assert [x["subject_id"] for x in r.list(status="open")] == ["102", "101"]      # soonest deadline first
    assert [x["subject_id"] for x in r.list(subject_id="101")] == ["101"]
    assert r.resolve(a["request_id"], "admin", "fulfilled", "done") is True
    assert r.resolve(a["request_id"], "admin", "fulfilled", "again") is False       # can't be closed twice
    done = r.get(a["request_id"])
    assert done["status"] == "resolved" and done["resolved_by"] == "admin" and done["overdue"] is False
    with pytest.raises(ValueError):
        r.create("101", "sell-my-data", None, 30)
    with pytest.raises(ValueError):
        r.resolve(b["request_id"], "admin", "maybe", None)
    with floorwatch_db.connection(dsn) as conn:
        conn.execute("UPDATE floorwatch_privacy_requests SET resolved_at = now() - interval '2000 days' "
                     "WHERE request_id=%s", (a["request_id"],))
    assert r.purge_resolved_older_than(1095) == 1
    assert [x["subject_id"] for x in r.list()] == ["102"]                           # the open one is never purged


def test_export_queries_find_one_employees_events_and_tasks_on_real_sql(dsn):
    from event_history import PostgresEventHistoryStore
    from task_store import PostgresTaskStore
    h = PostgresEventHistoryStore(dsn)
    now = datetime.now(timezone.utc).isoformat()
    h.record({"event_id": "a", "timestamp": now, "event_type": "task_assigned", "assigned_to": "101"})
    h.record({"event_id": "b", "timestamp": now, "event_type": "task_assigned", "assigned_to": "102"})
    h.record({"event_id": "c", "timestamp": now, "event_type": "zone_gap", "zone_id": "lobby"})
    assert [e["event_id"] for e in h.query_for_employee("101")] == ["a"]
    assert h.query_for_employee("nobody") == []

    t = PostgresTaskStore(dsn)
    base = {"task_name": "S", "task_type": "c", "zone_id": "lobby", "camera_id": None, "assigned_minutes": 5.0,
            "active_seconds": 0.0, "status": "open", "workflow_status": "notified", "assigned_by": None,
            "nudged": False, "status_nudge_sent": False, "reopened_for_review": False, "resolution_type": None,
            "started_at": now, "updated_at": now}
    t.upsert({**base, "task_id": "t1", "assigned_to": "101"})
    t.upsert({**base, "task_id": "t2", "assigned_to": "101", "status": "resolved"})   # finished tasks are included too
    t.upsert({**base, "task_id": "t3", "assigned_to": "102"})
    assert sorted(r["task_id"] for r in t.list_for_employee("101")) == ["t1", "t2"]


# ── two-step verification store on real SQL ─────────────────────────────────

def test_mfa_enrolment_lifecycle_on_real_sql(dsn):
    from mfa_store import PostgresMfaStore
    m = PostgresMfaStore(dsn)
    assert m.get("dashboard:alice") is None
    assert m.begin_enrollment("dashboard:alice", "enc1:first") is True
    assert m.get("dashboard:alice")["enabled"] is False
    assert m.begin_enrollment("dashboard:alice", "enc1:second") is True              # restarting an unfinished enrolment replaces it
    assert m.get("dashboard:alice")["secret_enc"] == "enc1:second"
    assert m.enable("dashboard:alice", ["h1", "h2"], last_step=100) is True
    assert m.enable("dashboard:alice", ["x"], 1) is False                            # can't enable twice
    assert m.begin_enrollment("dashboard:alice", "enc1:third") is False              # an enabled one is never overwritten
    assert m.get("dashboard:alice")["secret_enc"] == "enc1:second"
    assert m.enabled_subjects() == {"dashboard:alice"}
    assert m.disable("dashboard:alice") is True and m.disable("dashboard:alice") is False


def test_a_time_step_can_be_accepted_only_once_even_when_two_logins_race(dsn):
    import threading
    from mfa_store import PostgresMfaStore
    m = PostgresMfaStore(dsn)
    m.begin_enrollment("employee:1", "enc1:s")
    m.enable("employee:1", [], last_step=10)
    results = []

    def attempt():
        results.append(m.accept_step("employee:1", 11))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1                                                  # exactly one login wins the code
    assert m.accept_step("employee:1", 11) is False and m.accept_step("employee:1", 5) is False
    assert m.accept_step("employee:1", 12) is True


def test_a_recovery_code_can_be_used_only_once_even_when_two_logins_race(dsn):
    import threading
    from mfa_store import PostgresMfaStore
    m = PostgresMfaStore(dsn)
    m.begin_enrollment("employee:1", "enc1:s")
    m.enable("employee:1", ["aaa", "bbb", "ccc"], last_step=0)
    results = []
    threads = [threading.Thread(target=lambda: results.append(m.consume_recovery("employee:1", "bbb"))) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1
    assert m.get("employee:1")["recovery_hashes"] == ["aaa", "ccc"]
    assert m.consume_recovery("employee:1", "zzz") is False
    assert m.replace_recovery("employee:1", ["n1"]) is True and m.get("employee:1")["recovery_hashes"] == ["n1"]
