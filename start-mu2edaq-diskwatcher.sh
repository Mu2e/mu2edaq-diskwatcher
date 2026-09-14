#!/usr/bin/env bash
#
# start-mu2edaq-diskwatcher.sh - standardized Mu2e control-room start script.
#
# Launched by the control room as `crs-app start diskwatcher`, which exports
# CRS_PORT_HTTP from apps.yaml. Forwards it to diskwatcher.py as --port and
# runs in daemon mode with a pid file. Can also be run by hand.
#
# Port precedence: CRS_PORT_HTTP env > built-in default (matches apps.yaml).
#
# Any copy already running out of this directory is stopped first, so that
# restarting never leaves two daemons competing for the port. Pass --no-replace
# to refuse to start instead.
#
# This checkout is commonly on NFS, shared by several DAQ nodes. Everything the
# daemon writes therefore lives in a per-node run directory, run/<short host>,
# so two nodes never read each other's pid file or append to one log. The
# process-table sweep is per node by nature (ps only sees local processes).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=lib/diskwatcher-proc.sh
source "$SCRIPT_DIR/lib/diskwatcher-proc.sh"

CRS_PORT_HTTP="${CRS_PORT_HTTP:-5002}"   # web server port (diskwatcher.py --port)
NODE="$(dw_node)"
RUN_DIR="${DW_RUN_DIR:-$SCRIPT_DIR/run/$NODE}"
PID_FILE=""                              # resolved below once RUN_DIR is final
STOP_TIMEOUT="${CRS_STOP_TIMEOUT:-10}"
REPLACE=1
CONFIG_FILE=""
EXTRA=()

# Accept the config either as a bare first argument (the historical form, still
# used by crs-app) or as -c/--config, so that typing the flag does not silently
# become the filename. Anything else is forwarded to diskwatcher.py untouched.
while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config)
      [[ $# -ge 2 ]] || { echo "error: $1 requires a file argument" >&2; exit 2; }
      CONFIG_FILE="$2"; shift 2 ;;
    --config=*) CONFIG_FILE="${1#*=}"; shift ;;
    -p|--port)
      [[ $# -ge 2 ]] || { echo "error: $1 requires a port argument" >&2; exit 2; }
      CRS_PORT_HTTP="$2"; shift 2 ;;
    --port=*) CRS_PORT_HTTP="${1#*=}"; shift ;;
    --no-replace) REPLACE=0; shift ;;
    --pid-file)
      [[ $# -ge 2 ]] || { echo "error: $1 requires a file argument" >&2; exit 2; }
      PID_FILE="$2"; shift 2 ;;
    --pid-file=*) PID_FILE="${1#*=}"; shift ;;
    --run-dir)
      [[ $# -ge 2 ]] || { echo "error: $1 requires a directory argument" >&2; exit 2; }
      RUN_DIR="$2"; shift 2 ;;
    --run-dir=*) RUN_DIR="${1#*=}"; shift ;;
    -h|--help)
      cat <<EOF
usage: $(basename "$0") [CONFIG | -c CONFIG] [-p PORT] [--no-replace]
                                   [--run-dir DIR] [--pid-file FILE]
                                   [diskwatcher options...]

Starts the DAQ Disk Watcher as a daemon. Any copy already running out of this
directory is stopped first; --no-replace refuses to start instead.

The pid file and log go in the per-node run directory, by default
  $SCRIPT_DIR/run/$NODE
(override with --run-dir or \$DW_RUN_DIR), so a checkout shared over NFS is
safe to start on several nodes at once.
EOF
      exit 0 ;;
    -*) EXTRA+=("$1"); shift ;;
    *)
      if [[ -z "$CONFIG_FILE" ]]; then CONFIG_FILE="$1"; else EXTRA+=("$1"); fi
      shift ;;
  esac
done
CONFIG_FILE="${CONFIG_FILE:-./config/mu2edaq-diskwatcher.yaml}"
PID_FILE="${PID_FILE:-$RUN_DIR/mu2edaq-diskwatcher.pid}"
LEGACY_PID_FILE="$SCRIPT_DIR/diskwatcher.pid"   # where releases before 1.3.0 wrote it

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "error: config file not found: $CONFIG_FILE" >&2
  exit 1
fi

# ---- stop any copy already running out of this directory -------------------
# Both the pid file and the process table are consulted: the pid file misses a
# copy started by hand, and the process table is the only way to notice a
# daemon whose pid file was deleted. Either one left running would bind the
# port and silently defeat the restart. The pre-1.3.0 pid file is read too, so
# upgrading over a running daemon still replaces it.
RUNNING=()
# `mapfile` would be tidier but does not exist in bash 3.2, which is what macOS
# ships; this script has to run there as well as on the Linux DAQ nodes.
while read -r _pid; do
  [[ -n "$_pid" ]] && RUNNING+=("$_pid")
done < <({ dw_pid_from_file "$PID_FILE"; dw_pid_from_file "$LEGACY_PID_FILE";
           dw_running_pids "$SCRIPT_DIR"; } | sort -un)

if [[ ${#RUNNING[@]} -gt 0 ]]; then
  if [[ "$REPLACE" == 0 ]]; then
    echo "error: DAQ Disk Watcher already running (pid ${RUNNING[*]}); not starting (--no-replace)" >&2
    exit 1
  fi
  echo "Found running DAQ Disk Watcher (pid ${RUNNING[*]}); stopping it first"
  if [[ -z "${DW_DRY_RUN:-}" ]]; then
    dw_kill_pids "$STOP_TIMEOUT" "${RUNNING[@]}"
    rm -f "$PID_FILE" "$LEGACY_PID_FILE"
    echo "Previous instance stopped"
  fi
fi

# DW_DRY_RUN lets the test suite check argument handling without a venv and
# without starting a daemon. Deliberately absent from the man page: it is a
# test hook, not an interface.
if [[ -n "${DW_DRY_RUN:-}" ]]; then
  echo "Starting DAQ Disk Watcher (http=$CRS_PORT_HTTP, config: $CONFIG_FILE, node: $NODE, run dir: $RUN_DIR, pid file: $PID_FILE)"
  exit 0
fi

mkdir -p "$RUN_DIR"

if [[ ! -x ./venv/bin/python ]]; then
  echo "error: virtual environment not found; run ./bootstrap_diskwatcher.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
source ./venv/bin/activate
export PYTHONPATH="./src:${PYTHONPATH:-}"

echo "Starting DAQ Disk Watcher (http=$CRS_PORT_HTTP, config: $CONFIG_FILE, node: $NODE, run dir: $RUN_DIR)"
# ${EXTRA[@]+...} keeps `set -u` happy with an empty array under bash 3.2 (macOS).
# $PID_FILE and $RUN_DIR, not the literal defaults: the daemon must write the
# same files the next start reads, or an override here would change discovery
# but not the daemon. The log lands in the run directory unless the config or
# an extra --log-file says otherwise.
exec python diskwatcher.py --config "$CONFIG_FILE" --port "$CRS_PORT_HTTP" \
  --daemon --run-dir "$RUN_DIR" --pid-file "$PID_FILE" ${EXTRA[@]+"${EXTRA[@]}"}
