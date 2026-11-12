#!/bin/sh
# OLLAMA SYSTEM TEST - start the app.
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python ollama_system_test.py
fi
exec python3 ollama_system_test.py
