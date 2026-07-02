#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

url="${1:-https://printerval.com/}"
profile="${PRINTERVAL_PLAYWRIGHT_USER_DATA_DIR:-data/browser-profiles/printerval-main}"
timeout_seconds="${PRINTERVAL_VERIFY_TIMEOUT_SECONDS:-600}"

command=(
  .venv/bin/python -m app.tools.printerval_session
  --url "$url" \
  --profile "$profile" \
  --timeout-seconds "$timeout_seconds"
)

if [[ "${PRINTERVAL_USE_XVFB:-1}" != "0" ]]; then
  if ! command -v xvfb-run >/dev/null 2>&1; then
    echo "xvfb-run not found. Install it with: sudo apt-get install -y xvfb" >&2
    echo "For a real visible browser window, run: PRINTERVAL_USE_XVFB=0 $0 '<url>'" >&2
    exit 127
  fi
  exec xvfb-run -a --server-args="-screen 0 1280x1024x24" "${command[@]}"
fi

exec "${command[@]}"
