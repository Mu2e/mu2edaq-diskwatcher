# Changelog

All notable changes to `mu2edaq-diskwatcher`.

Entries are grouped by release. Each release names both the code version
(`__version__` in `src/mu2edaq_diskwatcher/__init__.py`, reported by
`--version`, `/about` and `/api/version`) and the repository tag the DAQ
release process uses (`tNN.NN.NN`), since the two number differently.

Changes that alter behaviour an operator or an integration depends on are
called out as **Breaking** or **Compatibility**; anything not so marked is
additive.

## [Unreleased] — 1.3.0

Not yet tagged. On branch `feature/peer-federation`.

### Added

- **Peer federation.** A `peers:` list in the YAML names other diskwatcher
  instances. Each peer's `/api/state` is fetched over HTTP on its own daemon
  thread, concurrently and with a per-fetch timeout, so a dead peer never
  delays the local poll or the web server. The peer's entries then appear on
  the Watcher, Disk Space and File Sizes pages as one collapsible group per
  peer, after the local host groups, headed by the peer's label, its URL
  (linking to the same page on the peer), the hostname and version it reports,
  and how old the data is. The stat cards count local entries plus every
  reachable peer; a strip under the cards shows one chip per peer.

  Each item takes `url` (required; scheme optional, `http` assumed), and
  optional `label` (default `host:port`), `timeout` and `enabled`; a bare URL
  string is accepted. Two `watcher` keys, `peer_timeout` (default 5 s) and
  `peer_interval` (default: the poll interval), set the defaults. Bad items
  are dropped with a warning and reported on `/config`, like watch entries.

  Three decisions worth knowing about:

  - *States are the peer's own.* A peer evaluated its thresholds against a
    filesystem it can see; this instance shows the result and does not
    recompute it. Recomputing would need the peer's config and the two
    dashboards would disagree.
  - *Aggregation is one hop.* A peer is asked for its own entries only (its
    `/api/state` without `?peers=1`), so two instances may list each other
    without looping and nothing appears twice. Every instance now publishes a
    random `instance_id`; a peer whose URL resolves back to this process is
    recognised by it and refused with a clear error rather than duplicating
    the local paths under a "remote" heading.
  - *An outage dims, it does not erase.* While a peer is unreachable its rows
    from the last good fetch stay on screen, greyed and italic, with the error
    and how long ago the data was current, and its group is held open. Those
    rows are excluded from every count and every `aggregate`. A control-room
    display that blanks a whole node's volumes because one request timed out
    is worse than one that says "unreachable since 14:02".
- **`--peer URL` (repeatable), `--no-peers`, `--peer-timeout`,
  `--peer-interval`**, and the environment variables
  `MU2EDAQ_DISKWATCHER_PEERS` (comma- or space-separated), `PEER_TIMEOUT`,
  `PEER_INTERVAL`. The peer list is layered like every other setting, but as
  a whole: `--peer` replaces `PEERS`, which replaces the file's `peers:`. A
  `PEERS` value containing one bad URL is ignored entirely, matching the
  all-or-nothing rule every other override follows.
- **`?peers=1`** on `/api/status`, `/api/state`, `/api/space`, `/api/sizes`
  and `/api/entries`. The first four gain `peers` (one record per configured
  peer: connection details, `status` of `ok`/`error`/`pending`/`disabled`,
  `error`, fetch timing, `stale`, the peer's entries filtered as the endpoint
  filters local ones, and their `summary`) and `aggregate` (the endpoint's
  summary over local plus reachable peers, with `local` and `peers` counts).
  `/api/entries?peers=1` folds reachable peers' entries into the flat list,
  each stamped `peer` and `peer_url`, and `&peer=LABEL` keeps one peer's.
- **`/api/peers`**: connection status of every configured peer, without
  entries, plus per-peer watch/space/size summaries and a `counts` total.
- **`hostname` and `instance_id`** on `/api/state` and `/api/version`;
  `instance_id` and a `peers` count dict on `/api/health`; `peers` and
  `peer_timeout` on `/api/config`; `alert_rank` on `/api/status` (the Watcher
  page's default-open threshold, `stale`), all additive.
- **Peers table on `/config`** with live status, and a peer count on
  `/about`.
- **Host grouping on the Watcher page.** `/` now groups its Files and
  Directories tables by host the way `/space` and `/sizes` already did, which
  is what gives the peer groups a place to go. A group opens by default once
  something in it is `stale` or `missing`.
- **Per-node runtime files for checkouts shared over NFS.** The checkout is
  usually one NFS directory mounted on every DAQ node, each running its own
  diskwatcher. Until now every node wrote the same `./diskwatcher.pid`, and
  the shipped configs pointed every node at the same `config/*.log`, so a
  start on one node read the pid another had written and logs interleaved.
  A new `watcher.run_dir` (default `run/{host}`, `{host}` the short hostname)
  is the per-node home for everything the process writes; `pid_file` and
  `log_file` default to `mu2edaq-diskwatcher.pid` / `.log` inside it, in
  daemon mode only. `run_dir`, `pid_file` and `log_file` accept `{host}`,
  `{hostname}`, `{port}`, `{user}` and (the two files) `{run_dir}`, expanded
  once after every config layer so `{port}` sees the final port; an unknown
  placeholder is left visible on `/config` rather than dropped. Parent
  directories are created on demand. `--run-dir` and
  `MU2EDAQ_DISKWATCHER_RUN_DIR` override it. The Config page shows the node,
  working directory and resolved run directory. Any history or database a
  future version keeps belongs in `run_dir`; nothing of the kind exists yet.
- **Start and stop scripts use the same per-node directory**
  (`./run/<short hostname>`, or `DW_RUN_DIR`, or `--run-dir`) and pass it to
  the daemon as `--run-dir`, so what the script looks for is what the daemon
  wrote. Both still read the pre-1.3.0 `./diskwatcher.pid`, so upgrading
  over a running daemon replaces it, and the stop script removes a stale
  legacy file only when it names nothing alive on this node — on a shared
  checkout it may belong to another node's daemon. A new `dw_node` helper in
  `lib/diskwatcher-proc.sh` yields the short hostname with fallbacks.
- **`tests/test_peers.py`**, driving the client against a real loopback HTTP
  server: refused, timed out, HTTP error, not JSON, not a diskwatcher payload,
  oversized, self-reference, retained data across a failure and recovery,
  record-shape parity across every state. `tests/test_web.py` gains a
  `federated` fixture and locks the `?peers=1` contract, including that the
  default payloads are byte-for-byte local-only.

### Fixed

- **The start and stop scripts could mistake any Python process for the
  daemon if its interpreter path contained the checkout's name.** The
  process-table matcher looked for `mu2edaq-diskwatcher` anywhere on the
  command line, and the checkout is called exactly that, so
  `../mu2edaq-diskwatcher/venv/bin/python -m pytest` run from a git worktree
  matched on its executable alone and was SIGTERMed by the stop script it was
  testing. The name is now looked for in the arguments only, and the
  interpreter is checked separately. Two tests in `tests/test_scripts.py`
  lock both directions: a `python` reached through such a path is left alone,
  and the three genuine spellings are still stopped.

### Removed

- **`start_diskwatcher.sh` and `stop_diskwatcher.sh`**, the pre-`t00.01.00`
  script names. They had been symlinks to the standardised
  `start-mu2edaq-diskwatcher.sh` / `stop-mu2edaq-diskwatcher.sh` since that
  release; `crs-app` has used the hyphenated names throughout. Anything still
  calling the old names must switch. A test asserts they stay gone.

### Compatibility

- Without `?peers=1` every JSON payload is unchanged apart from added keys:
  `alert_rank` on `/api/status`; `hostname`, `instance_id` on `/api/state`
  and `/api/version`; `peers`, `instance_id` on `/api/health`; `peers`,
  `peer_timeout`, `peer_interval` on `/api/config`. Nothing was removed or
  retyped. `/api/status` counts (`total`, `ok`, `stale`) stay local-only even
  with the flag.
- An instance with no `peers:` behaves and renders exactly as before, except
  that the Watcher page now shows a `local` group header row like the other
  two dashboards.
- **`/api/health` `status` is unaffected by peers.** An unreachable peer is a
  fact about that peer, answered by its own `/api/health`; it is reported here
  under `peers` for anyone who wants to alarm on it.
- **The pid file moved.** The start script used to write `./diskwatcher.pid`;
  it now writes `./run/<short hostname>/mu2edaq-diskwatcher.pid`, and a
  `--daemon` run with no `pid_file`/`log_file` configured now writes both
  defaults there instead of writing no pid file and discarding output. A
  foreground run is unchanged. Anything reading the old path should use
  `/api/health` or the new location; the old file is still honoured on stop.
  Configs that set `pid_file`/`log_file` explicitly keep working as written,
  but on a shared checkout they should be changed to the `{run_dir}` form
  the shipped configs now use.
- `run/` is git-ignored alongside the legacy `diskwatcher.pid`.

## [t01.00.00] — 1.2.0 — 2026-07-28

Tagged `t00.04.00` at commit `1b9e60c` and `t01.00.00` at `7902c9d` (the host
grouping landed between the two).

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
- **Host grouping on `/space` and `/sizes`** (`t01.00.00` only). Rows are
  grouped under a header per host — local paths first, then remote hosts
  alphabetically — and each group collapses on click. A collapsed group still shows one badge per alarm
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
- **Host configuration `config/mu2e-diskwatcher-dl-01.yaml`** for the
  `mu2e-dl-01` data-logger node: free-space monitoring on `/data`, `/daqlogs`,
  `/scratch`, `/home`, `/var`, `/var/log` and `/tmp`, and size monitoring on
  `/var/log/messages` and `/var/log/secure`. All entries are host-local (no
  `ssh:` blocks); the two system logs set `allow_empty: true` so logrotate
  truncation does not alarm, with a `delay:` on `messages` to catch a dead
  rsyslog instead.

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
