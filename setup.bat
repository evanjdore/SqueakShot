@echo off
REM SqueakShot Setup v9.0 - creates camera_config.json interactively
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "CONFIG_FILE=%SCRIPT_DIR%controller\camera_config.json"

echo ============================================================
echo  SQUEAKSHOT SETUP v9.0
echo ============================================================
echo.

if exist "%CONFIG_FILE%" (
    set /p OVERWRITE="Config exists. Overwrite? (y/N): "
    if /i not "!OVERWRITE!"=="y" (
        echo Cancelled.
        exit /b 0
    )
)

set /p NUM_CAMS="How many cameras? [3]: "
if "%NUM_CAMS%"=="" set NUM_CAMS=3

set /p DEFAULT_USER="Default SSH username on the Pis [maiya]: "
if "%DEFAULT_USER%"=="" set DEFAULT_USER=maiya

REM Build camera JSON as a temp Python script — easier than batch string-fiddling
set "PYTMP=%TEMP%\squeakshot_setup_%RANDOM%.py"
> "%PYTMP%" echo import json, sys
>> "%PYTMP%" echo cameras = []

for /l %%i in (0,1,99) do (
    if %%i geq !NUM_CAMS! goto :after_cams
    echo.
    echo --- Camera %%i ---
    set "ROLE=client"
    if %%i==0 (
        set "ROLE=server"
        echo   Role: server ^(cam0 always serves^)
    ) else (
        echo   Role: client
    )
    set /p CAM_NAME="  Name [cam%%i]: "
    if "!CAM_NAME!"=="" set "CAM_NAME=cam%%i"
    set /p CAM_IP="  IP address: "
    set /p CAM_USER="  SSH user [%DEFAULT_USER%]: "
    if "!CAM_USER!"=="" set "CAM_USER=%DEFAULT_USER%"
    >> "%PYTMP%" echo cameras.append({"name": "!CAM_NAME!", "ip": "!CAM_IP!", "user": "!CAM_USER!", "role": "!ROLE!"})
)

:after_cams
echo.
set /p VIDEO_DIR="Remote video dir on Pis [/home/%DEFAULT_USER%/camera_videos]: "
if "%VIDEO_DIR%"=="" set "VIDEO_DIR=/home/%DEFAULT_USER%/camera_videos"
set /p LOCAL_DIR="Local video directory [%USERPROFILE%\SqueakShot_Videos]: "
if "%LOCAL_DIR%"=="" set "LOCAL_DIR=%USERPROFILE%\SqueakShot_Videos"

echo.
echo Camera settings (press Enter for defaults):
set /p OUT_W="  Output width [1536]: "
if "%OUT_W%"=="" set OUT_W=1536
set /p OUT_H="  Output height [864]: "
if "%OUT_H%"=="" set OUT_H=864
set /p FPS="  Framerate [56]: "
if "%FPS%"=="" set FPS=56
set /p BITRATE="  Bitrate Mbps [25]: "
if "%BITRATE%"=="" set BITRATE=25

>> "%PYTMP%" echo cfg = {
>> "%PYTMP%" echo     "cameras": cameras,
>> "%PYTMP%" echo     "video_dir": "%VIDEO_DIR%",
>> "%PYTMP%" echo     "local_video_dir": r"%LOCAL_DIR%",
>> "%PYTMP%" echo     "camera_settings": {
>> "%PYTMP%" echo         "output_width": %OUT_W%,
>> "%PYTMP%" echo         "output_height": %OUT_H%,
>> "%PYTMP%" echo         "sensor_width": 2304,
>> "%PYTMP%" echo         "sensor_height": 1296,
>> "%PYTMP%" echo         "framerate": %FPS%,
>> "%PYTMP%" echo         "bitrate_mbps": %BITRATE%,
>> "%PYTMP%" echo     }
>> "%PYTMP%" echo }
>> "%PYTMP%" echo with open(r"%CONFIG_FILE%", "w") as f:
>> "%PYTMP%" echo     json.dump(cfg, f, indent=2)
>> "%PYTMP%" echo print("Config written.")

python "%PYTMP%"
del "%PYTMP%"

echo.
echo ============================================================
echo  CONFIG WRITTEN: %CONFIG_FILE%
echo ============================================================
echo.
echo Next steps:
echo   1. Set up SSH keys to each Pi: ssh-copy-id user@ip
echo   2. Deploy services to Pis (run from WSL or Linux):
echo        cd pi-deploy ^&^& ./install.sh
echo   3. Launch controller: SqueakShot.bat
echo.
pause
