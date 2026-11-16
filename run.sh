#!/usr/bin/env bash
# Start Invoice Desk locally:  ./run.sh   then open the address it prints (http://localhost:8000 by default)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating a virtual environment and installing dependencies (first run only)…"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements-dev.txt
fi
if ! command -v tesseract >/dev/null 2>&1; then
  echo "Note: tesseract is not installed, so scanned invoices cannot be read."
  echo "      macOS: brew install tesseract   |   Debian/Ubuntu: sudo apt-get install tesseract-ocr"
fi
if [ ! -f samples/invoices/01_shreeji_packaging_SPI-1047.pdf ]; then
  .venv/bin/python samples/generate_samples.py
fi

# Is anything already answering on this port, on either IPv4 or IPv6? Another program bound to
# the same port would not stop this one starting, but "localhost" could reach it instead of us.
port_busy() {
  .venv/bin/python - "$1" <<'PY'
import socket, sys
for host in ("127.0.0.1", "::1"):
    try:
        with socket.create_connection((host, int(sys.argv[1])), timeout=0.3):
            sys.exit(0)
    except OSError:
        pass
sys.exit(1)
PY
}

if [ -n "${PORT:-}" ]; then
  if port_busy "$PORT"; then
    echo "Port ${PORT} is already in use by another program. Choose another: PORT=8001 ./run.sh"
    exit 1
  fi
else
  PORT=8000
  while port_busy "$PORT"; do
    echo "Port ${PORT} is already in use by another program; trying $((PORT + 1))."
    PORT=$((PORT + 1))
    if [ "$PORT" -gt 8020 ]; then echo "No free port between 8000 and 8020."; exit 1; fi
  done
fi

echo
echo "  Invoice Desk is running. Open:  http://localhost:${PORT}"
echo "  Stop it with Ctrl+C."
echo
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
