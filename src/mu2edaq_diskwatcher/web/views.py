"""HTML pages.

The three dashboards (Watcher, Disk Space, File Sizes) render an empty shell and
fill it from their own JSON endpoint, so they stay live without a page reload.
Config, API and About are rendered server-side from settings that only change on
restart.
"""

import importlib.metadata
import os
import platform
import socket
import sys
from datetime import datetime, timezone

from flask import Blueprint, render_template

from .. import INSTANCE_ID, START_TIME, __version__
from ..formatting import fmt_duration
from ..settings import ENV_PREFIX, get_settings
from ..state import PEERS, STORE, size_entries, space_entries

bp = Blueprint("views", __name__)


@bp.route("/")
def index():
    return render_template("index.html", title="Watcher")


@bp.route("/space")
def space():
    return render_template("space.html", title="Disk Space")


@bp.route("/sizes")
def sizes():
    return render_template("sizes.html", title="File Sizes")


@bp.route("/config")
def config():
    settings = get_settings()
    raw_yaml = None
    if settings.config_path:
        try:
            with open(settings.config_path) as fh:
                raw_yaml = fh.read()
        except OSError as exc:
            raw_yaml = f"# Could not read {settings.config_path}: {exc}"

    entries = settings.entries
    return render_template(
        "config.html",
        title="Config",
        settings=settings,
        files=[e for e in entries if e.get("kind") == "file"],
        dirs=[e for e in entries if e.get("kind") == "directory"],
        peers=PEERS.snapshot(),
        peer_interval=settings.effective_peer_interval(),
        raw_yaml=raw_yaml,
        env_prefix=ENV_PREFIX,
        fmt_duration=fmt_duration,
    )


@bp.route("/api")
def api_docs():
    return render_template("api.html", title="API",
                           settings_port=get_settings().web_port)


@bp.route("/sitemap")
def sitemap():
    return render_template("sitemap.html", title="Sitemap")


@bp.route("/about")
def about():
    settings = get_settings()
    uptime = datetime.now(timezone.utc) - START_TIME
    h, rem = divmod(int(uptime.total_seconds()), 3600)
    m, s = divmod(rem, 60)

    def pkg_ver(name):
        try:
            return importlib.metadata.version(name)
        except Exception:
            return "?"

    entries = STORE.snapshot()
    return render_template(
        "about.html",
        title="About",
        settings=settings,
        uptime_str=f"{h}h {m}m {s}s",
        hostname=socket.gethostname(),
        os_info=platform.platform(),
        py_ver=sys.version.split()[0],
        flask_ver=pkg_ver("flask"),
        yaml_ver=pkg_ver("pyyaml"),
        jinja_ver=pkg_ver("jinja2"),
        app_version=__version__,
        pid=os.getpid(),
        total=len(entries),
        n_missing=sum(1 for e in entries if e.get("missing")),
        n_stale=sum(1 for e in entries if e.get("watch_state") == "stale"),
        n_space=len(space_entries(entries)),
        n_size=len(size_entries(entries)),
        poll_display=fmt_duration(settings.poll_interval),
        peer_counts=PEERS.counts(),
        instance_id=INSTANCE_ID,
    )
