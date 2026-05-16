@echo off
REM SqueakShot Launcher v9.2 (Windows)
REM
REM Prefers an isolated conda/mamba environment ("squeakshot") built from
REM environment.yml. That environment also provides ffmpeg/ffprobe, so no
REM separate FFmpeg download is needed.
REM
REM If neither conda nor mamba is on PATH, this falls back to the old
REM behaviour: pip-install flask + numpy into the current Python.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set ENV_NAME=squeakshot

REM First-run setup
if not exist "controller\camera_config.json" (
    echo ============================================================
    echo  First run: no config found
    echo ============================================================
    set /p ans="Run setup now? [Y/n]: "
    if /i "!ans!"=="n" (
        echo Cancelled. Run setup.bat when ready.
        pause
        exit /b 1
    )
    call setup.bat
)

REM Find a conda-family tool, preferring mamba (faster solver).
set "CONDA_TOOL="
where mamba >nul 2>nul && set "CONDA_TOOL=mamba"
if not defined CONDA_TOOL (
    where conda >nul 2>nul && set "CONDA_TOOL=conda"
)

if defined CONDA_TOOL (
    REM `env list` prints "name  *  C:\...\envs\name"; match a path that
    REM ends in \envs\squeakshot so a similarly named env won't false-match.
    call %CONDA_TOOL% env list | findstr /R /C:"\\envs\\%ENV_NAME%$" >nul
    if errorlevel 1 (
        echo Creating conda environment '%ENV_NAME%' from environment.yml...
        echo (first run only -- this can take a minute^)
        call %CONDA_TOOL% env create -f environment.yml
    )
    cd controller
    call %CONDA_TOOL% run -n %ENV_NAME% --no-capture-output python camera_controller.py
    pause
    exit /b 0
)

REM Fallback: no conda/mamba -- use the current Python directly.
echo conda/mamba not found -- using the current Python (no isolation).
echo For an isolated install see INSTALL.md.
python -c "import flask, numpy" 2>nul
if errorlevel 1 (
    echo Installing controller dependencies...
    python -m pip install -q -r controller\requirements.txt
)
cd controller
python camera_controller.py
pause
