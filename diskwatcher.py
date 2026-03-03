#!/usr/bin/env python3
"""
diskwatcher.py – File modification-time monitor with embedded web dashboard.

Reads a YAML configuration listing files and directories together with
acceptable staleness thresholds.  A background thread polls each entry's
mtime and updates shared state.  A Flask web server serves a Bootstrap
dashboard that refreshes automatically.

Usage:
    python diskwatcher.py [options]
    python diskwatcher.py --config diskwatcher.yaml --port 5002
"""

import argparse
import atexit
import html as _html
import importlib.metadata
import os
import platform
import socket as _socket
import sys
import threading
import time
from datetime import datetime, timezone

import yaml
from flask import Flask, jsonify

VERSION   = "1.0.0"
START_TIME = datetime.now(timezone.utc)

# ---------------------------------------------------------------------------
# Globals (defaults; overridden by YAML config and/or CLI flags)
# ---------------------------------------------------------------------------
WEB_HOST      = "0.0.0.0"
WEB_PORT      = 5002
POLL_INTERVAL = 30      # seconds between mtime checks
CONFIG_PATH   = None    # filled in by main()
WATCH_ENTRIES = []      # list of {path, delay, label} dicts
DAEMON        = False   # run as background daemon
PID_FILE      = None    # path to write daemon PID
LOG_FILE      = None    # path to redirect daemon stdout/stderr

# ---------------------------------------------------------------------------
# Daemon support
# ---------------------------------------------------------------------------
def daemonize(log_file: str = None) -> None:
    """Detach from the controlling terminal using the POSIX double-fork idiom.

    After this call returns (in the grandchild process only):
      - stdin  is redirected to /dev/null
      - stdout and stderr are redirected to *log_file* (or /dev/null)
      - the process has no controlling terminal and belongs to its own session

    The two intermediate parent processes exit via os._exit() so that no
    atexit handlers or Python finalizers run in them.

    Raises RuntimeError on platforms without os.fork() (e.g. Windows).
    """
    if not hasattr(os, "fork"):
        raise RuntimeError(
            "Daemon mode requires os.fork(), which is not available on this platform."
        )

    # Flush Python-level buffers before forking so they aren't duplicated.
    sys.stdout.flush()
    sys.stderr.flush()

    # ---- first fork ----
    pid = os.fork()
    if pid > 0:
        os._exit(0)          # parent exits; child becomes a new process group leader

    os.setsid()              # create a new session; child is the session leader
    os.umask(0o022)

    # ---- second fork ----
    pid = os.fork()
    if pid > 0:
        os._exit(0)          # session leader exits; grandchild can never acquire a tty

    # ---- grandchild: we are the daemon ----

    # Redirect stdin to /dev/null
    with open(os.devnull, "r") as dn:
        os.dup2(dn.fileno(), sys.stdin.fileno())
    sys.stdin = open(os.devnull, "r")

    # Redirect stdout and stderr to the log file (or /dev/null)
    log_dest = log_file if log_file else os.devnull
    log_mode = "a"   # append so previous runs are preserved
    log_fh   = open(log_dest, log_mode, buffering=1)   # line-buffered text
    os.dup2(log_fh.fileno(), sys.stdout.fileno())
    os.dup2(log_fh.fileno(), sys.stderr.fileno())
    sys.stdout = log_fh
    sys.stderr = log_fh


def _write_pid_file(path: str) -> None:
    """Write the current PID to *path* and register its removal at exit."""
    try:
        with open(path, "w") as fh:
            fh.write(f"{os.getpid()}\n")
        atexit.register(_remove_pid_file, path)
    except OSError as exc:
        print(f"[Daemon] Warning: could not write PID file {path}: {exc}",
              file=sys.stderr)


def _remove_pid_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Shared state (written by poller thread, read by Flask request handlers)
# ---------------------------------------------------------------------------
state_lock  = threading.Lock()
file_states: list = []   # list of status dicts


def fmt_duration(seconds) -> str:
    """Convert a number of seconds into a human-readable string."""
    if seconds is None or seconds < 0:
        return "\u2014"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s   = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
def load_config(path: str, required: bool = False) -> dict:
    """Load a YAML config file and return its contents as a dict."""
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        print(f"[Config] Loaded {path}")
        return data
    except FileNotFoundError:
        if required:
            print(f"[Config] Error: config file not found: {path}", file=sys.stderr)
            sys.exit(1)
        return {}


def entries_from_config(cfg: dict) -> list:
    """Build the flat list of watch entries from the config dict.

    Reads both the ``files`` and ``paths`` top-level keys.  Each item may
    be a mapping with ``path``, ``delay``, and optional ``label`` keys, or
    a bare string (treated as the path with the default delay).
    """
    entries = []
    for section in ("files", "paths"):
        kind = "file" if section == "files" else "directory"
        for item in cfg.get(section, []):
            if isinstance(item, dict):
                path  = item.get("path", "")
                delay = int(item.get("delay", 300))
                label = item.get("label") or path
            else:
                path  = str(item)
                delay = 300
                label = path
            if path:
                entries.append({"path": path, "delay": delay, "label": label, "kind": kind})
    return entries


# ---------------------------------------------------------------------------
# Poller thread
# ---------------------------------------------------------------------------
def _do_poll() -> None:
    """Stat every watched path once and update file_states."""
    now = time.time()
    new_states = []
    for entry in WATCH_ENTRIES:
        path  = entry["path"]
        delay = entry["delay"]
        label = entry.get("label") or path
        kind  = entry.get("kind", "file")
        try:
            mtime = os.path.getmtime(path)
            age_s = now - mtime
            new_states.append({
                "path":      path,
                "label":     label,
                "delay":     delay,
                "kind":      kind,
                "mtime":     mtime,
                "mtime_str": datetime.fromtimestamp(
                    mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "age_s":     age_s,
                "age_str":   fmt_duration(age_s),
                "delay_str": fmt_duration(delay),
                "stale":     age_s > delay,
                "missing":   False,
                "error":     None,
            })
        except OSError as exc:
            new_states.append({
                "path":      path,
                "label":     label,
                "delay":     delay,
                "kind":      kind,
                "mtime":     None,
                "mtime_str": "\u2014",
                "age_s":     None,
                "age_str":   "\u2014",
                "delay_str": fmt_duration(delay),
                "stale":     True,   # missing files are also considered stale
                "missing":   True,
                "error":     str(exc),
            })
    with state_lock:
        file_states.clear()
        file_states.extend(new_states)


def poll_loop() -> None:
    """Background thread: poll immediately, then sleep between polls."""
    while True:
        try:
            _do_poll()
        except Exception as exc:
            print(f"[Poller] Unexpected error: {exc}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/api/status")
def api_status():
    with state_lock:
        data = list(file_states)
    n_stale = sum(1 for s in data if s["stale"])
    return jsonify({
        "files":         data,
        "total":         len(data),
        "stale":         n_stale,
        "ok":            len(data) - n_stale,
        "poll_interval": POLL_INTERVAL,
    })


# ---------------------------------------------------------------------------
# HTML – Watcher (main) page
# ---------------------------------------------------------------------------
WATCHER_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>mu2edaq Disk Watcher</title>
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
    crossorigin="anonymous">
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
  <style>
    body { background: #f8f9fa; }
    .navbar-brand { font-weight: 700; letter-spacing: .05em; }
    .stat-card                { border-left: 4px solid; }
    .stat-card.ok-card        { border-color: #198754; }
    .stat-card.stale-card     { border-color: #dc3545; }
    .stat-card.missing-card   { border-color: #fd7e14; }
    .stat-card.total-card     { border-color: #0d6efd; }
    .badge-ok      { background-color: #198754 !important; color: #fff !important; }
    .badge-stale   { background-color: #dc3545 !important; color: #fff !important; }
    .badge-missing { background-color: #fd7e14 !important; color: #fff !important; }
    tr.row-missing td { background-color: rgba(253, 126, 20, 0.15) !important; }
    .path-cell { font-family: monospace; font-size: 0.85em; word-break: break-all; }
    .sort-th   { cursor: pointer; user-select: none; white-space: nowrap; }
    .sort-th:hover { background-color: rgba(0,0,0,.04); }
  </style>
</head>
<body>

<nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-4">
  <div class="container-fluid">
    <a class="navbar-brand" href="/">
      <i class="bi bi-hdd-stack"></i> mu2edaq Disk Watcher
    </a>
    <div class="collapse navbar-collapse">
      <ul class="navbar-nav me-auto">
        <li class="nav-item"><a class="nav-link active" href="/">Watcher</a></li>
        <li class="nav-item"><a class="nav-link" href="/config">Config</a></li>
        <li class="nav-item"><a class="nav-link" href="/about">About</a></li>
      </ul>
      <ul class="navbar-nav ms-auto align-items-center gap-3">
        <li class="nav-item">
          <span class="navbar-text small text-secondary" id="last-update"></span>
        </li>
      </ul>
    </div>
  </div>
</nav>

<div class="container-fluid px-4">

  <!-- Summary stat cards -->
  <div class="row g-3 mb-4">
    <div class="col-sm-3">
      <div class="card stat-card ok-card h-100">
        <div class="card-body">
          <div class="text-muted small">OK</div>
          <div class="display-6 fw-bold text-success" id="stat-ok">&mdash;</div>
        </div>
      </div>
    </div>
    <div class="col-sm-3">
      <div class="card stat-card stale-card h-100">
        <div class="card-body">
          <div class="text-muted small">STALE</div>
          <div class="display-6 fw-bold text-danger" id="stat-stale">&mdash;</div>
        </div>
      </div>
    </div>
    <div class="col-sm-3">
      <div class="card stat-card missing-card h-100">
        <div class="card-body">
          <div class="text-muted small">MISSING</div>
          <div class="display-6 fw-bold" style="color:#fd7e14" id="stat-missing">&mdash;</div>
        </div>
      </div>
    </div>
    <div class="col-sm-3">
      <div class="card stat-card total-card h-100">
        <div class="card-body">
          <div class="text-muted small">TOTAL</div>
          <div class="display-6 fw-bold text-primary" id="stat-total">&mdash;</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Files section -->
  <div class="card mb-4">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span>
        <i class="bi bi-file-earmark-text"></i> Files
        <small class="text-muted ms-1" id="files-count"></small>
      </span>
      <div class="d-flex align-items-center gap-2">
        <small class="text-muted" id="refresh-label">Auto-refreshes every 5 s</small>
        <select id="refresh-interval" class="form-select form-select-sm" style="width:auto"
                title="Auto-refresh interval">
          <option value="1000">1 s</option>
          <option value="2000">2 s</option>
          <option value="5000" selected>5 s</option>
          <option value="10000">10 s</option>
          <option value="30000">30 s</option>
          <option value="60000">60 s</option>
          <option value="120000">2 min</option>
          <option value="300000">5 min</option>
          <option value="0">Off</option>
        </select>
        <button class="btn btn-sm btn-outline-secondary" id="refresh-now" title="Refresh now">
          <i class="bi bi-arrow-clockwise"></i>
        </button>
      </div>
    </div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table class="table table-sm table-hover mb-0">
          <thead class="table-light">
            <tr>
              <th>Status</th>
              <th class="sort-th" onclick="toggleSort('files','name')">
                Label / Path <span id="sort-files-name"></span>
              </th>
              <th class="sort-th" onclick="toggleSort('files','mtime')">
                Last Modified <span id="sort-files-mtime"></span>
              </th>
              <th>Age</th>
              <th>Threshold</th>
            </tr>
          </thead>
          <tbody id="tbody-files">
            <tr><td colspan="5" class="text-muted p-3">Loading&hellip;</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Directories section -->
  <div class="card mb-4">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span>
        <i class="bi bi-folder2-open"></i> Directories
        <small class="text-muted ms-1" id="dirs-count"></small>
      </span>
    </div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table class="table table-sm table-hover mb-0">
          <thead class="table-light">
            <tr>
              <th>Status</th>
              <th class="sort-th" onclick="toggleSort('dirs','name')">
                Label / Path <span id="sort-dirs-name"></span>
              </th>
              <th class="sort-th" onclick="toggleSort('dirs','mtime')">
                Last Modified <span id="sort-dirs-mtime"></span>
              </th>
              <th>Age</th>
              <th>Threshold</th>
            </tr>
          </thead>
          <tbody id="tbody-dirs">
            <tr><td colspan="5" class="text-muted p-3">Loading&hellip;</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

</div><!-- /container-fluid -->

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
  crossorigin="anonymous"></script>
<script>
function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ---- sort state (independent per section) ----
const sortState = {
  files: { col: 'name', dir: 1 },
  dirs:  { col: 'name', dir: 1 },
};

const SORT_ICON = {
  none: '<i class="bi bi-arrow-down-up text-muted ms-1 small"></i>',
  asc:  '<i class="bi bi-arrow-up ms-1 small"></i>',
  desc: '<i class="bi bi-arrow-down ms-1 small"></i>',
};

function updateSortIndicators(section) {
  const s = sortState[section];
  for (const col of ['name', 'mtime']) {
    const el = document.getElementById('sort-' + section + '-' + col);
    if (el) el.innerHTML = (s.col === col)
      ? (s.dir > 0 ? SORT_ICON.asc : SORT_ICON.desc)
      : SORT_ICON.none;
  }
}

function sortEntries(entries, col, dir) {
  return [...entries].sort((a, b) => {
    if (col === 'name') {
      const an = a.label || a.path;
      const bn = b.label || b.path;
      return dir * an.localeCompare(bn);
    }
    // mtime: missing entries always sort to the end regardless of direction
    const sentinel = dir > 0 ? Infinity : -Infinity;
    const am = a.mtime != null ? a.mtime : sentinel;
    const bm = b.mtime != null ? b.mtime : sentinel;
    return dir * (am - bm);
  });
}

function toggleSort(section, col) {
  const s = sortState[section];
  s.dir = (s.col === col) ? -s.dir : 1;
  s.col = col;
  if (currentData) renderSection(section, currentData);
}

// ---- row builder (shared by both sections) ----
function buildRows(entries) {
  if (!entries.length) return null;
  let html = '';
  for (const f of entries) {
    let rowClass = '';
    if (f.missing)    rowClass = ' class="row-missing"';
    else if (f.stale) rowClass = ' class="table-danger"';
    html += '<tr' + rowClass + '>';

    // Status badge (first column)
    if (f.missing) {
      html += '<td><span class="badge badge-missing">missing</span></td>';
    } else if (f.stale) {
      html += '<td><span class="badge badge-stale">stale</span></td>';
    } else {
      html += '<td><span class="badge badge-ok">ok</span></td>';
    }

    // Label / Path
    if (f.label && f.label !== f.path) {
      html += '<td><strong>' + escHtml(f.label) + '</strong><br>' +
              '<span class="path-cell text-muted">' + escHtml(f.path) + '</span></td>';
    } else {
      html += '<td><span class="path-cell">' + escHtml(f.path) + '</span></td>';
    }

    // Last modified & age
    if (f.missing) {
      html += '<td colspan="2" class="small" style="color:#fd7e14">' +
              '<i class="bi bi-question-circle-fill me-1"></i>not found</td>';
    } else {
      html += '<td class="small text-muted text-nowrap">' + escHtml(f.mtime_str) + '</td>';
      html += '<td class="text-nowrap">' + escHtml(f.age_str) + '</td>';
    }

    // Threshold
    html += '<td class="text-muted small text-nowrap">' + escHtml(f.delay_str) + '</td>';

    html += '</tr>';
  }
  return html;
}

// ---- per-section render ----
function renderSection(section, data) {
  const kind    = section === 'files' ? 'file' : 'directory';
  const entries = (data.files || []).filter(f => f.kind === kind);
  const s       = sortState[section];
  const sorted  = sortEntries(entries, s.col, s.dir);

  const countEl = document.getElementById(section + '-count');
  if (countEl) countEl.textContent = '(' + entries.length + ')';

  updateSortIndicators(section);

  const tbody = document.getElementById('tbody-' + section);
  if (!tbody) return;

  const rows = buildRows(sorted);
  if (rows === null) {
    const noun = section === 'files' ? 'files' : 'directories';
    tbody.innerHTML = '<tr><td colspan="5" class="text-muted p-3">No ' + noun +
      ' configured. Add entries to the YAML config file.</td></tr>';
  } else {
    tbody.innerHTML = rows;
  }
}

// ---- stat cards + full render ----
let currentData = null;

function renderAll(data) {
  currentData = data;
  const all       = data.files || [];
  const n_missing = all.filter(f =>  f.missing).length;
  const n_stale   = all.filter(f =>  f.stale && !f.missing).length;
  const n_ok      = all.filter(f => !f.stale).length;
  document.getElementById('stat-ok').textContent      = n_ok;
  document.getElementById('stat-stale').textContent   = n_stale;
  document.getElementById('stat-missing').textContent = n_missing;
  document.getElementById('stat-total').textContent   = data.total;

  renderSection('files', data);
  renderSection('dirs',  data);

  const now = new Date().toLocaleTimeString();
  document.getElementById('last-update').textContent = '\u2713 Updated ' + now;
}

function refreshData() {
  fetch('/api/status', {cache: 'no-store'})
    .then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(renderAll)
    .catch(err => {
      console.error('Failed to fetch status:', err);
      document.getElementById('last-update').textContent = '\u26a0 Fetch failed';
    });
}

// ---- auto-refresh ----
let refreshTimer = null;
function applyRefreshInterval(ms) {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  const label = document.getElementById('refresh-label');
  if (ms > 0) {
    refreshTimer = setInterval(refreshData, ms);
    label.textContent = 'Auto-refreshes every ' +
      (ms >= 60000 ? (ms / 60000) + ' min' : (ms / 1000) + ' s');
  } else {
    label.textContent = 'Auto-refresh off';
  }
}

document.getElementById('refresh-interval').addEventListener('change', function () {
  applyRefreshInterval(parseInt(this.value, 10));
});
document.getElementById('refresh-now').addEventListener('click', refreshData);

// initialise sort indicators then fetch
updateSortIndicators('files');
updateSortIndicators('dirs');
refreshData();
applyRefreshInterval(5000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return WATCHER_HTML


# ---------------------------------------------------------------------------
# HTML – About page (dynamically built with live runtime values)
# ---------------------------------------------------------------------------
def _build_about_page() -> str:
    now    = datetime.now(timezone.utc)
    uptime = now - START_TIME
    h, rem = divmod(int(uptime.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    uptime_str = f"{h}h {m}m {s}s"

    hostname = _socket.gethostname()
    os_info  = platform.platform()
    py_ver   = sys.version.split()[0]

    def pkg_ver(name):
        try:
            return importlib.metadata.version(name)
        except Exception:
            return "?"

    flask_ver = pkg_ver("flask")
    yaml_ver  = pkg_ver("pyyaml")

    with state_lock:
        total_files = len(file_states)
        n_missing   = sum(1 for s in file_states if s.get("missing"))
        n_stale     = sum(1 for s in file_states if s["stale"] and not s.get("missing"))

    cfg_display  = CONFIG_PATH if CONFIG_PATH else "(defaults)"
    poll_display = fmt_duration(POLL_INTERVAL)

    if DAEMON:
        daemon_display = (
            f'<span class="badge bg-secondary">daemon</span> '
            f'PID&nbsp;{os.getpid()}'
        )
    else:
        daemon_display = '<span class="badge bg-secondary">foreground</span>'

    pid_display = f"<code class=\"small\">{PID_FILE}</code>" if PID_FILE else "(none)"
    log_display = f"<code class=\"small\">{LOG_FILE}</code>" if LOG_FILE else "(none)"

    nav = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>About &mdash; mu2edaq Disk Watcher</title>
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
    crossorigin="anonymous">
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
  <style>
    body { background: #f8f9fa; }
    .navbar-brand { font-weight: 700; letter-spacing: .05em; }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-4">
  <div class="container-fluid">
    <a class="navbar-brand" href="/"><i class="bi bi-hdd-stack"></i> mu2edaq Disk Watcher</a>
    <div class="collapse navbar-collapse">
      <ul class="navbar-nav">
        <li class="nav-item"><a class="nav-link" href="/">Watcher</a></li>
        <li class="nav-item"><a class="nav-link" href="/config">Config</a></li>
        <li class="nav-item"><a class="nav-link active" href="/about">About</a></li>
      </ul>
    </div>
  </div>
</nav>
<div class="container-fluid px-4">"""

    foot = """</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
  crossorigin="anonymous"></script>
</body>
</html>"""

    body = f"""
<div class="row g-3 mb-4">
  <div class="col-12">
    <div class="card border-primary">
      <div class="card-body">
        <div class="d-flex align-items-center gap-3">
          <i class="bi bi-hdd-stack text-primary" style="font-size:2.5rem"></i>
          <div>
            <h4 class="mb-1">mu2edaq Disk Watcher <small class="text-muted fs-6">v{VERSION}</small></h4>
            <p class="text-muted mb-0">
              File modification-time monitor for the Mu2e DAQ system.
              Reads a YAML configuration listing files and directories, checks each
              entry&rsquo;s last-modification time on a configurable interval, and serves
              a live Bootstrap dashboard showing staleness status.
            </p>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="row g-3 mb-4">
  <div class="col-md-6">
    <div class="card h-100">
      <div class="card-header"><i class="bi bi-info-circle"></i> Runtime Info</div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <tbody>
            <tr><th class="ps-3" style="width:40%">Host</th><td>{hostname}</td></tr>
            <tr><th class="ps-3">Platform</th><td><small>{os_info}</small></td></tr>
            <tr><th class="ps-3">Python</th><td>{py_ver}</td></tr>
            <tr><th class="ps-3">Started</th><td>{START_TIME.strftime("%Y-%m-%d %H:%M:%S UTC")}</td></tr>
            <tr><th class="ps-3">Uptime</th><td>{uptime_str}</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
  <div class="col-md-6">
    <div class="card h-100">
      <div class="card-header"><i class="bi bi-gear"></i> Active Configuration</div>
      <div class="card-body p-0">
        <table class="table table-sm mb-0">
          <tbody>
            <tr><th class="ps-3" style="width:40%">Config file</th>
                <td><code class="small">{cfg_display}</code></td></tr>
            <tr><th class="ps-3">Web bind</th>
                <td><code>{WEB_HOST}:{WEB_PORT}</code></td></tr>
            <tr><th class="ps-3">Poll interval</th>
                <td>{poll_display} ({POLL_INTERVAL} s)</td></tr>
            <tr><th class="ps-3">Files watched</th><td>{total_files}</td></tr>
            <tr><th class="ps-3">Currently stale</th><td>{n_stale}</td></tr>
            <tr><th class="ps-3">Currently missing</th><td>{n_missing}</td></tr>
            <tr><th class="ps-3">Run mode</th><td>{daemon_display}</td></tr>
            <tr><th class="ps-3">PID file</th><td>{pid_display}</td></tr>
            <tr><th class="ps-3">Log file</th><td>{log_display}</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="row g-3 mb-4">
  <div class="col-md-6">
    <div class="card h-100">
      <div class="card-body text-center">
        <i class="bi bi-clock-history text-primary" style="font-size:2rem"></i>
        <h6 class="mt-2">Modification-Time Polling</h6>
        <p class="text-muted small mb-0">
          A background thread checks <code>os.path.getmtime()</code> for each
          configured path every <strong>{POLL_INTERVAL}&nbsp;s</strong>.
          If the age exceeds the entry&rsquo;s configured threshold the entry is
          flagged <span class="badge" style="background:#dc3545">stale</span>.
        </p>
      </div>
    </div>
  </div>
  <div class="col-md-6">
    <div class="card h-100">
      <div class="card-body text-center">
        <i class="bi bi-globe text-success" style="font-size:2rem"></i>
        <h6 class="mt-2">Embedded Flask Server</h6>
        <p class="text-muted small mb-0">
          Serves the dashboard on <code>{WEB_HOST}:{WEB_PORT}</code>.
          The browser polls <code>/api/status</code> every few seconds
          and updates the table without a page reload.
        </p>
      </div>
    </div>
  </div>
</div>

<div class="card mb-4">
  <div class="card-header"><i class="bi bi-box-seam"></i> Python Dependencies</div>
  <div class="card-body p-0">
    <table class="table table-sm mb-0">
      <thead class="table-light">
        <tr><th class="ps-3">Package</th><th>Version</th><th>Purpose</th></tr>
      </thead>
      <tbody>
        <tr><td class="ps-3"><code>flask</code></td>
            <td>{flask_ver}</td>
            <td>Embedded web server &amp; REST API</td></tr>
        <tr><td class="ps-3"><code>pyyaml</code></td>
            <td>{yaml_ver}</td>
            <td>YAML configuration file parsing</td></tr>
      </tbody>
    </table>
  </div>
</div>
"""
    return nav + body + foot


@app.route("/about")
def about():
    return _build_about_page()


# ---------------------------------------------------------------------------
# HTML – Config page (dynamically built from WATCH_ENTRIES and globals)
# ---------------------------------------------------------------------------
def _build_config_page() -> str:
    files_entries = [e for e in WATCH_ENTRIES if e.get("kind") == "file"]
    dirs_entries  = [e for e in WATCH_ENTRIES if e.get("kind") == "directory"]

    cfg_display = CONFIG_PATH if CONFIG_PATH else "(defaults — no config file loaded)"

    # Try to read the raw YAML so the user can inspect exactly what was parsed.
    raw_yaml = None
    if CONFIG_PATH:
        try:
            with open(CONFIG_PATH) as fh:
                raw_yaml = fh.read()
        except OSError as exc:
            raw_yaml = f"# Could not read {CONFIG_PATH}: {exc}"

    daemon_badge = (
        '<span class="badge bg-success">enabled</span>' if DAEMON
        else '<span class="badge bg-secondary">disabled</span>'
    )
    pid_val = (f'<code class="small">{_html.escape(PID_FILE)}</code>'
               if PID_FILE else '<span class="text-muted">(none)</span>')
    log_val = (f'<code class="small">{_html.escape(LOG_FILE)}</code>'
               if LOG_FILE else '<span class="text-muted">(none)</span>')

    def watch_rows(entries, noun):
        if not entries:
            return (f'<tr><td colspan="2" class="text-muted p-3">'
                    f'No {noun} configured.</td></tr>')
        rows = ""
        for e in entries:
            label = e.get("label", "")
            path  = e["path"]
            delay = e["delay"]
            ep = _html.escape(path)
            el = _html.escape(label)
            if label and label != path:
                name_cell = (f'<strong>{el}</strong><br>'
                             f'<span class="path-cell text-muted">{ep}</span>')
            else:
                name_cell = f'<span class="path-cell">{ep}</span>'
            rows += (
                f'<tr>'
                f'<td class="ps-3">{name_cell}</td>'
                f'<td class="text-nowrap">{fmt_duration(delay)}'
                f' <small class="text-muted">({delay}&nbsp;s)</small></td>'
                f'</tr>'
            )
        return rows

    files_rows = watch_rows(files_entries, "files")
    dirs_rows  = watch_rows(dirs_entries,  "directories")

    raw_section = ""
    copy_js     = ""
    if raw_yaml is not None:
        raw_section = f"""
<div class="card mb-4">
  <div class="card-header d-flex justify-content-between align-items-center">
    <span><i class="bi bi-file-code"></i> Raw Configuration
      <small class="text-muted ms-2">{_html.escape(cfg_display)}</small>
    </span>
    <button class="btn btn-sm btn-outline-secondary" id="copy-btn" onclick="copyYaml()">
      <i class="bi bi-clipboard"></i> Copy
    </button>
  </div>
  <div class="card-body p-0">
    <pre id="yaml-pre" class="mb-0 p-3"
         style="background:#f8f9fa;border-radius:0 0 .375rem .375rem;
                font-size:.85em;overflow-x:auto">{_html.escape(raw_yaml)}</pre>
  </div>
</div>"""
        copy_js = """
<script>
function copyYaml() {
  const text = document.getElementById('yaml-pre').textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.innerHTML = '<i class="bi bi-clipboard-check"></i> Copied!';
    setTimeout(() => {
      btn.innerHTML = '<i class="bi bi-clipboard"></i> Copy';
    }, 2000);
  }).catch(() => {
    const el = document.getElementById('yaml-pre');
    const r  = document.createRange();
    r.selectNodeContents(el);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(r);
  });
}
</script>"""

    body = f"""
<div class="card mb-4">
  <div class="card-header"><i class="bi bi-gear"></i> Server Settings</div>
  <div class="card-body p-0">
    <table class="table table-sm mb-0">
      <tbody>
        <tr><th class="ps-3" style="width:30%">Config file</th>
            <td><code class="small">{_html.escape(cfg_display)}</code></td></tr>
        <tr><th class="ps-3">Web bind address</th>
            <td><code>{_html.escape(WEB_HOST)}:{WEB_PORT}</code></td></tr>
        <tr><th class="ps-3">Poll interval</th>
            <td>{fmt_duration(POLL_INTERVAL)}
              <small class="text-muted">({POLL_INTERVAL}&nbsp;s)</small></td></tr>
        <tr><th class="ps-3">Daemon mode</th><td>{daemon_badge}</td></tr>
        <tr><th class="ps-3">PID file</th><td>{pid_val}</td></tr>
        <tr><th class="ps-3">Log file</th><td>{log_val}</td></tr>
      </tbody>
    </table>
  </div>
</div>

<div class="card mb-4">
  <div class="card-header">
    <i class="bi bi-file-earmark-text"></i> Watched Files
    <small class="text-muted ms-2">{len(files_entries)} configured</small>
  </div>
  <div class="card-body p-0">
    <table class="table table-sm mb-0">
      <thead class="table-light">
        <tr><th class="ps-3">Label / Path</th><th>Threshold</th></tr>
      </thead>
      <tbody>{files_rows}</tbody>
    </table>
  </div>
</div>

<div class="card mb-4">
  <div class="card-header">
    <i class="bi bi-folder2-open"></i> Watched Directories
    <small class="text-muted ms-2">{len(dirs_entries)} configured</small>
  </div>
  <div class="card-body p-0">
    <table class="table table-sm mb-0">
      <thead class="table-light">
        <tr><th class="ps-3">Label / Path</th><th>Threshold</th></tr>
      </thead>
      <tbody>{dirs_rows}</tbody>
    </table>
  </div>
</div>
{raw_section}"""

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Config &mdash; mu2edaq Disk Watcher</title>
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
    crossorigin="anonymous">
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
  <style>
    body {{ background: #f8f9fa; }}
    .navbar-brand {{ font-weight: 700; letter-spacing: .05em; }}
    .path-cell {{ font-family: monospace; font-size: 0.85em; word-break: break-all; }}
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-4">
  <div class="container-fluid">
    <a class="navbar-brand" href="/"><i class="bi bi-hdd-stack"></i> mu2edaq Disk Watcher</a>
    <div class="collapse navbar-collapse">
      <ul class="navbar-nav">
        <li class="nav-item"><a class="nav-link" href="/">Watcher</a></li>
        <li class="nav-item"><a class="nav-link active" href="/config">Config</a></li>
        <li class="nav-item"><a class="nav-link" href="/about">About</a></li>
      </ul>
    </div>
  </div>
</nav>
<div class="container-fluid px-4">
{body}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
  crossorigin="anonymous"></script>{copy_js}
</body>
</html>"""
    return page


@app.route("/config")
def config():
    return _build_config_page()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    global WEB_HOST, WEB_PORT, POLL_INTERVAL, CONFIG_PATH, WATCH_ENTRIES
    global DAEMON, PID_FILE, LOG_FILE

    # ---- first pass: find --config before full argument parsing ----
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", "-c", default="diskwatcher.yaml")
    pre_args, _ = pre.parse_known_args()

    config_required = "--config" in sys.argv or "-c" in sys.argv
    cfg = load_config(pre_args.config, required=config_required)

    # ---- apply YAML values to module globals ----
    wcfg = cfg.get("watcher", {})
    if "web_host"      in wcfg: WEB_HOST      = wcfg["web_host"]
    if "web_port"      in wcfg: WEB_PORT      = int(wcfg["web_port"])
    if "poll_interval" in wcfg: POLL_INTERVAL = int(wcfg["poll_interval"])
    if "daemon"        in wcfg: DAEMON        = bool(wcfg["daemon"])
    if "pid_file"      in wcfg: PID_FILE      = wcfg["pid_file"]
    if "log_file"      in wcfg: LOG_FILE      = wcfg["log_file"]

    WATCH_ENTRIES = entries_from_config(cfg)
    CONFIG_PATH   = pre_args.config if cfg else None

    # ---- full CLI parse — flags override YAML ----
    parser = argparse.ArgumentParser(
        prog="mu2edaq-diskwatcher",
        description="Monitor file modification times and serve a web dashboard.",
    )
    parser.add_argument(
        "--config", "-c", default="diskwatcher.yaml", metavar="FILE",
        help="YAML configuration file (default: diskwatcher.yaml)",
    )
    parser.add_argument(
        "--host", default=None, metavar="ADDR",
        help=f"Web server bind address (default: {WEB_HOST})",
    )
    parser.add_argument(
        "--port", "-p", type=int, default=None, metavar="PORT",
        help=f"Web server port (default: {WEB_PORT})",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=None, metavar="SECONDS",
        help=f"Seconds between mtime checks (default: {POLL_INTERVAL})",
    )
    parser.add_argument(
        "--daemon", "-d", action="store_true", default=None,
        help="Run as a background daemon (POSIX only)",
    )
    parser.add_argument(
        "--pid-file", default=None, metavar="FILE",
        help="Write daemon PID to FILE (e.g. /tmp/diskwatcher.pid)",
    )
    parser.add_argument(
        "--log-file", default=None, metavar="FILE",
        help="Redirect daemon stdout/stderr to FILE (default: /dev/null)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {VERSION}",
    )
    args = parser.parse_args()

    # CLI flags override YAML (only when actually supplied)
    if args.host          is not None: WEB_HOST      = args.host
    if args.port          is not None: WEB_PORT      = args.port
    if args.poll_interval is not None: POLL_INTERVAL = args.poll_interval
    if args.daemon:                    DAEMON        = True
    if args.pid_file      is not None: PID_FILE      = args.pid_file
    if args.log_file      is not None: LOG_FILE      = args.log_file

    if not WATCH_ENTRIES:
        print("[Config] Warning: no files or paths configured. "
              "Add entries to the YAML config file.", file=sys.stderr)

    # ---- daemonize before starting threads ----
    if DAEMON:
        log_dest = LOG_FILE or os.devnull
        print(f"[Daemon] Daemonizing. Log: {log_dest}  "
              f"PID file: {PID_FILE or '(none)'}")
        try:
            daemonize(LOG_FILE)
        except RuntimeError as exc:
            print(f"[Daemon] {exc}", file=sys.stderr)
            sys.exit(1)
        # From here stdout/stderr go to the log file.
        if PID_FILE:
            _write_pid_file(PID_FILE)

    print(f"[Config] Watching {len(WATCH_ENTRIES)} path(s), "
          f"poll interval {POLL_INTERVAL} s")
    print(f"[Web]    Dashboard at http://localhost:{WEB_PORT}")

    # Run one poll synchronously so the first page load has data
    _do_poll()

    # Start background poller thread
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    # Start Flask (werkzeug development server)
    app.run(host=WEB_HOST, port=WEB_PORT, use_reloader=False,
            threaded=True)


if __name__ == "__main__":
    main()
