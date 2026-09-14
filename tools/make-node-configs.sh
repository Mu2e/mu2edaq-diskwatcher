#!/usr/bin/env bash
#
# make-node-configs.sh - generate one diskwatcher config per DAQ node from
# config/nodes/node.template.yaml.
#
# The nodes all watch the same six areas with the same thresholds, so the
# per-node files differ only in their header and their pid/log names.  Keeping
# them generated means a threshold change is one edit to the template and one
# run of this script, instead of the same edit in 27 files.
#
# usage: tools/make-node-configs.sh [-n] [-f] [-o DIR] [-t FILE] [-d DOMAIN] [HOST...]
#
#   -n, --dry-run     print what would be written; write nothing
#   -f, --force       overwrite files that were hand-edited (see below)
#   -o, --output DIR  where to write (default: config/nodes)
#   -t, --template F  template to use (default: config/nodes/node.template.yaml)
#   -d, --domain D    domain appended to short names (default: fnal.gov)
#   -a, --aggregator H  also write config/mu2e-diskwatcher-<H>.yaml for the
#                     aggregator node H from config/nodes/aggregator.template.yaml,
#                     with every node (and mu2e-dl-01) listed under peers.static
#                     (default: mu2e-mgr-01; --no-aggregator skips it)
#   -l, --list        print the default node list and exit
#   HOST...           short hostnames; default: the Mu2e DAQ node list below
#
# A file whose header no longer says GENERATED is treated as hand-edited and
# left alone unless --force is given, so a deliberate local change is never
# silently lost.  Exit status: 0 on success, 1 on a bad option or missing
# template, 2 if any file was skipped as hand-edited.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO/config/nodes/node.template.yaml"
AGG_TEMPLATE="$REPO/config/nodes/aggregator.template.yaml"
OUT_DIR="$REPO/config/nodes"
DOMAIN="fnal.gov"
AGGREGATOR="mu2e-mgr-01"
DRY_RUN=0
FORCE=0

# The Mu2e DAQ nodes, one diskwatcher each.  mu2e-dl-01 keeps its hand-written
# config/mu2e-diskwatcher-dl-01.yaml (it also watches system logs and /home).
default_nodes() {
  echo mu2egateway01 mu2egateway02
  echo mu2e-dl-02
  echo mu2e-cfo-01
  # printf, not `seq -w`: BSD seq pads to the width of the largest value, so
  # 1..8 comes out unpadded on macOS while Linux gives 01..08.
  for i in $(seq 1 14); do printf 'mu2e-trk-%02d\n' "$i"; done
  for i in $(seq 1 8);  do printf 'mu2e-calo-%02d\n' "$i"; done
  echo mu2e-crv-01
}

NODES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run)  DRY_RUN=1; shift ;;
    -f|--force)    FORCE=1; shift ;;
    -o|--output)   [[ $# -ge 2 ]] || { echo "error: $1 needs a directory" >&2; exit 1; }
                   OUT_DIR="$2"; shift 2 ;;
    --output=*)    OUT_DIR="${1#*=}"; shift ;;
    -t|--template) [[ $# -ge 2 ]] || { echo "error: $1 needs a file" >&2; exit 1; }
                   TEMPLATE="$2"; shift 2 ;;
    --template=*)  TEMPLATE="${1#*=}"; shift ;;
    -d|--domain)   [[ $# -ge 2 ]] || { echo "error: $1 needs a domain" >&2; exit 1; }
                   DOMAIN="$2"; shift 2 ;;
    --domain=*)    DOMAIN="${1#*=}"; shift ;;
    -a|--aggregator) [[ $# -ge 2 ]] || { echo "error: $1 needs a host" >&2; exit 1; }
                   AGGREGATOR="${2%%.*}"; shift 2 ;;
    --aggregator=*) AGGREGATOR="${1#*=}"; shift ;;
    --no-aggregator) AGGREGATOR=""; shift ;;
    -l|--list)     default_nodes | tr ' ' '\n'; exit 0 ;;
    -h|--help)     sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)            echo "error: unknown option $1" >&2; exit 1 ;;
    *)             NODES+=("$1"); shift ;;
  esac
done
if [[ ${#NODES[@]} -eq 0 ]]; then
  # shellcheck disable=SC2207
  NODES=($(default_nodes))
fi

[[ -f "$TEMPLATE" ]] || { echo "error: template not found: $TEMPLATE" >&2; exit 1; }
[[ "$DRY_RUN" == 1 ]] || mkdir -p "$OUT_DIR"

skipped=0
written=0
for node in "${NODES[@]}"; do
  node="${node%%.*}"                          # accept FQDNs too
  stem="mu2e-diskwatcher-${node#mu2e-}"       # mu2e-trk-01 -> mu2e-diskwatcher-trk-01
  [[ "$node" == mu2e-* ]] || stem="mu2e-diskwatcher-$node"   # mu2egateway01 -> ...-mu2egateway01
  file="$stem.yaml"
  target="$OUT_DIR/$file"
  fqdn="$node.$DOMAIN"

  if [[ -f "$target" && "$FORCE" == 0 ]] && ! grep -q '^# GENERATED from' "$target"; then
    echo "skip   $target (hand-edited; use --force to overwrite)" >&2
    skipped=1
    continue
  fi

  if [[ "$DRY_RUN" == 1 ]]; then
    echo "would write $target for $fqdn"
    continue
  fi
  sed -e "s|@FILE@|$file|g" -e "s|@FILE_STEM@|$stem|g" \
      -e "s|@HOST@|$node|g" -e "s|@FQDN@|$fqdn|g" "$TEMPLATE" > "$target"
  echo "wrote  $target"
  written=$((written + 1))
done

[[ "$DRY_RUN" == 1 ]] || echo "$written file(s) written to $OUT_DIR"

# ---- the aggregator ---------------------------------------------------------
# One node shows the whole fleet.  Its file lives in config/ (not config/nodes/)
# and lists every node statically, so federation does not depend on multicast
# reaching each one; discovery is enabled as well for anything not listed.
if [[ -n "$AGGREGATOR" ]]; then
  [[ -f "$AGG_TEMPLATE" ]] || { echo "error: template not found: $AGG_TEMPLATE" >&2; exit 1; }
  agg_stem="mu2e-diskwatcher-${AGGREGATOR#mu2e-}"
  agg_file="$agg_stem.yaml"
  agg_target="$REPO/config/$agg_file"
  agg_fqdn="$AGGREGATOR.$DOMAIN"
  # Every generated node, plus mu2e-dl-01 (hand-written config), minus itself.
  static=""
  probe=""
  for node in "${NODES[@]}" mu2e-dl-01; do
    node="${node%%.*}"
    [[ "$node" == "$AGGREGATOR" ]] && continue
    static+="    - {url: http://$node.$DOMAIN:5002, label: \"$node\"}"$'\n'
    probe+="      - $node.$DOMAIN"$'\n'
  done
  static="${static%$'\n'}"
  probe="${probe%$'\n'}"
  if [[ -f "$agg_target" && "$FORCE" == 0 ]] && ! grep -q '^# GENERATED from' "$agg_target"; then
    echo "skip   $agg_target (hand-edited; use --force to overwrite)" >&2
    skipped=1
  elif [[ "$DRY_RUN" == 1 ]]; then
    echo "would write $agg_target for $agg_fqdn (aggregator, $(printf '%s\n' "$static" | grep -c .) static peers)"
  else
    # The multi-line lists go through files: BSD awk (macOS) rejects a -v
    # value containing a newline, and sed has no portable multi-line insert.
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/dw-agg.XXXXXX")"
    printf '%s\n' "$static" > "$tmp/static"
    printf '%s\n' "$probe"  > "$tmp/probe"
    awk -v static_file="$tmp/static" -v probe_file="$tmp/probe" -v file="$agg_file" \
        -v stem="$agg_stem" -v host="$AGGREGATOR" -v fqdn="$agg_fqdn" '
      function cat_file(path,   line) { while ((getline line < path) > 0) print line; close(path) }
      /^@STATIC_PEERS@$/ { cat_file(static_file); next }
      /^@PROBE_HOSTS@$/  { cat_file(probe_file); next }
      { gsub(/@FILE@/, file); gsub(/@FILE_STEM@/, stem); gsub(/@HOST@/, host); gsub(/@FQDN@/, fqdn); print }
    ' "$AGG_TEMPLATE" > "$agg_target"
    rm -rf "$tmp"
    echo "wrote  $agg_target (aggregator)"
  fi
fi
[[ "$skipped" == 0 ]] || exit 2
