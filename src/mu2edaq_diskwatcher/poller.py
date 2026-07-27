"""The polling thread: stat every watched path and derive its states.

Every entry produces a dict with exactly the keys in :data:`STATE_KEYS`,
whether the stat succeeded or not.  This is enforced by building from
:func:`_null_state` and only ever ``update``-ing it — the previous code patched
the error branch by hand, so any key added to one path only became an
``undefined`` in the browser.
"""

import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Optional

from .config import limits_as_strings
from .formatting import DASH, fmt_bytes, fmt_duration
from .settings import get_settings
from .ssh import remote_probe
from .state import STORE
from .thresholds import (
    check_order,
    evaluate_size_state,
    evaluate_space_state,
    severity_rank,
    watch_rank,
)

LEVELS = ("warning", "critical", "full")


def _null_state(entry: dict) -> dict:
    """Every key the API can emit, at its "nothing is known yet" value."""
    ssh_cfg = entry.get("ssh")
    delay   = entry.get("delay")
    return {
        # ---- identity ----
        "path":     entry["path"],
        "label":    entry.get("label") or entry["path"],
        "kind":     entry.get("kind", "file"),
        "remote":   ssh_cfg is not None,
        "ssh_host": str(ssh_cfg.get("host", "")) if ssh_cfg else None,

        # ---- mtime / staleness ----
        "delay":       delay,
        "delay_str":   fmt_duration(delay),
        "mtime":       None,
        "mtime_str":   DASH,
        "age_s":       None,
        "age_str":     DASH,
        "stale":       False,
        "missing":     False,
        "error":       None,
        "watch_state": "unmonitored" if delay is None else "ok",
        "watch_rank":  watch_rank("unmonitored" if delay is None else "ok"),

        # ---- disk usage (directories) ----
        "disk_total":     None,
        "disk_used":      None,
        "disk_free":      None,
        "disk_pct":       None,
        "disk_free_pct":  None,
        "disk_total_str": None,
        "disk_used_str":  None,
        "disk_free_str":  None,

        # ---- free-space alarm ----
        "space_monitored":      entry.get("space") is not None,
        "space_state":          None,
        "space_rank":           None,
        "space_trigger":        None,
        "space_reason":         None,
        "space_limits":         None,
        "space_limits_str":     limits_as_strings(entry.get("space")),
        "space_limits_ordered": True,

        # ---- file size alarm ----
        "size":            None,
        "size_str":        DASH,
        "size_monitored":  entry.get("size") is not None,
        "size_state":      None,
        "size_rank":       None,
        "size_trigger":    None,
        "size_reason":     None,
        "size_limits":     None,
        "size_limits_str": limits_as_strings(entry.get("size")),
        "size_pct":        None,
        "size_pct_ref":    None,

        # ---- config problems for this entry ----
        "config_errors": list(entry.get("config_errors") or []),
    }


#: The frozen key set every state dict must carry.  Asserted by the tests.
STATE_KEYS = frozenset(_null_state({"path": "/", "kind": "file"}))


def _set_watch(state: dict, delay: Optional[int], name: str) -> None:
    state["watch_state"] = name
    state["watch_rank"]  = watch_rank(name)


def _evaluate_space(state: dict, entry: dict) -> None:
    """Resolve the entry's space thresholds and classify the free space."""
    limits_cfg = entry["space"]
    total = state["disk_total"]
    resolved = {level: (limits_cfg[level].resolve(total)
                        if limits_cfg.get(level) is not None else None)
                for level in LEVELS}
    state["space_limits"] = resolved

    # Ordering of a mixed percent/absolute block can only be checked once the
    # filesystem total is known.  Warn once per process, not once per poll.
    ordered = check_order(resolved, descending=True)
    state["space_limits_ordered"] = ordered
    if not ordered and not entry.get("_space_order_warned"):
        entry["_space_order_warned"] = True
        print(f"[Config] Warning: [{entry['path']}] space: thresholds resolve out of "
              f"order (expected warning > critical > full); some alarm states are "
              f"unreachable", file=sys.stderr)

    st, trigger, reason = evaluate_space_state(state["disk_free"], total, resolved)
    state["space_state"]   = st
    state["space_rank"]    = severity_rank(st)
    state["space_trigger"] = trigger
    state["space_reason"]  = reason


def _evaluate_size(state: dict, entry: dict) -> None:
    """Resolve the entry's size thresholds and classify the file size."""
    limits_cfg = entry["size"]
    resolved = {level: (limits_cfg[level].resolve(None)
                        if limits_cfg.get(level) is not None else None)
                for level in LEVELS}
    state["size_limits"] = resolved

    st, trigger, reason = evaluate_size_state(
        state["size"], resolved, allow_empty=entry.get("allow_empty", False))
    state["size_state"]   = st
    state["size_rank"]    = severity_rank(st)
    state["size_trigger"] = trigger
    state["size_reason"]  = reason

    # Progress-bar reference: the largest configured limit the file is measured
    # against, so the bar means "how close am I to the worst threshold".
    for level in ("full", "critical", "warning"):
        if resolved.get(level):
            state["size_pct_ref"] = level
            if state["size"] is not None:
                state["size_pct"] = round(state["size"] / resolved[level] * 100, 1)
            break


def poll_entry(entry: dict, now: float) -> dict:
    """Stat one watch entry and return its complete state dict."""
    state   = _null_state(entry)
    path    = entry["path"]
    delay   = entry.get("delay")
    kind    = entry.get("kind", "file")
    ssh_cfg = entry.get("ssh")
    want_disk = kind == "directory"

    try:
        # ---- one stat (and, for directories, disk usage) ----
        if ssh_cfg:
            probe = remote_probe(path, ssh_cfg, want_disk=want_disk)
            if probe.error:
                raise OSError(probe.error)
            mtime, size = probe.mtime, probe.size
            total, used, free = probe.total, probe.used, probe.free
        else:
            st = os.stat(path)
            mtime, size = st.st_mtime, st.st_size
            total = used = free = None
            if want_disk:
                try:
                    usage = shutil.disk_usage(path)
                    total, used, free = usage.total, usage.used, usage.free
                except OSError:
                    pass                 # exists but unmeasurable -> UNKNOWN

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
        if delay is None:
            _set_watch(state, delay, "unmonitored")
        else:
            _set_watch(state, delay, "stale" if state["stale"] else "ok")

        # A directory's st_size is an allocation figure, not a data size, and
        # would mislead an operator — only files report a size.
        if kind == "file":
            state["size"] = size
            state["size_str"] = fmt_bytes(size)

        if want_disk and total:
            state.update({
                "disk_total":     total,
                "disk_used":      used,
                "disk_free":      free,
                "disk_pct":       round(used / total * 100, 1),
                "disk_free_pct":  round(free / total * 100, 1),
                "disk_total_str": fmt_bytes(total),
                "disk_used_str":  fmt_bytes(used),
                "disk_free_str":  fmt_bytes(free),
            })

        if state["space_monitored"]:
            _evaluate_space(state, entry)
        if state["size_monitored"]:
            _evaluate_size(state, entry)

    except OSError as exc:
        state.update({
            "missing": True,
            "error":   str(exc),
            # Historical behaviour: a missing path counts as stale, but only
            # when staleness is actually being monitored.
            "stale":   delay is not None,
        })
        _set_watch(state, delay, "missing")
        if state["space_monitored"]:
            state.update({"space_state": "MISSING",
                          "space_rank": severity_rank("MISSING"),
                          "space_reason": str(exc)})
        if state["size_monitored"]:
            state.update({"size_state": "MISSING",
                          "size_rank": severity_rank("MISSING"),
                          "size_reason": str(exc)})

    return state


def do_poll() -> None:
    """Stat every watched path concurrently and publish the results."""
    entries = get_settings().entries
    if not entries:
        STORE.replace([], duration=0.0)
        return

    started = time.time()
    results: List[Optional[dict]] = [None] * len(entries)
    with ThreadPoolExecutor(max_workers=min(len(entries), 20)) as pool:
        future_map = {pool.submit(poll_entry, entry, started): i
                      for i, entry in enumerate(entries)}
        for fut in as_completed(future_map):
            results[future_map[fut]] = fut.result()

    STORE.replace(results, duration=round(time.time() - started, 3))


def poll_loop() -> None:
    """Background thread: poll, then sleep for the configured interval."""
    while True:
        try:
            do_poll()
        except Exception as exc:
            print(f"[Poller] Unexpected error: {exc}", file=sys.stderr)
        time.sleep(get_settings().poll_interval)
