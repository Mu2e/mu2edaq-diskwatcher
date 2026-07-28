import pathlib
import time

import pytest
import yaml

import mu2edaq_diskwatcher.web as _web
from mu2edaq_diskwatcher.config import entries_from_config
from mu2edaq_diskwatcher.poller import poll_entry
from mu2edaq_diskwatcher.state import STORE
from mu2edaq_diskwatcher.web.api import LEGACY_ENTRY_KEYS

# Resolved from the package rather than the repo, so these still point at the
# shipped assets when the tests run against an installed wheel.
_WEB = pathlib.Path(_web.__file__).parent
TEMPLATES = _WEB / "templates"
STATIC = _WEB / "static"

PAGES = ["/", "/space", "/sizes", "/config", "/about", "/api", "/sitemap"]
JSON_ENDPOINTS = ["status", "state", "space", "sizes", "entries",
                  "config", "health", "version"]


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
    for path in PAGES:
        assert client.get(path).status_code == 200
