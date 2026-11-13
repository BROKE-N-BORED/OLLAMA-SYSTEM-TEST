#!/bin/sh
# OLLAMA SYSTEM TEST - one-time install.
# Creates a private folder (.venv) with PySide6-Essentials, the only
# package the app needs (QtCore + QtWidgets; no WebEngine/Addons).
# The version is pinned in requirements.txt to the one the v6 suite
# was verified against. The install is verified before this script
# reports success: if the check fails, it says so loudly.
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
.venv/bin/python -m pip install -r requirements.txt

echo "Checking the install..."
if ! .venv/bin/python -c "import PySide6" 2>/dev/null; then
  echo "Install failed: PySide6 cannot be imported."
  echo "Try running sh install.sh again."
  exit 1
fi
if ! .venv/bin/python -m py_compile ollama_system_test.py; then
  echo "Install failed: ollama_system_test.py does not compile."
  exit 1
fi
echo "PySide6 OK."
echo ""
# Desktop launcher + icon: OST in the app menu. This step must never
# fail the install — if it has a problem it says so and the app still
# runs from run.sh.
if [ -f OST.desktop ] && [ -f ost-icon.png ]; then
  APPS_DIR="$HOME/.local/share/applications"
  ICONS_DIR="$HOME/.local/share/icons"
  # The launcher resolves its folder from the .desktop file's own
  # path (%k). The copy in the app menu lives in
  # ~/.local/share/applications, not next to the app, so the installed
  # copy gets this folder's .desktop path baked in — quoted, so a
  # folder with spaces still works as one argument.
  if mkdir -p "$APPS_DIR" "$ICONS_DIR" \
      && sed "s|%k|\"$(pwd)/OST.desktop\"|" OST.desktop \
          > "$APPS_DIR/OST.desktop" \
      && cp ost-icon.png "$ICONS_DIR/ost-icon.png" \
      && chmod +x "$APPS_DIR/OST.desktop"; then
    echo "Launcher installed: OST is now in your app menu."
  else
    echo "Launcher could not be installed — start with: sh run.sh"
  fi
fi
echo "Done. Start it with: sh run.sh"
