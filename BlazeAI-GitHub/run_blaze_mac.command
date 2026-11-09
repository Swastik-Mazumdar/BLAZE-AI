#!/bin/bash
# Double-clickable macOS runner for Blaze AI.
#
# The runtime lives outside the project so an iCloud-synced Desktop cannot
# corrupt Qt's framework metadata. Voice extras are optional:
#   "$BLAZE_VENV/bin/pip" install -r requirements-voice.txt
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PYTHON="${BLAZE_PYTHON:-python3}"

# Keep the venv in a local, non-synced folder. Desktop/Documents may be managed
# by iCloud Drive, which re-adds FinderInfo xattrs and breaks Qt + codesign.
DEFAULT_VENV="$HOME/Library/Application Support/BlazeAI/venv"
VENV="${BLAZE_VENV:-$DEFAULT_VENV}"
VENV_PY="$VENV/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "Blaze AI · first run"
  echo "Creating local virtual environment…"
  mkdir -p "$(dirname "$VENV")"
  "$PYTHON" -m venv "$VENV"
fi

# Reuse an existing .venv symlink so manual commands keep working.
if [ ! -e "$ROOT/.venv" ]; then
  ln -s "$VENV" "$ROOT/.venv"
fi

if ! "$VENV_PY" -c "import PyQt6, psutil, pynput, pyttsx3" >/dev/null 2>&1; then
  if [ "${BLAZE_SKIP_INSTALL:-0}" = "1" ]; then
    echo "Core dependencies are missing and BLAZE_SKIP_INSTALL=1 is set." >&2
    exit 1
  fi
  echo "Installing core dependencies (one time)…"
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -r "$ROOT/requirements.txt"
fi

if ! "$VENV_PY" -c "import torch, transformers" >/dev/null 2>&1; then
  echo
  echo "Optional: agent mode / local Hugging Face models need PyTorch + Transformers."
  echo "Install them once with:"
  echo "  \"$VENV_PY\" -m pip install -r \"$ROOT/requirements-local-model.txt\""
  echo
fi

exec "$VENV_PY" "$ROOT/run_blaze.py" "$@"
