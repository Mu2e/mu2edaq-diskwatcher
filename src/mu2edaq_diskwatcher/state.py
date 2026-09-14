"""Shared poll results.

Written by the poller thread, read by Flask request handlers.  ``StateStore``
replaces the bare ``state_lock`` / ``file_states`` pair.

Thread-safety contract: :meth:`StateStore.replace` swaps in a brand-new list of
brand-new dicts, and :meth:`StateStore.snapshot` returns a shallow copy of that
list.  Readers therefore never see a half-written entry.  **Do not** "optimise"
the poller to mutate existing dicts in place — that would break this guarantee.

:class:`PeerStore` holds the same kind of data fetched from other diskwatcher
instances, one record per configured peer, under the same contract: the peer
thread installs a whole new record per peer and never edits one in place.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

#: Order in which states are displayed and counted on the dashboards.
WATCH_STATES = ("ok", "stale", "missing", "unmonitored")
SPACE_STATES = ("GOOD", "WARNING", "CRITICAL", "FULL", "MISSING", "UNKNOWN")
SIZE_STATES  = ("GOOD", "EMPTY", "WARNING", "CRITICAL", "FULL", "MISSING", "UNKNOWN")

#: Peer connection states.  ``error`` may still carry the entries from the
#: last successful fetch, flagged ``stale`` so the UI can dim them.
PEER_STATUSES = ("pending", "ok", "error", "disabled")


class StateStore:
    """Latest poll results, guarded by a lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: List[dict] = []
        self._last_poll: Optional[float] = None
        self._poll_duration: Optional[float] = None

    def replace(self, entries: List[dict], duration: Optional[float] = None) -> None:
        """Atomically install a fresh set of poll results."""
        with self._lock:
            self._entries = list(entries)
            self._last_poll = time.time()
            self._poll_duration = duration

    def snapshot(self) -> List[dict]:
        """Return a copy of the current entry list."""
        with self._lock:
            return list(self._entries)

    def meta(self) -> Dict[str, Optional[float]]:
        """Poll timing, for /api/health and the dashboard footers."""
        with self._lock:
            last = self._last_poll
            duration = self._poll_duration
        return {
            "last_poll":       last,
            "poll_age_s":      None if last is None else round(time.time() - last, 1),
            "poll_duration_s": duration,
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


#: The process-wide store.
STORE = StateStore()


# ---------------------------------------------------------------------------
# Peers
# ---------------------------------------------------------------------------
def _stamp(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def peer_record(peer: dict, status: str = "pending") -> dict:
    """Every key a peer record carries, at its "nothing fetched yet" value.

    Built once from the config and then only ever replaced whole, so a record
    in any state has the same shape; ``tests/test_peers.py`` asserts parity.
    """
    return {
        # ---- identity, from our own config ----
        "label":     peer["label"],
        "url":       peer["url"],
        "enabled":   peer.get("enabled", True),
        "timeout":   peer.get("timeout"),
        "config_errors": list(peer.get("config_errors") or []),

        # ---- provenance: written in the config, or found by discovery ----
        "source":              peer.get("source", "static"),
        "discovery_id":        peer.get("discovery_id"),
        "discovery_last_seen": peer.get("discovery_last_seen"),
        "discovery_missing":   bool(peer.get("discovery_missing", False)),

        # ---- connection ----
        "status":           status,          # one of PEER_STATUSES
        "ok":               status == "ok",
        "error":            None,
        "fetched_at":       None,            # last attempt, success or not
        "fetch_duration_s": None,
        "last_ok":          None,            # last successful fetch
        "attempts":         0,
        "failures":         0,               # consecutive, reset on success

        # ---- what the peer said about itself ----
        "version":            None,
        "hostname":           None,
        "instance_id":        None,
        "peer_poll_interval": None,
        "peer_poll_age_s":    None,          # as reported at fetch time
        "peer_generated":     None,

        # ---- its entries, stamped with peer/peer_url ----
        "entries": [],
    }


class PeerStore:
    """Latest fetch result per configured peer, in configuration order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._order: List[str] = []
        self._records: Dict[str, dict] = {}

    def configure(self, peers: List[dict]) -> None:
        """Install one pending (or disabled) record per configured peer.

        Called once at startup.  Records for peers no longer configured are
        dropped; a peer already present keeps whatever it had, so a reconfigure
        in tests does not throw away fetched data.
        """
        with self._lock:
            self._order = [p["url"] for p in peers]
            fresh = {}
            for peer in peers:
                status = "pending" if peer.get("enabled", True) else "disabled"
                fresh[peer["url"]] = self._records.get(peer["url"]) or \
                    peer_record(peer, status)
            self._records = fresh

    def update(self, url: str, record: dict) -> None:
        """Atomically replace the record for one peer."""
        with self._lock:
            if url not in self._records:
                self._order.append(url)
            self._records[url] = record

    def remove(self, url: str) -> None:
        """Forget a peer, e.g. one discovery has not seen for the grace period."""
        with self._lock:
            self._records.pop(url, None)
            self._order = [u for u in self._order if u != url]

    def get(self, url: str) -> Optional[dict]:
        with self._lock:
            return self._records.get(url)

    def snapshot(self) -> List[dict]:
        """Records in config order, each a shallow copy with derived fields added.

        The derived fields are ages relative to *now* and display stamps, so
        the JSON API and the templates do not each redo the arithmetic.
        """
        now = time.time()
        with self._lock:
            records = [dict(self._records[url]) for url in self._order
                       if url in self._records]
        for rec in records:
            rec["entries"] = list(rec["entries"])
            fetched, last_ok = rec["fetched_at"], rec["last_ok"]
            rec["fetched_age_s"] = None if fetched is None else round(now - fetched, 1)
            rec["last_ok_age_s"] = None if last_ok is None else round(now - last_ok, 1)
            rec["fetched_at_str"] = _stamp(fetched)
            rec["last_ok_str"] = _stamp(last_ok)
            seen = rec.get("discovery_last_seen")
            rec["discovery_seen_age_s"] = None if seen is None else round(now - seen, 1)
            # Entries left over from a fetch that has since failed.
            rec["stale"] = rec["status"] == "error" and bool(rec["entries"])
            rec["total"] = len(rec["entries"])
        return records

    def counts(self) -> Dict[str, int]:
        """``{configured, ok, error, pending, disabled}`` for health and the UI."""
        with self._lock:
            statuses = [r["status"] for r in self._records.values()]
        counts = {name: statuses.count(name) for name in PEER_STATUSES}
        counts["configured"] = len(statuses)
        return counts

    def clear(self) -> None:
        with self._lock:
            self._order = []
            self._records = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


#: The process-wide peer store.
PEERS = PeerStore()


def reachable_entries(peers: List[dict]) -> List[dict]:
    """Entries from every peer currently in the ``ok`` state, concatenated.

    Data retained from a peer that has since become unreachable is excluded
    on purpose: it is shown, dimmed, under that peer's heading, but it must
    not be counted as a current reading.
    """
    out: List[dict] = []
    for peer in peers:
        if peer.get("status") == "ok":
            out.extend(peer.get("entries") or [])
    return out


# ---------------------------------------------------------------------------
# Summaries — one implementation shared by the HTML pages and the JSON API
# ---------------------------------------------------------------------------
def watch_summary(entries: List[dict]) -> Dict[str, int]:
    """Count mtime/staleness states across *entries* (files and directories)."""
    counts = {name: 0 for name in WATCH_STATES}
    for entry in entries:
        state = entry.get("watch_state")
        if state in counts:
            counts[state] += 1
    counts["total"] = len(entries)
    return counts


def _state_summary(entries: List[dict], key: str, names) -> Dict[str, int]:
    counts = {name.lower(): 0 for name in names}
    for entry in entries:
        state = entry.get(key)
        if state and state.lower() in counts:
            counts[state.lower()] += 1
    # "I cannot see this volume/file" — what an operator reacts to as one thing.
    counts["unavailable"] = counts["missing"] + counts["unknown"]
    counts["total"] = len(entries)
    counts["monitored"] = len(entries)
    return counts


def space_summary(entries: List[dict]) -> Dict[str, int]:
    """Count free-space states.  *entries* must already be space-monitored dirs."""
    return _state_summary(entries, "space_state", SPACE_STATES)


def size_summary(entries: List[dict]) -> Dict[str, int]:
    """Count file-size states.  *entries* must already be size-monitored files."""
    return _state_summary(entries, "size_state", SIZE_STATES)


def space_entries(entries: List[dict]) -> List[dict]:
    """Directories that have a ``space:`` block."""
    return [e for e in entries if e.get("space_monitored")]


def size_entries(entries: List[dict]) -> List[dict]:
    """Files that have a ``size:`` block."""
    return [e for e in entries if e.get("size_monitored")]
