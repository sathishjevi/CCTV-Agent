#!/usr/bin/env python3
"""
Pre-commit secret guard — catches the single most common real-world leak
(an accidental `git add -A`/`-f` that pulls in a filled-in secrets file or
a hardcoded key) before it reaches version control. `.gitignore` only
protects against the accident it's told about; this catches it even if
someone forces past that.

Two detection strategies, run together:
  1. Known Floorwatch secret env-var names (see config/secrets.env.template
     and config/README.md "Why AUTH_SECRET is handled separately") assigned
     a non-empty, non-placeholder-looking value.
  2. Vendor API-key/connection-string SHAPES (Anthropic, Twilio, Postgres
     DSN-with-password) — a safety net for secrets that leaked into a file
     under a name this script doesn't already know about.

Usage:
  python tools/check_no_secrets.py              # scans the whole working tree
  python tools/check_no_secrets.py --staged      # scans only git-staged content
                                                  # (use this as a pre-commit hook
                                                  # once this repo has a .git dir)

Exit code 0 = clean, 1 = found something that looks like a real secret.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories never worth scanning — noisy, large, or already handled by
# other tooling (e.g. dependency trees can contain third-party code that
# legitimately mentions "token"/"secret" in comments/docs).
EXCLUDED_DIRS = {
    ".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv",
    "build", "dist", ".eggs",
}

# Files that are SUPPOSED to name these variables with placeholder/empty
# values — never flag the templates themselves.
EXCLUDED_FILENAME_SUFFIXES = (".env.template", ".template")

# This guard's own test suite necessarily contains realistic-shaped fake
# secrets to verify detection actually fires (e.g. "does a Twilio-SID-
# shaped string get flagged" requires a Twilio-SID-shaped string). Some of
# those fixtures are deliberately NOT `# nosecret`-marked because the test
# itself asserts the finding is non-empty — marking them would suppress
# the very behavior under test. Excluded by exact relative path (not by
# directory or pattern) so this stays a narrow, auditable exception rather
# than a way to quietly exempt other files.
SELF_TEST_EXCLUSION = "tools/test_check_no_secrets.py"

KNOWN_SECRET_VARS = [
    "FLOORWATCH_AUTH_SECRET",
    "FLOORWATCH_TWILIO_AUTH_TOKEN",
    "FLOORWATCH_TWILIO_ACCOUNT_SID",
    "ANTHROPIC_API_KEY",
    "VOYAGE_API_KEY",
    "FLOORWATCH_POSTGRES_DSN",
]

# A value counts as "real" if it's non-empty and doesn't look like a
# placeholder (angle brackets, all-caps CHANGE_ME-style, or literally "xxx").
PLACEHOLDER_VALUE_RE = re.compile(
    r'^\s*(<.*>|xxx+|change_?me|your[_-].*|todo|fixme|example|placeholder|\.\.\.)?\s*$',
    re.IGNORECASE,
)

VAR_ASSIGNMENT_RE = re.compile(
    r'^\s*(?:export\s+)?(' + "|".join(re.escape(v) for v in KNOWN_SECRET_VARS) + r')\s*[:=]\s*(.+)$'
)

VENDOR_PATTERNS = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")),
    ("Twilio Account SID", re.compile(r"\bAC[0-9a-fA-F]{32}\b")),
    ("Postgres/DSN with embedded password", re.compile(r"postgres(?:ql)?://[^:\s]+:[^@\s]+@")),
    ("AWS access key ID", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Azure Storage connection string", re.compile(r"AccountKey=[A-Za-z0-9+/=]{20,}")),
]


def _is_placeholder(value: str) -> bool:
    value = value.strip().strip('"').strip("'")
    return bool(PLACEHOLDER_VALUE_RE.match(value))


def _is_env_shaped_file(source: str) -> bool:
    """The known-var-name=value check only makes sense against actual env
    files (secrets.env, .env, .env.local, etc.) — application source code
    legitimately contains `VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")`
    (the lookup, not a value) and test fixtures legitimately contain fake
    values like `ANTHROPIC_API_KEY = "sk-fake-key"` for mocking. Scanning
    those as if they were real secrets is all false positives — restrict
    to files that are actually shaped like an env file."""
    name = Path(source).name
    return ".env" in name and not name.endswith(".template")


SUPPRESS_MARKER = "nosecret"  # e.g. trailing `# nosecret` — for test fixtures that need a
                               # realistic-shaped fake secret to test detection itself


def scan_text(text: str, source: str) -> list:
    findings = []
    env_shaped = _is_env_shaped_file(source)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if SUPPRESS_MARKER in line:
            continue
        if env_shaped:
            m = VAR_ASSIGNMENT_RE.match(line)
            if m:
                var_name, value = m.group(1), m.group(2)
                if not _is_placeholder(value):
                    findings.append(f"{source}:{lineno}: looks like a real value for {var_name}")
        for label, pattern in VENDOR_PATTERNS:
            if pattern.search(line):
                findings.append(f"{source}:{lineno}: looks like a {label}")
    return findings


def iter_working_tree_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.name.endswith(EXCLUDED_FILENAME_SUFFIXES):
            continue
        # Skip obvious binaries by extension — text-scanning them is wasted
        # work and occasionally produces garbage matches on binary bytes.
        if path.suffix.lower() in (".onnx", ".pt", ".pth", ".sqlite3", ".jpg", ".jpeg", ".png", ".mp4", ".pyc"):
            continue
        yield path


def scan_working_tree(root: Path) -> list:
    findings = []
    for path in iter_working_tree_files(root):
        rel_path = str(path.relative_to(root)).replace("\\", "/")
        if rel_path == SELF_TEST_EXCLUSION:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_text(text, rel_path))
    return findings


def scan_staged(root: Path) -> list:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("Not a git repository (or git not available) — nothing staged to check.", file=sys.stderr)
        return []

    findings = []
    for rel_path in result.stdout.splitlines():
        if not rel_path.strip():
            continue
        if rel_path.endswith(EXCLUDED_FILENAME_SUFFIXES):
            continue
        if rel_path.replace("\\", "/") == SELF_TEST_EXCLUSION:
            continue
        show = subprocess.run(
            ["git", "show", f":{rel_path}"], cwd=root, capture_output=True, text=True,
        )
        if show.returncode != 0:
            continue
        findings.extend(scan_text(show.stdout, rel_path))
    return findings


def main():
    parser = argparse.ArgumentParser(description="Floorwatch pre-commit secret guard")
    parser.add_argument("--staged", action="store_true",
                        help="Scan only git-staged content (for use as a pre-commit hook)")
    parser.add_argument("--root", type=str, default=str(REPO_ROOT))
    args = parser.parse_args()

    root = Path(args.root)
    findings = scan_staged(root) if args.staged else scan_working_tree(root)

    if findings:
        print("POSSIBLE SECRETS FOUND — review before committing:\n")
        for f in findings:
            print(f"  {f}")
        print(f"\n{len(findings)} finding(s). If these are real secrets, remove them and rotate the "
              f"credential (assume it's compromised once it touches a file, even briefly).")
        sys.exit(1)

    print("No secret-shaped content found.")
    sys.exit(0)


if __name__ == "__main__":
    main()
