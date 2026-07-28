"""JSON API.

``/api/status`` is frozen: its top-level keys and the per-entry keys it carried
before free-space and file-size monitoring existed are guaranteed to stay, with
the same types.  Keys are added, never removed or retyped — the one documented
exception being that ``delay`` may now be ``null`` for an entry that opts out of
mtime monitoring.  ``tests/test_web.py::test_status_legacy_schema`` locks this.
"""

import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from .. import START_TIME, __version__
from ..settings import get_settings
from ..state import (
    STORE,
    size_entries,
    size_summary,
    space_entries,
    space_summary,
    watch_summary,
)
from ..thresholds import SEVERITY

bp = Blueprint("api", __name__, url_prefix="/api")

#: Rank at or above which the dashboards hold a host group open, however the
#: operator last left it.  Shipped to the browser rather than hardcoded there,
#: so the severity ordering stays defined in exactly one place: bump CRITICAL
#: in SEVERITY and the front end follows without being touched.
#:
#: Because it is a rank and not a name, everything the server ranks *above*
#: CRITICAL qualifies too -- FULL, UNKNOWN and MISSING.  That is intended: a
#: vanished path or a filesystem that cannot be measured is at least as urgent
#: as one that is nearly out of room.
ALERT_RANK = SEVERITY["CRITICAL"]

#: The per-entry keys /api/status carried before this feature landed.
LEGACY_ENTRY_KEYS = frozenset({
    "path", "label", "delay", "kind", "remote", "ssh_host", "mtime", "mtime_str",
    "age_s", "age_str", "delay_str", "stale", "missing", "error",
    "disk_total", "disk_used", "disk_free", "disk_pct",
    "disk_total_str", "disk_free_str",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@bp.route("/status")
def api_status():
    """Legacy endpoint.  Shape frozen; entries are simply richer than before."""
    data = STORE.snapshot()
    n_stale = sum(1 for s in data if s["stale"])
    return jsonify({
        "files":         data,
        "total":         len(data),
        "stale":         n_stale,
        "ok":            len(data) - n_stale,
        "poll_interval": get_settings().poll_interval,
    })


@bp.route("/state")
def api_state():
    """Everything: all entries plus all three summaries."""
    data = STORE.snapshot()
    meta = STORE.meta()
    return jsonify({
        "version":       __version__,
        "generated":     _now_iso(),
        "poll_interval": get_settings().poll_interval,
        "total":         len(data),
        "summary": {
            "watch": watch_summary(data),
            "space": space_summary(space_entries(data)),
            "size":  size_summary(size_entries(data)),
        },
        "entries": data,
        **meta,
    })


@bp.route("/space")
def api_space():
    """Feed for the Disk Space page: directories with a ``space:`` block."""
    entries = space_entries(STORE.snapshot())
    return jsonify({
        "poll_interval": get_settings().poll_interval,
        "alert_rank":    ALERT_RANK,
        "summary":       space_summary(entries),
        "entries":       entries,
        **STORE.meta(),
    })


@bp.route("/sizes")
def api_sizes():
    """Feed for the File Sizes page: files with a ``size:`` block."""
    entries = size_entries(STORE.snapshot())
    return jsonify({
        "poll_interval": get_settings().poll_interval,
        "alert_rank":    ALERT_RANK,
        "summary":       size_summary(entries),
        "entries":       entries,
        **STORE.meta(),
    })


@bp.route("/entries")
def api_entries():
    """Generic filtered list: ?kind=file|directory&state=CRITICAL&monitored=space."""
    entries = STORE.snapshot()

    kind = request.args.get("kind")
    if kind:
        entries = [e for e in entries if e["kind"] == kind]

    monitored = request.args.get("monitored")
    if monitored in ("space", "size"):
        entries = [e for e in entries if e.get(f"{monitored}_monitored")]

    state = request.args.get("state")
    if state:
        wanted = state.upper()
        entries = [e for e in entries
                   if wanted in (str(e.get("space_state")).upper(),
                                 str(e.get("size_state")).upper(),
                                 str(e.get("watch_state")).upper())]

    return jsonify({"total": len(entries), "entries": entries})


@bp.route("/config")
def api_config():
    """Active settings, the parsed watch entries, and any config problems."""
    settings = get_settings()
    entries = []
    for entry in settings.entries:
        ssh_cfg = entry.get("ssh")
        entries.append({
            "path":          entry["path"],
            "label":         entry.get("label"),
            "kind":          entry.get("kind"),
            "delay":         entry.get("delay"),
            "ssh_host":      str(ssh_cfg.get("host", "")) if ssh_cfg else None,
            "space":         _limits_json(entry.get("space")),
            "size":          _limits_json(entry.get("size")),
            "allow_empty":   entry.get("allow_empty", False),
            "config_errors": entry.get("config_errors", []),
        })
    payload = settings.as_dict()
    payload["issues"] = settings.config_issues
    payload["entries"] = entries
    return jsonify(payload)


def _limits_json(limits):
    if limits is None:
        return None
    return {level: (t.raw if t is not None else None)
            for level, t in limits.items()}


@bp.route("/health")
def api_health():
    """Liveness of the poller itself, for crs-app and external monitoring."""
    settings = get_settings()
    meta = STORE.meta()
    age = meta["poll_age_s"]
    issues = len(settings.config_issues)
    stalled = age is None or age > 3 * settings.poll_interval
    return jsonify({
        "status":        "degraded" if (stalled or issues) else "ok",
        "version":       __version__,
        "uptime_s":      int(time.time() - START_TIME.timestamp()),
        "entries":       len(STORE),
        "poll_interval": settings.poll_interval,
        "config_issues": issues,
        **meta,
    })


@bp.route("/version")
def api_version():
    return jsonify({"name": "mu2edaq-diskwatcher", "version": __version__})
