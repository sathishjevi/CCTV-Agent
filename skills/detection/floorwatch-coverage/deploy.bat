@echo off
REM deploy.bat — Bootstrapper for Floorwatch Coverage Skill (Windows)
REM
REM Runs no ML model of its own (only consumes another skill's detection
REM output), but is NOT dependency-free: zone membership is tested with
REM supervision.PolygonZone, so requirements.txt must be installed before
REM the import check below can pass.
REM
REM Python floor is 3.10, not 3.9 — that's supervision's own Requires-Python.
REM
REM Exit codes:
REM   0 = success
REM   1 = fatal error (no suitable Python, or dependency install failed)

setlocal enabledelayedexpansion

set "SKILL_DIR=%~dp0"
if "%SKILL_DIR:~-1%"=="\" set "SKILL_DIR=%SKILL_DIR:~0,-1%"
set "LOG_PREFIX=[FLOORWATCH-COVERAGE-deploy]"

echo %LOG_PREFIX% Searching for Python...>&2

set "PYTHON_CMD="
for %%V in (3.12 3.11 3.10) do (
    if not defined PYTHON_CMD (
        py -%%V --version >nul 2>&1
        if !errorlevel! equ 0 (
            set "PYTHON_CMD=py -%%V"
        )
    )
)

if not defined PYTHON_CMD (
    python --version >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON_CMD=python"
    )
)

if not defined PYTHON_CMD (
    REM Worded as "3.10+" deliberately: a bare `>` here would be parsed as a
    REM redirect (the original wrote a stray "=3.9" file and truncated the
    REM message), and `^>` prints the caret literally under delayed expansion.
    echo {"event":"error","message":"No Python 3.10+ interpreter found","retriable":false}
    echo %LOG_PREFIX% FATAL: no suitable Python interpreter found>&2
    exit /b 1
)

echo %LOG_PREFIX% Using !PYTHON_CMD!>&2

if not exist "%SKILL_DIR%\requirements.txt" (
    echo {"event":"error","message":"requirements.txt not found next to deploy.bat","retriable":false}
    echo %LOG_PREFIX% FATAL: requirements.txt not found>&2
    exit /b 1
)

echo {"event":"progress","stage":"deps","message":"Installing Python dependencies..."}
!PYTHON_CMD! -m pip install -r "%SKILL_DIR%\requirements.txt" -q >&2
if !errorlevel! neq 0 (
    echo {"event":"error","message":"Dependency install failed","retriable":true}
    echo %LOG_PREFIX% FATAL: pip install -r requirements.txt failed>&2
    exit /b 1
)

REM Import check runs AFTER the install above — zone_utils imports numpy and
REM supervision at module level, so on a clean machine this check is exactly
REM what catches a dependency install that silently didn't take. stderr is
REM surfaced (not sent to nul) so the real ImportError is diagnosable.
echo {"event":"progress","stage":"verify","message":"Verifying skill modules import..."}

!PYTHON_CMD! -c "import sys; sys.path.insert(0, r'%SKILL_DIR%\scripts'); import zone_utils" >&2
if !errorlevel! neq 0 (
    echo {"event":"error","message":"Skill module import check failed","retriable":true}
    echo %LOG_PREFIX% FATAL: zone_utils.py failed to import>&2
    exit /b 1
)

if not exist "%SKILL_DIR%\zones" mkdir "%SKILL_DIR%\zones"

echo {"event":"complete","backend":"cpu","message":"Installed!"}
echo %LOG_PREFIX% Deployment complete.>&2
