# Staged issues — mu2edaq-diskwatcher

Findings from the September 2026 DAQ mega-review (`mu2e-sept-mega-review`),
staged here because `gh issue create` against `Mu2e/mu2edaq-diskwatcher` was
denied by this environment's permission policy. One attempt was made, per the
review contract, and not retried.

The repository exists and has issues enabled (4 issues: #7, #6 open; #2, #1
closed). Dedup was performed against the full list before writing: no
fingerprint below collides, and no title shares two significant tokens with an
open issue. **#7 is adjacent but distinct** — it concerns
`bootstrap_diskwatcher.sh` not being location-independent and reporting false
success; DW-006 below concerns only its *name*. If DW-006 is filed, comment on
#7 instead of opening a second issue if a maintainer prefers to fold them.

Each block below is ready to paste into `gh issue create --body-file`.
Fingerprints are `sha1("Mu2e/mu2edaq-diskwatcher:<path>:<rule_id>")[:8]` and
contain no line number, so a re-run comments rather than duplicates.

Labels to apply: `mega-review` plus exactly one of `bug` / `documentation` /
`enhancement`.

```bash
gh label create mega-review -R Mu2e/mu2edaq-diskwatcher -c 6F42C1 \
  -d "Filed by the Sept 2026 DAQ mega-review" 2>/dev/null || true
```

---

## DW-001 — S2

**Title:** `[hygiene] committed 8,815-line runtime Flask log in config/`
**Labels:** `mega-review`, `bug`

```markdown
**Severity:** S2 — production breakage or silent wrong behaviour; workaround exists
**Category:** bug
**Location:** `config/mu2edaq-diskwatcher.log`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-09-30

## What
An 8,815-line / 780 KiB Flask access log is committed to the repository at
`config/mu2edaq-diskwatcher.log`. It is a developer's runtime output, not
configuration, and it sits inside `config/` where `.gitignore`'s `*.log` rule
cannot save it, because the file is already tracked.

## Evidence
```
$ git ls-files config/mu2edaq-diskwatcher.log
config/mu2edaq-diskwatcher.log
$ wc -l config/mu2edaq-diskwatcher.log
    8815 config/mu2edaq-diskwatcher.log
$ head -2 config/mu2edaq-diskwatcher.log
[Config] Watching 7 path(s), poll interval 30 s
[Web]    Dashboard at http://localhost:5002
$ tail -1 config/mu2edaq-diskwatcher.log
127.0.0.1 - - [10/Sep/2026 14:04:21] "GET /api/sizes HTTP/1.1" 200 -
$ grep -n '^\*\.log' .gitignore
75:*.log
```

## Why it matters
Every node in the fleet has this repository on a shared NFS checkout, so the
stale log is present on all 28 of them. It records one developer's local poll
history inside `config/`, where an operator reasonably reads files as current
node configuration. The repository's own `.gitignore:75` declares `*.log`
should not be tracked, so the file contradicts the stated hygiene rule and will
keep growing on any commit that touches it.

## Suggested fix
`git rm --cached config/mu2edaq-diskwatcher.log` and commit. `.gitignore:75`
then keeps it out. Optionally add an explicit `config/*.log` line so the intent
is visible next to the config directory.

## Related
- Mu2e/mu2edaq-diskwatcher#6 (committed venv — same class of defect, already fixed)

<!-- mega-review-fingerprint: 5a10df6d -->
```

---

## DW-002 — S3

**Title:** `[ci] 314 tests across 13 files with no CI workflow`
**Labels:** `mega-review`, `enhancement`

```markdown
**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** enhancement
**Location:** repository root (no `.github/` directory)
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-09-30

## What
The package carries a substantial pytest suite — 13 files, 314 test functions —
and no continuous integration of any kind. Nothing runs it on push or on a pull
request.

## Evidence
```
$ ls -d .github
ls: .github: No such file or directory
$ ls tests/test_*.py | wc -l
      12
$ grep -c '^def test_' tests/*.py | grep -v ':0'
tests/test_cli.py:15
tests/test_config.py:61
tests/test_formatting.py:5
tests/test_packaging.py:16
tests/test_peers.py:44
tests/test_poller.py:27
tests/test_scripts.py:24
tests/test_settings.py:26
tests/test_ssh.py:20
tests/test_states.py:15
tests/test_thresholds.py:12
tests/test_web.py:49
```

## Why it matters
Several of these tests encode expensive lessons and exist specifically to stop
a regression recurring. `tests/test_packaging.py` sweeps filesystem totals from
10 GiB to 64 TiB to keep the shipped node templates pure-percentage, because a
mixed threshold block read `FULL` on empty disks across 27 nodes on 2026-09-23
(`config/nodes/node.template.yaml:74-82`). `tests/test_web.py` locks the frozen
`/api/status` schema that peers and `crs-app` depend on. A guard that only runs
when someone remembers to run it is not a guard.

## Suggested fix
A minimal `.github/workflows/test.yml`: checkout, set up Python 3.9 and 3.12
(3.9 is the declared floor in `pyproject.toml:11`), `pip install -e .[test]`,
`pytest`. No matrix beyond those two versions is needed. `mu2edaq-discovery` is
not on PyPI, so the workflow must not try to install it — the code degrades
gracefully without it and the tests substitute canned records
(`src/mu2edaq_diskwatcher/peers.py:181`).

<!-- mega-review-fingerprint: b9b19326 -->
```

---

## DW-003 — S3

**Title:** `[config] node list is a fourth copy of the cluster topology and disagrees with the other three`
**Labels:** `mega-review`, `bug`

```markdown
**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `tools/make-node-configs.sh:42-51`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-09-30

## What
`default_nodes()` hardcodes the Mu2e DAQ node inventory — 14 trackers and 8
calorimeters — making this the fourth independent assertion of the cluster
topology in the superproject. It disagrees with the other three, which say 18
trackers and 14 calorimeters. There is no generator and no shared source.

## Evidence
```
$ sed -n '42,51p' tools/make-node-configs.sh
default_nodes() {
  echo mu2egateway01 mu2egateway02
  echo mu2e-dl-02
  echo mu2e-cfo-01
  for i in $(seq 1 14); do printf 'mu2e-trk-%02d\n' "$i"; done
  for i in $(seq 1 8);  do printf 'mu2e-calo-%02d\n' "$i"; done
  echo mu2e-crv-01
}

$ sed -n '55,56p' ../mu2edaq-operations/scripts/nodes_config.yaml
      - {category: trk, start: 1, end: 18}
      - {category: calo, start: 1, end: 14}

$ sed -n '95,96p' ../mu2edaq-power-recovery/config/topology.yaml
      - {category: trk, start: 1, end: 18}
      - {category: calo, start: 1, end: 14}

$ sed -n '97,98p' ../mu2edaq-power-recovery/config/power-sequence.yaml
        - {category: trk,  start: 1, end: 14}
        - {category: calo, start: 1, end: 11}
```

| Source | trk | calo | MC-2 lab hosts |
|---|---|---|---|
| `mu2edaq-diskwatcher/tools/make-node-configs.sh:48-49` | 14 | 8 | 28 |
| `mu2edaq-operations/scripts/nodes_config.yaml:55-56` | 18 | 14 | 47 |
| `mu2edaq-power-recovery/config/topology.yaml:95-96` | 18 | 14 | 48 |
| `mu2edaq-power-recovery/config/power-sequence.yaml:97-98` | 14 | 11 | 28 (readout stage) |

Nineteen MC-2 hosts the other inventories declare have no diskwatcher config:
`mu2e-trk-15..18`, `mu2e-calo-09..14`, `mu2e-dcs-01..03`, `mu2e-stm-01/02`,
`mu2e-extmon-01`, `mu2e-worker-01..03`. The entire HEERC test stand (17 lab
hosts) is likewise uncovered. diskwatcher invents no host the others do not
know about — it is a strict subset — but the four numbers do not reconcile.

## Why it matters
If `trk-15..18` and `calo-09..14` are live, their disks are unmonitored: no
free-space alarm, no staleness alarm, and they are absent from the aggregator's
28 static peers, so nothing on the fleet dashboard indicates the gap. An
operator reading a green `/space` page reasonably concludes every DAQ volume is
healthy. If those nodes are *not* live, then `nodes_config.yaml` and
`topology.yaml` are the stale ones and power recovery will try to boot hosts
that do not exist. Which of the two it is cannot be determined from this
repository, which is itself the point.

Note `power-sequence.yaml`'s `calo end: 11` matches neither diskwatcher's 8 nor
topology's 14, so the disagreement is not a simple two-way split.

## Suggested fix
Short term, and entirely within this repository: add a comment above
`default_nodes()` stating the scope explicitly — which nodes carry a
diskwatcher and why the list stops where it does — so a reader can tell
deliberate scoping from staleness.

Long term, and the real fix: one topology file with a generator, consumed by
all three packages. `mu2edaq-power-recovery/config/topology.yaml` is the most
complete (48 hosts, roles, subnets, `mu2e-mgr-01`, explicit `mc1` placeholder)
and is already documented as copy-compatible with `nodes_config.yaml`
(`topology.yaml:3-7`), so it is the natural base. `make-node-configs.sh` would
then read the node list rather than restate it.

## Related
- `mu2e-mgr-01` is absent from `mu2edaq-operations/scripts/nodes_config.yaml`
  entirely, yet it is the NFS server, the diskwatcher aggregator, and is listed
  in `topology.yaml:88` — a separate gap, to be filed against `Mu2e/daq-operations`.

<!-- mega-review-fingerprint: 5daa3ce8 -->
```

---

## DW-004 — S3

**Title:** `[ssh] options: given as a bare Key=Value list is passed to ssh without -o`
**Labels:** `mega-review`, `bug`

```markdown
**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_diskwatcher/ssh.py:63-67`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-09-30

## What
An `ssh.options:` list is spliced into the `ssh` argv verbatim, with no `-o`
inserted. Every piece of documentation in the repository shows the list form as
bare `Key=Value` pairs, which produces a malformed command line: the pair lands
as a positional argument, so `ssh` takes it as the destination host.

## Evidence
```
$ sed -n '63,67p' src/mu2edaq_diskwatcher/ssh.py
    extra = ssh_cfg.get("options", "")
    if isinstance(extra, list):
        cmd += extra
    elif extra:
        cmd += shlex.split(str(extra))

$ python3 -c "
import sys; sys.path.insert(0,'src')
from mu2edaq_diskwatcher.ssh import build_ssh_cmd
print(build_ssh_cmd({'host':'n','options':['StrictHostKeyChecking=no']}, ['ls']))"
['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'StrictHostKeyChecking=no', 'n', 'ls']
```

Every documented example uses exactly the form that breaks:

```
$ grep -n -A1 'options:' config/mu2edaq-diskwatcher.yaml | tail -2
205:  #    options:                           # extra ssh -o Key=Value pairs
206:  #      - StrictHostKeyChecking=no
$ sed -n '456,457p' man/man5/mu2edaq-diskwatcher.conf.5
      options:
        - StrictHostKeyChecking=no
$ grep -n 'options:' README.md
95:      options: [StrictHostKeyChecking=no]
```

## Why it matters
An operator who follows the documented example to reach a host needing, say,
`StrictHostKeyChecking=no` or a `ProxyJump` gets an `ssh` invocation whose
first positional argument is the option text. `ssh` treats that as the
destination, the real host becomes the remote command, and the probe fails —
so the entry reads `MISSING` and looks like a vanished path rather than a
configuration error. The failure is silent in the sense that the operator's
config is exactly what the manual told them to write.

The string form `options: "-o StrictHostKeyChecking=no"` works correctly. The
gap survived because `tests/test_ssh.py:52` only exercises the list form *with*
an explicit `-o` already present:

```
$ sed -n '51,54p' tests/test_ssh.py
def test_options_as_list_and_string():
    as_list = build_ssh_cmd({"host": "n", "options": ["-o", "StrictHostKeyChecking=no"]}, ["ls"])

    as_string = build_ssh_cmd({"host": "n", "options": "-o StrictHostKeyChecking=no"}, ["ls"])
```

## Suggested fix
Prefix any list element that is not already a flag:

```python
if isinstance(extra, list):
    for opt in extra:
        opt = str(opt)
        cmd += [opt] if opt.startswith("-") else ["-o", opt]
```

This keeps the existing `["-o", "Key=Value"]` spelling working — the test above
still passes — and makes the documented `["Key=Value"]` spelling work too. Add
a test for the bare form.

<!-- mega-review-fingerprint: 55737abc -->
```

---

## DW-005 — S3

**Title:** `[peers] --discover-peers federates any responder advertising app=diskwatcher`
**Labels:** `mega-review`, `enhancement`

```markdown
**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** enhancement
**Location:** `src/mu2edaq_diskwatcher/config.py:336-348`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-09-30

## What
Peer discovery applies no authentication and no origin check. Any host that
answers a DISCOVER query advertising `app=diskwatcher` becomes a peer, and its
entries are rendered and counted on the discovering instance's dashboards. The
default filter matches on `app` alone and the default `exclude` list is empty,
so the only thing scoping federation is network reachability.

## Evidence
```
$ sed -n '336,348p' src/mu2edaq_diskwatcher/config.py
def default_discover() -> dict:
    """The ``peers.discover`` block with nothing set: discovery off."""
    return {
        "enabled":  False,
        "filter":   {"app": DISCOVER_APP},
        "interval": None,
        "timeout":  2.0,
        "exclude":  [],
        "grace":    None,
        "probe":    [],
    }

$ sed -n '88,94p' config/nodes/aggregator.template.yaml
  discover:
    enabled:  true
    filter:   {app: diskwatcher}
    interval: 60
    timeout:  2.0
    grace:    180
    exclude:  []
```

`record_to_peer` (`src/mu2edaq_diskwatcher/peers.py:253-282`) rejects only:
malformed records, a scheme other than http/https, our own `instance_id`, and
hosts matching an `exclude` glob. Nothing checks that the responder belongs to
this cluster. The shipped aggregator config enables discovery with
`exclude: []`.

## Why it matters
On a flat or bridged network segment, a second cluster, a developer's test
instance, or a deliberately crafted responder is federated automatically, and
its rows appear on the control-room dashboard indistinguishably from real DAQ
nodes except for the peer label. It skews the aggregate counts on `/space` and
`/sizes`, so a fabricated `GOOD` inflates the healthy total and a fabricated
`CRITICAL` raises a false alarm.

The blast radius is genuinely limited and the severity is S3 rather than S2 for
concrete reasons: the API is entirely read-only (no POST/PUT/DELETE route
exists), federation is one hop so a rogue peer cannot pivot further, responses
are capped at 8 MiB (`peers.py:54`), and every interpolated field is
HTML-escaped in the front end (`web/static/diskwatcher.js:12`), so injected
labels and paths cannot become script. The exposure is display integrity, not
code execution or data loss. `unicast probe:` is the mechanism actually used on
the real fleet, and that list is explicit, so the practical exposure today is
smaller than the configuration implies.

## Suggested fix
Change the shipped aggregator template to scope discovery by host, so the
default deployment is closed rather than open:

```yaml
    filter: {app: diskwatcher, host: "mu2e-*"}
```

`host` is already a supported filter key (`config.py:25`,
`DISCOVER_FILTER_KEYS`), matched as an fnmatch glob against both FQDN and short
form, so this needs no code change. Document in
`man/man5/mu2edaq-diskwatcher.conf.5` that `discover.filter` is the trust
boundary and that a discovered peer is trusted to report its own states — a
point the code comments already make
(`src/mu2edaq_diskwatcher/peers.py:14-16`) but the operator-facing docs do not.

<!-- mega-review-fingerprint: 314212b4 -->
```

---

## DW-006 — S4

**Title:** `[scripts] bootstrap_diskwatcher.sh does not follow the dashed naming convention`
**Labels:** `mega-review`, `bug`

```markdown
**Severity:** S4 — cosmetic, stale, hygiene
**Category:** bug
**Location:** `bootstrap_diskwatcher.sh`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-09-30

## What
The bootstrap script uses an underscore and the short app name, while both
other top-level scripts in the same directory use the dashed, fully-qualified
form.

## Evidence
```
$ ls *.sh
bootstrap_diskwatcher.sh
start-mu2edaq-diskwatcher.sh
stop-mu2edaq-diskwatcher.sh
```

## Why it matters
Purely cosmetic — the script works. It is inconsistent with its two siblings
and with the convention across the DAQ suite, so tab-completion and any
cross-package tooling that assumes `*-mu2edaq-*.sh` skips it.

Issue #1 in this repository already renamed `start_diskwatcher.sh` /
`stop_diskwatcher.sh` to the dashed convention; this file was missed by that
change.

## Suggested fix
`git mv bootstrap_diskwatcher.sh bootstrap-mu2edaq-diskwatcher.sh` and update
the references. Note this is a behavioural change for anything that invokes it
by name, so it needs a grep across the superproject first — at minimum
`README.md`, `start-mu2edaq-diskwatcher.sh:125`,
`src/mu2edaq_diskwatcher/cli.py:230`, `requirements.txt:12` and
`tests/test_scripts.py`. A compatibility symlink for one release would avoid
breaking any node runbook that names the old path.

## Related
- Mu2e/mu2edaq-diskwatcher#1 (the rename that missed this file)
- Mu2e/mu2edaq-diskwatcher#7 (other defects in the same script — consider folding)

<!-- mega-review-fingerprint: 233f8311 -->
```

---

## DW-007 — S4

**Title:** `[docs] Flask development server is the production WSGI server, undocumented`
**Labels:** `mega-review`, `documentation`

```markdown
**Severity:** S4 — cosmetic, stale, hygiene
**Category:** documentation
**Location:** `src/mu2edaq_diskwatcher/cli.py:297`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-09-30

## What
The daemon serves the control-room dashboard from Flask's built-in development
server. That is a defensible choice for this workload, but it is stated nowhere
in the documentation, and Werkzeug prints its own warning into every node's log
where an operator will eventually read it and escalate.

## Evidence
```
$ sed -n '296,299p' src/mu2edaq_diskwatcher/cli.py
    try:
        app.run(host=settings.web_host, port=settings.web_port,
                use_reloader=False, threaded=True)

$ head -5 config/mu2edaq-diskwatcher.log | tail -1
WARNING: This is a development server. Do not use it in a production deployment. Use a production WSGI server instead.

$ grep -in 'wsgi\|waitress\|gunicorn\|development server' README.md man/man1/mu2edaq-diskwatcher.1 requirements.txt
(no matches)
```

## Why it matters
The choice is reasonable — the app is read-only, serves a handful of
control-room browsers plus up to 28 peer fetches per interval on a trusted DAQ
LAN, and `threaded=True` is set — but an undocumented one. An operator who
reads the Werkzeug banner in a node log has no way to tell whether it is a
known accepted risk or a deployment mistake, and the natural response is to
escalate or to "fix" it by adding a WSGI server nobody validated.

This is S4 rather than S3 because nothing is currently broken and the exposure
is a trusted internal network.

## Suggested fix
One paragraph in `README.md` and in the DESCRIPTION section of
`man/man1/mu2edaq-diskwatcher.1`, stating that the built-in server is used
deliberately, why it is adequate for this workload, and that the banner in the
log is expected. If a production server is ever wanted, `waitress` is
pure-Python and offline-installable, which matters here because the deployment
hosts are offline-installed (`src/mu2edaq_diskwatcher/peers.py:23`).

<!-- mega-review-fingerprint: c3759933 -->
```

---

## DW-008 — S4

**Title:** `[docs] clock skew between watcher and remote/peer hosts is unhandled and unmentioned`
**Labels:** `mega-review`, `documentation`

```markdown
**Severity:** S4 — cosmetic, stale, hygiene
**Category:** documentation
**Location:** `src/mu2edaq_diskwatcher/poller.py:185`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-09-30

## What
Staleness is `now - mtime`, where `now` is the local clock and `mtime` may come
from a different host — over SSH for a remote entry, or one level up from a
peer's `/api/state`. Nothing cross-checks clocks and nothing in the
documentation notes that synchronised time is a prerequisite for the staleness
alarm to mean anything on remote entries.

## Evidence
```
$ sed -n '185,194p' src/mu2edaq_diskwatcher/poller.py
        age_s = now - mtime
        state.update({
            "mtime":     mtime,
            "mtime_str": datetime.fromtimestamp(
                             mtime, tz=timezone.utc
                         ).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "age_s":     age_s,
            "age_str":   fmt_duration(age_s),
            "stale":     delay is not None and age_s > delay,
        })

$ grep -n 'st.st_mtime' src/mu2edaq_diskwatcher/ssh.py
39:    "print(s.st_mtime, s.st_size, d[0], d[1], d[2])\n"

$ grep -in 'clock\|ntp\|skew' README.md man/man1/*.1 man/man5/*.5
(no matches)
```

## Why it matters
Skew greater than an entry's `delay` produces a false `stale` when the remote
clock is behind, or masks a genuinely stale file when it is ahead. With
`default_delay: 300` a five-minute offset is enough. Federation inherits the
property: a peer's `age_s` was computed against that peer's own clock, so the
aggregator's `/` page mixes ages from up to 28 independent clocks.

Two mitigating facts keep this at S4. Timezones are not a hazard: both sides of
the subtraction are POSIX epoch seconds and display is explicitly UTC
(`poller.py:188`), so only absolute offset matters, not locale. And a
future-dated mtime yields a negative `age_s`, which `fmt_duration` renders as
an em dash (`src/mu2edaq_diskwatcher/formatting.py:12`) while `stale` correctly
stays `false` — so that direction fails visibly as a blank age rather than as a
wrong alarm, which is the better of the two outcomes.

## Suggested fix
Documentation only. Add a note to the `ssh` section of
`man/man5/mu2edaq-diskwatcher.conf.5` and the peers section of
`man/man1/mu2edaq-diskwatcher.1` stating that `delay` on a remote or federated
entry is only meaningful when the hosts share a time source, and that the
`delay` chosen should exceed the worst expected offset.

If a code fix is ever wanted, `/api/state` already carries `generated` as an
ISO timestamp (`src/mu2edaq_diskwatcher/web/api.py:152`); comparing it to local
receipt time would give a free per-peer skew estimate for the Config page. Not
proposed as part of this issue.

<!-- mega-review-fingerprint: 40e2927f -->
```

---

## Observations deliberately not filed

- **28 peers, a pool of 10, and a 5 s timeout** give a 15 s worst-case peer
  cycle against a 30 s `peer_interval` (`src/mu2edaq_diskwatcher/peers.py:486`,
  `settings.py:153`). Under 2x headroom, but it fits, and no page render is
  affected because Flask handlers only read `PEERS.snapshot()`. A runbook note,
  not a defect.
- **`used + free < total`** on volumes with a root reserve, because
  `shutil.disk_usage` reports `f_bavail`. This is the correct figure for "can
  the DAQ still write here" and matches `df`; documented in
  `docs/INTERFACES.md` §5 rather than filed.
- **`ssh.host` and `ssh.options` accept arbitrary `ssh` arguments** from config
  (`src/mu2edaq_diskwatcher/ssh.py:63-68`), including a leading `-` host that
  `ssh` parses as an option. Requires write access to the YAML, which equals
  the privilege to edit the start script, and no remote input reaches it. Not a
  vulnerability; recorded in `docs/INTERFACES.md` §4.
- **Watch-entry `path` is not injectable.** Both probe paths quote correctly —
  `shlex.quote` per argv element at `ssh.py:72` and on the fallback shell
  string at `ssh.py:150` — and the Python snippet takes the path as `sys.argv[1]`
  rather than interpolating it. Checked specifically; it is right.
- **Node configs have not drifted.** All 27 generated files were re-rendered
  from `config/nodes/node.template.yaml` and compared byte-for-byte against
  what is committed. Zero differences. The generator is the source of truth in
  practice, not just in the header comment.
- **The submodule is ahead of its upstream and the superproject gitlink is
  stale.** Pre-existing, outside this review's scope, not filed.
