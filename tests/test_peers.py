"""Federation: fetching another instance's /api/state and showing it here.

The peer is a real HTTP server on a loopback port, not a patched urlopen, so
the whole client path is exercised: connect, read, size limit, JSON decode,
payload validation.  Its canned payload is built from real poll_entry()
results, so the entry shapes are the shipped ones.  The test settings and
stores are per-process singletons, which is why the peer is a stub server
rather than a second Flask app -- two instances cannot share one STORE.
"""

import http.server
import json
import threading
import time

import pytest
import yaml

from mu2edaq_diskwatcher import INSTANCE_ID, __version__, peers
from mu2edaq_diskwatcher.config import entries_from_config, peers_from_urls
from mu2edaq_diskwatcher.peers import PeerError, do_peer_poll, fetch_state, poll_peer
from mu2edaq_diskwatcher.poller import poll_entry
from mu2edaq_diskwatcher.state import PEERS, peer_record, reachable_entries


# ---------------------------------------------------------------- a fake peer
class _Stub(http.server.BaseHTTPRequestHandler):
    """Serves whatever `server.responses[path]` holds; 404 otherwise.

    A response is ``(status, body_bytes, content_type)``.  ``body`` may also be
    a callable returning bytes so a test can change the answer between fetches.
    """

    def do_GET(self):                                        # noqa: N802
        self.server.requests.append(self.path)
        spec = self.server.responses.get(self.path)
        if spec is None:
            self.send_error(404)
            return
        status, body, ctype = spec
        if callable(body):
            body = body()
        if self.server.delay:
            time.sleep(self.server.delay)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):                           # keep pytest quiet
        pass


@pytest.fixture
def stub():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    server.responses = {}
    server.requests = []
    server.delay = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_port}"
    yield server
    server.shutdown()
    server.server_close()


def state_payload(tmp_tree, **overrides):
    """A realistic /api/state body from real entries."""
    entries, _ = entries_from_config(yaml.safe_load(f"""
files:
  - path: {tmp_tree['big']}
    delay: 3600
    size:
      warning: 1MiB
  - path: {tmp_tree['missing']}
    delay: 60
paths:
  - path: {tmp_tree['root']}
    space:
      warning: 1B
"""))
    now = time.time()
    payload = {
        "version": "1.2.0",
        "instance_id": "peer-instance",
        "hostname": "mu2e-dl-99",
        "generated": "2026-09-11T00:00:00Z",
        "poll_interval": 30,
        "poll_age_s": 4.2,
        "entries": [poll_entry(e, now) for e in entries],
    }
    payload.update(overrides)
    return payload


def serve_state(stub, payload, path="/api/state"):
    stub.responses[path] = (200, json.dumps(payload).encode(), "application/json")


def one_peer(url, **kw):
    peer, issues = peers_from_urls([url])
    assert not issues
    peer[0].update(kw)
    return peer[0]


# ------------------------------------------------------------- fetch_state
def test_fetch_state_returns_the_payload(stub, tmp_tree):
    serve_state(stub, state_payload(tmp_tree))
    payload = fetch_state(stub.url, timeout=2)
    assert payload["hostname"] == "mu2e-dl-99"
    assert len(payload["entries"]) == 3
    assert stub.requests == ["/api/state"]


def test_fetch_state_asks_for_local_entries_only(stub, tmp_tree):
    """One hop: the request must not carry ?peers=1, or A<->B would recurse."""
    serve_state(stub, state_payload(tmp_tree))
    fetch_state(stub.url + "/", timeout=2)
    assert stub.requests == ["/api/state"]


def test_connection_refused_is_a_peer_error():
    with pytest.raises(PeerError):
        fetch_state("http://127.0.0.1:9", timeout=1)      # discard port, closed


def test_http_error_is_reported_with_the_code(stub):
    stub.responses["/api/state"] = (503, b"down", "text/plain")
    with pytest.raises(PeerError, match="HTTP 503"):
        fetch_state(stub.url, timeout=2)


def test_not_json_is_a_peer_error(stub):
    stub.responses["/api/state"] = (200, b"<html>hello</html>", "text/html")
    with pytest.raises(PeerError, match="not JSON"):
        fetch_state(stub.url, timeout=2)


def test_json_that_is_not_a_state_payload_is_rejected(stub):
    """Pointing a peer at some other JSON service must not half-work."""
    stub.responses["/api/state"] = (200, b'{"status": "ok"}', "application/json")
    with pytest.raises(PeerError, match="not a diskwatcher"):
        fetch_state(stub.url, timeout=2)


def test_timeout_is_a_peer_error(stub, tmp_tree):
    serve_state(stub, state_payload(tmp_tree))
    stub.delay = 1.5
    with pytest.raises(PeerError, match="timed out"):
        fetch_state(stub.url, timeout=0.3)


def test_oversized_response_is_refused(stub, monkeypatch):
    monkeypatch.setattr(peers, "MAX_RESPONSE_BYTES", 64)
    stub.responses["/api/state"] = (200, b'{"entries": [' + b"0," * 100 + b"0]}",
                                    "application/json")
    with pytest.raises(PeerError, match="larger than"):
        fetch_state(stub.url, timeout=2)


# --------------------------------------------------------------- poll_peer
def test_successful_poll_stamps_entries_and_records_identity(stub, tmp_tree):
    serve_state(stub, state_payload(tmp_tree))
    peer = one_peer(stub.url, label="dl-99")
    record = poll_peer(peer, previous=None)
    assert record["status"] == "ok" and record["ok"] is True
    assert record["error"] is None
    assert record["version"] == "1.2.0"
    assert record["hostname"] == "mu2e-dl-99"
    assert record["peer_poll_age_s"] == 4.2
    assert record["last_ok"] == record["fetched_at"]
    assert len(record["entries"]) == 3
    for entry in record["entries"]:
        assert entry["peer"] == "dl-99"
        assert entry["peer_url"] == stub.url
        # The peer's evaluation is trusted, not redone.
        assert "space_state" in entry and "watch_state" in entry


def test_record_shape_is_identical_in_every_state(stub, tmp_tree):
    """Like STATE_KEYS for entries: no key may exist in one branch only."""
    serve_state(stub, state_payload(tmp_tree))
    peer = one_peer(stub.url)
    good = poll_peer(peer, previous=None)
    bad = poll_peer(one_peer("http://127.0.0.1:9"), previous=None)
    pending = peer_record(peer)
    assert set(good) == set(bad) == set(pending)


def test_failed_poll_keeps_the_last_good_entries(stub, tmp_tree):
    """The control-room case: a blip must dim a node's volumes, not erase them."""
    serve_state(stub, state_payload(tmp_tree))
    peer = one_peer(stub.url)
    good = poll_peer(peer, previous=None)

    stub.responses["/api/state"] = (500, b"boom", "text/plain")
    bad = poll_peer(peer, previous=good)
    assert bad["status"] == "error"
    assert "HTTP 500" in bad["error"]
    assert bad["entries"] == good["entries"]              # retained
    assert bad["last_ok"] == good["last_ok"]              # not advanced
    assert bad["fetched_at"] > good["fetched_at"] or bad["attempts"] == 2
    assert bad["version"] == "1.2.0"                      # identity kept too
    assert bad["failures"] == 1 and bad["attempts"] == 2

    worse = poll_peer(peer, previous=bad)
    assert worse["failures"] == 2


def test_failed_first_poll_has_no_entries(stub):
    record = poll_peer(one_peer(stub.url), previous=None)   # 404: nothing served
    assert record["status"] == "error"
    assert record["entries"] == []
    assert record["last_ok"] is None


def test_recovery_clears_the_error_and_failure_count(stub, tmp_tree):
    peer = one_peer(stub.url)
    bad = poll_peer(peer, previous=None)
    serve_state(stub, state_payload(tmp_tree))
    good = poll_peer(peer, previous=bad)
    assert good["status"] == "ok" and good["error"] is None
    assert good["failures"] == 0 and good["attempts"] == 2


def test_a_peer_that_is_ourselves_is_refused(stub, tmp_tree):
    """A URL resolving back to this process must not duplicate local paths."""
    serve_state(stub, state_payload(tmp_tree, instance_id=INSTANCE_ID))
    record = poll_peer(one_peer(stub.url), previous=None)
    assert record["status"] == "error"
    assert "this instance" in record["error"]
    assert record["entries"] == []


def test_entries_that_are_not_entries_are_dropped(stub, tmp_tree):
    payload = state_payload(tmp_tree)
    payload["entries"] += ["garbage", 42, {"no": "path"}]
    serve_state(stub, payload)
    record = poll_peer(one_peer(stub.url), previous=None)
    assert record["status"] == "ok"
    assert len(record["entries"]) == 3


def test_per_peer_timeout_beats_the_global_one(stub, tmp_tree, settings):
    serve_state(stub, state_payload(tmp_tree))
    stub.delay = 0.6
    settings.peer_timeout = 5.0
    record = poll_peer(one_peer(stub.url, timeout=0.2), previous=None)
    assert record["status"] == "error" and "timed out" in record["error"]


def test_user_agent_names_the_application(stub, tmp_tree):
    seen = {}

    class Recorder(_Stub):
        def do_GET(self):                                    # noqa: N802
            seen["ua"] = self.headers.get("User-Agent")
            super().do_GET()

    stub.RequestHandlerClass = Recorder
    serve_state(stub, state_payload(tmp_tree))
    fetch_state(stub.url, timeout=2)
    assert seen["ua"] == f"mu2edaq-diskwatcher/{__version__}"


# ------------------------------------------------------------ do_peer_poll
def test_do_peer_poll_publishes_every_peer(stub, tmp_tree, settings):
    serve_state(stub, state_payload(tmp_tree))
    good = one_peer(stub.url, label="good")
    dead = one_peer("http://127.0.0.1:9", label="dead")
    off = one_peer("http://127.0.0.1:10", label="off", enabled=False)
    settings.peers = [good, dead, off]
    PEERS.configure(settings.peers)
    assert [p["status"] for p in PEERS.snapshot()] == ["pending", "pending", "disabled"]

    do_peer_poll()
    by_label = {p["label"]: p for p in PEERS.snapshot()}
    assert by_label["good"]["status"] == "ok"
    assert by_label["dead"]["status"] == "error"
    assert by_label["off"]["status"] == "disabled"
    assert PEERS.counts() == {"pending": 0, "ok": 1, "error": 1, "disabled": 1,
                              "configured": 3}
    # Config order is preserved regardless of which fetch finished first.
    assert [p["label"] for p in PEERS.snapshot()] == ["good", "dead", "off"]


def test_do_peer_poll_with_no_peers_is_a_no_op(settings):
    settings.peers = []
    do_peer_poll()
    assert PEERS.snapshot() == []


def test_snapshot_derives_ages_and_staleness(stub, tmp_tree, settings):
    serve_state(stub, state_payload(tmp_tree))
    settings.peers = [one_peer(stub.url)]
    PEERS.configure(settings.peers)
    do_peer_poll()
    (rec,) = PEERS.snapshot()
    assert rec["stale"] is False
    assert rec["fetched_age_s"] is not None and rec["fetched_age_s"] < 5
    assert rec["fetched_at_str"].endswith("UTC")
    assert rec["total"] == 3

    stub.responses["/api/state"] = (500, b"", "text/plain")
    do_peer_poll()
    (rec,) = PEERS.snapshot()
    assert rec["status"] == "error"
    assert rec["stale"] is True                 # error *with* retained entries
    assert rec["total"] == 3


def test_reachable_entries_excludes_retained_data(stub, tmp_tree, settings):
    """Retained rows are shown dimmed, never counted as current."""
    serve_state(stub, state_payload(tmp_tree))
    settings.peers = [one_peer(stub.url)]
    PEERS.configure(settings.peers)
    do_peer_poll()
    assert len(reachable_entries(PEERS.snapshot())) == 3
    stub.responses["/api/state"] = (500, b"", "text/plain")
    do_peer_poll()
    assert reachable_entries(PEERS.snapshot()) == []


def test_snapshot_copies_do_not_alias_the_store(stub, tmp_tree, settings):
    serve_state(stub, state_payload(tmp_tree))
    settings.peers = [one_peer(stub.url)]
    PEERS.configure(settings.peers)
    do_peer_poll()
    snap = PEERS.snapshot()[0]
    snap["entries"].clear()
    snap["label"] = "mutated"
    fresh = PEERS.snapshot()[0]
    assert len(fresh["entries"]) == 3 and fresh["label"] != "mutated"


def test_configure_drops_peers_no_longer_present(settings):
    a, b = one_peer("http://a:1"), one_peer("http://b:1")
    PEERS.configure([a, b])
    PEERS.configure([b])
    assert [p["url"] for p in PEERS.snapshot()] == ["http://b:1"]


# ------------------------------------------------------------- discovery
# The multicast scan itself belongs to mu2edaq-discovery and is tested there.
# What is ours is everything done with the records: turning them into peers,
# excluding ourselves and unwanted hosts, merging with the static list, and
# not dropping a peer on one missed reply.  `_run_discover` is the seam.
from mu2edaq_diskwatcher.peers import (  # noqa: E402
    DISCOVERY, active_peers, host_excluded, merge_peers, record_to_peer,
)


def announce(host, port=5002, **extra):
    """A record shaped like protocol.build_announce() output."""
    rec = {"proto": "mu2edaq-discovery/1", "type": "ANNOUNCE", "id": f"id-{host}",
           "name": "Disk Watcher", "app": "diskwatcher", "host": host, "port": port,
           "scheme": "http", "version": "1.3.0", "pid": 1, "started": "2026-09-14T00:00:00Z"}
    rec.update(extra)
    return rec


@pytest.fixture
def scans(monkeypatch):
    """Queue of canned scan results; each call to _run_discover pops one.
    Records the filters it was asked for."""
    class Queue(list):
        pass

    queue, asked = Queue(), []

    def fake(filter, timeout):
        asked.append(dict(filter))
        if not queue:
            return []
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(peers, "_run_discover", fake)
    queue.asked = asked
    return queue


def enable_discovery(settings, **overrides):
    settings.discover.update({"enabled": True, "interval": 10, "grace": 30,
                              "timeout": 0.1, "filter": {"app": "diskwatcher"},
                              "exclude": []})
    settings.discover.update(overrides)


def test_record_becomes_a_peer_with_short_label():
    peer, reason = record_to_peer(announce("mu2e-dl-01.fnal.gov"), exclude=[])
    assert reason is None
    assert peer["url"] == "http://mu2e-dl-01.fnal.gov:5002"
    assert peer["label"] == "mu2e-dl-01"
    assert peer["source"] == "discovered" and peer["discovery_id"] == "id-mu2e-dl-01.fnal.gov"


def test_our_own_responder_is_set_aside_before_any_http():
    rec = announce("here", meta={"instance_id": INSTANCE_ID})
    assert record_to_peer(rec, exclude=[]) == (None, "self")


@pytest.mark.parametrize("patterns, host, hit", [
    (["mu2e-dl-99"], "mu2e-dl-99.fnal.gov", True),      # short form matches
    (["*.fnal.gov"], "mu2e-dl-01.fnal.gov", True),      # full form matches
    (["test-*"], "test-node", True),
    (["mu2e-dl-99"], "mu2e-dl-01.fnal.gov", False),
])
def test_host_exclusion_globs(patterns, host, hit):
    assert host_excluded(host, patterns) is hit
    peer, reason = record_to_peer(announce(host), exclude=patterns)
    assert (peer is None) is hit and (reason == "excluded") is hit


def test_non_http_and_malformed_records_are_set_aside():
    assert record_to_peer(announce("x", scheme="vnc"), [])[1] == "scheme vnc"
    assert record_to_peer({"host": "x"}, [])[1] == "malformed"


def test_merge_static_wins_on_url():
    static = one_peer("http://a:5002", label="A-by-hand", timeout=9)
    found, _ = record_to_peer(announce("a"), [])
    other, _ = record_to_peer(announce("b"), [])
    merged = merge_peers([static], [found, other])
    assert [p["label"] for p in merged] == ["A-by-hand", "b"]
    assert merged[0]["timeout"] == 9 and merged[0]["source"] == "static"


def test_active_peers_runs_a_scan_when_due_and_reuses_it_otherwise(settings, scans):
    enable_discovery(settings, filter={"app": "diskwatcher", "host": "mu2e-*"})
    scans.append([announce("mu2e-dl-01"), announce("mu2e-dl-02")])
    now = 1000.0
    found = active_peers(now)
    assert [p["label"] for p in found] == ["mu2e-dl-01", "mu2e-dl-02"]
    assert scans.asked == [{"app": "diskwatcher", "host": "mu2e-*"}]
    assert all(p["discovery_missing"] is False for p in found)
    # Not due yet: no second scan, same peers.
    assert [p["label"] for p in active_peers(now + 5)] == ["mu2e-dl-01", "mu2e-dl-02"]
    assert len(scans.asked) == 1
    assert DISCOVERY.snapshot(settings.discover)["responders"] == 2


def test_disabled_discovery_never_scans(settings, scans):
    settings.discover["enabled"] = False
    settings.peers = [one_peer("http://a:1")]
    scans.append([announce("b")])
    assert [p["url"] for p in active_peers(0.0)] == ["http://a:1"]
    assert scans.asked == []


def test_one_missed_reply_keeps_the_peer_flagged_not_dropped(settings, scans):
    enable_discovery(settings)
    scans.append([announce("a"), announce("b")])
    active_peers(0.0)
    scans.append([announce("a")])                         # b lost this time
    found = active_peers(10.0)
    by = {p["label"]: p for p in found}
    assert set(by) == {"a", "b"}
    assert by["b"]["discovery_missing"] is True and by["a"]["discovery_missing"] is False
    assert by["b"]["discovery_last_seen"] == 0.0


def test_peer_unseen_past_grace_is_dropped_and_forgotten(settings, scans):
    enable_discovery(settings, grace=25)
    scans.append([announce("a"), announce("b")])
    active_peers(0.0)
    PEERS.update("http://b:5002", peer_record({"url": "http://b:5002", "label": "b"}, "ok"))
    scans.extend([[announce("a")], [announce("a")], [announce("a")]])
    active_peers(10.0)
    active_peers(20.0)
    assert {p["label"] for p in active_peers(30.0)} == {"a"}   # 30 s > grace 25
    assert PEERS.get("http://b:5002") is None                  # record removed too
    assert DISCOVERY.snapshot(settings.discover)["peers"] == ["http://a:5002"]


def test_a_static_peer_that_discovery_also_finds_is_never_removed(settings, scans):
    enable_discovery(settings, grace=5)
    static = one_peer("http://a:5002", label="A")
    settings.peers = [static]
    PEERS.configure(settings.peers)
    scans.extend([[announce("a")], []])
    active_peers(0.0)
    found = active_peers(100.0)                            # a vanished from discovery
    assert [p["label"] for p in found] == ["A"]
    assert PEERS.get("http://a:5002") is not None


def test_scan_failure_is_recorded_and_leaves_known_peers_alone(settings, scans):
    enable_discovery(settings)
    scans.append([announce("a")])
    active_peers(0.0)
    scans.append(OSError("multicast unavailable"))
    found = active_peers(10.0)
    assert [p["label"] for p in found] == ["a"]
    snap = DISCOVERY.snapshot(settings.discover)
    assert "multicast unavailable" in snap["error"]
    assert snap["responders"] == 0 and snap["scans"] == 2


def test_missing_package_is_a_clear_error(settings, scans):
    enable_discovery(settings)
    scans.append(ImportError("No module named 'mu2edaq_discovery'"))
    assert active_peers(0.0) == []
    assert DISCOVERY.snapshot(settings.discover)["error"] == "mu2edaq-discovery is not installed"


def test_excluded_reasons_are_counted_for_the_operator(settings, scans):
    enable_discovery(settings, exclude=["skip-*"])
    scans.append([announce("a"), announce("skip-me"),
                  announce("me", meta={"instance_id": INSTANCE_ID}),
                  announce("v", scheme="vnc")])
    assert [p["label"] for p in active_peers(0.0)] == ["a"]
    snap = DISCOVERY.snapshot(settings.discover)
    assert snap["responders"] == 4
    assert snap["excluded"] == {"excluded": 1, "self": 1, "scheme vnc": 1}


def test_do_peer_poll_fetches_discovered_peers_and_records_provenance(stub, tmp_tree, settings, scans):
    serve_state(stub, state_payload(tmp_tree))
    host, port = "127.0.0.1", stub.server_port
    enable_discovery(settings)
    scans.append([announce(host, port=port)])
    do_peer_poll()
    (rec,) = PEERS.snapshot()
    assert rec["status"] == "ok" and rec["source"] == "discovered"
    assert rec["url"] == stub.url and rec["label"] == "127"
    assert rec["discovery_id"] == f"id-{host}"
    assert rec["discovery_missing"] is False
    assert rec["discovery_seen_age_s"] is not None
    assert all(e["peer"] == "127" for e in rec["entries"])
