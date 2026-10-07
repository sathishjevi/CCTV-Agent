"""Test-suite-wide setup.

scrypt is deliberately slow (~0.3-0.7 s per hash in production). With hundreds
of tests creating and logging in accounts that would add many minutes, so the
suite runs with a cheap scrypt cost. This changes SPEED ONLY: the hash format,
the verify path and the upgrade-on-login logic are exactly the production ones,
and test_password_hashing.py checks the production parameters directly
(via PRODUCTION_SCRYPT_PARAMS) and exercises one real-strength hash."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "lib"))

import floorwatch_auth  # noqa: E402

PRODUCTION_SCRYPT_PARAMS = (floorwatch_auth.SCRYPT_N, floorwatch_auth.SCRYPT_R, floorwatch_auth.SCRYPT_P)
floorwatch_auth.PRODUCTION_SCRYPT_PARAMS = PRODUCTION_SCRYPT_PARAMS   # for the tests that assert on it


def pytest_configure(config):
    floorwatch_auth.SCRYPT_N = 2 ** 10
    floorwatch_auth.SCRYPT_P = 1
