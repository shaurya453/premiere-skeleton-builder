#!/bin/bash
set -e
export PATH="$HOME/.local/share/skeleton-builder-tools/runtime/bin:$PATH"
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Please follow the Mac setup instructions in README.md first."
  read -r -p "Press Return to close."
  exit 1
fi
exec .venv/bin/python app.py
