"""JSON API.

``/api/status`` is frozen: its top-level keys and the per-entry keys it carried
before free-space and file-size monitoring existed are guaranteed to stay, with
the same types.  Keys are added, never removed or retyped — the one documented
exception being that ``delay`` may now be ``null`` for an entry that opts out of
mtime monitoring.  ``tests/test_web.py::test_status_legacy_schema`` locks this.

Peers
-----
Every list endpoint returns this instance's own entries by default.  Adding
``?peers=1`` appends two keys:

``peers``
    One record per configured peer — connection details and status, that
    peer's entries filtered the same way the endpoint filters local ones, and
    a summary of just those entries.
``aggregate``
    The endpoint's summary recomputed over local entries plus the entries of
    every peer currently reachable, with a ``peers`` sub-dict of connection
    counts.  Data retained from an unreachable peer is not counted.

The default stays local-only for two reasons: existing consumers see exactly
the payload they always did, and a peer fetching *our* ``/api/state`` gets our
own entries and nothing further away, so federation is one hop and cannot
loop.  See :mod:`mu2edaq_diskwatcher.peers`.
"""

import socket
import time
from datetime import datetime, timezone
from typing import Callable, List

from flask import Blueprint, jsonify, request

from .. import INSTANCE_ID, START_TIME, __version__
from ..settings import get_settings
from ..state import (
    PEERS,
    STORE,
    reachable_entries,
    size_entries,
    size_summary,
    space_entries,
    space_summary,
    watch_summary,
)
from ..thresholds import SEVERITY, WATCH_SEVERITY

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

#: Same idea for the Watcher page: a group opens once something is stale or
#: missing.  ``unmonitored`` ranks below this on purpose.
WATCH_ALERT_RANK = WATCH_SEVERITY["stale"]

#: The per-entry keys /api/status carried before this feature landed.
LEGACY_ENTRY_KEYS = frozenset({
    "path", "label", "delay", "kind", "remote", "ssh_host", "mtime", "mtime_str",
    "age_s", "age_str", "delay_str", "stale", "missing", "error",
    "disk_total", "disk_used", "disk_free", "disk_pct",
    "disk_total_str", "disk_free_str",
})

_TRUTHY = {"1", "true", "yes", "on", "all"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _want_peers() -> bool:
    return request.args.get("peers", "").strip().lower() in _TRUTHY


def _peer_views(select: Callable[[List[dict]], List[dict]],
                summarise: Callable[[List[dict]], dict]) -> List[dict]:
    """Peer records with entries narrowed by *select* and summarised."""
    views = []
    for record in PEERS.snapshot():
        entries = select(record["entries"])
        record["entries"] = entries
        record["total"] = len(entries)
        record["summary"] = summarise(entries)
        views.append(record)
    return views


def _aggregate(local: List[dict], peers: List[dict],
               summarise: Callable[[List[dict]], dict]) -> dict:
    """*summarise* over local plus reachable-peer entries, with peer counts."""
    result = summarise(list(local) + reachable_entries(peers))
    result["local"] = len(local)
    result["peers"] = PEERS.counts()
    return result


def _with_peers(payload: dict, local: List[dict], select, summarise) -> dict:
    if _want_peers():
        peers = _peer_views(select, summarise)
        payload["peers"] = peers
        payload["aggregate"] = _aggregate(local, peers, summarise)
    return payload


@bp.route("/status")
def api_status():
    """Legacy endpoint.  Shape frozen; entries are simply richer than before."""
    data = STORE.snapshot()
    n_stale = sum(1 for s in data if s["stale"])
    payload = {
        "files":         data,
        "total":         len(data),
        "stale":         n_stale,
        "ok":            len(data) - n_stale,
        "poll_interval": get_settings().poll_interval,
        "alert_rank":    WATCH_ALERT_RANK,
    }
    return jsonify(_with_peers(payload, data, list, watch_summary))


def _all_summaries(entries: List[dict]) -> dict:
    return {
        "watch": watch_summary(entries),
        "space": space_summary(space_entries(entries)),
        "size":  size_summary(size_entries(entries)),
    }


@bp.route("/state")
def api_state():
    """Everything: all entries plus all three summaries.

    Also what a peer fetches from us.  ``hostname`` and ``instance_id`` are
    here so the fetching side can name us and recognise itself.
    """
    data = STORE.snapshot()
    meta = STORE.meta()
    payload = {
        "version":       __version__,
        "instance_id":   INSTANCE_ID,
        "hostname":      socket.gethostname(),
        "generated":     _now_iso(),
        "poll_interval": get_settings().poll_interval,
        "total":         len(data),
        "summary":       _all_summaries(data),
        "entries":       data,
        **meta,
    }
    return jsonify(_with_peers(payload, data, list, _all_summaries))


@bp.route("/space")
def api_space():
    """Feed for the Disk Space page: directories with a ``space:`` block."""
    entries = space_entries(STORE.snapshot())
    payload = {
        "poll_interval": get_settings().poll_interval,
        "alert_rank":    ALERT_RANK,
        "summary":       space_summary(entries),
        "entries":       entries,
        **STORE.meta(),
    }
    return jsonify(_with_peers(payload, entries, space_entries, space_summary))


@bp.route("/sizes")
def api_sizes():
    """Feed for the File Sizes page: files with a ``size:`` block."""
    entries = size_entries(STORE.snapshot())
    payload = {
        "poll_interval": get_settings().poll_interval,
        "alert_rank":    ALERT_RANK,
        "summary":       size_summary(entries),
        "entries":       entries,
        **STORE.meta(),
    }
    return jsonify(_with_peers(payload, entries, size_entries, size_summary))


@bp.route("/entries")
def api_entries():
    """Generic filtered list: ?kind=file|directory&state=CRITICAL&monitored=space.

    ``?peers=1`` folds in the entries of every reachable peer (each stamped
    with ``peer`` and ``peer_url``) before the filters run; ``?peer=LABEL``
    then keeps only that peer's entries.
    """
    entries = STORE.snapshot()
    if _want_peers():
        entries = entries + reachable_entries(PEERS.snapshot())

    peer = request.args.get("peer")
    if peer:
        entries = [e for e in entries
                   if e.get("peer") == peer or e.get("peer_url") == peer]

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


@bp.route("/peers")
def api_peers():
    """Connection status of every configured peer, without their entries.

    For diagnostics and the Config page.  Each record carries per-peer watch,
    space and size summaries so a script can see at a glance what a peer is
    contributing, and ``counts`` totals the connection states.
    """
    peers = []
    for record in PEERS.snapshot():
        entries = record.pop("entries")
        record["summary"] = _all_summaries(entries)
        peers.append(record)
    settings = get_settings()
    return jsonify({
        "instance_id":   INSTANCE_ID,
        "hostname":      socket.gethostname(),
        "peer_interval": settings.effective_peer_interval(),
        "peer_timeout":  settings.peer_timeout,
        "counts":        PEERS.counts(),
        "peers":         peers,
    })


@bp.route("/config")
def api_config():
    """Active settings, the parsed watch entries, peers, and any config problems."""
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
    payload["peers"] = [dict(p) for p in settings.peers]
    return jsonify(payload)


def _limits_json(limits):
    if limits is None:
        return None
    return {level: (t.raw if t is not None else None)
            for level, t in limits.items()}


@bp.route("/health")
def api_health():
    """Liveness of the poller itself, for crs-app and external monitoring.

    ``status`` reflects this instance only: an unreachable peer is reported
    under ``peers`` but does not degrade us, because the peer's own
    ``/api/health`` is the place that answers for it.
    """
    settings = get_settings()
    meta = STORE.meta()
    age = meta["poll_age_s"]
    issues = len(settings.config_issues)
    stalled = age is None or age > 3 * settings.poll_interval
    return jsonify({
        "status":        "degraded" if (stalled or issues) else "ok",
        "version":       __version__,
        "instance_id":   INSTANCE_ID,
        "uptime_s":      int(time.time() - START_TIME.timestamp()),
        "entries":       len(STORE),
        "poll_interval": settings.poll_interval,
        "config_issues": issues,
        "peers":         PEERS.counts(),
        **meta,
    })


@bp.route("/version")
def api_version():
    return jsonify({"name": "mu2edaq-diskwatcher", "version": __version__,
                    "instance_id": INSTANCE_ID, "hostname": socket.gethostname()})
