#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f requirements.txt ]; then
  echo "error: requirements.txt not found in $ROOT" >&2
  exit 2
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
else
  echo ".venv already exists; reusing it"
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "Virtual environment ready."
echo "Activate it with: source .venv/bin/activate"
