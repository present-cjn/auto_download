@echo off
setlocal
cd /d "%~dp0"

set RCLONE_EXE=vendor\rclone\rclone.exe
if not exist "%RCLONE_EXE%" (
  set RCLONE_EXE=rclone
)

echo This will open rclone Google Drive configuration.
echo Use remote name: gdrive
echo Use Google Drive readonly scope when asked.
echo.
"%RCLONE_EXE%" config

endlocal
