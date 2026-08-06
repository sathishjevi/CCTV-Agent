@echo off
REM deploy.bat — Bootstrapper for Floorwatch Ingest Skill (Windows)

setlocal enabledelayedexpansion

set "SKILL_DIR=%~dp0"
if "%SKILL_DIR:~-1%"=="\" set "SKILL_DIR=%SKILL_DIR:~0,-1%"
set "LOG_PREFIX=[FLOORWATCH-INGEST-deploy]"

echo %LOG_PREFIX% Searching for Python...>&2

set "PYTHON_CMD="
for %%V in (3.12 3.11 3.10 3.9) do (
    if not defined PYTHON_CMD (
        py -%%V --version >nul 2>&1
        if !errorlevel! equ 0 set "PYTHON_CMD=py -%%V"
    )
)
if not defined PYTHON_CMD (
    python --version >nul 2>&1
    if !errorlevel! equ 0 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo {"event":"error","message":"No Python >=3.9 interpreter found","retriable":false}
    echo %LOG_PREFIX% FATAL: no suitable Python interpreter found>&2
    exit /b 1
)

echo {"event":"progress","stage":"deps","message":"Installing core dependencies (opencv, numpy, httpx)..."}
!PYTHON_CMD! -m pip install -q -r "%SKILL_DIR%\requirements.txt"
if !errorlevel! neq 0 (
    echo {"event":"error","message":"pip install failed","retriable":true}
    echo %LOG_PREFIX% FATAL: dependency install failed>&2
    exit /b 1
)

set "CAMERAS_FILE=%SKILL_DIR%\cameras.json"
if exist "%CAMERAS_FILE%" (
    findstr /C:"\"source_type\": \"s3\"" "%CAMERAS_FILE%" >nul 2>&1
    if !errorlevel! equ 0 (
        echo {"event":"progress","stage":"deps","message":"Installing boto3 (S3 configured)..."}
        !PYTHON_CMD! -m pip install -q boto3
    )
    findstr /C:"\"source_type\": \"azure" "%CAMERAS_FILE%" >nul 2>&1
    if !errorlevel! equ 0 (
        echo {"event":"progress","stage":"deps","message":"Installing azure-storage-blob (Azure configured)..."}
        !PYTHON_CMD! -m pip install -q azure-storage-blob
    )
    findstr /C:"\"source_type\": \"gcs\"" "%CAMERAS_FILE%" >nul 2>&1
    if !errorlevel! equ 0 (
        echo {"event":"progress","stage":"deps","message":"Installing google-cloud-storage (GCS configured)..."}
        !PYTHON_CMD! -m pip install -q google-cloud-storage
    )
) else (
    echo %LOG_PREFIX% No cameras.json found yet — copy cameras.json.template and re-run once configured.>&2
)

echo {"event":"complete","backend":"cpu","message":"Installed!"}
echo %LOG_PREFIX% Deployment complete.>&2
