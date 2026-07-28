# Changelog

All notable changes to `mu2edaq-diskwatcher`.

Entries are grouped by release. Each release names both the code version
(`__version__` in `src/mu2edaq_diskwatcher/__init__.py`, reported by
`--version`, `/about` and `/api/version`) and the repository tag the DAQ
release process uses (`tNN.NN.NN`), since the two number differently.

Changes that alter behaviour an operator or an integration depends on are
called out as **Breaking** or **Compatibility**; anything not so marked is
additive.

## [Unreleased] — 1.2.0

Not yet tagged. On `main` as of commit `6f4bdb6` (2026-07-27).

### Added

- **Free-space monitoring.** An optional `space:` block on a `paths:` entry
  alarms on the filesystem holding that directory. States are `GOOD` →
  `WARNING` → `CRITICAL` → `FULL`, plus `MISSING` (directory unreadable) and
  `UNKNOWN` (exists, but the filesystem could not be measured). Thresholds say
  how much must remain *free*, so they descend.
- **File-size monitoring.** An optional `size:` block on a `files:` entry
  alarms on file size. `GOOD` → `WARNING` → `CRITICAL` → `FULL`, plus `EMPTY`
  for a zero-length file and `MISSING`. `EMPTY` is checked before any
  threshold, so a truncated file alarms whatever its limits say;
  `allow_empty: true` suppresses it. Thresholds ascend.
- **Threshold syntax** accepting SI (`500 GB`) and IEC (`500 GiB`) units, bare
  byte counts, and — in `space:` blocks only — a percentage of total capacity
  still free. A percentage in a `size:` block is rejected with a warning; the
  rest of the block keeps working.
- **`/space` and `/sizes` dashboards**, each with state-count cards, a sortable
  table and its own JSON feed.
- **`/api` and `/sitemap` pages**, documenting the endpoints and the structure
  of the application.
- **JSON endpoints**: `/api/state`, `/api/space`, `/api/sizes`,
  `/api/entries` (filterable by `kind`, `monitored`, `state`), `/api/config`,
  `/api/health` and `/api/version`.
- **`watcher.default_delay`** (default 300 s), the staleness fallback for
  entries that omit `delay:`.
- **Environment overrides** named `MU2EDAQ_DISKWATCHER_*`, sitting between the
  config file and the command line: `CONFIG`, `WEB_HOST`, `WEB_PORT`,
  `POLL_INTERVAL`, `DEFAULT_DELAY`, `DAEMON`, `PID_FILE`, `LOG_FILE`,
  `VERBOSE`. An unparseable value is warned about and ignored.
- **Single-instance start.** `start-mu2edaq-diskwatcher.sh` stops any copy
  already running out of its own directory before launching. Without it, a
  second start left the first daemon holding the port while the new one died on
  bind — a restart that silently was not one. `--no-replace` refuses to start
  instead. Shutdown is `SIGTERM` then `SIGKILL` after `CRS_STOP_TIMEOUT`
  seconds (default 10).
- **`lib/diskwatcher-proc.sh`**, the process-discovery logic shared by the start
  and stop scripts.
- **`--pid-file`, `-c/--config`, `-p/--port` and `--no-replace`** on the start
  script; unrecognised options are still forwarded to `diskwatcher.py`.
- **`README.md`**, **`man/mu2edaq-diskwatcher.conf.5`** (the YAML schema) and a
  rewritten **`man/mu2edaq-diskwatcher.1`**.
- **`pyproject.toml`**, installing a `mu2edaq-diskwatcher` console script. Three
  entry points now work: `python diskwatcher.py`,
  `python -m mu2edaq_diskwatcher`, and the console script.
- **`tests/`** — a pytest suite covering the threshold parser and state
  evaluators, the config loader, poller state-key parity, SSH command
  construction, every route, the CLI, and the start/stop scripts driven against
  real processes.
- **Host grouping on `/space` and `/sizes`.** Rows are grouped under a header
  per host — local paths first, then remote hosts alphabetically — and each
  group collapses on click. A collapsed group still shows one badge per alarm
  state inside it, so hiding a host never hides a problem. Sorting applies
  within each group.

  A group opens by default as soon as one of its entries reaches `CRITICAL` or
  worse (`FULL`, `UNKNOWN`, `MISSING`), and stays collapsed otherwise — `GOOD`,
  `EMPTY` and `WARNING` alone do not force it open, so a host with only minor
  concerns still folds down to one line, and only what genuinely needs
  attention stays on screen. The threshold is a new field on the API payload,
  `alert_rank`, resolved once server-side from the same severity table the
  states are ranked by, rather than a number copied into the JavaScript — move
  the line for what counts as an alarm and the dashboards follow without being
  touched. The comparison is against that numeric rank, never a state name,
  and an entry whose health cannot be established counts as needing attention
  — an unmeasurable filesystem opens its group rather than being quietly
  folded away.

  Clicking a header overrides the default for that host and that page, and the
  choice is remembered across refreshes, reloads and navigation. While an
  override is in place the group stops following its health, so a host pinned
  shut stays shut even if it later alarms; its header badges still report the
  state. Clearing the browser's site data resets every group to the default.
- **This changelog**, linked from the README, the man page `FILES` section and
  the `/about` page, and published as a browsable page for anyone without the
  source tree to hand. Two checks in `tests/test_packaging.py` catch it going
  stale: `__version__` must appear in a heading, and every released heading must
  name a real git tag.

### Changed

- **Package split.** The application moved from a single 1360-line
  `diskwatcher.py` into `src/mu2edaq_diskwatcher/`, with Jinja templates and a
  navbar defined once. `diskwatcher.py` remains as a shim, so
  `crs-app start diskwatcher` is unaffected.
- **`delay:` is now optional.** Resolution order: an explicit value wins;
  `delay: null` disables staleness monitoring; absent alongside a
  `space:`/`size:` block also disables it; absent with nothing else falls back
  to `watcher.default_delay`. That last case preserves pre-1.2.0 behaviour, so
  an existing config alarms exactly as before. Entries with no staleness
  threshold show as `unmonitored` and are excluded from the OK/STALE counts
  while still appearing in TOTAL.
- **Remote directories cost one SSH round trip instead of two**, and the probe
  no longer requires GNU `stat` — it works against macOS and BSD remotes, with
  a `stat` fallback cached per host for remotes lacking `python3`.
- **Module-level globals became a `Settings` singleton.** Across modules,
  `from .settings import X` binds a copy, so the previous `global` pattern
  could not survive the split.
- **Version unified at 1.2.0**, defined once in
  `src/mu2edaq_diskwatcher/__init__.py`. The man page had said 1.1.0 while the
  code said 1.0.0.
- **`.gitignore` no longer ignores `lib/`.** That rule came from the stock
  Python template (setuptools build output) and was silently swallowing
  `lib/diskwatcher-proc.sh`, which both scripts `source`.

### Fixed

- **`-c`/`--config` on the start script became the filename.** The script took
  the config positionally, so `-c FILE` passed the literal string `-c` to
  `diskwatcher.py`, which then failed with a misleading error. It now parses
  options properly and still accepts the positional form `crs-app` uses.
- **A CLI pre-parser blamed `--config` for every argument error**, so a bad
  `--port` reported `argument --config/-c: expected one argument`. Removed.
- **`MU2EDAQ_DISKWATCHER_CONFIG` could be reported as the active config even
  when `--config` won**, because it was the pre-parser's default rather than a
  precedence layer.
- **A stale PID file could get an unrelated process killed.** PID numbers are
  recycled, so a file left behind by a `SIGKILL`ed daemon eventually names some
  other process — `kill -0` reports it alive. Observed for real during
  development. A PID is now signalled only once its command line confirms it is
  this application, and a process-table candidate must also have the script's
  own directory as its working directory, so two checkouts on one host never
  stop each other. Locked by
  `tests/test_scripts.py::test_stale_pid_naming_a_live_stranger_is_not_killed`.
- **`--pid-file` on the start script changed discovery but not the daemon**,
  which still wrote the hardcoded `./diskwatcher.pid`, so the next start read
  the wrong file. Both halves now use the same path.
- **A per-poll `print()` spammed the daemon log** once per remote directory per
  poll cycle.
- **Directories whose filesystem could not be measured were silently treated as
  having no disk information.** That case is now `UNKNOWN`, distinct from
  `MISSING`.

### Compatibility

- `/api/status` is unchanged in shape: same top-level keys, and every element of
  `files` keeps the twenty keys it carried before this release. Keys are added,
  never removed or retyped. The one documented exception is that `delay` may now
  be `null`, for an entry that opts out of staleness monitoring. A test locks
  this. New integrations should prefer `/api/state`.
- `disk_pct` still means *used* percent. `disk_free_pct` is the new field.
- The start script now passes `--pid-file ./diskwatcher.pid`, which overrides
  `watcher.pid_file` from the YAML.
- `crs-app start diskwatcher` / `crs-app stop diskwatcher` and `CRS_PORT_HTTP`
  work as before.

## [t00.03.01] — 2026-07-24

- Made `bootstrap_diskwatcher.sh` fail loudly instead of continuing past a
  failed install, and fixed the `mu2edaq-discovery` dependency.
- Added the MIT `LICENSE`.
- Stopped tracking `venv/` and `__pycache__/`; added `.gitignore`, later
  extended to cover `diskwatcher.pid`.

## [t00.01.00-pre / t00.02.00-rc] — 2026-07-02

Both tags point at the same commit (`e4dfbc9`).

- Added `mu2edaq-discovery` support: the app advertises its HTTP port so it
  appears in `mu2edaq-discover` scans. Its absence is harmless.
- Standardised the start and stop script names and accepted `CRS_PORT_HTTP`,
  making `crs-app start diskwatcher` work.

## [t00.00.01-test] — 2026-04-21

First working version (code version 1.0.0; the man page said 1.1.0).

- Modification-time monitoring of files and directories, with per-entry
  `delay:` thresholds and OK / STALE / MISSING states.
- Web dashboard with separate Files and Directories sections, sortable columns,
  Status first, and a decorative disk-space bar on directories.
- `/config` page.
- Remote file and directory probes over SSH.
- `bootstrap`, start and stop scripts, with a PID file.
