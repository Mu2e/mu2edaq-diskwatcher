# mu2edaq-diskwatcher

File modification-time, free-space and file-size monitor with an embedded web
dashboard, for Mu2e DAQ control-room use.

It answers three questions about the paths you point it at:

| Question | Configured by | Dashboard |
|---|---|---|
| Has this path been written to recently? | `delay:` | `/` — Watcher |
| Is this volume running out of space? | `space:` | `/space` — Disk Space |
| Is this file empty, or growing out of control? | `size:` | `/sizes` — File Sizes |

Each page shows summary cards counting the entries in every alarm state,
followed by a sortable table. Local and remote (SSH) paths are both supported,
and instances can be [federated](#peers-aggregating-other-instances): one
dashboard can show what the diskwatchers on other nodes are monitoring, each
under its own heading.

## Quick start

```bash
./bootstrap_diskwatcher.sh                 # create venv, install deps
./start-mu2edaq-diskwatcher.sh             # start as a daemon on port 5002
./stop-mu2edaq-diskwatcher.sh              # stop it
```

Then open <http://localhost:5002/>.

Starting is idempotent: if a copy is already running out of this directory it is
stopped before the new one launches, so re-running the start script restarts the
daemon rather than failing on the bound port. See
[Start and stop](#start-and-stop) for the details.

To run in the foreground against a specific config:

```bash
source venv/bin/activate
python diskwatcher.py --config config/mu2edaq-diskwatcher.yaml --port 5002
```

Three equivalent entry points exist: `python diskwatcher.py`,
`python -m mu2edaq_diskwatcher`, and the `mu2edaq-diskwatcher` console script
installed by `pip install -e .`.

## Configuration

The YAML file has four top-level keys: `watcher`, `files`, `paths` and
`peers`. Full reference in `man 5 mu2edaq-diskwatcher.conf`; the shipped
`config/mu2edaq-diskwatcher.yaml` is heavily commented and doubles as a
worked example.

```yaml
watcher:
  web_host:      "0.0.0.0"
  web_port:      5002
  poll_interval: 30          # seconds between polls
  default_delay: 300         # staleness fallback for entries that omit delay:
  daemon:        false
  run_dir:       "run/{host}"                        # per node; see below
  pid_file:      "{run_dir}/mu2edaq-diskwatcher.pid"
  log_file:      "{run_dir}/mu2edaq-diskwatcher.log"

files:                       # regular files
  - path:  /data/mu2e/run_latest.dat
    delay: 120               # alarm if not written within 2 minutes
    label: "Latest run data"
    size:                    # enables the File Sizes page for this entry
      warning:  "1 GiB"
      critical: "4 GiB"
      full:     "8 GiB"
      allow_empty: false     # default: a zero-length file is an alarm

paths:                       # directories
  - path:  /data/mu2e
    delay: 300
    label: "Main data"
    space:                   # enables the Disk Space page for this entry
      warning:  "10%"        # percent of capacity still FREE
      critical: "500 GiB"
      full:     "50 GiB"
```

Remote paths take an `ssh:` block; only `host` is required:

```yaml
    ssh:
      host:    daq-node01.example.com
      port:    22
      key:     ~/.ssh/id_ed25519
      timeout: 10
      options: [StrictHostKeyChecking=no]
```

### Threshold syntax

Both `space:` and `size:` thresholds take a number with an optional
case-insensitive unit. A bare number means bytes.

| Suffix | Multiplier | | Suffix | Multiplier |
|---|---|---|---|---|
| *(none)*, `B` | 1 | | `KiB` | 1024 |
| `KB`, `K` | 1000 | | `MiB` | 1024² |
| `MB`, `M` | 1000² | | `GiB` | 1024³ |
| `GB`, `G` | 1000³ | | `TiB` | 1024⁴ |
| `TB`, `T` | 1000⁴ | | `PiB` | 1024⁵ |
| `N%` | percent — `space:` only | | | |

So `500 GiB` is 536 870 912 000 bytes and `500 GB` is 500 000 000 000. The
dashboards always *display* IEC units, so a threshold written `2TB` is shown as
`1.8 TiB` — the same quantity, written the other way round.

In a `space:` block a percentage means **percent of total capacity still free**:
`warning: "10%"` fires once free space drops to a tenth of the filesystem. A
file has no total capacity, so a percentage in a `size:` block is rejected —
that one threshold is dropped with a warning and the rest of the block keeps
working.

### Alarm states

**Free space** — thresholds say how much must remain *free*, so they descend
(`warning > critical > full`):

`GOOD` → `WARNING` (free ≤ warning) → `CRITICAL` → `FULL`, plus `MISSING` (the
directory could not be read) and `UNKNOWN` (it exists but the filesystem could
not be measured).

**File size** — thresholds say how large a file may grow, so they ascend
(`warning < critical < full`):

`GOOD` → `WARNING` (size ≥ warning) → `CRITICAL` → `FULL`, plus `EMPTY` for a
zero-length file and `MISSING`. `EMPTY` is checked *before* any threshold, so a
truncated file alarms whatever its limits say; `allow_empty: true` suppresses
it. A block containing only `allow_empty: false` is valid and means "alarm if
this file is ever zero bytes".

Every threshold is optional, and states are evaluated most-severe-first, so any
subset works. Thresholds that do not escalate in the expected order are
reported at startup and flagged on the dashboard, but never silently corrected.

### `delay:` is optional

| In the entry | Result |
|---|---|
| `delay: N` | staleness threshold of N seconds |
| `delay: null` | staleness not monitored |
| absent, but `space:`/`size:` present | staleness not monitored |
| absent, nothing else | `watcher.default_delay` (300 s) |

The last row preserves pre-1.2.0 behaviour, so an existing config keeps
alarming exactly as before. Entries with no staleness threshold show as
`unmonitored` on the Watcher page and are excluded from the OK/STALE counts.

### Shared checkouts: per-node runtime files

The checkout is usually on NFS, shared by several DAQ nodes that each run
their own diskwatcher. Everything a copy writes therefore goes in a per-node
run directory, `watcher.run_dir`, default `run/{host}` where `{host}` is the
short hostname. The pid file and log default to `mu2edaq-diskwatcher.pid` and
`.log` inside it (daemon mode only; a foreground run still logs to the terminal
and writes no pid file). Any history or database a future version keeps
belongs there too. `run/` is git-ignored.

`run_dir`, `pid_file` and `log_file` accept placeholders, expanded once at
startup: `{host}`, `{hostname}`, `{port}`, `{user}`, and in the two files
`{run_dir}`. An unknown placeholder is left visible rather than silently
dropped. Overrides: `--run-dir`, `MU2EDAQ_DISKWATCHER_RUN_DIR`.

The start and stop scripts use the same directory (`./run/<short hostname>`,
or `DW_RUN_DIR`, or `--run-dir`) and pass it to the daemon, so what the script
looks for is always what the daemon wrote. Both still read the pre-1.3.0
`./diskwatcher.pid`, so a daemon started by an older release is found and
replaced. The process-table sweep needs no such care: `ps` only ever sees the
node it runs on.

```bash
./start-mu2edaq-diskwatcher.sh                              # run/<host>/...
./start-mu2edaq-diskwatcher.sh --run-dir /var/run/dw         # node-local disk
python diskwatcher.py --daemon --run-dir '/var/run/dw-{host}-{port}'
```

### Peers: aggregating other instances

Every DAQ node runs its own diskwatcher against its own disks. A `peers:` list
lets one instance also show what the others are watching:

```yaml
watcher:
  peer_timeout:  5             # seconds per fetch (default 5)
  #peer_interval: 30           # seconds between fetches (default: poll_interval)

peers:
  - url:   http://mu2e-dl-01.fnal.gov:5002
    label: "mu2e-dl-01"        # default: host:port
  - mu2e-dl-02.fnal.gov:5002   # bare form; scheme optional
```

Each peer's `/api/state` is fetched on a background thread, concurrently and
with a timeout, so a dead peer never delays the local poll or the web server.
The peer's entries then appear on all three dashboards as their own collapsible
group after the local host groups, headed by the peer's label, its URL (linking
to the same page there), the hostname and version it reports, and how old the
data is. The stat cards count local entries plus every reachable peer, and a
strip under the cards shows one chip per peer.

Three deliberate choices:

- **States are the peer's own.** It evaluated its thresholds against disks it
  can see; this instance displays the result and never recomputes it.
- **Aggregation is one hop.** A peer is asked for its own entries only, so two
  instances may list each other without looping. An instance recognises its
  own `instance_id` and refuses a peer URL that resolves back to itself.
- **An outage dims, it does not erase.** While a peer is unreachable its last
  good rows stay on screen, greyed and italic, with the error and how long ago
  the data was current. They are excluded from every count. A peer's problem
  does not degrade this instance's `/api/health`.

The list can be replaced without editing the file:

```bash
MU2EDAQ_DISKWATCHER_PEERS="dl-01:5002 dl-02:5002" python diskwatcher.py
python diskwatcher.py --peer dl-01:5002 --peer http://dl-02:5002
python diskwatcher.py --no-peers            # ignore every configured peer
```

`--peer-timeout` and `--peer-interval` (or `PEER_TIMEOUT` / `PEER_INTERVAL` in
the environment) override the two `watcher` keys. Connection status is on the
Config page and at `/api/peers`.

### Precedence

```
command line  >  environment  >  config file  >  built-in defaults
```

Environment overrides are named `MU2EDAQ_DISKWATCHER_*`: `CONFIG`, `WEB_HOST`,
`WEB_PORT`, `POLL_INTERVAL`, `DEFAULT_DELAY`, `DAEMON`, `RUN_DIR`, `PID_FILE`,
`LOG_FILE`, `VERBOSE`, `PEERS`, `PEER_TIMEOUT`, `PEER_INTERVAL`. An unparseable value is
warned about and ignored. The peer list is layered too: `--peer` replaces
`PEERS`, which replaces the file's `peers:`.

## Pages

| Path | Contents |
|---|---|
| `/` | Watcher — every entry, with mtime, age and staleness threshold |
| `/space` | Disk Space — directories with a `space:` block |
| `/sizes` | File Sizes — files with a `size:` block |
| `/config` | Settings in effect, parsed watch list, config problems, raw YAML |
| `/api` | Reference for every JSON endpoint and entry field |
| `/about` | Version, uptime, host, dependency versions |
| `/sitemap` | Structure of the application |

The three dashboards poll their own JSON endpoint at a selectable interval
(default 5 s), remembered across pages.

## JSON API

| Endpoint | Returns |
|---|---|
| `/api/status` | All entries, original flat shape — see compatibility below |
| `/api/state` | Everything: entries plus watch/space/size summaries |
| `/api/space` | Directories with a `space:` block, plus a summary |
| `/api/sizes` | Files with a `size:` block, plus a summary |
| `/api/entries` | Filtered list: `?kind=`, `?monitored=`, `?state=`, `?peer=` |
| `/api/peers` | Connection status of every configured peer, without entries |
| `/api/config` | Active settings, parsed watch list, peer list, config problems |
| `/api/health` | Poller liveness; `degraded` if stalled or misconfigured; peer counts |
| `/api/version` | Name, version, `instance_id`, hostname |

The first five return this instance's own entries by default. Add `?peers=1`
and `/api/status`, `/api/state`, `/api/space` and `/api/sizes` gain `peers`
(one record per configured peer: connection details, status, that peer's
entries filtered as the endpoint filters local ones, and their summary) and
`aggregate` (the summary over local plus reachable peers). `/api/entries?peers=1`
folds reachable peers' entries into the flat list, each stamped `peer` and
`peer_url`. Without the flag the payloads are unchanged, which is also what
keeps federation to one hop: a peer fetching us gets our entries and nothing
further away.

```bash
# every volume in a CRITICAL or FULL state
curl -s localhost:5002/api/space | python3 -c \
  'import json,sys; print([e["path"] for e in json.load(sys.stdin)["entries"]
   if e["space_state"] in ("CRITICAL","FULL")])'

# every zero-length file
curl -s "localhost:5002/api/entries?state=EMPTY"

# is the poller alive?
curl -s localhost:5002/api/health | python3 -m json.tool

# which peers are unreachable?
curl -s localhost:5002/api/peers | python3 -c \
  'import json,sys; print([p["label"] for p in json.load(sys.stdin)["peers"]
   if p["status"] != "ok"])'

# every CRITICAL volume here or on any reachable peer
curl -s "localhost:5002/api/entries?peers=1&monitored=space&state=CRITICAL"
```

Each entry carries a numeric `*_rank` alongside its state — sort on that rather
than re-deriving a severity ordering from the state names — plus `*_trigger`
(which threshold fired) and `*_reason` (a human sentence, e.g.
`"93.1 GiB free ≤ critical 465.7 GiB"`).

**Compatibility.** `/api/status` always returns `files`, `total`, `stale`, `ok`
and `poll_interval`, and every element of `files` keeps the twenty keys it
carried before free-space and file-size monitoring existed. Keys are added,
never removed or retyped. The one documented exception: `delay` may now be
`null` for an entry that opts out of staleness monitoring. A test locks this.
New integrations should prefer `/api/state`.

## Start and stop

```bash
./start-mu2edaq-diskwatcher.sh                       # default config
./start-mu2edaq-diskwatcher.sh config/test.yaml      # positional (crs-app form)
./start-mu2edaq-diskwatcher.sh -c config/test.yaml   # or -c / --config / --config=
./start-mu2edaq-diskwatcher.sh -p 5010               # or --port / --port=
./start-mu2edaq-diskwatcher.sh --no-replace          # refuse if already running
./stop-mu2edaq-diskwatcher.sh
```

Unrecognised options are forwarded to `diskwatcher.py` unchanged, so
`./start-mu2edaq-diskwatcher.sh --verbose` works.

The start script runs the daemon with `--run-dir ./run/<host>` and
`--pid-file ./run/<host>/mu2edaq-diskwatcher.pid`, which override the YAML.
`--run-dir DIR` and `--pid-file FILE` change both halves together — where the
script looks for a running copy, and where the daemon it starts writes — so the
next start reads the file the last one wrote.

**Only one copy runs at a time.** Before starting, the script looks for a copy
already running out of its own directory and stops it. Without that, a second
start would leave the first daemon holding the port while the new one died on
bind — a restart that silently isn't one. `--no-replace` inverts the choice: it
exits 1 rather than displacing the running instance.

Both scripts identify a running copy two ways, because either alone has a blind
spot:

| Source | Catches | Misses |
|---|---|---|
| PID file | the daemon the last start recorded | one started by hand, or whose PID file was deleted |
| Process table | any copy with this directory as its cwd | a copy started from elsewhere |

A PID is only acted on once its command line confirms it really is this
application. PID numbers get recycled, so a file left behind by a `SIGKILL`ed
daemon eventually names some unrelated process — `kill -0` would say it exists,
and signalling it would hit a stranger. Matching also requires the process's
working directory to equal the script's own, so two checkouts on one host do not
stop each other.

Shutdown is `SIGTERM`, then `SIGKILL` after `CRS_STOP_TIMEOUT` seconds
(default 10), bounding how long a wedged daemon can block a restart.

The shared discovery logic lives in `lib/diskwatcher-proc.sh`, sourced by both
scripts. It uses `lsof` to read another process's working directory, since
`/proc` is Linux-only; if `lsof` is missing, process-table candidates are
skipped rather than killed on a guess, and the PID-file path still works.

## Control-room integration

`crs-app start diskwatcher` runs `start-mu2edaq-diskwatcher.sh`, which reads
`CRS_PORT_HTTP` (default 5002) and forwards it as `--port`, then runs in daemon
mode with a PID file. The first positional argument overrides the config path.
`crs-app stop diskwatcher` runs the stop script.

If `mu2edaq-discovery` is installed, the app advertises its HTTP port so it
appears in `mu2edaq-discover` scans. It is not on PyPI; `bootstrap_diskwatcher.sh`
installs it from a sibling checkout when one exists. Its absence is harmless.

## Development

```bash
source venv/bin/activate
pip install -e . -r requirements-dev.txt
pytest
```

The test suite covers the threshold parser and state evaluators (pure
functions, where all the alarm logic lives), the config loader's fallback and
validation rules, poller state-key parity between the success and failure
paths, SSH command construction and probe parsing with `subprocess` stubbed,
the peer client against a real loopback HTTP server (refused, timed out, HTTP
error, not JSON, not a diskwatcher, oversized, self-reference, retained data
on failure), and a Flask test-client pass over every route with and without
`?peers=1`. `tests/test_scripts.py` drives
the start and stop scripts against real processes, including the case that must
*not* happen: a stale PID file naming a recycled PID never gets an unrelated
process signalled.

### Layout

```
diskwatcher.py                     entry-point shim (control room runs this)
pyproject.toml                     packaging; installs the console script
CHANGELOG.md                       what changed in each release
config/mu2edaq-diskwatcher.yaml    configuration, heavily commented
run/<host>/                        per-node pid file and log (git-ignored)
lib/diskwatcher-proc.sh            process discovery shared by start and stop
man/                               mu2edaq-diskwatcher.1, .conf.5
tests/                             pytest suite
src/mu2edaq_diskwatcher/
    settings.py    process-wide settings singleton (defaults→YAML→env→CLI)
    state.py       StateStore holding the latest poll results, plus summaries
    thresholds.py  threshold parsing and state evaluation — pure functions
    config.py      YAML loading; watch-entry and peer construction
    ssh.py         one-round-trip remote probe, with a stat(1) fallback
    poller.py      the polling thread
    peers.py       the peer-fetch thread: urllib GET of each peer's /api/state
    formatting.py  fmt_duration / fmt_bytes / fmt_pct
    daemon.py      double-fork daemonisation and PID files
    discovery.py   best-effort service-discovery responder
    cli.py         argument parsing and startup
    web/           Flask app factory, views, JSON API, templates, static
```

Notes for anyone extending it:

- Add user-visible changes to `CHANGELOG.md` under `[Unreleased]` as you make
  them, not at release time. Anything that changes how an existing config
  behaves, or what the JSON API returns, belongs under **Compatibility**.
- `settings.get_settings()` must be called *inside* functions, never bound at
  module scope — the CLI mutates the singleton after import.
- `poller._null_state()` defines every key the API can emit. Add new fields
  there so the success and error paths stay identical; `STATE_KEYS` and a test
  enforce it. `state.peer_record()` plays the same role for peer records.
- Peer entries are shown as the peer evaluated them. Do not "improve" them by
  re-running the threshold code here; the peer's config is not available and
  the two dashboards would disagree.
- `/api/state` without `?peers=1` is what a peer fetches from us. Keep the
  default local-only, or federation stops being one hop.
- The navbar is defined once, in `web/nav.py`.
- Python 3.9 compatibility is maintained: use `Optional[X]`, not `X | None`.

## Requirements

Python 3.9+, Flask 3.0+, PyYAML 6.0+. Jinja2 arrives with Flask. Bootstrap
5.3.3 and Bootstrap Icons are loaded from jsDelivr — on a host with no outbound
internet access the pages still work but render unstyled.

Runs on Linux, macOS and Windows. Daemon mode (`--daemon`) requires `fork(2)`
and is POSIX-only; on Windows run in the foreground under a service wrapper.

## Changes

[CHANGELOG.md](CHANGELOG.md) tracks what changed in each release, including
which changes affect an existing config or an integration reading the JSON API.

## License

MIT. See [LICENSE](LICENSE).
