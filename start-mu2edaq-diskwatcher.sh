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
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=lib/diskwatcher-proc.sh
source "$SCRIPT_DIR/lib/diskwatcher-proc.sh"

CRS_PORT_HTTP="${CRS_PORT_HTTP:-5002}"   # web server port (diskwatcher.py --port)
PID_FILE="$SCRIPT_DIR/diskwatcher.pid"
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
    -h|--help)
      cat <<EOF
usage: $(basename "$0") [CONFIG | -c CONFIG] [-p PORT] [--no-replace]
                                   [--pid-file FILE] [diskwatcher options...]

Starts the DAQ Disk Watcher as a daemon. Any copy already running out of this
directory is stopped first; --no-replace refuses to start instead.
EOF
      exit 0 ;;
    -*) EXTRA+=("$1"); shift ;;
    *)
      if [[ -z "$CONFIG_FILE" ]]; then CONFIG_FILE="$1"; else EXTRA+=("$1"); fi
      shift ;;
  esac
done
CONFIG_FILE="${CONFIG_FILE:-./config/mu2edaq-diskwatcher.yaml}"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "error: config file not found: $CONFIG_FILE" >&2
  exit 1
fi

# ---- stop any copy already running out of this directory -------------------
# Both the pid file and the process table are consulted: the pid file misses a
# copy started by hand, and the process table is the only way to notice a
# daemon whose pid file was deleted. Either one left running would bind the
# port and silently defeat the restart.
RUNNING=()
# `mapfile` would be tidier but does not exist in bash 3.2, which is what macOS
# ships; this script has to run there as well as on the Linux DAQ nodes.
while read -r _pid; do
  [[ -n "$_pid" ]] && RUNNING+=("$_pid")
done < <({ dw_pid_from_file "$PID_FILE"; dw_running_pids "$SCRIPT_DIR"; } | sort -un)

if [[ ${#RUNNING[@]} -gt 0 ]]; then
  if [[ "$REPLACE" == 0 ]]; then
    echo "error: DAQ Disk Watcher already running (pid ${RUNNING[*]}); not starting (--no-replace)" >&2
    exit 1
  fi
  echo "Found running DAQ Disk Watcher (pid ${RUNNING[*]}); stopping it first"
  if [[ -z "${DW_DRY_RUN:-}" ]]; then
    dw_kill_pids "$STOP_TIMEOUT" "${RUNNING[@]}"
    rm -f "$PID_FILE"
    echo "Previous instance stopped"
  fi
fi

# DW_DRY_RUN lets the test suite check argument handling without a venv and
# without starting a daemon. Deliberately absent from the man page: it is a
# test hook, not an interface.
if [[ -n "${DW_DRY_RUN:-}" ]]; then
  echo "Starting DAQ Disk Watcher (http=$CRS_PORT_HTTP, config: $CONFIG_FILE)"
  exit 0
fi

if [[ ! -x ./venv/bin/python ]]; then
  echo "error: virtual environment not found; run ./bootstrap_diskwatcher.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
source ./venv/bin/activate
export PYTHONPATH="./src:${PYTHONPATH:-}"

echo "Starting DAQ Disk Watcher (http=$CRS_PORT_HTTP, config: $CONFIG_FILE)"
# ${EXTRA[@]+...} keeps `set -u` happy with an empty array under bash 3.2 (macOS).
# $PID_FILE, not the literal default: the daemon must write the same file the
# next start reads, or --pid-file would change discovery but not the daemon.
exec python diskwatcher.py --config "$CONFIG_FILE" --port "$CRS_PORT_HTTP" \
  --daemon --pid-file "$PID_FILE" ${EXTRA[@]+"${EXTRA[@]}"}
