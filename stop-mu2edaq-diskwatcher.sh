#!/usr/bin/env bash
#
# stop-mu2edaq-diskwatcher.sh - standardized Mu2e control-room stop script.
# Launched as `crs-app stop diskwatcher`. SIGTERM then SIGKILL after a timeout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${1:-$SCRIPT_DIR/diskwatcher.pid}"
TIMEOUT="${CRS_STOP_TIMEOUT:-10}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "DAQ Disk Watcher not running (no pid file: $PID_FILE)"
  exit 0
fi
pid="$(cat "$PID_FILE")"
if ! kill -0 "$pid" 2>/dev/null; then
  echo "DAQ Disk Watcher not running (stale pid $pid); cleaning up"
  rm -f "$PID_FILE"
  exit 0
fi

echo "Stopping DAQ Disk Watcher (pid $pid)..."
kill -TERM "$pid" 2>/dev/null || true
for ((i = 0; i < TIMEOUT; i++)); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done
if kill -0 "$pid" 2>/dev/null; then
  echo "did not exit within ${TIMEOUT}s; sending SIGKILL"
  kill -KILL "$pid" 2>/dev/null || true
  sleep 1
fi
rm -f "$PID_FILE"
echo "DAQ Disk Watcher stopped"
