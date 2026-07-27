"""Process-wide runtime settings.

The application is configured by four layers, lowest priority first:

    defaults  ->  YAML config file  ->  environment  ->  command line

Everything lives on a single mutable :class:`Settings` instance reached through
:func:`get_settings`.

.. important::
   Call ``get_settings()`` **inside** function bodies.  Never bind it at module
   scope (``SETTINGS = get_settings()``) and never do ``from .settings import
   web_port`` — those capture a value before :func:`~mu2edaq_diskwatcher.cli.main`
   has applied the config file, the environment and the CLI flags.  ``main()``
   only ever calls :meth:`Settings.apply`; it never rebinds the singleton, so a
   live ``get_settings()`` call always sees the current configuration.
"""

import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional

#: Prefix for every environment-variable override.
ENV_PREFIX = "MU2EDAQ_DISKWATCHER_"

#: Environment variable name -> (settings attribute, coercion function).
_ENV_MAP = {
    ENV_PREFIX + "CONFIG":        ("config_path",   str),
    ENV_PREFIX + "WEB_HOST":      ("web_host",      str),
    ENV_PREFIX + "WEB_PORT":      ("web_port",      int),
    ENV_PREFIX + "POLL_INTERVAL": ("poll_interval", int),
    ENV_PREFIX + "DEFAULT_DELAY": ("default_delay", int),
    ENV_PREFIX + "DAEMON":        ("daemon",        None),   # bool, see _as_bool
    ENV_PREFIX + "PID_FILE":      ("pid_file",      str),
    ENV_PREFIX + "LOG_FILE":      ("log_file",      str),
    ENV_PREFIX + "VERBOSE":       ("verbose",       None),
}

_TRUE  = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _as_bool(raw: str) -> Optional[bool]:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return None


@dataclass
class Settings:
    """Everything ``main()`` used to keep in module-level globals."""

    web_host:      str = "0.0.0.0"
    web_port:      int = 5002
    poll_interval: int = 30              # seconds between polls
    default_delay: Optional[int] = 300   # staleness fallback for entries with no delay:
    config_path:   Optional[str] = None
    daemon:        bool = False
    pid_file:      Optional[str] = None
    log_file:      Optional[str] = None
    verbose:       bool = False

    #: Flat list of watch-entry dicts produced by :func:`config.entries_from_config`.
    entries: List[dict] = field(default_factory=list)

    #: Human-readable config problems, shown on /config and in /api/config.
    config_issues: List[str] = field(default_factory=list)

    def apply(self, **kwargs: Any) -> None:
        """Set attributes from *kwargs*, ignoring any whose value is ``None``.

        Skipping ``None`` is what makes the layering work: argparse defaults and
        absent environment variables both arrive as ``None`` and leave the lower
        layer's value in place.
        """
        known = {f.name for f in fields(self)}
        for key, value in kwargs.items():
            if value is None:
                continue
            if key not in known:
                raise AttributeError(f"unknown setting: {key!r}")
            setattr(self, key, value)

    def apply_env(self, environ: Optional[Dict[str, str]] = None) -> List[str]:
        """Apply ``MU2EDAQ_DISKWATCHER_*`` overrides; return warnings for bad values."""
        env = os.environ if environ is None else environ
        issues: List[str] = []
        for name, (attr, coerce) in _ENV_MAP.items():
            if name not in env:
                continue
            raw = env[name]
            if coerce is None:                       # boolean
                value = _as_bool(raw)
                if value is None:
                    issues.append(f"{name}: {raw!r} is not a boolean; ignored")
                    continue
            else:
                try:
                    value = coerce(raw)
                except (TypeError, ValueError):
                    issues.append(f"{name}: {raw!r} is not valid; ignored")
                    continue
            setattr(self, attr, value)
        return issues

    def as_dict(self) -> Dict[str, Any]:
        """Scalar settings only — for the /config page and /api/config."""
        return {f.name: getattr(self, f.name)
                for f in fields(self)
                if f.name not in ("entries", "config_issues")}


_SETTINGS = Settings()


def get_settings() -> Settings:
    """Return the process-wide settings object."""
    return _SETTINGS


def reset_settings(**kwargs: Any) -> Settings:
    """Restore defaults, then apply *kwargs*.  Intended for tests only."""
    fresh = Settings()
    for f in fields(fresh):
        setattr(_SETTINGS, f.name, getattr(fresh, f.name))
    _SETTINGS.apply(**kwargs)
    return _SETTINGS
