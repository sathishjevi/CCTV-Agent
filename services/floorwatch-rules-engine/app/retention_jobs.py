"""Automated retention: deleting personal data once it's no longer needed.

`retention.py` has always been a command-line script "meant to run on a
schedule" — and nothing ever scheduled it on Railway, so in production no
history was ever deleted. The privacy checklist asks for retention that is
*enforced through automated deletion*, so the service now does it itself: a
background task (see main.py's retention_loop) calls `run_retention` daily.
The command-line script is still there for a manual or dry-run pass.

What is removed, and when (all periods come from config.py — the same values
the privacy notice shows staff):
  - shift summaries (shift_digest)           RETENTION_DAYS
  - dashboard accounts deactivated           RETENTION_DAYS
  - zone/task event history                  EVENT_HISTORY_RETENTION_DAYS
  - finished task records                    TASK_RETENTION_DAYS   (open tasks are never touched)
  - staff deactivated long enough            EMPLOYEE_RETENTION_DAYS -> ERASED (anonymised), not deleted:
                                              the employee number stays so audit records remain coherent
  - resolved privacy requests                PRIVACY_REQUEST_RETENTION_DAYS

It never touches anything that is active/open, and every step is independent:
one failing doesn't stop the others. `dry_run` reports what would go and
changes nothing (only the steps that can preview do; the rest say so)."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from floorwatch_auth import purge_stale_deactivated_accounts
from floorwatch_logging import get_logger
from floorwatch_retention import prune_jsonl_file

log = get_logger("rules-engine.retention")


def _step(name: str, fn, summary: dict):
    try:
        summary[name] = fn()
    except Exception as e:      # one failing step must not stop the rest
        summary[name] = {"error": f"{type(e).__name__}: {e}"}
        log(f"retention step '{name}' failed: {type(e).__name__}: {e}", level="error")


def run_retention(*, config, digest_path: Path, users, event_history, task_store, employee_directory,
                  privacy_requests, archive_dir: Optional[Path] = None, dry_run: bool = False,
                  on_employee_erased=None) -> dict:
    """`on_employee_erased(employee_number)` is called after each erasure (main.py uses it to
    end that person's sessions). Returns a JSON-friendly summary."""
    summary: dict = {"ran_at": datetime.now(timezone.utc).isoformat(), "dry_run": dry_run}
    archive = None if dry_run else archive_dir

    _step("shift_digest", lambda: prune_jsonl_file(
        digest_path, config.RETENTION_DAYS, archive_dir=archive, dry_run=dry_run,
        archive_prefix="shift_digest_archived"), summary)

    def accounts():
        archive_path = (archive / f"accounts_archived_{datetime.now(timezone.utc):%Y%m%d}.jsonl") if archive else None
        return purge_stale_deactivated_accounts(users, config.RETENTION_DAYS, archive_path=archive_path, dry_run=dry_run)
    _step("accounts", accounts, summary)

    _step("event_history", lambda: event_history.purge_older_than(
        config.EVENT_HISTORY_RETENTION_DAYS, archive_dir=archive, archive_prefix="event_history_archived",
        dry_run=dry_run), summary)

    def tasks():
        if dry_run:
            return {"skipped": "dry run — finished-task purge cannot preview"}
        return {"purged": task_store.purge_older_than(config.TASK_RETENTION_DAYS)}
    _step("tasks", tasks, summary)

    def employees():
        due = employee_directory.list_deactivated_for_longer_than(config.EMPLOYEE_RETENTION_DAYS)
        if dry_run:
            return {"would_erase": len(due)}
        erased = 0
        for number in due:
            if employee_directory.anonymize(number):
                erased += 1
                log("retention: erased personal data of a long-deactivated employee", level="info",
                    security_event="data_erasure", employee_number=number, reason="retention")
                if on_employee_erased:
                    on_employee_erased(number)
        return {"erased": erased}
    _step("employees", employees, summary)

    def requests():
        overdue = sum(1 for r in privacy_requests.list(status="open") if r["overdue"])
        if dry_run:
            return {"overdue": overdue}
        return {"purged": privacy_requests.purge_resolved_older_than(config.PRIVACY_REQUEST_RETENTION_DAYS),
                "overdue": overdue}
    _step("privacy_requests", requests, summary)

    return summary
