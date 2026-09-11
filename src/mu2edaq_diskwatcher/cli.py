"""Command-line entry point.

Configuration layers, lowest priority first::

    dataclass defaults -> YAML config file -> environment -> command line

Every option except ``--config`` defaults to ``None`` so that
:meth:`Settings.apply` can tell "not supplied" from "supplied".

The command line is parsed *before* the config file is read.  An earlier
version pre-scanned ``sys.argv`` with a throwaway parser that knew only about
``--config``; when anything on the line was malformed, that parser was the one
that reported it, printing a usage line listing a single option and blaming
``--config`` for unrelated mistakes.  Parsing once means the real parser always
produces the diagnostic, and the config path used is provably the one argparse
resolved.
"""

import argparse
import os
import sys
import threading

from . import __version__
from .config import entries_from_config, load_config, peers_from_config, peers_from_urls
from .daemon import daemonize, write_pid_file
from .discovery import start_responder, stop_responder
from .peers import peer_loop
from .poller import do_poll, poll_loop
from .settings import ENV_PREFIX, get_settings
from .state import PEERS

#: Config file used when neither --config nor the environment names one.
DEFAULT_CONFIG = "diskwatcher.yaml"


def build_parser(defaults) -> argparse.ArgumentParser:
    """Second-pass parser.  All defaults are None so overrides are detectable."""
    parser = argparse.ArgumentParser(
        prog="mu2edaq-diskwatcher",
        description="Monitor file modification times, free disk space and file "
                    "sizes, and serve a web dashboard.",
    )
    parser.add_argument(
        "--config", "-c", default=None, metavar="FILE",
        help=f"YAML configuration file (default: {DEFAULT_CONFIG}, or "
             f"${ENV_PREFIX}CONFIG)",
    )
    parser.add_argument(
        "--host", default=None, metavar="ADDR",
        help=f"Web server bind address (default: {defaults.web_host})",
    )
    parser.add_argument(
        "--port", "-p", type=int, default=None, metavar="PORT",
        help=f"Web server port (default: {defaults.web_port})",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=None, metavar="SECONDS",
        help=f"Seconds between checks (default: {defaults.poll_interval})",
    )
    parser.add_argument(
        "--default-delay", type=int, default=None, metavar="SECONDS",
        help="Staleness threshold for entries that omit delay: "
             f"(default: {defaults.default_delay})",
    )
    parser.add_argument(
        "--daemon", "-d", action="store_true", default=None,
        help="Run as a background daemon (POSIX only)",
    )
    parser.add_argument(
        "--pid-file", default=None, metavar="FILE",
        help="Write daemon PID to FILE (e.g. /tmp/diskwatcher.pid)",
    )
    parser.add_argument(
        "--log-file", default=None, metavar="FILE",
        help="Redirect daemon stdout/stderr to FILE (default: /dev/null)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=None,
        help="Log every remote probe, poll cycle and peer fetch",
    )
    peers = parser.add_argument_group(
        "peers",
        "Other diskwatcher instances whose entries are shown on this "
        "dashboard too.  --peer replaces the peers: list from the config "
        f"file and ${ENV_PREFIX}PEERS; --no-peers disables federation.",
    )
    peers.add_argument(
        "--peer", action="append", default=None, metavar="URL", dest="peers",
        help="Peer base URL, e.g. http://mu2e-dl-01:5002 (repeatable; the "
             "scheme may be omitted)",
    )
    peers.add_argument(
        "--no-peers", action="store_true", default=None,
        help="Ignore every configured peer",
    )
    peers.add_argument(
        "--peer-timeout", type=float, default=None, metavar="SECONDS",
        help=f"HTTP timeout per peer fetch (default: {defaults.peer_timeout:g})",
    )
    peers.add_argument(
        "--peer-interval", type=int, default=None, metavar="SECONDS",
        help="Seconds between peer fetches (default: the poll interval)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    return parser


def main() -> None:
    settings = get_settings()
    args = build_parser(settings).parse_args()

    # The config path has its own precedence chain, resolved here because every
    # later layer is read out of the file it names.  A path given explicitly --
    # on the command line or in the environment -- must exist; the built-in
    # default is allowed to be absent, so the app still starts with no config.
    env_config = os.environ.get(ENV_PREFIX + "CONFIG")
    config_path = args.config or env_config or DEFAULT_CONFIG
    cfg = load_config(config_path,
                      required=bool(args.config or env_config))

    # ---- layer 2: YAML ----
    wcfg = cfg.get("watcher", {}) or {}
    settings.apply(
        web_host      = wcfg.get("web_host"),
        web_port      = int(wcfg["web_port"])      if "web_port"      in wcfg else None,
        poll_interval = int(wcfg["poll_interval"]) if "poll_interval" in wcfg else None,
        default_delay = int(wcfg["default_delay"]) if "default_delay" in wcfg else None,
        daemon        = bool(wcfg["daemon"])       if "daemon"        in wcfg else None,
        pid_file      = wcfg.get("pid_file"),
        log_file      = wcfg.get("log_file"),
        peer_timeout  = float(wcfg["peer_timeout"]) if "peer_timeout"  in wcfg else None,
        peer_interval = int(wcfg["peer_interval"])  if "peer_interval" in wcfg else None,
        config_path   = config_path if cfg else None,
    )
    # The peer list is a layered setting too: the YAML `peers:` list, then
    # $MU2EDAQ_DISKWATCHER_PEERS, then --peer, each *replacing* the previous.
    yaml_peers, peer_issues = peers_from_config(cfg)
    settings.peers = yaml_peers

    # ---- layer 3: environment ----
    env_issues = settings.apply_env()
    for issue in env_issues:
        print(f"[Config] Warning: {issue}", file=sys.stderr)

    # ---- layer 4: command line (parsed above; applied last so it wins) ----
    cli_peers = None
    if args.no_peers:
        cli_peers = []
    elif args.peers:
        cli_peers, cli_peer_issues = peers_from_urls(args.peers)
        for issue in cli_peer_issues:
            print(f"[Config] Warning: {issue}", file=sys.stderr)
        peer_issues += cli_peer_issues
    settings.apply(
        web_host      = args.host,
        web_port      = args.port,
        poll_interval = args.poll_interval,
        default_delay = args.default_delay,
        daemon        = args.daemon or None,      # store_true never means "off"
        pid_file      = args.pid_file,
        log_file      = args.log_file,
        verbose       = args.verbose,
        peer_timeout  = args.peer_timeout,
        peer_interval = args.peer_interval,
        peers         = cli_peers,
        # Re-assert the resolved path: apply_env() would otherwise leave the
        # /config page showing the environment's value even when --config won.
        config_path   = config_path if cfg else None,
    )

    # Entries are built last: the delay fallback depends on the final
    # default_delay, which any of the three layers above may have set.
    entries, issues = entries_from_config(cfg, default_delay=settings.default_delay)
    settings.entries = entries
    settings.config_issues = env_issues + issues + peer_issues

    if not entries and not settings.peers:
        print("[Config] Warning: no files, paths or peers configured. "
              "Add entries to the YAML config file.", file=sys.stderr)

    # ---- daemonize before starting threads ----
    if settings.daemon:
        log_dest = settings.log_file or os.devnull
        print(f"[Daemon] Daemonizing. Log: {log_dest}  "
              f"PID file: {settings.pid_file or '(none)'}")
        try:
            daemonize(settings.log_file)
        except RuntimeError as exc:
            print(f"[Daemon] {exc}", file=sys.stderr)
            sys.exit(1)
        # From here stdout/stderr go to the log file.
        if settings.pid_file:
            write_pid_file(settings.pid_file)

    n_space = sum(1 for e in entries if e.get("space"))
    n_size  = sum(1 for e in entries if e.get("size"))
    print(f"[Config] Watching {len(entries)} path(s) "
          f"({n_space} for free space, {n_size} for file size), "
          f"poll interval {settings.poll_interval} s")
    print(f"[Web]    Dashboard at http://localhost:{settings.web_port}")

    # One synchronous poll so the first page load has data.
    do_poll()

    threading.Thread(target=poll_loop, daemon=True).start()

    # Peers are fetched on their own thread from the start, never
    # synchronously here: an unreachable peer would otherwise hold up the
    # web server for a full timeout.  Until the first fetch lands the peer
    # shows as "connecting" on the dashboards.
    PEERS.configure(settings.peers)
    if settings.peers:
        enabled = [p for p in settings.peers if p.get("enabled", True)]
        print(f"[Peers]  Federating {len(enabled)} peer(s), every "
              f"{settings.effective_peer_interval()} s, timeout "
              f"{settings.peer_timeout:g} s: "
              + ", ".join(f"{p['label']} <{p['url']}>" for p in enabled))
        threading.Thread(target=peer_loop, daemon=True).start()

    # Imported here so `--version` and `--help` never pay for Flask.
    from .web import create_app
    app = create_app()

    responder = start_responder(settings.web_port)
    try:
        app.run(host=settings.web_host, port=settings.web_port,
                use_reloader=False, threaded=True)
    finally:
        stop_responder(responder)


if __name__ == "__main__":
    main()
