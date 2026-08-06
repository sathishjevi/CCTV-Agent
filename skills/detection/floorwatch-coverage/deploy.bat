@echo off
REM deploy.bat — Bootstrapper for Floorwatch Coverage Skill (Windows)
REM
REM No ML/GPU dependencies (pure-Python zone geometry) — just locates
REM Python >=3.9 and verifies the skill's modules import cleanly.
REM
REM Exit codes:
REM   0 = success
REM   1 = fatal error (no Python found)

setlocal enabledelayedexpansion

set "SKILL_DIR=%~dp0"
if "%SKILL_DIR:~-1%"=="\" set "SKILL_DIR=%SKILL_DIR:~0,-1%"
set "LOG_PREFIX=[FLOORWATCH-COVERAGE-deploy]"

echo %LOG_PREFIX% Searching for Python...>&2

set "PYTHON_CMD="
for %%V in (3.12 3.11 3.10 3.9) do (
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
    echo {"event":"error","message":"No Python >=3.9 interpreter found","retriable":false}
    echo %LOG_PREFIX% FATAL: no suitable Python interpreter found>&2
    exit /b 1
)

echo %LOG_PREFIX% Using !PYTHON_CMD!>&2
echo {"event":"progress","stage":"verify","message":"Verifying skill modules import..."}

!PYTHON_CMD! -c "import sys; sys.path.insert(0, r'%SKILL_DIR%\scripts'); import zone_utils" >nul 2>&1
if !errorlevel! neq 0 (
    echo {"event":"error","message":"Skill module import check failed","retriable":true}
    echo %LOG_PREFIX% FATAL: zone_utils.py failed to import>&2
    exit /b 1
)

if not exist "%SKILL_DIR%\zones" mkdir "%SKILL_DIR%\zones"

echo {"event":"complete","backend":"cpu","message":"Installed!"}
echo %LOG_PREFIX% Deployment complete.>&2
