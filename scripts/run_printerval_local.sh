#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

command=(
  env
  "PRINTERVAL_PLAYWRIGHT_ENABLED=${PRINTERVAL_PLAYWRIGHT_ENABLED:-1}"
  "PRINTERVAL_PLAYWRIGHT_USER_DATA_DIR=${PRINTERVAL_PLAYWRIGHT_USER_DATA_DIR:-data/browser-profiles/printerval-main}"
  "PRINTERVAL_PLAYWRIGHT_HEADLESS=${PRINTERVAL_PLAYWRIGHT_HEADLESS:-0}"
  "PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS=${PRINTERVAL_PLAYWRIGHT_TIMEOUT_SECONDS:-120}"
  "PRINTERVAL_CURL_TIMEOUT_SECONDS=${PRINTERVAL_CURL_TIMEOUT_SECONDS:-30}"
  "DRIVE_DOWNLOAD_BACKEND=${DRIVE_DOWNLOAD_BACKEND:-rclone}"
  "RCLONE_DRIVE_REMOTES=${RCLONE_DRIVE_REMOTES:-gdrive}"
  "RCLONE_TRANSFERS=${RCLONE_TRANSFERS:-1}"
  "RCLONE_CHECKERS=${RCLONE_CHECKERS:-1}"
  "RCLONE_DRIVE_PACER_MIN_SLEEP=${RCLONE_DRIVE_PACER_MIN_SLEEP:-500ms}"
  "RCLONE_DRIVE_PACER_BURST=${RCLONE_DRIVE_PACER_BURST:-5}"
  "ADMIN_USERNAME=${ADMIN_USERNAME:-admin}"
  "ADMIN_PASSWORD=${ADMIN_PASSWORD:-change-me}"
  .venv/bin/python -m uvicorn app.main:app
  --host "${HOST:-127.0.0.1}"
  --port "${PORT:-8000}"
)

if [[ "${PRINTERVAL_USE_XVFB:-1}" != "0" ]]; then
  if ! command -v xvfb-run >/dev/null 2>&1; then
    echo "xvfb-run not found. Install it with: sudo apt-get install -y xvfb" >&2
    echo "For a real visible browser window, run: PRINTERVAL_USE_XVFB=0 $0" >&2
    exit 127
  fi
  exec xvfb-run -a --server-args="-screen 0 1280x1024x24" "${command[@]}"
fi

exec "${command[@]}"
