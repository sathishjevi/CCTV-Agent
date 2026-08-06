@echo off
REM deploy.bat — Bootstrapper for Floorwatch Pose Skill (Windows)
REM
REM Installs mediapipe/pillow/numpy/redis, then attempts to download the
REM MediaPipe PoseLandmarker model. Falls back to frame-differencing mode
REM (still a full, working install) if the download fails — see SKILL.md.

setlocal enabledelayedexpansion

set "SKILL_DIR=%~dp0"
if "%SKILL_DIR:~-1%"=="\" set "SKILL_DIR=%SKILL_DIR:~0,-1%"
set "MODEL_DIR=%SKILL_DIR%\models"
set "MODEL_PATH=%MODEL_DIR%\pose_landmarker_lite.task"
set "MODEL_URL=https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
set "LOG_PREFIX=[FLOORWATCH-POSE-deploy]"

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

echo {"event":"progress","stage":"deps","message":"Installing mediapipe/pillow/numpy/redis..."}
!PYTHON_CMD! -m pip install -q -r "%SKILL_DIR%\requirements.txt"
if !errorlevel! neq 0 (
    echo {"event":"error","message":"pip install failed","retriable":true}
    echo %LOG_PREFIX% FATAL: dependency install failed>&2
    exit /b 1
)

if not exist "%MODEL_DIR%" mkdir "%MODEL_DIR%"
if exist "%MODEL_PATH%" (
    echo %LOG_PREFIX% Pose model already present at %MODEL_PATH%>&2
) else (
    echo {"event":"progress","stage":"model","message":"Downloading MediaPipe pose model..."}
    curl -fsSL -o "%MODEL_PATH%" "%MODEL_URL%" >nul 2>&1
    if !errorlevel! neq 0 (
        del /q "%MODEL_PATH%" >nul 2>&1
        echo %LOG_PREFIX% WARNING: could not download pose model ^(network/firewall?^).>&2
        echo %LOG_PREFIX% WARNING: floorwatch-pose will run in FALLBACK mode until a model file>&2
        echo %LOG_PREFIX% WARNING: is placed at %MODEL_PATH% — see SKILL.md.>&2
        echo {"event":"progress","stage":"model","message":"Model download failed — will run in fallback mode"}
    ) else (
        echo %LOG_PREFIX% Downloaded pose model to %MODEL_PATH%>&2
    )
)

echo {"event":"complete","backend":"cpu","message":"Installed!"}
echo %LOG_PREFIX% Deployment complete.>&2
