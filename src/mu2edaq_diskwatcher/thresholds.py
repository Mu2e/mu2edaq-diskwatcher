"""Threshold parsing and state evaluation.

Pure functions only — no I/O, no globals, no Flask.  Everything here is
directly unit-testable, which matters because this module decides when an
operator gets woken up.

Two independent alarm dimensions:

  * **free space** on a directory's filesystem.  Thresholds name how much space
    must remain *free*, so they descend: ``warning > critical > full``.
  * **file size**.  Thresholds name how large a file may get, so they ascend:
    ``warning < critical < full``.  Zero-length files are their own state.

Both evaluators return a ``(state, trigger, reason)`` triple.  ``trigger`` names
the threshold that fired and ``reason`` is a human sentence for the UI tooltip.
The triple is deliberately shaped so a future notifier can consume it without
re-deriving anything.
"""

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .formatting import fmt_bytes

#: Unit suffix -> multiplier.  SI (decimal) and IEC (binary) are both accepted;
#: lookup is case-insensitive.  Display is always IEC (see :func:`fmt_bytes`),
#: so a ``2TB`` threshold renders as ``1.8 TiB``.
UNITS = {
    "":    1,
    "B":   1,
    "K":   1000,          "KB":  1000,
    "M":   1000 ** 2,     "MB":  1000 ** 2,
    "G":   1000 ** 3,     "GB":  1000 ** 3,
    "T":   1000 ** 4,     "TB":  1000 ** 4,
    "P":   1000 ** 5,     "PB":  1000 ** 5,
    "KIB": 1024,
    "MIB": 1024 ** 2,
    "GIB": 1024 ** 3,
    "TIB": 1024 ** 4,
    "PIB": 1024 ** 5,
}

_PERCENT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*%$")
_SIZE_RE    = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*([A-Za-z]*)$")

#: Severity ordering, shipped to the browser so the JS sorter never has to
#: re-encode it.  Higher is worse.
SEVERITY = {
    "GOOD": 0, "EMPTY": 1, "WARNING": 2, "CRITICAL": 3,
    "FULL": 4, "UNKNOWN": 5, "MISSING": 6,
}

#: Same idea for the Watcher page's mtime states.
WATCH_SEVERITY = {"ok": 0, "unmonitored": 1, "stale": 2, "missing": 3}


class ThresholdError(ValueError):
    """Raised for a threshold value that cannot be understood."""


@dataclass(frozen=True)
class Threshold:
    """A single configured limit, either absolute bytes or a percentage."""

    raw: str                        # exactly as written in the YAML, for display
    bytes: Optional[int] = None     # set when absolute
    percent: Optional[float] = None  # 0 < p <= 100, set when relative

    def resolve(self, total: Optional[int]) -> Optional[int]:
        """Return this threshold in bytes, given the filesystem *total*.

        A percentage cannot be resolved without a total, in which case ``None``
        is returned and the caller reports UNKNOWN.
        """
        if self.bytes is not None:
            return self.bytes
        if self.percent is None or not total:
            return None
        return int(round(total * self.percent / 100.0))

    def describe(self, total: Optional[int] = None) -> str:
        """``'10% (46.5 GiB)'`` — the raw value plus what it resolves to."""
        if self.percent is None:
            return self.raw
        resolved = self.resolve(total)
        if resolved is None:
            return self.raw
        return f"{self.raw} ({fmt_bytes(resolved)})"


def parse_size_threshold(value, allow_percent: bool = True) -> Threshold:
    """Parse a YAML threshold value into a :class:`Threshold`.

    Accepts ``"500 GiB"``, ``"2TB"``, ``"1.5 gb"``, ``"10%"``, ``"5 %"``, and
    bare numbers (interpreted as bytes).  Raises :class:`ThresholdError` on
    anything else.
    """
    if isinstance(value, bool):
        # bool is an int subclass; a bare `true` is certainly a mistake.
        raise ThresholdError(f"unrecognised size threshold: {value!r}")

    if isinstance(value, (int, float)):
        if value < 0:
            raise ThresholdError(f"size threshold cannot be negative: {value!r}")
        return Threshold(raw=str(value), bytes=int(round(value)))

    if not isinstance(value, str):
        raise ThresholdError(f"unrecognised size threshold: {value!r}")

    text = value.strip()
    if not text:
        raise ThresholdError("size threshold is empty")

    match = _PERCENT_RE.match(text)
    if match:
        if not allow_percent:
            raise ThresholdError(
                "percentage thresholds are not meaningful for file sizes; "
                "a file has no total capacity"
            )
        percent = float(match.group(1))
        if not 0 < percent <= 100:
            raise ThresholdError(
                f"percentage threshold must be >0 and <=100: {text!r}")
        return Threshold(raw=text, percent=percent)

    match = _SIZE_RE.match(text)
    if not match:
        raise ThresholdError(f"unrecognised size threshold: {value!r}")

    number, suffix = match.group(1), match.group(2).upper()
    if suffix not in UNITS:
        raise ThresholdError(f"unknown size unit {match.group(2)!r} in {value!r}")
    amount = float(number)
    if amount < 0:
        raise ThresholdError(f"size threshold cannot be negative: {value!r}")
    return Threshold(raw=text, bytes=int(round(amount * UNITS[suffix])))


def severity_rank(state: Optional[str]) -> Optional[int]:
    """Sort rank for a space/size state name, or ``None`` if unmonitored."""
    if state is None:
        return None
    return SEVERITY.get(state.upper(), SEVERITY["UNKNOWN"])


def watch_rank(state: Optional[str]) -> int:
    """Sort rank for a Watcher-page state name."""
    return WATCH_SEVERITY.get(state or "ok", 0)


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------
# Both walk from most severe to least, so any subset of thresholds works and a
# misordered configuration still produces a defined answer (some states simply
# become unreachable).  Boundaries are inclusive on the *bad* side.

def _loosest(limits: Dict[str, Optional[int]], pick_max: bool):
    """Return ``(level, bytes)`` for the limit a GOOD reading cleared.

    Which limit that is depends on the direction: free space must exceed the
    *largest* threshold, a file size must stay below the *smallest*.  Found by
    value rather than by name so a partially configured block still reports the
    threshold that actually applied.
    """
    present = [(name, value) for name, value in limits.items() if value is not None]
    if not present:
        return None
    return (max if pick_max else min)(present, key=lambda pair: pair[1])

def evaluate_space_state(
    free: Optional[int],
    total: Optional[int],
    limits: Dict[str, Optional[int]],
) -> Tuple[str, Optional[str], str]:
    """Classify remaining free space.

    *limits* maps ``warning``/``critical``/``full`` to already-resolved byte
    counts (``None`` for thresholds that are unset or unresolvable).
    """
    if free is None or not total:
        return "UNKNOWN", None, "no disk usage available"

    free_str = fmt_bytes(free)
    for name in ("full", "critical", "warning"):
        limit = limits.get(name)
        if limit is not None and free <= limit:
            state = name.upper()
            return state, name, f"{free_str} free ≤ {name} {fmt_bytes(limit)}"

    # GOOD means free space is above the *loosest* limit — for descending
    # free-space thresholds that is the largest one, normally `warning`.
    loosest = _loosest(limits, pick_max=True)
    if loosest is None:
        return "GOOD", None, f"{free_str} free (no thresholds configured)"
    name, limit = loosest
    return "GOOD", None, f"{free_str} free > {name} {fmt_bytes(limit)}"


def evaluate_size_state(
    size: Optional[int],
    limits: Dict[str, Optional[int]],
    allow_empty: bool = False,
) -> Tuple[str, Optional[str], str]:
    """Classify a file size.

    A zero-length file is EMPTY unless *allow_empty* is set — that check comes
    first, because a truncated file is an alarm regardless of its thresholds.
    """
    if size is None:
        return "UNKNOWN", None, "no size available"

    if size == 0 and not allow_empty:
        return "EMPTY", "empty", "file is zero length"

    size_str = fmt_bytes(size)
    for name in ("full", "critical", "warning"):
        limit = limits.get(name)
        if limit is not None and size >= limit:
            state = name.upper()
            return state, name, f"{size_str} ≥ {name} {fmt_bytes(limit)}"

    # GOOD means the file is below the *loosest* limit — for ascending size
    # thresholds that is the smallest one, normally `warning`.
    loosest = _loosest(limits, pick_max=False)
    if loosest is None:
        return "GOOD", None, f"{size_str} (no size thresholds configured)"
    name, limit = loosest
    return "GOOD", None, f"{size_str} < {name} {fmt_bytes(limit)}"


def check_order(limits: Dict[str, Optional[int]], descending: bool) -> bool:
    """Return True if the present thresholds escalate in the expected direction.

    *descending* is True for free-space limits (warning > critical > full) and
    False for file-size limits (warning < critical < full).  Thresholds that are
    unset or unresolvable are skipped.
    """
    present = [limits.get(name) for name in ("warning", "critical", "full")]
    present = [v for v in present if v is not None]
    pairs = zip(present, present[1:])
    if descending:
        return all(a > b for a, b in pairs)
    return all(a < b for a, b in pairs)
