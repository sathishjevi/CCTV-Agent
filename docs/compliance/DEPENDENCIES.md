# Dependencies

## How they are managed

* Direct dependencies live in `requirements.in` per service; `requirements.txt` is the hash-pinned lock:
  ```
  uv pip compile requirements.in --python-version 3.13 --python-platform x86_64-manylinux_2_28 --generate-hashes -o requirements.txt
  ```
* Dockerfiles install with `pip install --require-hashes`, so a tampered or swapped package fails the build.
* Dev/test locks: `tests/requirements-dev.in/.txt`. The intelligence service has a separate full CI lock
  (`tests/requirements-ci.txt`) because google-genai and websockets pins are unsatisfiable together in the dev lock.
* CI (`security.yml`): pip-audit against each lock, bandit, CodeQL, gitleaks, optional ZAP baseline.
* Dependabot opens weekly update PRs.
* SBOMs (CycloneDX) are in `sbom/`; regenerate whenever a lock changes.

## Checks performed at audit

* pip-audit on all hash-locked files: no known vulnerabilities.
* Every direct dependency exists on PyPI under the expected name (no dependency-confusion candidates), and none is
  abandoned (recent releases).
* Flutter: `flutter pub outdated` reviewed; no discontinued packages in use.

## Updating

1. Edit `requirements.in`, recompile with the command above, run the tests.
2. Regenerate the SBOM, commit lock + SBOM together.
