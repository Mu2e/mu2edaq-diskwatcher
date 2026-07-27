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
followed by a sortable table. Local and remote (SSH) paths are both supported.

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

The YAML file has three top-level keys: `watcher`, `files` and `paths`. Full
reference in `man 5 mu2edaq-diskwatcher.conf`; the shipped
`config/mu2edaq-diskwatcher.yaml` is heavily commented and doubles as a
worked example.

```yaml
watcher:
  web_host:      "0.0.0.0"
  web_port:      5002
  poll_interval: 30          # seconds between polls
  default_delay: 300         # staleness fallback for entries that omit delay:
  daemon:        false
  pid_file:      "./config/mu2edaq-diskwatcher.pid"
  log_file:      "./config/mu2edaq-diskwatcher.log"

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

### Precedence

```
command line  >  environment  >  config file  >  built-in defaults
```

Environment overrides are named `MU2EDAQ_DISKWATCHER_*`: `CONFIG`, `WEB_HOST`,
`WEB_PORT`, `POLL_INTERVAL`, `DEFAULT_DELAY`, `DAEMON`, `PID_FILE`, `LOG_FILE`,
`VERBOSE`. An unparseable value is warned about and ignored.

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
| `/api/entries` | Filtered list: `?kind=`, `?monitored=`, `?state=` |
| `/api/config` | Active settings, parsed watch list, config problems |
| `/api/health` | Poller liveness; `degraded` if stalled or misconfigured |
| `/api/version` | Name and version |

```bash
# every volume in a CRITICAL or FULL state
curl -s localhost:5002/api/space | python3 -c \
  'import json,sys; print([e["path"] for e in json.load(sys.stdin)["entries"]
   if e["space_state"] in ("CRITICAL","FULL")])'

# every zero-length file
curl -s "localhost:5002/api/entries?state=EMPTY"

# is the poller alive?
curl -s localhost:5002/api/health | python3 -m json.tool
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

The start script runs the daemon with `--pid-file ./diskwatcher.pid`, which
overrides `watcher.pid_file` in the YAML. `--pid-file FILE` changes both halves
together — where the script looks for a running copy, and where the daemon it
starts writes — so the next start reads the file the last one wrote.

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
and a Flask test-client pass over every route. `tests/test_scripts.py` drives
the start and stop scripts against real processes, including the case that must
*not* happen: a stale PID file naming a recycled PID never gets an unrelated
process signalled.

### Layout

```
diskwatcher.py                     entry-point shim (control room runs this)
pyproject.toml                     packaging; installs the console script
config/mu2edaq-diskwatcher.yaml    configuration, heavily commented
lib/diskwatcher-proc.sh            process discovery shared by start and stop
man/                               mu2edaq-diskwatcher.1, .conf.5
tests/                             pytest suite
src/mu2edaq_diskwatcher/
    settings.py    process-wide settings singleton (defaults→YAML→env→CLI)
    state.py       StateStore holding the latest poll results, plus summaries
    thresholds.py  threshold parsing and state evaluation — pure functions
    config.py      YAML loading and watch-entry construction
    ssh.py         one-round-trip remote probe, with a stat(1) fallback
    poller.py      the polling thread
    formatting.py  fmt_duration / fmt_bytes / fmt_pct
    daemon.py      double-fork daemonisation and PID files
    discovery.py   best-effort service-discovery responder
    cli.py         argument parsing and startup
    web/           Flask app factory, views, JSON API, templates, static
```

Notes for anyone extending it:

- `settings.get_settings()` must be called *inside* functions, never bound at
  module scope — the CLI mutates the singleton after import.
- `poller._null_state()` defines every key the API can emit. Add new fields
  there so the success and error paths stay identical; `STATE_KEYS` and a test
  enforce it.
- The navbar is defined once, in `web/nav.py`.
- Python 3.9 compatibility is maintained: use `Optional[X]`, not `X | None`.

## Requirements

Python 3.9+, Flask 3.0+, PyYAML 6.0+. Jinja2 arrives with Flask. Bootstrap
5.3.3 and Bootstrap Icons are loaded from jsDelivr — on a host with no outbound
internet access the pages still work but render unstyled.

Runs on Linux, macOS and Windows. Daemon mode (`--daemon`) requires `fork(2)`
and is POSIX-only; on Windows run in the foreground under a service wrapper.

## License

MIT. See [LICENSE](LICENSE).
