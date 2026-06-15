#!/bin/bash
# Resolve paths from this script's location so it works from any cwd, and
# invoke the venv interpreter directly. Sourcing venv/bin/activate is
# fragile: if the venv has been moved, activate prepends a dead path and
# `python` silently falls back to the system interpreter (often Python 2).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: venv interpreter not found at $PYTHON" >&2
    echo "       Run ./bootstrap_diskwatcher.sh first to create the venv." >&2
    exit 1
fi
export PYTHONPATH="$SCRIPT_DIR/src:${PYTHONPATH:-}"

# Configuration file: first argument, else the default.
CONFIG_FILE="${1:-$SCRIPT_DIR/config/mu2edaq-diskwatcher.yaml}"
echo "Starting DAQ Diskwatcher with configuration: $CONFIG_FILE"

#"$PYTHON" "$SCRIPT_DIR/diskwatcher.py" --config "$CONFIG_FILE" --daemon
"$PYTHON" "$SCRIPT_DIR/diskwatcher.py" --config "$CONFIG_FILE"
if [ $? -eq 0 ]; then
    echo "Diskwatcher started successfully"
else
    echo "Failed to start Diskwatcher"
fi
