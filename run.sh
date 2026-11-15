#!/usr/bin/env bash
# Start Invoice Desk locally:  ./run.sh   then open http://localhost:8000
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

PORT="${PORT:-8000}"
echo "Invoice Desk is starting on http://localhost:${PORT}"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
