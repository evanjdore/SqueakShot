@echo off
REM SqueakShot Launcher v9.0 (Windows)
cd /d "%~dp0"

REM First-run setup
if not exist "controller\camera_config.json" (
    echo ============================================================
    echo  First run: no config found
    echo ============================================================
    set /p ans="Run setup now? [Y/n]: "
    if /i "%ans%"=="n" (
        echo Cancelled. Run setup.bat when ready.
        pause
        exit /b 1
    )
    call setup.bat
)

REM Check Python deps
python -c "import flask, numpy" 2>nul
if errorlevel 1 (
    echo Installing controller dependencies...
    python -m pip install -q -r controller\requirements.txt
)

REM Launch
cd controller
python camera_controller.py
pause
