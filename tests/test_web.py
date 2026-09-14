import pathlib
import time

import pytest
import yaml

import mu2edaq_diskwatcher.web as _web
from mu2edaq_diskwatcher.config import entries_from_config, peers_from_urls
from mu2edaq_diskwatcher.poller import poll_entry
from mu2edaq_diskwatcher.state import PEERS, STORE, peer_record
from mu2edaq_diskwatcher.web.api import LEGACY_ENTRY_KEYS

# Resolved from the package rather than the repo, so these still point at the
# shipped assets when the tests run against an installed wheel.
_WEB = pathlib.Path(_web.__file__).parent
TEMPLATES = _WEB / "templates"
STATIC = _WEB / "static"

PAGES = ["/", "/space", "/sizes", "/config", "/about", "/api", "/sitemap"]
JSON_ENDPOINTS = ["status", "state", "space", "sizes", "entries",
                  "peers", "config", "health", "version"]
#: Endpoints that grow `peers` and `aggregate` under ?peers=1.
PEER_AWARE = ["status", "state", "space", "sizes"]


@pytest.fixture
def populated(tmp_tree, settings):
    """A store holding one of every interesting state."""
    settings.config_path = None
    settings.entries, _ = entries_from_config(yaml.safe_load(f"""
files:
  - path: {tmp_tree['empty']}
    label: "Empty file"
    size:
      warning: 1MiB
  - path: {tmp_tree['big']}
    delay: 3600
    size:
      warning: 1MiB
      critical: 4MiB
  - path: {tmp_tree['missing']}
    delay: 60
paths:
  - path: {tmp_tree['root']}
    delay: 3600
    space:
      warning: 1B
  - path: {tmp_tree['subdir']}
    delay: 3600
"""))
    now = time.time()
    STORE.replace([poll_entry(e, now) for e in settings.entries])
    return settings


@pytest.fixture
def federated(populated, tmp_tree):
    """`populated` plus two peers: one connected, one unreachable with data.

    The peer records are installed directly -- the HTTP client has its own
    tests in test_peers.py; here the question is what the API does with them.
    The connected peer's entries are the same five as the local ones, re-polled
    and stamped, so every count below is a known multiple.
    """
    now = time.time()
    (up,), _ = peers_from_urls(["http://up:5002"])
    up["label"] = "up"
    (down,), _ = peers_from_urls(["http://down:5002"])
    down["label"] = "down"
    populated.peers = [up, down]
    PEERS.configure(populated.peers)

    def stamped(peer):
        out = []
        for e in populated.entries:
            s = poll_entry(e, now)
            s["peer"], s["peer_url"] = peer["label"], peer["url"]
            out.append(s)
        return out

    good = peer_record(up, "ok")
    good.update(entries=stamped(up), fetched_at=now, last_ok=now,
                version="1.2.0", hostname="up-host", instance_id="up-id",
                peer_poll_age_s=2.0, peer_poll_interval=30)
    PEERS.update(up["url"], good)

    bad = peer_record(down, "error")
    bad.update(entries=stamped(down), error="connection refused",
               fetched_at=now, last_ok=now - 600, version="1.2.0",
               hostname="down-host", failures=3, attempts=5)
    PEERS.update(down["url"], bad)
    return populated


# ------------------------------------------------------------------- pages
@pytest.mark.parametrize("path", PAGES)
def test_pages_render(client, populated, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.mimetype == "text/html"


@pytest.mark.parametrize("path", PAGES)
def test_navbar_appears_exactly_once(client, populated, path):
    # It used to be copy-pasted per page and had already drifted apart.
    assert client.get(path).get_data(as_text=True).count('class="navbar ') == 1


@pytest.mark.parametrize("path", PAGES)
def test_every_page_links_to_every_other(client, populated, path):
    body = client.get(path).get_data(as_text=True)
    for target in PAGES:
        assert f'href="{target}"' in body


def test_unknown_page_is_404(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert b"Sitemap" in response.data          # the error page is themed


def test_config_page_shows_thresholds(client, populated):
    body = client.get("/config").get_data(as_text=True)
    assert "1MiB" in body and "4MiB" in body


def test_config_page_survives_a_missing_config_file(client, settings):
    settings.config_path = "/definitely/not/here.yaml"
    body = client.get("/config").get_data(as_text=True)
    assert client.get("/config").status_code == 200
    assert "Could not read" in body


# ------------------------------------------------------------------- JSON
@pytest.mark.parametrize("name", JSON_ENDPOINTS)
def test_json_endpoints_respond(client, populated, name):
    response = client.get(f"/api/{name}")
    assert response.status_code == 200
    assert response.mimetype == "application/json"


def test_status_legacy_schema(client, populated):
    """The compatibility promise: no key ever disappears from /api/status."""
    payload = client.get("/api/status").get_json()
    assert {"files", "total", "stale", "ok", "poll_interval"} <= set(payload)
    for entry in payload["files"]:
        assert LEGACY_ENTRY_KEYS <= set(entry), LEGACY_ENTRY_KEYS - set(entry)
    assert payload["total"] == payload["ok"] + payload["stale"]


def test_status_counts_match_the_entries(client, populated):
    payload = client.get("/api/status").get_json()
    assert payload["total"] == len(payload["files"]) == 5
    assert payload["stale"] == sum(1 for f in payload["files"] if f["stale"])


def test_state_carries_all_three_summaries(client, populated):
    payload = client.get("/api/state").get_json()
    assert set(payload["summary"]) == {"watch", "space", "size"}
    assert payload["summary"]["watch"]["total"] == 5
    assert payload["summary"]["watch"]["missing"] == 1
    assert payload["version"] and payload["generated"]


def test_space_returns_only_space_monitored_directories(client, populated):
    payload = client.get("/api/space").get_json()
    assert len(payload["entries"]) == 1
    assert all(e["space_monitored"] for e in payload["entries"])
    assert payload["summary"]["total"] == 1
    # Counts must add up to the total, or the cards lie.
    counted = sum(payload["summary"][k] for k in
                  ("good", "warning", "critical", "full", "missing", "unknown"))
    assert counted == payload["summary"]["total"]


def test_sizes_returns_only_size_monitored_files(client, populated):
    payload = client.get("/api/sizes").get_json()
    assert len(payload["entries"]) == 2
    assert all(e["size_monitored"] for e in payload["entries"])
    states = {e["size_state"] for e in payload["entries"]}
    assert "EMPTY" in states
    counted = sum(payload["summary"][k] for k in
                  ("good", "empty", "warning", "critical", "full", "missing", "unknown"))
    assert counted == payload["summary"]["total"]


# ---- host grouping -----------------------------------------------------
# The dashboards group rows by host and let each group collapse.  That is
# client-side JS, so pytest cannot execute it; what it can lock is the
# server->client contract the grouping stands on, and the wiring being present
# in both halves at once.
@pytest.mark.parametrize("endpoint", ["/api/space", "/api/sizes"])
def test_grouping_fields_are_present_on_every_entry(client, populated, endpoint):
    """hostOf() keys on these two. Drop them and every remote path silently
    collapses into one bogus 'unknown host' group."""
    for entry in client.get(endpoint).get_json()["entries"]:
        assert "remote" in entry, entry.get("path")
        assert "ssh_host" in entry, entry.get("path")
        # A local entry must be falsy-remote, not merely missing the key.
        if not entry["remote"]:
            assert entry["ssh_host"] is None


@pytest.mark.parametrize("page, table", [("space.html", "space"),
                                         ("sizes.html", "sizes")])
def test_dashboards_render_rows_through_the_grouper(page, table):
    src = (TEMPLATES / page).read_text()
    assert f"groupedRows('{table}'" in src, "page no longer groups by host"
    # The row builder must not short-circuit on an empty list any more:
    # groupedRows() owns the empty case, and a null here would print "null".
    assert "if (!entries.length) return null;" not in src


def test_shared_js_defines_the_grouping_helpers():
    src = (STATIC / "diskwatcher.js").read_text()
    # The trailing "(" matters: without it "function groupedRows" also matches
    # a renamed "function groupedRowsX", and the guard never fires.
    for fn in ("groupedRows", "toggleGroup", "hostOf", "groupByHost",
               "isCollapsed", "needsAttention"):
        assert f"function {fn}(" in src, fn
    # Open/closed state must live outside the DOM: the tbody is rebuilt on
    # every poll, so a DOM-only toggle would spring back within seconds.
    assert "hostOverrides" in src
    assert "diskwatcher.hostGroups" in src


def test_group_default_opens_on_rank_not_a_hardcoded_state_name():
    """A group opens once any entry's rank reaches the server's alert_rank.

    The comparison must stay against the numeric rank the server ships in
    every /api/space and /api/sizes payload (test_alert_rank_is_shipped_on_
    space_and_sizes below), never a literal state name -- that is what lets a
    state be renamed or a new one inserted above CRITICAL without touching the
    client.
    """
    src = (STATIC / "diskwatcher.js").read_text()
    body = src[src.index("function needsAttention("):src.index("function isCollapsed(")]
    assert ">= alertRank" in body, body
    for name in ("GOOD", "CRITICAL", "WARNING", "FULL", "MISSING", "UNKNOWN"):
        assert name not in body, f"needsAttention() should not name {name!r}"


@pytest.mark.parametrize("endpoint", ["/api/space", "/api/sizes"])
def test_alert_rank_is_shipped_on_space_and_sizes(client, populated, endpoint):
    """The default-open threshold is defined once, server-side, in SEVERITY.

    The JS only has a hardcoded fallback for a payload that omits this field;
    a real payload must always carry it, or every group silently falls back to
    collapsing only when literally nothing has a rank at all.
    """
    from mu2edaq_diskwatcher.thresholds import SEVERITY
    payload = client.get(endpoint).get_json()
    assert payload["alert_rank"] == SEVERITY["CRITICAL"]


def test_js_default_alert_rank_matches_the_servers_critical_rank():
    """The client's fallback constant must agree with the server's SEVERITY,
    or a payload missing alert_rank (an old cached response, a stripped-down
    test double) would open/close groups at the wrong threshold."""
    from mu2edaq_diskwatcher.thresholds import SEVERITY
    src = (STATIC / "diskwatcher.js").read_text()
    assert f"DEFAULT_ALERT_RANK = {SEVERITY['CRITICAL']};" in src


@pytest.mark.parametrize("query, expected", [
    ("kind=file", 3),
    ("kind=directory", 2),
    ("monitored=space", 1),
    ("monitored=size", 2),
    ("state=EMPTY", 1),
    ("state=missing", 1),
    ("kind=file&monitored=size", 2),
])
def test_entries_filtering(client, populated, query, expected):
    assert client.get(f"/api/entries?{query}").get_json()["total"] == expected


def test_config_endpoint_reports_settings_and_entries(client, populated):
    payload = client.get("/api/config").get_json()
    assert payload["poll_interval"] == 30
    assert len(payload["entries"]) == 5
    assert "issues" in payload
    sized = [e for e in payload["entries"] if e["size"]]
    assert sized[0]["size"]["warning"] == "1MiB"


def test_health_is_ok_after_a_fresh_poll(client, populated):
    payload = client.get("/api/health").get_json()
    assert payload["status"] == "ok"
    assert payload["entries"] == 5
    assert payload["poll_age_s"] is not None


def test_health_is_degraded_when_the_config_has_problems(client, populated):
    populated.config_issues = ["something is wrong"]
    assert client.get("/api/health").get_json()["status"] == "degraded"


def test_health_is_degraded_when_the_poller_has_stalled(client, populated):
    populated.poll_interval = 1
    STORE._last_poll = time.time() - 600
    assert client.get("/api/health").get_json()["status"] == "degraded"


def test_version(client):
    payload = client.get("/api/version").get_json()
    assert payload["name"] == "mu2edaq-diskwatcher"
    assert payload["version"].count(".") == 2


def test_endpoints_work_with_an_empty_store(client):
    # A fresh process with no config must not 500 anywhere.
    for name in JSON_ENDPOINTS:
        assert client.get(f"/api/{name}").status_code == 200
        assert client.get(f"/api/{name}?peers=1").status_code == 200
    for path in PAGES:
        assert client.get(path).status_code == 200


# ---- peers ---------------------------------------------------------------
@pytest.mark.parametrize("name", PEER_AWARE)
def test_default_payload_is_local_only(client, federated, name):
    """Existing consumers -- and a peer fetching *us* -- see no peer data
    unless they ask.  This is what keeps federation to one hop."""
    payload = client.get(f"/api/{name}").get_json()
    assert "peers" not in payload and "aggregate" not in payload


@pytest.mark.parametrize("name", PEER_AWARE)
def test_peers_flag_adds_peers_and_aggregate(client, federated, name):
    payload = client.get(f"/api/{name}?peers=1").get_json()
    assert [p["label"] for p in payload["peers"]] == ["up", "down"]
    assert payload["aggregate"]["peers"] == {"configured": 2, "ok": 1, "error": 1,
                                             "pending": 0, "disabled": 0}
    # Local keys are untouched by the flag.
    plain = client.get(f"/api/{name}").get_json()
    for key in plain:
        if key not in ("generated", "poll_age_s"):
            assert payload[key] == plain[key], key


def test_status_legacy_schema_survives_peers_flag(client, federated):
    payload = client.get("/api/status?peers=1").get_json()
    assert {"files", "total", "stale", "ok", "poll_interval"} <= set(payload)
    assert payload["total"] == 5                       # local only
    for entry in payload["files"]:
        assert LEGACY_ENTRY_KEYS <= set(entry)
        assert "peer" not in entry                     # local entries unstamped


def test_status_ships_the_watch_alert_rank(client, populated):
    from mu2edaq_diskwatcher.thresholds import WATCH_SEVERITY
    assert client.get("/api/status").get_json()["alert_rank"] == WATCH_SEVERITY["stale"]


def test_space_peers_are_filtered_and_summarised_like_local(client, federated):
    payload = client.get("/api/space?peers=1").get_json()
    for peer in payload["peers"]:
        assert len(peer["entries"]) == 1                # of the five, one has space:
        assert all(e["space_monitored"] for e in peer["entries"])
        assert all(e["peer"] == peer["label"] for e in peer["entries"])
        assert peer["total"] == 1
        assert peer["summary"]["total"] == 1


def test_sizes_peers_are_filtered_like_local(client, federated):
    payload = client.get("/api/sizes?peers=1").get_json()
    assert [len(p["entries"]) for p in payload["peers"]] == [2, 2]


def test_aggregate_counts_local_plus_reachable_peers_only(client, federated):
    """The unreachable peer's retained rows are shown but never counted."""
    payload = client.get("/api/space?peers=1").get_json()
    assert payload["summary"]["total"] == 1             # local
    assert payload["aggregate"]["total"] == 2           # local + "up", not "down"
    assert payload["aggregate"]["local"] == 1

    payload = client.get("/api/status?peers=1").get_json()
    assert payload["aggregate"]["total"] == 10
    assert payload["aggregate"]["missing"] == 2


def test_unreachable_peer_record_is_marked_stale(client, federated):
    payload = client.get("/api/state?peers=1").get_json()
    down = next(p for p in payload["peers"] if p["label"] == "down")
    assert down["status"] == "error" and down["ok"] is False
    assert down["error"] == "connection refused"
    assert down["stale"] is True and down["total"] == 5
    assert down["last_ok_age_s"] >= 600
    up = next(p for p in payload["peers"] if p["label"] == "up")
    assert up["stale"] is False
    assert set(up["summary"]) == {"watch", "space", "size"}


def test_state_identifies_this_instance(client, populated):
    from mu2edaq_diskwatcher import INSTANCE_ID
    payload = client.get("/api/state").get_json()
    assert payload["instance_id"] == INSTANCE_ID
    assert payload["hostname"]


def test_entries_fold_in_reachable_peers_on_request(client, federated):
    assert client.get("/api/entries").get_json()["total"] == 5
    payload = client.get("/api/entries?peers=1").get_json()
    assert payload["total"] == 10                       # "down" excluded
    assert sum(1 for e in payload["entries"] if e.get("peer") == "up") == 5
    assert client.get("/api/entries?peers=1&peer=up&kind=file").get_json()["total"] == 3
    assert client.get("/api/entries?peers=1&monitored=space").get_json()["total"] == 2


def test_peers_endpoint_reports_status_without_entries(client, federated):
    payload = client.get("/api/peers").get_json()
    assert payload["counts"]["configured"] == 2
    assert payload["peer_interval"] == 30 and payload["peer_timeout"] == 5.0
    for peer in payload["peers"]:
        assert "entries" not in peer
        assert set(peer["summary"]) == {"watch", "space", "size"}
        assert peer["summary"]["watch"]["total"] == 5


def test_health_and_config_carry_peer_information(client, federated):
    health = client.get("/api/health").get_json()
    assert health["peers"]["error"] == 1
    assert health["status"] == "ok"                     # a dead peer is theirs
    config = client.get("/api/config").get_json()
    assert [p["url"] for p in config["peers"]] == ["http://up:5002", "http://down:5002"]
    assert config["peer_timeout"] == 5.0


def test_config_page_lists_peers(client, federated):
    body = client.get("/config").get_data(as_text=True)
    assert "http://up:5002" in body and "connection refused" in body
    assert "unreachable" in body and "connected" in body


def test_config_page_shows_the_node_and_run_directory(client, populated):
    from mu2edaq_diskwatcher.settings import short_hostname
    populated.run_dir = "run/some-node"
    body = client.get("/config").get_data(as_text=True)
    assert "run/some-node" in body
    assert f"<code>{short_hostname()}</code>" in body


def test_about_page_counts_peers(client, federated):
    body = client.get("/about").get_data(as_text=True)
    assert "1 connected, 1 unreachable" in body


@pytest.mark.parametrize("page, call, url", [
    ("index.html", "groupedRows(table,",   "/api/status?peers=1"),   # loops over both tables
    ("space.html", "groupedRows('space'", "/api/space?peers=1"),
    ("sizes.html", "groupedRows('sizes'", "/api/sizes?peers=1"),
])
def test_dashboards_request_peers_and_pass_them_to_the_grouper(page, call, url):
    src = (TEMPLATES / page).read_text()
    assert f"startDashboard('{url}'" in src
    assert call in src
    assert "peers: " in src                     # in the groupedRows opts
    assert "renderPeerStrip(" in src
    assert "data.aggregate" in src              # cards count peers too
    assert "peer_strip()" in src


def test_shared_js_defines_the_peer_helpers():
    src = (STATIC / "diskwatcher.js").read_text()
    for fn in ("peerHeaderRow", "isPeerCollapsed", "peerNeedsAttention",
               "renderPeerStrip", "peerKey", "sortPeers", "countLabel"):
        assert f"function {fn}(" in src, fn
    # A peer group's collapse memory shares the host-group store, under its
    # own prefix, so a host and a peer with the same name cannot collide.
    assert "PEER_KEY_PREFIX = 'peer:'" in src
    # Retained rows are dimmed, never passed off as current.
    assert "peer-stale" in src


def test_watcher_page_now_groups_by_host_too():
    src = (TEMPLATES / "index.html").read_text()
    assert "if (!entries.length) return null;" not in src
