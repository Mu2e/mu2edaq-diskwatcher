#!/usr/bin/env bash
#
# diskwatcher-proc.sh - process discovery shared by the start and stop scripts.
#
# Sourced, never executed. Provides:
#
#   dw_pid_from_file FILE     echo the live pid recorded in FILE, if any
#   dw_running_pids DIR       echo every diskwatcher.py pid belonging to DIR
#   dw_kill_pids TIMEOUT ...  SIGTERM, then SIGKILL after TIMEOUT seconds
#
# A stale pid file is the normal failure mode -- the daemon was SIGKILLed, or
# the machine lost power -- so a pid is only believed once kill -0 confirms it.
# Scanning the process table as well catches the opposite case: a copy started
# by hand, or one whose pid file was deleted, which a pid-file-only check would
# miss and which would then fight the new instance for the port.

# True if $1 is a live process whose command line is this application.
#
# `kill -0` alone is NOT sufficient. Pid numbers are recycled, so a pid file
# left behind by a SIGKILLed daemon will eventually name some unrelated
# process -- and acting on that would mean SIGTERMing a stranger. Observed
# during development: a stale diskwatcher.pid pointed at a live, unrelated pid.
#
# The application name is looked for in the *arguments*, never in the
# interpreter path. The checkout is itself called mu2edaq-diskwatcher, so any
# process run as /path/to/mu2edaq-diskwatcher/venv/bin/python -- pytest, pip,
# a REPL -- would otherwise match on its executable alone and be stopped as
# though it were the daemon. That happened to the test suite when run from a
# git worktree, where the interpreter path had to be spelled out.
dw_is_diskwatcher() {
  local pid="$1" args exe rest
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  args="$(ps -o args= -p "$pid" 2>/dev/null)"
  exe="${args%% *}"
  rest="${args#"$exe"}"
  case "$exe" in *[Pp]ython*) ;; *) return 1 ;; esac
  case "$rest" in
    *diskwatcher.py*|*mu2edaq_diskwatcher*|*mu2edaq-diskwatcher*) return 0 ;;
  esac
  return 1
}

# Echo the pid recorded in $1 if it is a live diskwatcher. Silent otherwise.
dw_pid_from_file() {
  local pid_file="$1" pid
  [[ -f "$pid_file" ]] || return 0
  pid="$(tr -dc '0-9' < "$pid_file")"
  dw_is_diskwatcher "$pid" && echo "$pid"
  return 0
}

# Echo every pid running diskwatcher.py out of directory $1, newline separated.
#
# Matching is deliberately narrow: the command line must mention diskwatcher.py
# or the module entry point, AND the process must have $1 as its working
# directory. Two checkouts on one host therefore do not kill each other, and an
# unrelated process that merely has "diskwatcher" in its arguments -- an editor,
# a tail, this script's own parent -- is never matched. Own pid excluded.
dw_running_pids() {
  local dir="$1" pid cwd
  dir="$(cd "$dir" && pwd -P)"

  while read -r pid _rest; do
    [[ -n "$pid" ]] || continue
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    # Same command-line test as the pid-file path, kept in one place.
    dw_is_diskwatcher "$pid" || continue

    # lsof is the portable way to read another process's cwd; /proc is Linux
    # only and macOS has no equivalent file. When lsof is absent the cwd cannot
    # be confirmed, so the pid is skipped rather than killed on a guess.
    if command -v lsof >/dev/null 2>&1; then
      cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
      [[ "$cwd" == "$dir" ]] || continue
    else
      continue
    fi
    echo "$pid"
  done < <(ps -u "$(id -u)" -o pid=,args= 2>/dev/null)
  return 0
}

# SIGTERM every pid in $2.. , escalating to SIGKILL after $1 seconds.
dw_kill_pids() {
  local timeout="$1"; shift
  local pid i alive

  for pid in "$@"; do
    kill -TERM "$pid" 2>/dev/null || true
  done

  for ((i = 0; i < timeout; i++)); do
    alive=0
    for pid in "$@"; do
      kill -0 "$pid" 2>/dev/null && alive=1
    done
    [[ "$alive" == 0 ]] && return 0
    sleep 1
  done

  for pid in "$@"; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "  pid $pid did not exit within ${timeout}s; sending SIGKILL"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  sleep 1
  return 0
}
