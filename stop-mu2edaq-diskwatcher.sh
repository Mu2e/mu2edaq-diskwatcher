#!/usr/bin/env bash
#
# stop-mu2edaq-diskwatcher.sh - standardized Mu2e control-room stop script.
# Launched as `crs-app stop diskwatcher`. SIGTERM then SIGKILL after a timeout.
#
# Stops every copy running out of this directory, not just the one named in the
# pid file: a daemon whose pid file was deleted, or a copy started by hand,
# would otherwise survive a "stop" and hold the port against the next start.
#
# The pid file lives in the per-node run directory (see the start script), so
# on a checkout shared over NFS this stops the daemon on *this* node only. The
# pre-1.3.0 location ./diskwatcher.pid is still read, so a daemon started by an
# older release is found too.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/diskwatcher-proc.sh
source "$SCRIPT_DIR/lib/diskwatcher-proc.sh"

NODE="$(dw_node)"
RUN_DIR="${DW_RUN_DIR:-$SCRIPT_DIR/run/$NODE}"
PID_FILE="${1:-$RUN_DIR/mu2edaq-diskwatcher.pid}"
LEGACY_PID_FILE="$SCRIPT_DIR/diskwatcher.pid"
TIMEOUT="${CRS_STOP_TIMEOUT:-10}"

RUNNING=()
# bash 3.2 (macOS) has no `mapfile`.
while read -r _pid; do
  [[ -n "$_pid" ]] && RUNNING+=("$_pid")
done < <({ dw_pid_from_file "$PID_FILE"; dw_pid_from_file "$LEGACY_PID_FILE";
           dw_running_pids "$SCRIPT_DIR"; } | sort -un)

if [[ ${#RUNNING[@]} -eq 0 ]]; then
  if [[ -f "$PID_FILE" ]]; then
    echo "DAQ Disk Watcher not running on $NODE (stale pid file); cleaning up"
    rm -f "$PID_FILE"
  else
    echo "DAQ Disk Watcher not running on $NODE (no pid file: $PID_FILE)"
  fi
  # A stale legacy file is only ours to remove if it names nothing alive here;
  # on a shared checkout it may belong to a daemon on another node.
  [[ -f "$LEGACY_PID_FILE" ]] && ! dw_is_diskwatcher "$(tr -dc '0-9' < "$LEGACY_PID_FILE")" \
    && rm -f "$LEGACY_PID_FILE"
  exit 0
fi

echo "Stopping DAQ Disk Watcher on $NODE (pid ${RUNNING[*]})..."
dw_kill_pids "$TIMEOUT" "${RUNNING[@]}"
rm -f "$PID_FILE" "$LEGACY_PID_FILE"
echo "DAQ Disk Watcher stopped"
