"""Shared secrets-handling helpers for both Floorwatch services — small,
additive hardening on top of the plain os.environ.get() config pattern:

  1. load_deployment_config() — loads config/deployment.env then
     config/secrets.env (if present) via python-dotenv, WITHOUT
     overriding any real environment variable already set. Local/dev
     convenience; a real deployment can ignore these files entirely and
     just set real env vars via its process manager/secrets store.
  2. check_file_permissions() — warns (does not block startup) if a
     secrets file looks broadly readable. Windows-aware (icacls), POSIX
     fallback (mode bits).
  3. install_stderr_redaction() — wraps sys.stderr so any known secret
     VALUE written to it (by any log()/print() call anywhere, or an
     uncaught exception traceback) is scrubbed before reaching the
     terminal/log file. Covers the whole codebase's existing log() calls
     without editing every call site.

None of this replaces a real secrets manager — see config/README.md
"What this doesn't solve."
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional


def load_deployment_config(repo_root: Path):
    """Loads config/deployment.env then config/secrets.env, in that
    order, if they exist. override=False means a real environment
    variable already set (by the OS/process manager) always wins —
    these files never clobber a genuine production value."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return  # python-dotenv not installed — fine, these files are optional convenience

    config_dir = repo_root / "config"
    load_dotenv(config_dir / "deployment.env", override=False)
    load_dotenv(config_dir / "secrets.env", override=False)


def check_file_permissions(path: Path, log=lambda msg: print(msg, file=sys.stderr, flush=True)) -> Optional[str]:
    """Warns (returns a message, logs it) if `path` looks readable beyond
    the current user/service account. Best-effort — never raises, never
    blocks startup; a false negative here is not a regression (the file
    was equally unprotected before this check existed), so failure modes
    all fail open rather than breaking a legitimate deployment."""
    if not path.exists():
        return None

    if sys.platform == "win32":
        return _check_windows_acl(path, log)
    return _check_posix_mode(path, log)


def _check_windows_acl(path: Path, log) -> Optional[str]:
    try:
        result = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, timeout=5,
        )
        output = result.stdout
    except Exception as e:
        log(f"[secrets-guard] Could not check ACL on {path}: {e}")
        return None

    broad_grantees = ("Everyone", "BUILTIN\\Users", "Authenticated Users", "NT AUTHORITY\\Authenticated Users")
    for grantee in broad_grantees:
        if grantee in output:
            msg = (f"[secrets-guard] WARNING: {path} appears readable by '{grantee}' — "
                   f"this file may contain secrets. Restrict with: "
                   f'icacls "{path}" /inheritance:r /grant:r "%USERNAME%:(R,W)"')
            log(msg)
            return msg
    return None


def _check_posix_mode(path: Path, log) -> Optional[str]:
    import stat
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        msg = (f"[secrets-guard] WARNING: {path} is readable/writable by group or others "
               f"(mode {oct(mode)[-3:]}) — this file may contain secrets. Restrict with: "
               f"chmod 600 {path}")
        log(msg)
        return msg
    return None


class _RedactingStream:
    """Wraps a text stream (sys.stderr) and scrubs known secret values
    from anything written to it before passing through to the real
    stream. Values are matched exactly, longest-first, so a short value
    fully contained in a longer one doesn't partially redact the longer
    one incorrectly."""

    def __init__(self, wrapped, secret_values: list):
        self._wrapped = wrapped
        # Ignore blank/placeholder values — nothing to redact, and an
        # empty string would match everything.
        self._secrets = sorted({v for v in secret_values if v and len(v) >= 6}, key=len, reverse=True)

    def write(self, text):
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***REDACTED***")
        return self._wrapped.write(text)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def install_stderr_redaction(secret_values: list):
    """Idempotent — safe to call more than once (e.g. if both a service's
    config module and its main module both import this)."""
    if isinstance(sys.stderr, _RedactingStream):
        sys.stderr._secrets = sorted(
            set(sys.stderr._secrets) | {v for v in secret_values if v and len(v) >= 6},
            key=len, reverse=True,
        )
        return
    sys.stderr = _RedactingStream(sys.stderr, secret_values)
