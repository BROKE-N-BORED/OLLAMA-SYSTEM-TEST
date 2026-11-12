#!/bin/sh
# OLLAMA SYSTEM TEST - one-time install.
# Creates a private folder (.venv) with PySide6-Essentials, the only
# package the app needs (QtCore + QtWidgets; no WebEngine/Addons).
set -e
cd "$(dirname "$0")"

command -v python3 >/dev/null 2>&1 || { echo "python3 is missing. Install it first."; exit 1; }

if [ ! -x .venv/bin/python ]; then
  echo "Creating the app folder (.venv)..."
  if ! python3 -m venv .venv; then
    rm -rf .venv
    echo "python3-venv is missing. Run: sudo apt install python3-venv"
    echo "Then run sh install.sh again."
    exit 1
  fi
fi

echo "Installing PySide6-Essentials (one time, may take a minute)..."
.venv/bin/python -m pip install PySide6-Essentials
echo ""
echo "Done. Start it with: sh run.sh"
