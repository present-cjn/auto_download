@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "scripts\start_local_app.py"
) else (
  python "scripts\start_local_app.py"
)

endlocal
