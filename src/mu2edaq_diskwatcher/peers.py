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

Peers can also be *discovered* rather than listed: with ``peers.discover``
enabled, the loop runs a ``mu2edaq-discovery`` multicast scan every scan
interval, turns each ``diskwatcher`` responder into a peer spec, merges those
with the static list (static wins on URL) and drops a peer only after it has
gone unseen for a grace period.  Discovery decides what is *listed*; HTTP
still decides what is *up*.  See :class:`DiscoveryState`.
"""

import fnmatch
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

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


# ---------------------------------------------------------------------------
# Discovery: find peers with mu2edaq-discovery instead of listing them
# ---------------------------------------------------------------------------
def discovery_available() -> bool:
    try:
        import mu2edaq_discovery  # noqa: F401
        return True
    except ImportError:
        return False


def _run_discover(filter: dict, timeout: float) -> List[dict]:
    """One multicast scan.  Separate so tests can substitute canned records."""
    from mu2edaq_discovery import discover
    return discover(filter=filter, timeout=timeout)


def _run_probe(hosts: List[str], filter: dict, timeout: float) -> Tuple[List[dict], List[str]]:
    """Ask each host directly, by unicast, the same DISCOVER question.

    Multicast only reaches the switch the querier sits on when IGMP snooping
    runs without a querier -- the Mu2e DAQ network splits into islands that
    way -- but every responder also answers a datagram sent straight to its
    port.  One socket, one query, every host, then collect replies until the
    timeout.  A ``host:port`` entry overrides the protocol port.  Returns the
    records and one message per host that could not even be sent to.
    """
    from mu2edaq_discovery import protocol

    query = protocol.build_query(filter=filter or None)
    payload = protocol.encode(query)
    errors: List[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", 0))
        sent = 0
        for spec in hosts:
            host, port = spec, protocol.PORT
            if spec.count(":") == 1:
                host, port_text = spec.rsplit(":", 1)
                try:
                    port = int(port_text)
                except ValueError:
                    errors.append(f"{spec}: bad port")
                    continue
            try:
                sock.sendto(payload, (host, port))
                sent += 1
            except OSError as exc:                 # name does not resolve, etc.
                errors.append(f"{spec}: {exc}")
        found: Dict[str, dict] = {}
        deadline = time.monotonic() + timeout
        while sent and len(found) < sent:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(protocol.MAX_DATAGRAM + 1)
            except socket.timeout:
                break
            try:
                msg = protocol.decode(data)
            except protocol.ProtocolError:
                continue
            if msg.get("type") != "ANNOUNCE" or msg.get("qid") != query["qid"]:
                continue
            if not protocol.matches_filter(msg, filter):
                continue
            msg = dict(msg)
            msg["addr"] = addr[0]                  # who actually answered
            found[msg["id"]] = msg
        return list(found.values()), errors
    finally:
        sock.close()


def host_excluded(host: str, patterns: List[str]) -> bool:
    """fnmatch *host* -- full and short forms -- against the exclude globs."""
    short = host.split(".", 1)[0]
    return any(fnmatch.fnmatch(host, p) or fnmatch.fnmatch(short, p) for p in patterns)


def record_to_peer(record: dict, exclude: List[str]) -> Tuple[Optional[dict], Optional[str]]:
    """Turn one ANNOUNCE record into a peer spec, or say why not.

    The reason strings are the keys counted in the discovery status, so an
    operator can see "3 responders, 1 excluded: self" rather than a bare
    number.
    """
    host = str(record.get("host") or "")
    scheme = str(record.get("scheme") or "")
    port = record.get("port")
    if not host or not isinstance(port, int):
        return None, "malformed"
    if scheme not in ("http", "https"):
        return None, f"scheme {scheme or '?'}"
    meta = record.get("meta") or {}
    if meta.get("instance_id") == INSTANCE_ID:
        return None, "self"
    if host_excluded(host, exclude):
        return None, "excluded"
    return {
        "url":           f"{scheme}://{host}:{port}",
        "label":         host.split(".", 1)[0],
        "timeout":       None,
        "enabled":       True,
        "config_errors": [],
        "source":        "discovered",
        "discovery_id":  record.get("id"),
        "discovery_host": host,
        "discovery_version": record.get("version"),
    }, None


class DiscoveryState:
    """Everything known about peer discovery, for the poll loop and /api/peers.

    ``_seen`` maps URL to the peer spec plus the time it last answered a scan.
    A peer that stops answering is kept, and still fetched, until it has been
    unseen for the grace period -- multicast replies do get lost, and one lost
    reply must not blank a node's rows.  HTTP remains the source of truth for
    whether the peer is *up*; discovery only decides whether it is *listed*.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: Dict[str, dict] = {}
        self.last_scan: Optional[float] = None
        self.last_scan_duration_s: Optional[float] = None
        self.responders = 0
        self.multicast_replies = 0
        self.probe_replies = 0
        self.probe_errors: List[str] = []
        self.excluded: Dict[str, int] = {}
        self.error: Optional[str] = None
        self.scans = 0

    def due(self, now: float, interval: float) -> bool:
        return self.last_scan is None or now - self.last_scan >= interval

    def scan(self, cfg: dict, now: float) -> None:
        """Run one discovery -- multicast, then unicast probes -- into ``_seen``.

        The two answer sets are merged by responder id, so a host reached both
        ways counts once.  A failure of one path is reported but does not
        discard the other's answers.
        """
        started = time.time()
        filt = cfg.get("filter") or {}
        timeout = cfg.get("timeout") or 2.0
        errors: List[str] = []
        records: Dict[str, dict] = {}

        try:
            multicast = _run_discover(filt, timeout)
        except ImportError:
            multicast, errors = [], ["mu2edaq-discovery is not installed"]
        except Exception as exc:                   # socket errors, bad datagrams
            multicast, errors = [], [f"multicast: {type(exc).__name__}: {exc}"]
        for record in multicast:
            records[record.get("id") or record.get("host")] = record

        probe_hosts = list(cfg.get("probe") or [])
        probe_replies = 0
        probe_errors: List[str] = []
        if probe_hosts and "mu2edaq-discovery is not installed" not in errors:
            try:
                probed, probe_errors = _run_probe(probe_hosts, filt, timeout)
            except Exception as exc:
                probed, probe_errors = [], [f"probe: {type(exc).__name__}: {exc}"]
            probe_replies = len(probed)
            for record in probed:
                records.setdefault(record.get("id") or record.get("host"), record)

        excluded: Dict[str, int] = {}
        found: Dict[str, dict] = {}
        for record in records.values():
            peer, reason = record_to_peer(record, cfg.get("exclude") or [])
            if peer is None:
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            found[peer["url"]] = peer

        with self._lock:
            for url, peer in found.items():
                previous = self._seen.get(url)
                if previous is None:
                    print(f"[Peers] discovered {peer['label']} at {peer['url']} "
                          f"(diskwatcher {peer.get('discovery_version')})")
                peer["discovery_last_seen"] = now
                peer["discovery_first_seen"] = (previous or {}).get("discovery_first_seen", now)
                self._seen[url] = peer
            self.last_scan = now
            self.last_scan_duration_s = round(time.time() - started, 3)
            self.responders = len(records)
            self.multicast_replies = len(multicast)
            self.probe_replies = probe_replies
            self.probe_errors = probe_errors
            self.excluded = excluded
            self.error = "; ".join(errors) if errors else None
            self.scans += 1
        if errors and (self.scans == 1 or get_settings().verbose):
            print(f"[Peers] discovery failed: {'; '.join(errors)}", file=sys.stderr)
        if probe_errors and (self.scans == 1 or get_settings().verbose):
            print(f"[Peers] discovery probe: {len(probe_errors)} host(s) could not be "
                  f"sent to: {'; '.join(probe_errors)}", file=sys.stderr)

    def expire(self, now: float, grace: float) -> List[str]:
        """Drop peers unseen for *grace* seconds; return their URLs."""
        with self._lock:
            gone = [url for url, p in self._seen.items()
                    if now - p["discovery_last_seen"] > grace]
            for url in gone:
                peer = self._seen.pop(url)
                print(f"[Peers] {peer['label']} ({url}) not seen by discovery for "
                      f"{grace:g} s; dropping it", file=sys.stderr)
        return gone

    def current(self) -> List[dict]:
        """Peer specs still within grace, flagged if the last scan missed them."""
        with self._lock:
            last = self.last_scan
            out = []
            for peer in self._seen.values():
                spec = dict(peer)
                spec["discovery_missing"] = last is not None and \
                    peer["discovery_last_seen"] < last
                out.append(spec)
        return sorted(out, key=lambda p: (p["label"], p["url"]))

    def snapshot(self, cfg: dict) -> dict:
        """For /api/peers and the Config page."""
        now = time.time()
        with self._lock:
            last = self.last_scan
            return {
                "enabled":              bool(cfg.get("enabled")),
                "available":            discovery_available(),
                "filter":               dict(cfg.get("filter") or {}),
                "interval":             cfg.get("interval"),
                "timeout":              cfg.get("timeout"),
                "grace":                cfg.get("grace"),
                "exclude":              list(cfg.get("exclude") or []),
                "probe":                list(cfg.get("probe") or []),
                "scans":                self.scans,
                "last_scan":            last,
                "last_scan_age_s":      None if last is None else round(now - last, 1),
                "last_scan_duration_s": self.last_scan_duration_s,
                "responders":           self.responders,
                "multicast_replies":    self.multicast_replies,
                "probe_replies":        self.probe_replies,
                "probe_errors":         list(self.probe_errors),
                "excluded":             dict(self.excluded),
                "error":                self.error,
                "peers":                sorted(self._seen.keys()),
            }

    def clear(self) -> None:
        with self._lock:
            self._seen = {}
            self.last_scan = None
            self.last_scan_duration_s = None
            self.responders = 0
            self.multicast_replies = 0
            self.probe_replies = 0
            self.probe_errors = []
            self.excluded = {}
            self.error = None
            self.scans = 0


#: Process-wide discovery bookkeeping.
DISCOVERY = DiscoveryState()


def merge_peers(static: List[dict], discovered: List[dict]) -> List[dict]:
    """Static entries first and winning on URL, so an operator's label,
    timeout or ``enabled: false`` is respected for a peer discovery also finds."""
    urls = {p["url"] for p in static}
    return list(static) + [p for p in discovered if p["url"] not in urls]


def active_peers(now: Optional[float] = None) -> List[dict]:
    """Every peer to fetch this cycle, running a discovery scan when due."""
    settings = get_settings()
    static = list(settings.peers)
    cfg = settings.discover
    if not cfg.get("enabled"):
        return static
    now = time.time() if now is None else now
    interval = cfg.get("interval") or settings.effective_peer_interval()
    if DISCOVERY.due(now, interval):
        DISCOVERY.scan(cfg, now)
    static_urls = {p["url"] for p in static}
    for url in DISCOVERY.expire(now, cfg.get("grace") or 3 * interval):
        if url not in static_urls:
            PEERS.remove(url)
    return merge_peers(static, DISCOVERY.current())


def do_peer_poll() -> None:
    """Fetch every enabled peer concurrently and publish the results."""
    peers = active_peers()
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
        # A discovery scan can be due sooner than the fetch interval; sleep
        # for the shorter of the two so neither cadence stretches the other.
        settings = get_settings()
        sleep_for = settings.effective_peer_interval()
        if settings.discover.get("enabled") and settings.discover.get("interval"):
            sleep_for = min(sleep_for, settings.discover["interval"])
        time.sleep(sleep_for)
