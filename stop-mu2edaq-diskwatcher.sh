#!/usr/bin/env bash
#
# stop-mu2edaq-diskwatcher.sh - standardized Mu2e control-room stop script.
# Launched as `crs-app stop diskwatcher`. SIGTERM then SIGKILL after a timeout.
#
# Stops every copy running out of this directory, not just the one named in the
# pid file: a daemon whose pid file was deleted, or a copy started by hand,
# would otherwise survive a "stop" and hold the port against the next start.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/diskwatcher-proc.sh
source "$SCRIPT_DIR/lib/diskwatcher-proc.sh"

PID_FILE="${1:-$SCRIPT_DIR/diskwatcher.pid}"
TIMEOUT="${CRS_STOP_TIMEOUT:-10}"

RUNNING=()
# bash 3.2 (macOS) has no `mapfile`.
while read -r _pid; do
  [[ -n "$_pid" ]] && RUNNING+=("$_pid")
done < <({ dw_pid_from_file "$PID_FILE"; dw_running_pids "$SCRIPT_DIR"; } | sort -un)

if [[ ${#RUNNING[@]} -eq 0 ]]; then
  if [[ -f "$PID_FILE" ]]; then
    echo "DAQ Disk Watcher not running (stale pid file); cleaning up"
    rm -f "$PID_FILE"
  else
    echo "DAQ Disk Watcher not running (no pid file: $PID_FILE)"
  fi
  exit 0
fi

echo "Stopping DAQ Disk Watcher (pid ${RUNNING[*]})..."
dw_kill_pids "$TIMEOUT" "${RUNNING[@]}"
rm -f "$PID_FILE"
echo "DAQ Disk Watcher stopped"
