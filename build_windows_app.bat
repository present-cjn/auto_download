@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 goto :error
)

echo Installing build dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 goto :error

echo Running tests...
".venv\Scripts\python.exe" -m pytest
if errorlevel 1 goto :error

if not exist "vendor\rclone\rclone.exe" (
  echo.
  echo Missing vendor\rclone\rclone.exe
  echo Download the Windows rclone package and place rclone.exe there before building.
  goto :error
)

echo Building Windows app...
".venv\Scripts\pyinstaller.exe" --clean auto_download_local.spec
if errorlevel 1 goto :error

echo.
echo Build complete:
echo dist\AutoDownload\AutoDownload.exe
echo Distribute the whole dist\AutoDownload folder.
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
