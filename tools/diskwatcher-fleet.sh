#!/usr/bin/env bash
#
# diskwatcher-fleet.sh - start, stop or check the disk watcher on every DAQ node.
#
# Runs the per-node start/stop script over ssh on each node, in parallel
# batches, and reports one line per node plus a summary. Intended to run from
# mu2e-mgr-01, but works from any machine whose ssh reaches the nodes without a
# prompt. The checkout is a shared NFS directory, so the same path is used on
# every node unless --dir says otherwise.
#
# usage: tools/diskwatcher-fleet.sh [ACTION] [OPTIONS] [HOST...]
#
# actions:
#   start             start (replacing a running copy) and verify /api/health   [default]
#   stop              stop
#   status            report /api/health from each node
#   list              print the node -> config mapping and exit
#
# options:
#   -n, --dry-run       print the ssh commands; run nothing
#   -j, --jobs N        nodes to work on at once (default 8)
#   -u, --user USER     ssh as USER (default: the current user)
#   -d, --dir PATH      checkout path on the nodes (default: this checkout's path)
#   -D, --domain DOM    domain appended to short names (default fnal.gov)
#   -p, --port PORT     dashboard port, forwarded as CRS_PORT_HTTP (default 5002)
#   -t, --timeout SEC   ssh connect timeout (default 10)
#   -J, --jump HOST     reach the nodes through a jump host (ssh -J), e.g. from
#                       outside the DAQ network: -J mu2egateway01.fnal.gov
#   -x, --exclude HOST  skip HOST; repeatable
#   -i, --interactive   allow ssh to prompt (default: BatchMode=yes, fail fast)
#   --no-verify         after start, do not fetch /api/health from each node
#   --no-replace        forward to the start script: refuse if already running
#   -h, --help          this text
#
# HOST... limits the run to those nodes (short names or FQDNs). With none, every
# node that has a file in config/nodes/ is used, plus mu2e-dl-01 with its
# hand-written config/mu2e-diskwatcher-dl-01.yaml.
#
# Exit status: 0 if every node succeeded, 1 on a usage error, 3 if any node
# failed or was unreachable.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ACTION="start"
DRY_RUN=0
JOBS=8
SSH_USER=""
REMOTE_DIR="$REPO"
DOMAIN="fnal.gov"
PORT="${CRS_PORT_HTTP:-5002}"
CONNECT_TIMEOUT=10
JUMP=""
BATCH="-o BatchMode=yes"
VERIFY=1
START_EXTRA=()
EXCLUDE=()
HOSTS=()

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    start|stop|status|list) ACTION="$1"; shift ;;
    -n|--dry-run)     DRY_RUN=1; shift ;;
    -j|--jobs)        [[ $# -ge 2 ]] || { echo "error: $1 needs a number" >&2; exit 1; }
                      JOBS="$2"; shift 2 ;;
    --jobs=*)         JOBS="${1#*=}"; shift ;;
    -u|--user)        [[ $# -ge 2 ]] || { echo "error: $1 needs a user" >&2; exit 1; }
                      SSH_USER="$2"; shift 2 ;;
    --user=*)         SSH_USER="${1#*=}"; shift ;;
    -d|--dir)         [[ $# -ge 2 ]] || { echo "error: $1 needs a path" >&2; exit 1; }
                      REMOTE_DIR="$2"; shift 2 ;;
    --dir=*)          REMOTE_DIR="${1#*=}"; shift ;;
    -D|--domain)      [[ $# -ge 2 ]] || { echo "error: $1 needs a domain" >&2; exit 1; }
                      DOMAIN="$2"; shift 2 ;;
    --domain=*)       DOMAIN="${1#*=}"; shift ;;
    -p|--port)        [[ $# -ge 2 ]] || { echo "error: $1 needs a port" >&2; exit 1; }
                      PORT="$2"; shift 2 ;;
    --port=*)         PORT="${1#*=}"; shift ;;
    -t|--timeout)     [[ $# -ge 2 ]] || { echo "error: $1 needs seconds" >&2; exit 1; }
                      CONNECT_TIMEOUT="$2"; shift 2 ;;
    --timeout=*)      CONNECT_TIMEOUT="${1#*=}"; shift ;;
    -J|--jump)        [[ $# -ge 2 ]] || { echo "error: $1 needs a host" >&2; exit 1; }
                      JUMP="$2"; shift 2 ;;
    --jump=*)         JUMP="${1#*=}"; shift ;;
    -x|--exclude)     [[ $# -ge 2 ]] || { echo "error: $1 needs a host" >&2; exit 1; }
                      EXCLUDE+=("${2%%.*}"); shift 2 ;;
    --exclude=*)      EXCLUDE+=("${1#*=}"); shift ;;
    -i|--interactive) BATCH=""; shift ;;
    --no-verify)      VERIFY=0; shift ;;
    --no-replace)     START_EXTRA+=("--no-replace"); shift ;;
    -h|--help)        usage; exit 0 ;;
    -*)               echo "error: unknown option $1" >&2; exit 1 ;;
    *)                HOSTS+=("${1%%.*}"); shift ;;
  esac
done
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo "error: --jobs must be a positive integer" >&2; exit 1; }

# ---- which nodes, which config -------------------------------------------
# A node's config is the generated file in config/nodes/, or a hand-written
# file in config/ with the same naming, whichever exists. Paths are relative
# to the checkout so they mean the same thing on every node.
config_for() {
  local node="$1" short
  short="${node#mu2e-}"
  for candidate in "config/nodes/mu2e-diskwatcher-$short.yaml" \
                   "config/mu2e-diskwatcher-$short.yaml" \
                   "config/nodes/mu2e-diskwatcher-$node.yaml" \
                   "config/mu2e-diskwatcher-$node.yaml"; do
    if [[ -f "$REPO/$candidate" ]]; then echo "$candidate"; return 0; fi
  done
  return 1
}

all_nodes() {
  local f stem
  for f in "$REPO"/config/nodes/mu2e-diskwatcher-*.yaml "$REPO"/config/mu2e-diskwatcher-*.yaml; do
    [[ -f "$f" ]] || continue
    stem="$(basename "$f" .yaml)"; stem="${stem#mu2e-diskwatcher-}"
    case "$stem" in
      mu2e*) echo "$stem" ;;           # mu2egateway01 keeps its full name
      *)     echo "mu2e-$stem" ;;      # trk-03 -> mu2e-trk-03
    esac
  done | sort -u
}

if [[ ${#HOSTS[@]} -eq 0 ]]; then
  # shellcheck disable=SC2207
  HOSTS=($(all_nodes))
fi
NODES=()
for h in "${HOSTS[@]}"; do
  skip=0
  for x in ${EXCLUDE[@]+"${EXCLUDE[@]}"}; do [[ "$h" == "$x" ]] && skip=1; done
  [[ "$skip" == 1 ]] || NODES+=("$h")
done
[[ ${#NODES[@]} -gt 0 ]] || { echo "error: no nodes selected" >&2; exit 1; }

if [[ "$ACTION" == "list" ]]; then
  for node in "${NODES[@]}"; do
    printf '%-16s %s\n' "$node" "$(config_for "$node" || echo '(no config file)')"
  done
  exit 0
fi

# ---- per-node work --------------------------------------------------------
SSH_OPTS=(-o ConnectTimeout="$CONNECT_TIMEOUT" -o StrictHostKeyChecking=accept-new
          -o LogLevel=ERROR)
[[ -z "$BATCH" ]] || SSH_OPTS+=("$BATCH")

# The jump host is itself one of the nodes (the gateways are in the list), and
# ssh refuses to jump through a host to reach that same host.  Go direct then.
ssh_opts_for() {
  local node="$1" jump_host="${JUMP#*@}"
  printf '%s\n' "${SSH_OPTS[@]}"
  if [[ -n "$JUMP" && "$node.$DOMAIN" != "$jump_host" && "$node" != "$jump_host" ]]; then
    printf '%s\n' -J "$JUMP"
  fi
}

remote_command() {
  local node="$1" cfg="$2"
  case "$ACTION" in
    start)
      printf "cd %q && CRS_PORT_HTTP=%q ./start-mu2edaq-diskwatcher.sh -c %q" \
        "$REMOTE_DIR" "$PORT" "$cfg"
      for extra in ${START_EXTRA[@]+"${START_EXTRA[@]}"}; do printf ' %q' "$extra"; done
      if [[ "$VERIFY" == 1 ]]; then
        # The web server binds only after the first poll, which waits on any
        # unreachable ssh: entries, so allow up to 30 s before calling it down.
        # The loop's own exit status is that of its last `sleep`, so a final
        # curl outside the loop is what decides.
        printf ' && for i in $(seq 1 30); do curl -sf --max-time 3 -o /dev/null localhost:%q/api/health && break; sleep 1; done; curl -sf --max-time 3 localhost:%q/api/health' "$PORT" "$PORT"
      fi ;;
    stop)
      printf "cd %q && ./stop-mu2edaq-diskwatcher.sh" "$REMOTE_DIR" ;;
    status)
      printf "curl -sf --max-time 5 localhost:%q/api/health" "$PORT" ;;
  esac
}

# Reduce a /api/health body to one line; pass anything else through.
summarise_health() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, sys
# The body may carry ssh banners or warnings before the JSON, so decode from
# the first brace and ignore anything after the object.
text = sys.stdin.read()
start = text.find("{")
if start < 0:
    sys.stdout.write(" ".join(text.split())[-200:]); sys.exit(0)
try:
    d, _end = json.JSONDecoder().raw_decode(text[start:])
except ValueError:
    sys.stdout.write(" ".join(text.split())[-200:]); sys.exit(0)
p = d.get("peers") or {}
print("%s v%s, %d entries, poll %ss ago, up %ss, %d config issue(s), peers %d/%d ok"
      % (d.get("status"), d.get("version"), d.get("entries", 0), d.get("poll_age_s"),
         d.get("uptime_s"), d.get("config_issues", 0), p.get("ok", 0), p.get("configured", 0)))
'
  else
    tr -d '\n' | tail -c 200
  fi
}

run_node() {
  local node="$1" out="$2" cfg target cmd rc
  target="$node.$DOMAIN"
  [[ -z "$SSH_USER" ]] || target="$SSH_USER@$target"
  if ! cfg="$(config_for "$node")"; then
    echo "SKIP no config file for $node" > "$out"; return 0
  fi
  cmd="$(remote_command "$node" "$cfg")"
  local opts=()
  while IFS= read -r line; do opts+=("$line"); done < <(ssh_opts_for "$node")
  if [[ "$DRY_RUN" == 1 ]]; then
    local via=""
    for o in "${opts[@]}"; do [[ "$o" == "-J" ]] && via=" -J $JUMP"; done
    echo "DRY ssh$via $target $cmd" > "$out"; return 0
  fi
  if body="$(ssh "${opts[@]}" "$target" "$cmd" 2>&1)"; then rc=0; else rc=$?; fi
  case "$rc" in
    0)   case "$ACTION" in
           status) echo "OK $(printf '%s' "$body" | summarise_health)" ;;
           start)  if [[ "$VERIFY" == 1 ]]; then
                     echo "OK $(printf '%s' "$body" | summarise_health)"
                   else
                     echo "OK $(printf '%s' "$body" | tail -n 1)"
                   fi ;;
           *)      echo "OK $(printf '%s' "$body" | tail -n 1)" ;;
         esac > "$out" ;;
    255) echo "UNREACHABLE $(printf '%s' "$body" | tail -n 1)" > "$out" ;;
    *)   if [[ "$ACTION" == "status" ]]; then
           echo "DOWN no answer on port $PORT (curl exit $rc)" > "$out"
         else
           echo "FAILED exit $rc: $(printf '%s' "$body" | grep -v '^$' | tail -n 2 | tr '\n' ' ')" > "$out"
         fi ;;
  esac
}

# ---- run in batches -------------------------------------------------------
# Batches rather than `wait -n`, which macOS's bash 3.2 lacks: launch up to
# JOBS nodes, wait for the batch, print it in order, repeat.
TMP="$(mktemp -d "${TMPDIR:-/tmp}/dw-fleet.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

echo "$ACTION: ${#NODES[@]} node(s), $JOBS at a time, checkout $REMOTE_DIR, port $PORT"
ok=0; bad=0; skipped=0
i=0
while [[ $i -lt ${#NODES[@]} ]]; do
  batch=("${NODES[@]:$i:$JOBS}")
  for node in "${batch[@]}"; do
    run_node "$node" "$TMP/$node" &
  done
  wait
  for node in "${batch[@]}"; do
    line="$(cat "$TMP/$node")"
    printf '%-16s %s\n' "$node" "$line"
    case "$line" in
      OK*|DRY*) ok=$((ok + 1)) ;;
      SKIP*)    skipped=$((skipped + 1)) ;;
      *)        bad=$((bad + 1)) ;;
    esac
  done
  i=$((i + JOBS))
done

echo "summary: $ok ok, $bad failed/unreachable, $skipped skipped"
[[ "$bad" == 0 ]] || exit 3
