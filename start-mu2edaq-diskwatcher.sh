#!/usr/bin/env bash
#
# start-mu2edaq-diskwatcher.sh - standardized Mu2e control-room start script.
#
# Launched by the control room as `crs-app start diskwatcher`, which exports
# CRS_PORT_HTTP from apps.yaml. Forwards it to diskwatcher.py as --port and
# runs in daemon mode with a pid file. Can also be run by hand.
#
# Port precedence: CRS_PORT_HTTP env > built-in default (matches apps.yaml).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CRS_PORT_HTTP="${CRS_PORT_HTTP:-5002}"   # web server port (diskwatcher.py --port)
CONFIG_FILE="${1:-./config/mu2edaq-diskwatcher.yaml}"

if [[ ! -x ./venv/bin/python ]]; then
  echo "error: virtual environment not found; run ./bootstrap_diskwatcher.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
source ./venv/bin/activate
export PYTHONPATH="./src:${PYTHONPATH:-}"

echo "Starting DAQ Disk Watcher (http=$CRS_PORT_HTTP, config: $CONFIG_FILE)"
exec python diskwatcher.py --config "$CONFIG_FILE" --port "$CRS_PORT_HTTP" \
  --daemon --pid-file "$SCRIPT_DIR/diskwatcher.pid"
