---
title: mu2edaq-diskwatcher interfaces
group: monitoring
tier: A
audience: expert
summary: The HTTP/JSON peer-federation contract, YAML config schema, mu2edaq-discovery integration and SSH remote-read mechanism
---

# mu2edaq-diskwatcher — interfaces

Four external interfaces, in decreasing order of how much other software
depends on them:

1. **HTTP/JSON** — eight endpoints on 5002, one of which (`/api/state`) is a
   real inter-instance API consumed by other diskwatchers.
2. **YAML configuration** — the file schema.
3. **UDP service discovery** — optional, via `mu2edaq-discovery`.
4. **SSH** — outbound only, for remote watch entries.

---

## 1. HTTP: the peer-federation contract

### Port and binding

`5002/tcp`, bound to `0.0.0.0` by default
(`src/mu2edaq_diskwatcher/settings.py:136-137`). Overridable by
`watcher.web_port`, `$MU2EDAQ_DISKWATCHER_WEB_PORT`, `--port`, or
`$CRS_PORT_HTTP` (which `start-mu2edaq-diskwatcher.sh:26` forwards as
`--port`).

### The contract, precisely

A diskwatcher acting as an aggregator issues, once per `peer_interval` seconds,
to each enabled peer:

```
GET <peer_url>/api/state HTTP/1.1
Accept: application/json
User-Agent: mu2edaq-diskwatcher/<version>
```

Construction at `src/mu2edaq_diskwatcher/peers.py:70-73`. Note what is
**absent**: no `?peers=1`, no authentication, no TLS in any shipped config.

The reply must be a JSON object with an `entries` array; anything else is
rejected as `"response is not a diskwatcher /api/state payload"`
(`src/mu2edaq_diskwatcher/peers.py:97`).

#### `/api/state` response schema

| Key | Type | Meaning |
|---|---|---|
| `version` | string | the peer's `__version__` |
| `instance_id` | string | random 32-hex per-process id |
| `hostname` | string | `socket.gethostname()` on the peer |
| `generated` | string | `YYYY-MM-DDTHH:MM:SSZ` |
| `poll_interval` | int | the peer's configured poll cadence |
| `total` | int | number of entries |
| `summary` | object | `{watch, space, size}` sub-summaries |
| `entries` | array | one state dict per watched path |
| `last_poll`, `poll_age_s`, `poll_duration_s` | number/null | `STORE.meta()` |

Source: `src/mu2edaq_diskwatcher/web/api.py:141-161`.

#### Entry schema

The full key set is frozen as `STATE_KEYS`
(`src/mu2edaq_diskwatcher/poller.py:98`), built from `_null_state`. Groups:
identity (`path`, `label`, `kind`, `remote`, `ssh_host`), mtime
(`delay`, `mtime`, `age_s`, `stale`, `missing`, `error`, `watch_state`,
`watch_rank`), disk usage (`disk_total/used/free/pct/free_pct` plus `_str`
forms), free-space alarm (`space_state`, `space_rank`, `space_trigger`,
`space_reason`, `space_limits`, `space_limits_ordered`), size alarm
(`size`, `size_state`, `size_rank`, `size_trigger`, `size_reason`,
`size_limits`, `size_pct`, `size_pct_ref`), and `config_errors`.

A separate, narrower guarantee applies to `/api/status`: the 21 keys in
`LEGACY_ENTRY_KEYS` (`src/mu2edaq_diskwatcher/web/api.py:69-74`) keep their
names and types forever. Keys are added, never removed or retyped; the one
documented exception is that `delay` may now be `null`.
`tests/test_web.py::test_status_legacy_schema` locks this.

### One hop — the cycle-breaking design decision

**Verified in code, not merely asserted in a comment.**

The aggregation depth is exactly one because of the interaction of two facts:

- The fetch URL is `STATE_PATH = "/api/state"` with no query string
  (`src/mu2edaq_diskwatcher/peers.py:50`, `:71`).
- `/api/state` only appends `peers`/`aggregate` when `_want_peers()` returns
  true, and `_want_peers()` reads `request.args.get("peers")` against the
  truthy set `{"1","true","yes","on","all"}`
  (`src/mu2edaq_diskwatcher/web/api.py:76`, `:83`, `:161`).

So a peer's peers are structurally unreachable. A listens to B; B's reply
contains B's own entries only, even if B is itself aggregating C. The rationale
recorded at `src/mu2edaq_diskwatcher/peers.py:10-13` is that "A watching B
watching A never loops and never shows a path three times", and the
consequence — "Every dashboard shows exactly what its own config names: local
paths plus the paths of its direct peers" — is the actual UI contract.

Three further design points, all deliberate, all documented at
`src/mu2edaq_diskwatcher/peers.py:8-24`:

- **A peer's states are trusted, not re-derived.** The peer owns its thresholds
  and evaluated them against a filesystem it can see. Recomputing locally would
  require its config and would disagree with its own dashboard.
- **Failure keeps the last good data.** On a failed fetch the previous record's
  `entries`, `last_ok`, `version`, `hostname` and timing are carried forward
  (`src/mu2edaq_diskwatcher/peers.py:158-162`). `PeerStore.snapshot` then sets
  `stale = (status == "error" and entries non-empty)`
  (`src/mu2edaq_diskwatcher/state.py:185`), and `reachable_entries` excludes
  anything not in `ok` from every aggregate count
  (`src/mu2edaq_diskwatcher/state.py:211`). The UI dims those rows and shows
  when they were last current.
- **Self-reference is refused.** `payload["instance_id"] == INSTANCE_ID` raises
  `PeerError` (`src/mu2edaq_diskwatcher/peers.py:135`).

### Timeout and failure isolation

A slow peer **cannot** stall a page render. Three independent mechanisms:

1. **The web request never fetches.** Pages read `PEERS.snapshot()`, which is a
   lock-guarded in-memory copy (`src/mu2edaq_diskwatcher/state.py:165`). No
   Flask handler performs an outbound HTTP request.
2. **Peer fetching is a dedicated thread.** `peer_loop`
   (`src/mu2edaq_diskwatcher/peers.py:518`) runs separately from `poll_loop`,
   so a slow peer never delays a local stat either. `cli.main` deliberately
   never does a synchronous first peer poll
   (`src/mu2edaq_diskwatcher/cli.py:270-273`); until the first fetch lands the
   peer shows as pending.
3. **Peers are fetched concurrently and bounded.** A `ThreadPoolExecutor` with
   `max_workers=min(len(enabled), 10)` (`src/mu2edaq_diskwatcher/peers.py:486`)
   applies `urlopen(..., timeout=timeout)` per peer, defaulting to
   `peer_timeout` 5.0 s, overridable per peer
   (`src/mu2edaq_diskwatcher/peers.py:128`).

A response is capped at `MAX_RESPONSE_BYTES = 8 MiB`
(`src/mu2edaq_diskwatcher/peers.py:54`), read as `read(MAX+1)` and rejected if
oversized — so a hostile or broken responder cannot exhaust memory.

**Worst case, with defaults:** the aggregator has 28 peers and a pool of 10, so
a cycle in which every peer times out takes `ceil(28/10) * 5 s = 15 s`, against
a `peer_interval` of 30 s. It fits, but with under 2x headroom; at 60 peers, or
a `peer_timeout` raised past 10 s, consecutive cycles would overlap. Worth
noting in a runbook, not a defect today.

`urlopen`'s `timeout` is a per-socket-operation timeout, not a deadline for the
whole transfer, so a peer trickling bytes below the 8 MiB cap could hold a
worker longer than `peer_timeout`. With cooperating peers on a LAN this is
theoretical.

### Endpoint inventory

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | legacy; frozen shape. Entries + `stale`/`ok` counts |
| GET | `/api/state` | **the peer contract.** Everything: entries + all three summaries + identity |
| GET | `/api/space` | Disk Space feed: directories with a `space:` block |
| GET | `/api/sizes` | File Sizes feed: files with a `size:` block |
| GET | `/api/entries` | filtered list: `?kind=`, `?state=`, `?monitored=`, `?peer=` |
| GET | `/api/peers` | per-peer connection status and discovery diagnostics, without entries |
| GET | `/api/config` | active settings, parsed entries, peers, config issues |
| GET | `/api/health` | `ok`/`degraded` liveness for `crs-app` and external monitoring |
| GET | `/api/version` | name, version, `instance_id`, hostname |

`?peers=1` on `/api/status`, `/api/state`, `/api/space` and `/api/sizes` adds
two keys: `peers` (one record per peer, entries filtered the same way the
endpoint filters local ones) and `aggregate` (the summary recomputed over local
plus reachable-peer entries).

`/api/health` reports `degraded` when the poller has stalled
(`poll_age_s > 3 * poll_interval`) or any config issue exists
(`src/mu2edaq_diskwatcher/web/api.py:299`). An unreachable **peer** does not
degrade us — the peer's own `/api/health` answers for it.

All endpoints are **unauthenticated and read-only**. There is no POST, PUT or
DELETE route anywhere in the application. That is consistent with the
measure-only design, and it is why a diskwatcher is safe to expose to the
control-room browser; it is also why it must not be exposed off-site.

### HTML pages

| Path | Title | Content |
|---|---|---|
| `/` | Watcher | every entry, by mtime freshness: `ok` / `stale` / `missing` / `unmonitored` |
| `/space` | Disk Space | directories with a `space:` block, by free-space state |
| `/sizes` | File Sizes | files with a `size:` block, by size state |
| `/config` | Config | resolved settings, parsed entries, peers, discovery status, raw YAML |
| `/api` | API | human-readable endpoint documentation |
| `/sitemap` | Sitemap | page index |
| `/about` | About | versions, uptime, dependency list, counts |

The three dashboards render an empty shell and fill it from their own JSON
endpoint on a client-side interval (default 5 s, stored in `localStorage`), so
they stay live without a reload (`src/mu2edaq_diskwatcher/web/views.py:1-7`).
Config, API and About are server-rendered from settings that only change on
restart. Navigation is defined once in `src/mu2edaq_diskwatcher/web/nav.py`.

---

## 2. Configuration schema

Full reference: `man/man5/mu2edaq-diskwatcher.conf.5`. Structural summary:

```yaml
watcher:                     # scalar settings; all optional
  web_host: "0.0.0.0"
  web_port: 5002
  poll_interval: 30          # seconds between local polls
  default_delay: 300         # staleness fallback for entries omitting delay:
  daemon: false
  run_dir: "run/{host}"      # placeholders: {host} {hostname} {port} {user}
  pid_file: "{run_dir}/mu2edaq-diskwatcher.pid"   # +{run_dir}
  log_file: "{run_dir}/mu2edaq-diskwatcher.log"
  peer_timeout: 5            # seconds per peer HTTP fetch
  peer_interval: 30          # seconds between peer fetches; default poll_interval

files:                       # kind=file; size: blocks valid here only
  - path: /var/log/messages
    label: "syslog"
    delay: 3600              # null = do not monitor mtime
    ssh: {host: user@node}   # optional; see §4
    size:
      allow_empty: true
      warning: "500 MiB"     # ascending: warning < critical < full
      critical: "2 GiB"
      full: "8 GiB"          # "max:" is an accepted synonym

paths:                       # kind=directory; space: blocks valid here only
  - path: /data
    space:
      warning: "10%"         # descending: warning > critical > full
      critical: "5%"         # % means percent of total capacity still FREE
      full: "2%"

peers:                       # a bare list (static only), or this mapping
  static:
    - {url: http://node:5002, label: "node", timeout: 5, enabled: true}
  discover:
    enabled: false
    filter: {app: diskwatcher}    # fnmatch globs on app, name, host
    interval: 60
    timeout: 2.0
    grace: 180               # unseen-for seconds before a peer is dropped
    exclude: []              # host globs never to federate
    probe: []                # hosts also asked by unicast
```

Accepted keys are enumerated as module constants:
`ENTRY_KEYS`, `PEER_KEYS`, `PEERS_SECTION_KEYS`, `DISCOVER_KEYS`,
`DISCOVER_FILTER_KEYS`, `SPACE_KEYS`, `SIZE_KEYS`
(`src/mu2edaq_diskwatcher/config.py:17-32`). An unknown key is reported as a
config issue and ignored, never fatal.

### Validation philosophy

`config.py` **never raises and never exits** on a bad entry
(`src/mu2edaq_diskwatcher/config.py:3-6`). A control-room daemon must keep
running on a partly bad config. Problems are collected into
`settings.config_issues`, echoed once to stderr, shown on `/config`, returned
in `/api/config`, and counted into `/api/health`'s `degraded` verdict. A
threshold that fails to parse is dropped; its siblings survive
(`src/mu2edaq_diskwatcher/config.py:51-80`). The one hard exit is a `--config`
or `$MU2EDAQ_DISKWATCHER_CONFIG` path that does not exist
(`src/mu2edaq_diskwatcher/config.py:45-47`); the built-in default
`diskwatcher.yaml` is allowed to be absent.

### Threshold grammar

`src/mu2edaq_diskwatcher/thresholds.py:92`. Accepts SI (`KB`=1000) and IEC
(`KiB`=1024) suffixes case-insensitively, bare numbers as bytes, and `N%`.
Display is always IEC (`src/mu2edaq_diskwatcher/formatting.py:27`), so a `2TB`
threshold renders as `1.8 TiB` — the same number written the other way round.
Percentages are accepted by `space:` only; a percentage in a `size:` block is
rejected with the explanation that a file has no total capacity
(`src/mu2edaq_diskwatcher/thresholds.py:118`).

### Environment variables

Prefix `MU2EDAQ_DISKWATCHER_` (`src/mu2edaq_diskwatcher/settings.py:101-117`):
`CONFIG`, `WEB_HOST`, `WEB_PORT`, `POLL_INTERVAL`, `DEFAULT_DELAY`, `DAEMON`,
`PID_FILE`, `LOG_FILE`, `RUN_DIR`, `VERBOSE`, `PEERS`, `PEER_TIMEOUT`,
`PEER_INTERVAL`, `DISCOVER_PEERS`, `DISCOVER_FILTER`. A value that fails
coercion produces a warning and is ignored; it does not abort startup.

Additionally `CRS_PORT_HTTP` (control-room, read by the start script) and
`DW_RUN_DIR`, `CRS_STOP_TIMEOUT` (scripts). `DW_DRY_RUN` is a test hook,
deliberately undocumented in the man page
(`start-mu2edaq-diskwatcher.sh:114-116`).

---

## 3. `mu2edaq-discovery` integration

Optional in both directions, and a missing package must never block startup
(`src/mu2edaq_diskwatcher/discovery.py:1-7`). It is not on PyPI, so it is
deliberately absent from `requirements.txt` — naming it there would make pip
abort the whole file (`requirements.txt:3-8`).

### Outbound: the responder

At startup `start_responder(web_port)` registers
`Responder(name="Disk Watcher", app="diskwatcher", port=<web_port>,
scheme="http", version=..., meta={"instance_id": INSTANCE_ID})`
(`src/mu2edaq_diskwatcher/discovery.py:21-26`). The whole call is wrapped in a
bare `except Exception`; failure prints a note and returns `None`. Publishing
`instance_id` in `meta` lets a discovering diskwatcher recognise itself
*before* making any HTTP request (`src/mu2edaq_diskwatcher/peers.py:268`).

### Inbound: finding peers

With `peers.discover.enabled`, `active_peers()` runs a scan when due
(`src/mu2edaq_diskwatcher/peers.py:453`). Each scan does two things and merges
the answers by responder id (`src/mu2edaq_diskwatcher/peers.py:311`):

- **Multicast** — `discover(filter=..., timeout=...)` on group
  239.255.42.99:28999.
- **Unicast probe** — the same DISCOVER datagram sent directly to each host in
  `discover.probe` (`src/mu2edaq_diskwatcher/peers.py:187`). The comment at
  `:190-195` records why this exists: IGMP snooping without a querier splits
  the Mu2e DAQ network into islands, so multicast only reaches the querier's
  own switch. On the real fleet the probe list "is what actually finds them"
  (`config/nodes/aggregator.template.yaml:81-84`). A `host:port` entry
  overrides the protocol port.

Each `ANNOUNCE` record becomes a peer spec via `record_to_peer`
(`src/mu2edaq_diskwatcher/peers.py:253`), which rejects: malformed records, a
scheme other than `http`/`https`, our own `instance_id`, and hosts matching an
`exclude` glob (matched against both FQDN and short form,
`src/mu2edaq_diskwatcher/peers.py:247`). Rejections are counted by reason so
`/api/peers` can say "3 responders, 1 excluded: self".

`merge_peers` puts static entries first and lets them win on URL
(`src/mu2edaq_diskwatcher/peers.py:446`), so an operator's label, timeout or
`enabled: false` survives rediscovery. `DiscoveryState.expire` drops a peer
only after `grace` seconds unseen — default three scan intervals — because one
lost multicast reply must not blank a node's rows
(`src/mu2edaq_diskwatcher/peers.py:378`). Discovery decides what is *listed*;
HTTP still decides what is *up*.

### Trust boundary

`--discover-peers` applies **no authentication and no origin check**. Any host
that answers a DISCOVER query advertising `app=diskwatcher` becomes a peer and
its entries are rendered on the aggregator's dashboards. The default filter is
`{app: diskwatcher}` and the default `exclude` is `[]`
(`src/mu2edaq_diskwatcher/config.py:336-348`), so on a flat network a test
instance, a second cluster, or a deliberately hostile responder on the same
L2/L3 reach would be federated. The shipped aggregator config leaves
`exclude: []` (`config/nodes/aggregator.template.yaml:94`).

Concrete consequences are bounded by the read-only design — a rogue peer can
inject rows and skew the aggregate counts, not execute anything — and by
`escHtml` on every interpolated field in the front end
(`src/mu2edaq_diskwatcher/web/static/diskwatcher.js:12`), which prevents the
injected `label`/`path` from becoming script. The peer `url` is escaped before
being used as an `href`
(`src/mu2edaq_diskwatcher/web/static/diskwatcher.js:360`, `:463`), though
`record_to_peer` has already constrained the scheme to `http`/`https`.

The mitigation is a `host:` glob in the filter, or an `exclude:` list, or
leaving discovery off and relying on the static list. The aggregator's static
list is the fallback that does not depend on any of this.

---

## 4. SSH remote read

### Purpose and shape

An `ssh:` block on a watch entry makes that path be read on another host.
`remote_probe` returns mtime, size and (for directories) filesystem usage in
**one** round trip (`src/mu2edaq_diskwatcher/ssh.py:102`). The docstring
records the reason: a remote directory previously cost two SSH invocations per
poll cycle — one `stat`, one `python3 -c` — which on a slow link doubled the
poll time for every remote entry.

### Command construction

```
ssh -o BatchMode=yes -o ConnectTimeout=<timeout>
    [-p <port>] [-i <expanded key>] [<options...>]
    <host> <quoted remote argv | verbatim remote shell>
```

`src/mu2edaq_diskwatcher/ssh.py:43-73`. `BatchMode=yes` is unconditional, so
the daemon can never block on a password prompt.

The primary probe runs a small `python3 -c` snippet
(`src/mu2edaq_diskwatcher/ssh.py:29-40`), which behaves identically on Linux,
macOS and BSD. Hosts without `python3` fall back to `stat`, whose flags *are*
platform-specific, so the fallback tries GNU then BSD:
`stat -c '%Y %s' P 2>/dev/null || stat -f '%m %z' P`
(`src/mu2edaq_diskwatcher/ssh.py:152`). The choice is cached per host in
`_PROBE_MODE` for the life of the process, so the extra round trip is paid
once, not once per poll.

### Injection analysis

**The config-supplied `path` is safe.** Two distinct paths, both correct:

- Primary probe: `argv = ["python3", "-c", _PY_PROBE, path]` passed as
  `remote_argv`, each element run through `shlex.quote` before being appended
  (`src/mu2edaq_diskwatcher/ssh.py:72`, `:114`). The quoting is *required*,
  not decorative: `ssh` joins remote arguments into one string and hands it to
  the remote shell, so an unquoted `path` containing `;` would execute. The
  code gets this right and the docstring says why.
- Fallback: the path is `shlex.quote`d into the shell string before
  interpolation (`src/mu2edaq_diskwatcher/ssh.py:150-153`). Correct, and the
  `remote_shell` form exists precisely because `||` needs shell syntax; the
  docstring makes the caller responsible for quoting and the only caller does
  quote.

Additionally the python snippet takes the path as `sys.argv[1]` rather than
interpolating it into source, so there is no Python-level injection either.

**Two residual, config-trust-bounded concerns:**

1. `ssh_cfg["host"]` is appended to the argv unquoted and unvalidated
   (`src/mu2edaq_diskwatcher/ssh.py:68`). A host beginning with `-` is parsed
   by `ssh` as an option; `{host: "-oProxyCommand=..."}` yields
   `['ssh', ..., '-oProxyCommand=id', 'ls']` and executes locally. There is no
   shell involved, so this is argv confusion, not shell injection.
2. `ssh_cfg["options"]` is `shlex.split` (string form) or spliced verbatim
   (list form) into the argv (`src/mu2edaq_diskwatcher/ssh.py:63-67`), so
   arbitrary `ssh` options — including `ProxyCommand` — can be set from config.

Both require write access to the YAML config, which is the same privilege as
editing the systemd unit or the start script. They are **not** remote-input
paths: nothing a peer or a watched filesystem can say reaches them. Recorded
here for completeness, not filed as a vulnerability.

### A real defect in the `options` handling

The list form is spliced **verbatim**, without inserting `-o`:

```python
extra = ssh_cfg.get("options", "")
if isinstance(extra, list):
    cmd += extra                      # src/mu2edaq_diskwatcher/ssh.py:64-65
elif extra:
    cmd += shlex.split(str(extra))
```

Every piece of documentation shows the list form as bare `Key=Value` pairs:

- `config/mu2edaq-diskwatcher.yaml:205-206` — `options:` / `- StrictHostKeyChecking=no`
- `man/man5/mu2edaq-diskwatcher.conf.5:456-457` — identical
- `README.md:95` — `options: [StrictHostKeyChecking=no]`

Rendered, that produces
`ssh -o BatchMode=yes -o ConnectTimeout=10 StrictHostKeyChecking=no host ...`
— the pair lands as a positional argument, so `ssh` treats the *first*
positional as the destination and the option string becomes the host. The
string form `options: "-o StrictHostKeyChecking=no"` works correctly, and
`tests/test_ssh.py:52` only exercises the list form **with** an explicit `-o`,
which is why the gap survived. Filed as S3.

### Error and state mapping

| Condition | Result |
|---|---|
| non-zero exit from the remote probe | `OSError` → entry `MISSING`, `error` set |
| disk-usage sentinels of `-1` | path exists, `shutil.disk_usage` failed → `UNKNOWN` |
| `ssh` not on PATH | `"ssh executable not found in PATH"` |
| subprocess exceeds `timeout + 2` | `"SSH timed out after N s"` |

The `MISSING` / `UNKNOWN` distinction is load-bearing and is preserved
deliberately (`src/mu2edaq_diskwatcher/ssh.py:105-107`): a vanished path and an
unmeasurable filesystem are different operator problems.

A remote probe is bounded by `ssh`'s own `ConnectTimeout` plus
`subprocess.run(timeout=timeout+2)` and runs inside the local poll pool
(`max_workers=min(len(entries), 20)`, `src/mu2edaq_diskwatcher/poller.py:253`),
so a hung remote delays that poll cycle, never a page render — pages read
`STORE.snapshot()`.

---

## 5. Units and time semantics

**Free-space math.** `disk_pct = used/total*100`, `disk_free_pct =
free/total*100`, both rounded to one decimal
(`src/mu2edaq_diskwatcher/poller.py:211-212`). These come straight from
`shutil.disk_usage`, which on POSIX uses `statvfs`: `total = f_blocks*f_frsize`
and `free = f_bavail*f_frsize` — space available to an *unprivileged* user,
excluding the root reserve. Consequently `used + free < total` on a typical
ext4/xfs volume with 5% reserved, and `disk_pct + disk_free_pct` does not sum
to 100. That is the correct figure for an operator asking "can the DAQ still
write here", and the same convention `df` reports, but it is not stated in the
docs. Percentage thresholds resolve against `total`
(`src/mu2edaq_diskwatcher/thresholds.py:80`), i.e. against the full capacity
rather than the available capacity, so a `2%` floor on a volume with a 5% root
reserve can never fire from unprivileged writes alone. Worth knowing when
choosing `full:`.

Threshold comparisons are inclusive on the bad side: space fires on
`free <= limit`, size on `size >= limit`
(`src/mu2edaq_diskwatcher/thresholds.py:189`, `:221`). Both evaluators walk
most-severe-first, so any subset of thresholds works and a misordered block
still yields a defined answer with some states merely unreachable.

**Time.** Ages are `now - mtime` in POSIX epoch seconds, where `now` is the
single `started` timestamp of the poll cycle
(`src/mu2edaq_diskwatcher/poller.py:185`, `:251`) — every entry in a cycle is
aged against the same instant, so two paths written together never differ by
scheduling jitter. Display is UTC
(`datetime.fromtimestamp(mtime, tz=timezone.utc)`,
`src/mu2edaq_diskwatcher/poller.py:188`), so there is **no timezone hazard**:
both sides of the subtraction are epoch seconds and rendering is
timezone-explicit.

**Clock skew is a real edge case, and it is unhandled.** For a remote entry the
mtime comes from the *remote* host's clock while `now` is local
(`src/mu2edaq_diskwatcher/ssh.py:38`). Skew of more than `delay` produces a
false `stale` (remote clock behind) or masks a genuinely stale file (remote
clock ahead). Federation has the same property one level up: a peer's `age_s`
was computed against the peer's clock. A future-dated mtime yields a negative
`age_s`, which `fmt_duration` renders as the em dash
(`src/mu2edaq_diskwatcher/formatting.py:12`) while `stale` correctly stays
false — so the failure is visible as a blank age rather than a wrong alarm,
which is the better of the two outcomes. Nothing cross-checks clocks, and
nothing in the documentation warns that NTP is a prerequisite. Filed as S4
(documentation).
