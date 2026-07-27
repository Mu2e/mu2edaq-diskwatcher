"""Shared poll results.

Written by the poller thread, read by Flask request handlers.  ``StateStore``
replaces the bare ``state_lock`` / ``file_states`` pair.

Thread-safety contract: :meth:`StateStore.replace` swaps in a brand-new list of
brand-new dicts, and :meth:`StateStore.snapshot` returns a shallow copy of that
list.  Readers therefore never see a half-written entry.  **Do not** "optimise"
the poller to mutate existing dicts in place — that would break this guarantee.
"""

import threading
import time
from typing import Dict, List, Optional

#: Order in which states are displayed and counted on the dashboards.
WATCH_STATES = ("ok", "stale", "missing", "unmonitored")
SPACE_STATES = ("GOOD", "WARNING", "CRITICAL", "FULL", "MISSING", "UNKNOWN")
SIZE_STATES  = ("GOOD", "EMPTY", "WARNING", "CRITICAL", "FULL", "MISSING", "UNKNOWN")


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
