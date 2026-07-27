"""YAML configuration loading and watch-entry construction.

Validation here never raises and never exits: a control-room daemon must keep
running on a partly bad config.  Problems are collected into a list of strings
(surfaced on the /config page and in /api/config) and echoed to stderr once.
"""

import sys
from typing import Dict, List, Optional, Tuple

import yaml

from .thresholds import Threshold, ThresholdError, check_order, parse_size_threshold

#: Keys accepted on a `files:` / `paths:` entry.
ENTRY_KEYS = {"path", "delay", "label", "ssh", "space", "size"}
#: Keys accepted inside a `space:` block.
SPACE_KEYS = {"warning", "critical", "full"}
#: Keys accepted inside a `size:` block.  `max:` is a synonym for `full:`,
#: because "a full file" reads oddly.
SIZE_KEYS  = {"warning", "critical", "full", "max", "allow_empty"}

LEVELS = ("warning", "critical", "full")


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


def _parse_limits(
    block: dict,
    where: str,
    allow_percent: bool,
    issues: List[str],
) -> Tuple[Dict[str, Optional[Threshold]], bool]:
    """Parse a `space:` or `size:` block into ``{level: Threshold|None}``.

    A threshold that fails to parse is dropped; its siblings survive.  Returns
    the limits and whether ``allow_empty`` was requested.
    """
    allowed = SPACE_KEYS if allow_percent else SIZE_KEYS
    for key in block:
        if key not in allowed:
            issues.append(f"{where}: unknown key {key!r}; ignored")

    limits: Dict[str, Optional[Threshold]] = {level: None for level in LEVELS}
    for level in LEVELS:
        raw = block.get(level)
        if level == "full" and raw is None:
            raw = block.get("max")           # documented synonym
        if raw is None:
            continue
        try:
            limits[level] = parse_size_threshold(raw, allow_percent=allow_percent)
        except ThresholdError as exc:
            issues.append(f"{where}: {level}: {exc}")

    allow_empty = bool(block.get("allow_empty", False))
    return limits, allow_empty


def _validate_order(
    limits: Dict[str, Optional[Threshold]],
    where: str,
    descending: bool,
    issues: List[str],
) -> None:
    """Warn if same-kind thresholds do not escalate in the expected direction.

    Mixed percent/absolute blocks cannot be checked here — their ordering
    depends on the filesystem size — so the poller re-checks them once the total
    is known and reports ``space_limits_ordered``.
    """
    present = [t for t in (limits[level] for level in LEVELS) if t is not None]
    if len(present) < 2:
        return
    if any(t.percent is not None for t in present) and \
       any(t.bytes is not None for t in present):
        return                                # deferred to poll time
    resolved = {level: (t.percent if t.percent is not None else t.bytes)
                for level, t in limits.items() if t is not None}
    if not check_order(resolved, descending=descending):
        order = " > ".join(f"{lvl} {limits[lvl].raw}"
                           for lvl in LEVELS if limits[lvl] is not None)
        direction = "warning > critical > full" if descending \
            else "warning < critical < full"
        issues.append(
            f"{where}: thresholds are misordered ({order}); expected "
            f"{direction}, so some alarm states are unreachable"
        )


def entries_from_config(cfg: dict, default_delay: Optional[int] = 300) -> Tuple[List[dict], List[str]]:
    """Build the flat list of watch entries, plus a list of config problems.

    Reads the ``files`` and ``paths`` top-level keys.  Each item may be a mapping
    with ``path``, optional ``delay``, ``label``, ``ssh``, and either a ``space``
    block (directories) or a ``size`` block (files); or a bare string, treated as
    the path with the default delay.

    ``delay`` resolution, in order:

    * present            -> that value
    * ``delay: null``    -> ``None`` (mtime not monitored)
    * absent, but a ``space:``/``size:`` block is present -> ``None``
    * absent, nothing else -> *default_delay*  (preserves historical behaviour)
    """
    entries: List[dict] = []
    issues: List[str] = []

    for section in ("files", "paths"):
        kind = "file" if section == "files" else "directory"
        for item in cfg.get(section, []) or []:
            if not isinstance(item, dict):
                path = str(item)
                if path:
                    entries.append(_make_entry(path, default_delay, path, kind, None,
                                               None, None, False, []))
                continue

            path = item.get("path", "")
            if not path:
                issues.append(f"[{section}] entry with no path:; ignored")
                continue
            where = f"[{path}]"
            label = item.get("label") or path
            ssh   = item.get("ssh") or None
            entry_issues: List[str] = []

            for key in item:
                if key not in ENTRY_KEYS:
                    entry_issues.append(f"{where}: unknown key {key!r}; ignored")

            # ---- space: / size: blocks (each valid on one section only) ----
            space_limits = size_limits = None
            allow_empty = False

            raw_space = item.get("space")
            if raw_space is not None:
                if kind != "directory":
                    entry_issues.append(
                        f"{where}: space: applies to directories only "
                        f"(move this entry to paths:); ignored")
                elif not isinstance(raw_space, dict):
                    entry_issues.append(f"{where}: space: must be a mapping; ignored")
                elif not raw_space:
                    entry_issues.append(
                        f"{where}: space: is empty; free-space monitoring disabled")
                else:
                    space_limits, _ = _parse_limits(
                        raw_space, f"{where} space", allow_percent=True,
                        issues=entry_issues)
                    if all(v is None for v in space_limits.values()):
                        entry_issues.append(
                            f"{where}: space: has no usable thresholds; "
                            f"free-space monitoring disabled")
                        space_limits = None
                    else:
                        _validate_order(space_limits, f"{where} space",
                                        descending=True, issues=entry_issues)

            raw_size = item.get("size")
            if raw_size is not None:
                if kind != "file":
                    entry_issues.append(
                        f"{where}: size: applies to files only "
                        f"(move this entry to files:); ignored")
                elif not isinstance(raw_size, dict):
                    entry_issues.append(f"{where}: size: must be a mapping; ignored")
                else:
                    size_limits, allow_empty = _parse_limits(
                        raw_size, f"{where} size", allow_percent=False,
                        issues=entry_issues)
                    # An empty-only block (`size: {allow_empty: false}`) is
                    # legitimate: "alarm if this file is zero bytes".
                    _validate_order(size_limits, f"{where} size",
                                    descending=False, issues=entry_issues)

            # ---- delay ----
            has_thresholds = space_limits is not None or size_limits is not None
            if "delay" in item:
                raw_delay = item["delay"]
                if raw_delay is None:
                    delay = None
                else:
                    try:
                        delay = int(raw_delay)
                    except (TypeError, ValueError):
                        entry_issues.append(
                            f"{where}: delay: {raw_delay!r} is not a number; "
                            f"using {default_delay}")
                        delay = default_delay
            elif has_thresholds:
                delay = None
            else:
                delay = default_delay

            if delay is None and not has_thresholds:
                entry_issues.append(
                    f"{where}: nothing is monitored (no delay:, no space:/size:); "
                    f"it will only be listed")

            entries.append(_make_entry(path, delay, label, kind, ssh,
                                       space_limits, size_limits, allow_empty,
                                       entry_issues))
            issues.extend(entry_issues)

    for issue in issues:
        print(f"[Config] Warning: {issue}", file=sys.stderr)

    return entries, issues


def _make_entry(path, delay, label, kind, ssh, space, size, allow_empty, errors) -> dict:
    return {
        "path":          path,
        "delay":         delay,
        "label":         label,
        "kind":          kind,
        "ssh":           ssh,
        "space":         space,          # {level: Threshold|None} or None
        "size":          size,           # {level: Threshold|None} or None
        "allow_empty":   allow_empty,
        "config_errors": errors,
    }


def limits_as_strings(limits: Optional[Dict[str, Optional[Threshold]]]) -> Optional[dict]:
    """``{level: raw YAML string}`` for display, or ``None``."""
    if limits is None:
        return None
    return {level: (t.raw if t is not None else None) for level, t in limits.items()}
