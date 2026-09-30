---
title: mu2edaq-diskwatcher operations
group: monitoring
tier: A
audience: runco
summary: Daemon operation, the per-node config workflow, fleet start/stop and what the three dashboards show
---

# mu2edaq-diskwatcher — operations

Companion to the 572-line `README.md`, which is the tutorial reference and is
one of the better ones in the suite. This page covers what an operator needs at
the console and does not repeat it.

---

## 1. Running the daemon

### Entry points

Four ways in, all reaching `mu2edaq_diskwatcher.cli:main`:

| Invocation | When |
|---|---|
| `./start-mu2edaq-diskwatcher.sh [-c CONFIG]` | **normal.** Control-room standard; daemonises |
| `python diskwatcher.py --config FILE` | source checkout shim (`diskwatcher.py:19`) |
| `python -m mu2edaq_diskwatcher` | installed package |
| `mu2edaq-diskwatcher` | console script (`pyproject.toml:23`) |

The `diskwatcher.py` shim inserts `src` at the **front** of `sys.path`: a plain
`PYTHONPATH=./src` is not enough, because the directory holding the script
always precedes `PYTHONPATH` and would shadow the package.

### Bootstrap

```bash
./bootstrap_diskwatcher.sh
```

Creates `venv/`, installs `requirements.txt`, installs the package editable,
and makes a best-effort install of `mu2edaq-discovery` from a sibling checkout
or GitHub. Both pip steps fail loudly; the discovery step degrades to a note.

> The name deviates from the suite's `<verb>-<app>.sh` convention — every other
> script here is `start-mu2edaq-diskwatcher.sh` / `stop-mu2edaq-diskwatcher.sh`.
> Filed as S4; not renamed, because a rename is a behavioural change to
> anything that calls it.

### Start, stop, restart

```bash
# start with the node's own config
./start-mu2edaq-diskwatcher.sh -c ./config/nodes/mu2e-diskwatcher-trk-01.yaml

# start on a non-default port
./start-mu2edaq-diskwatcher.sh -c ./config/nodes/mu2e-diskwatcher-trk-01.yaml -p 5102

# refuse to start if one is already running, instead of replacing it
./start-mu2edaq-diskwatcher.sh -c ./config/... --no-replace

./stop-mu2edaq-diskwatcher.sh
```

**Restart is idempotent by design.** The start script stops any copy already
running out of this directory *before* starting
(`start-mu2edaq-diskwatcher.sh:87-112`). It consults both the pid file and the
process table, because the pid file misses a copy started by hand and the
process table is the only way to notice a daemon whose pid file was deleted.
Either one left running would bind 5002 and silently defeat the restart. The
pre-1.3.0 location `./diskwatcher.pid` is also read, so upgrading over a
running daemon still replaces it.

`stop` likewise kills *every* copy running out of the directory, not just the
pid-file one, then SIGTERM → SIGKILL after `CRS_STOP_TIMEOUT` (default 10 s).

### Shared NFS checkouts — the per-node run directory

The checkout is commonly on NFS and mounted by every DAQ node. Everything the
daemon writes therefore lives in `run/{host}`, keyed by short hostname
(`src/mu2edaq_diskwatcher/settings.py:35`), so two nodes never read each
other's pid file or append to one log:

```
run/mu2e-trk-01/mu2edaq-diskwatcher.pid
run/mu2e-trk-01/mu2edaq-diskwatcher.log
```

The start script passes `--run-dir` and `--pid-file` explicitly so that it and
the daemon always agree on where to look — an override on the command line
would otherwise change where the *next* start searches but not where the daemon
writes (`start-mu2edaq-diskwatcher.sh:134-137`). Override with `--run-dir` or
`$DW_RUN_DIR`. The process-table sweep is per-node by nature, since `ps` only
sees local processes.

Placeholders available in `run_dir`, `pid_file` and `log_file`: `{host}`,
`{hostname}`, `{port}`, `{user}`, and `{run_dir}` (in the latter two only).
Unknown placeholders are left visible in the result so `/config` shows the
operator what was not understood
(`src/mu2edaq_diskwatcher/settings.py:70-82`).

### Daemon mechanics

POSIX double-fork (`src/mu2edaq_diskwatcher/daemon.py:8`); stdin to
`/dev/null`, stdout/stderr to the log; intermediate parents exit via
`os._exit()` so no atexit handler runs twice. `--daemon` on a platform without
`os.fork()` exits 1 with a clear message rather than misbehaving. The pid file
is removed by an `atexit` handler.

### Health checking

```bash
curl -s localhost:5002/api/health | python3 -m json.tool
```

`status` is `degraded` when the poller has stalled
(`poll_age_s > 3 * poll_interval`) or any config issue exists; otherwise `ok`.
An unreachable **peer** never degrades the local status — the peer's own
`/api/health` answers for it. `config_issues` is the count shown on `/config`.

> The web server bound here is Flask's built-in development server
> (`src/mu2edaq_diskwatcher/cli.py:297`), which prints its own "do not use in
> production" banner into the log. For a read-only dashboard on a trusted DAQ
> LAN this is a deliberate, tolerable choice, but it is not stated anywhere in
> the docs and the log banner alarms new operators. Filed as S4.

---

## 2. The per-node configuration workflow

### Layout

```
config/
├── mu2edaq-diskwatcher.yaml            # shipped example / built-in default
├── mu2edaq-diskwatcher-test.yaml       # test fixture
├── mu2e-diskwatcher-dl-01.yaml         # hand-written (also watches /home)
├── mu2e-diskwatcher-mgr-01.yaml        # GENERATED aggregator
└── nodes/
    ├── node.template.yaml              # source of truth for the 27 leaves
    ├── aggregator.template.yaml        # source of truth for mgr-01
    └── mu2e-diskwatcher-<node>.yaml    # 27 generated leaf configs
```

The 27 generated leaves are `trk-01..14`, `calo-01..08`, `crv-01`, `cfo-01`,
`dl-02`, `mu2egateway01`, `mu2egateway02`. With hand-written `dl-01` that is
**28 nodes carrying a leaf config**, which is exactly the 28 static peers the
aggregator lists.

Note the filename stem is not the hostname: the generator strips the `mu2e-`
prefix, so `mu2e-diskwatcher-trk-01.yaml` targets host `mu2e-trk-01.fnal.gov`
(`tools/make-node-configs.sh:89-90`). The two gateway files are the exception,
because those hostnames do not start with `mu2e-`.

### Regenerating

```bash
# see what would change, write nothing
tools/make-node-configs.sh --dry-run

# regenerate all 27 leaves and the aggregator
tools/make-node-configs.sh

# just the default node list
tools/make-node-configs.sh --list

# a one-off node, elsewhere
tools/make-node-configs.sh -o /tmp/try mu2e-trk-15
```

**Edit the template, never a generated file.** Each generated file carries
`# GENERATED from ...` on line 3, and the script refuses to overwrite a file
whose header no longer says that, treating it as hand-edited
(`tools/make-node-configs.sh:95-99`). Exit status 2 means at least one file was
skipped for that reason; `--force` overrides. This is how `dl-01` stays
hand-written.

The node list lives in `default_nodes()` at `tools/make-node-configs.sh:42-51`
and is where a new node is added. The aggregator's `peers.static` and
`discover.probe` lists are both derived from the same loop
(`tools/make-node-configs.sh:126-131`), so they can never drift apart.

**Verification performed during this review:** all 27 generated files were
re-rendered from `node.template.yaml` and compared byte-for-byte against what
is committed. Zero drift. The generator is genuinely the source of truth.

**Topology cross-check — they disagree.** The 28-node list is a fourth,
independent assertion of the cluster topology:

| Source | trackers | calorimeters | MC-2 lab hosts |
|---|---|---|---|
| `mu2edaq-diskwatcher/tools/make-node-configs.sh:48-49` | **14** | **8** | 28 |
| `mu2edaq-operations/scripts/nodes_config.yaml:55-56` | 18 | 14 | 47 |
| `mu2edaq-power-recovery/config/topology.yaml:95-96` | 18 | 14 | 48 |

diskwatcher is a strict subset — it invents no host the others do not know —
but 19 MC-2 hosts (`trk-15..18`, `calo-09..14`, `dcs-01..03`, `stm-01/02`,
`extmon-01`, `worker-01..03`) and the entire HEERC test stand have no
diskwatcher config. Whether that is deliberate scoping or a stale list is
**not determinable from this repository**, so nothing here was changed; the
discrepancy is filed as S3 with evidence. Topology is triplicated across three
packages with no generator.

### Threshold policy

Every threshold in the shipped templates is a **percentage**, and that is load
bearing. From `config/nodes/node.template.yaml:74-82`: the template is deployed
to disks from 20 GiB to 15 TiB, and an absolute floor that suits a big volume
exceeds the percentage thresholds on a small one. `10% / 5% / 500 GiB` on a
50 GiB disk resolves to 5 GiB / 2.5 GiB / 500 GiB — misordered, and reading
`FULL` on an empty disk. **It did, on 27 nodes, on 2026-09-23.**

Two guards now exist. A pure-percentage block is order-checked at config load
and cannot misorder on any disk size. A mixed block can only be checked once
the filesystem total is known, so the poller re-checks it per cycle and warns
once per process, setting `space_limits_ordered: false` which the UI marks
(`src/mu2edaq_diskwatcher/poller.py:117-123`). `tests/test_packaging.py` sweeps
10 GiB to 64 TiB to hold the templates pure-percentage.

If you must mix, put the absolute value where it stays the smallest on the
smallest disk you deploy to, and check `/config` for the ordering warning.

### Fleet operations

```bash
# what each node would use
tools/diskwatcher-fleet.sh list

# start everywhere, 8 at a time, verifying /api/health afterwards
tools/diskwatcher-fleet.sh start

# from outside the DAQ network
tools/diskwatcher-fleet.sh status -J mu2egateway01.fnal.gov

# a subset, dry run
tools/diskwatcher-fleet.sh -n stop mu2e-trk-01 mu2e-trk-02
```

Actions: `start` (default), `stop`, `status`, `list`. Runs the per-node
start/stop script over ssh in parallel batches (`--jobs`, default 8) and
reports one line per node plus a summary. Exit 0 if every node succeeded, 1 on
a usage error, 3 if any node failed or was unreachable.

After `start` it polls `/api/health` for up to 30 s before calling a node down
(`tools/diskwatcher-fleet.sh:174-178`): the web server binds only after the
first synchronous poll, which waits on any unreachable `ssh:` entries.

`--jump` handles the case where the jump host is itself one of the nodes — ssh
refuses to jump through a host to reach that same host, so the script goes
direct for that one (`tools/diskwatcher-fleet.sh:154-161`). The checkout is
shared NFS, so the same path is used on every node unless `--dir` says
otherwise.

### Control-room integration

`crs-app start diskwatcher` exports `CRS_PORT_HTTP` from `apps.yaml` and
invokes `start-mu2edaq-diskwatcher.sh`, which forwards it as `--port`
(`start-mu2edaq-diskwatcher.sh:9`, `:26`). Port precedence there is
`CRS_PORT_HTTP` env > built-in 5002, which matches `apps.yaml`.

---

## 3. What the three pages show

All three render an empty shell and refresh from their own JSON endpoint on a
client-side interval (default 5 s, remembered in `localStorage`). Sorting,
grouping, filtering and expand/collapse are entirely client-side; nothing an
operator clicks reaches the server. Peer entries appear grouped under a heading
carrying the peer's label, URL, hostname and version.

### `/` — Watcher

Every configured entry, files and directories alike, classified by **mtime
freshness**:

| State | Meaning |
|---|---|
| `ok` | modified within `delay` |
| `stale` | `now - mtime > delay` |
| `missing` | the path could not be statted (counts as stale when `delay` is set) |
| `unmonitored` | no `delay:` — listed only |

`unmonitored` ranks *below* the alert threshold on purpose
(`src/mu2edaq_diskwatcher/web/api.py:66`), so a size-only entry never opens a
group by itself. Feed: `/api/status`.

### `/space` — Disk Space

Only directories carrying a `space:` block. Thresholds name how much must
remain **free**, so they descend:

| State | Meaning |
|---|---|
| `GOOD` | free above every threshold |
| `WARNING` / `CRITICAL` / `FULL` | `free <= warning` / `critical` / `full` |
| `MISSING` | the directory could not be read |
| `UNKNOWN` | it exists but the filesystem could not be measured |

`MISSING` and `UNKNOWN` rank *above* `FULL`
(`src/mu2edaq_diskwatcher/thresholds.py:49-52`), which is intended: a vanished
path or an unmeasurable filesystem is at least as urgent as a nearly full one.
A group holds open at `CRITICAL` or worse, however the operator last left it.
Clicking a stat card filters to that state. Feed: `/api/space`.

Note `free` is the unprivileged figure from `statvfs` (`f_bavail`), so
`used + free < total` on a volume with a root reserve, and the two percentages
do not sum to 100. See `docs/INTERFACES.md` §5.

### `/sizes` — File Sizes

Only files carrying a `size:` block. Thresholds name file length, so they
ascend:

| State | Meaning |
|---|---|
| `GOOD` | below every threshold |
| `EMPTY` | zero length — checked **before** any threshold |
| `WARNING` / `CRITICAL` / `FULL` | `size >= warning` / `critical` / `full` |
| `MISSING` | the file could not be read |

`EMPTY` is its own alarm because a truncated file is a fault regardless of
thresholds (`src/mu2edaq_diskwatcher/thresholds.py:215`). `allow_empty: true`
suppresses it, which the templates set on both rotated system logs —
logrotate's truncate is a routine transient, and a dead rsyslog is caught by
the `delay: 3600` staleness check on `messages` instead
(`config/nodes/node.template.yaml:38-40`).

A directory's size is never reported: `st_size` on a directory is an allocation
figure, not a data size, and would mislead an operator
(`src/mu2edaq_diskwatcher/poller.py:201-203`). Feed: `/api/sizes`.

### `/config`, `/api`, `/about`

`/config` is the first place to look when behaviour does not match the file: it
shows the resolved settings after all four layers, the parsed entries with
their thresholds, every peer with its connection state, the discovery scan
summary, and the raw YAML as read. Every config problem is listed there
verbatim. `/api` documents the endpoints; `/about` reports versions, uptime,
dependency list and counts.

---

## 4. Troubleshooting

| Symptom | Check |
|---|---|
| Daemon will not start | `--config`/`$MU2EDAQ_DISKWATCHER_CONFIG` naming a missing file is the one fatal config error (exit 1) |
| Port already bound | another copy is running; `start-...sh` replaces it unless `--no-replace` |
| Thresholds seem inert | `/config` — a misordered block makes some states unreachable; look for the ordering warning |
| A volume reads `FULL` on an empty disk | a mixed percent/absolute block resolved out of order; make it pure-percentage |
| Peer shows dimmed rows | last-good data retained after a failed fetch; the heading gives the time it was last current |
| Peer never connects | `/api/peers` gives the exact reason string per peer; `curl <peer>/api/state` directly |
| Peer refuses as "this instance" | the configured URL resolves back to this process |
| Discovery finds nothing | `mu2edaq-discovery` installed? multicast does not cross DAQ switches — use `discover.probe` |
| Remote entry always `MISSING` | `ssh -o BatchMode=yes <host> true` by hand; `BatchMode` means no password prompt is possible |
| Remote `ssh:` `options:` ignored | a bare `Key=Value` list is passed without `-o`; use the string form `"-o Key=Value"` until the S3 issue is fixed |
| Remote age wrong by a constant | clock skew between this node and the remote; nothing cross-checks NTP |
| Editing a node config has no effect | it is regenerated from `config/nodes/node.template.yaml`; edit the template |

Raise verbosity with `--verbose` (or `$MU2EDAQ_DISKWATCHER_VERBOSE=1`) to log
every poll cycle, remote probe and peer fetch instead of only state
transitions.

### Tests

```bash
source venv/bin/activate
pytest                      # 314 test functions across 13 files
```

There is **no CI** — no `.github/` directory exists. The suite runs only when
someone remembers. Filed as S3.
