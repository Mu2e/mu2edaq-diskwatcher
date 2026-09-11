"""Federation: fetch other diskwatcher instances' results over their JSON API.

Each configured peer is asked for ``/api/state`` — the one endpoint that
carries every entry with every field, plus the peer's version and poll timing.
The reply is validated, each entry is stamped with the peer it came from, and
the whole record is installed in :data:`~mu2edaq_diskwatcher.state.PEERS`.

Design points, each of which a future maintainer might otherwise "fix":

* **Aggregation is one hop.**  A peer is asked for its *own* entries only
  (``/api/state`` without ``?peers=1``), so A watching B watching A never
  loops and never shows a path three times.  Every dashboard shows exactly
  what its own config names: local paths plus the paths of its direct peers.
* **A peer's states are trusted, not re-derived.**  The peer owns its
  thresholds and evaluated them against a filesystem it can see.  Recomputing
  here would need its config and would disagree with its own dashboard.
* **Failure keeps the last good data.**  A control-room display that blanks a
  whole node's volumes because one HTTP request timed out is worse than one
  that dims them and says "unreachable since 14:02".  The retained entries are
  flagged ``stale`` and excluded from every aggregate count.
* **A peer that is this very process is refused.**  Both sides publish an
  ``instance_id``; matching ours means the URL resolved back here.
* **stdlib only.**  ``urllib`` is enough for one GET per peer per interval, and
  the deployment hosts are offline-installed.
"""

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from . import INSTANCE_ID, __version__
from .settings import get_settings
from .state import PEERS, peer_record

#: Endpoint fetched from every peer.  Deliberately without ``?peers=1``.
STATE_PATH = "/api/state"

#: Refuse to buffer more than this from one peer.  A real /api/state is tens
#: of kilobytes; a megabyte means we are talking to something else.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

USER_AGENT = f"mu2edaq-diskwatcher/{__version__}"


class PeerError(Exception):
    """A fetch that failed for a reason worth showing to an operator."""


def fetch_state(url: str, timeout: float) -> dict:
    """GET ``<url>/api/state`` and return the decoded payload.

    Raises :class:`PeerError` with a one-line, operator-readable reason for
    every failure mode — refused, timed out, HTTP error, not JSON, not a
    diskwatcher — so the caller has nothing to interpret.
    """
    request = urllib.request.Request(
        url.rstrip("/") + STATE_PATH,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise PeerError(f"HTTP {response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise PeerError(f"HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise PeerError(f"timed out after {timeout:g} s") from exc
        raise PeerError(str(reason)) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise PeerError(f"timed out after {timeout:g} s") from exc
    except OSError as exc:
        raise PeerError(str(exc)) from exc

    if len(raw) > MAX_RESPONSE_BYTES:
        raise PeerError(f"response larger than {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PeerError("response is not JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise PeerError("response is not a diskwatcher /api/state payload")
    return payload


def stamp_entries(raw_entries: List, peer: dict) -> List[dict]:
    """Copy the peer's entries, adding ``peer`` and ``peer_url`` to each.

    Anything that is not an entry-shaped dict is dropped rather than passed on
    to the browser, where a missing ``path`` would render as ``undefined``.
    """
    out = []
    for entry in raw_entries:
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        stamped = dict(entry)
        stamped["peer"] = peer["label"]
        stamped["peer_url"] = peer["url"]
        out.append(stamped)
    return out


def poll_peer(peer: dict, previous: Optional[dict], now: Optional[float] = None) -> dict:
    """Fetch one peer and return its complete new record.

    *previous* is the record being replaced; on failure its entries and
    identity are carried forward so the UI can keep showing the last good
    data, dimmed, with the time it was last current.
    """
    now = time.time() if now is None else now
    settings = get_settings()
    timeout = peer.get("timeout") or settings.peer_timeout
    record = peer_record(peer, status="pending")
    attempts = (previous or {}).get("attempts", 0) + 1
    started = time.time()

    try:
        payload = fetch_state(peer["url"], timeout)
        if payload.get("instance_id") and payload["instance_id"] == INSTANCE_ID:
            raise PeerError("peer is this instance (the URL resolves to ourselves)")
        record.update({
            "status":           "ok",
            "ok":               True,
            "error":            None,
            "entries":          stamp_entries(payload["entries"], peer),
            "last_ok":          now,
            "failures":         0,
            "version":            payload.get("version"),
            "hostname":           payload.get("hostname"),
            "instance_id":        payload.get("instance_id"),
            "peer_poll_interval": payload.get("poll_interval"),
            "peer_poll_age_s":    payload.get("poll_age_s"),
            "peer_generated":     payload.get("generated"),
        })
    except PeerError as exc:
        record.update({
            "status":   "error",
            "ok":       False,
            "error":    str(exc),
            "failures": (previous or {}).get("failures", 0) + 1,
        })
        if previous:
            # Keep what we knew.  `stale` is derived in PeerStore.snapshot().
            for key in ("entries", "last_ok", "version", "hostname", "instance_id",
                        "peer_poll_interval", "peer_poll_age_s", "peer_generated"):
                record[key] = previous.get(key, record[key])

    record["attempts"] = attempts
    record["fetched_at"] = now
    record["fetch_duration_s"] = round(time.time() - started, 3)
    return record


def do_peer_poll() -> None:
    """Fetch every enabled peer concurrently and publish the results."""
    settings = get_settings()
    peers = list(settings.peers)
    if not peers:
        return

    enabled = [p for p in peers if p.get("enabled", True)]
    for peer in peers:
        if not peer.get("enabled", True):
            PEERS.update(peer["url"], peer_record(peer, status="disabled"))
    if not enabled:
        return

    now = time.time()
    previous = {p["url"]: PEERS.get(p["url"]) for p in enabled}
    with ThreadPoolExecutor(max_workers=min(len(enabled), 10)) as pool:
        futures = {pool.submit(poll_peer, p, previous[p["url"]], now): p
                   for p in enabled}
        for future in as_completed(futures):
            peer = futures[future]
            try:
                record = future.result()
            except Exception as exc:              # a bug, not a network fault
                record = peer_record(peer, status="error")
                record.update({"error": f"internal error: {exc}",
                               "fetched_at": now, "attempts":
                               (previous[peer["url"]] or {}).get("attempts", 0) + 1})
            _log_transition(previous[peer["url"]], record)
            PEERS.update(peer["url"], record)


def _log_transition(before: Optional[dict], after: dict) -> None:
    """One log line per change of connection state; every fetch when verbose."""
    was = (before or {}).get("status")
    now = after["status"]
    verbose = get_settings().verbose
    if now == "ok" and (was != "ok" or verbose):
        print(f"[Peers] {after['label']} ({after['url']}): connected, "
              f"{len(after['entries'])} entries, diskwatcher {after['version']}"
              f" on {after['hostname']}, {after['fetch_duration_s']} s")
    elif now == "error" and (was != "error" or verbose):
        kept = f"; keeping {len(after['entries'])} entries from the last good fetch" \
            if after["entries"] else ""
        print(f"[Peers] {after['label']} ({after['url']}): unreachable: "
              f"{after['error']}{kept}", file=sys.stderr)


def peer_loop() -> None:
    """Background thread: fetch every peer, then sleep for the peer interval.

    Separate from the local poll loop so a slow peer never delays a local
    stat, and so the two cadences can differ.
    """
    while True:
        try:
            do_peer_poll()
        except Exception as exc:
            print(f"[Peers] Unexpected error: {exc}", file=sys.stderr)
        time.sleep(get_settings().effective_peer_interval())
