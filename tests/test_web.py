import time

import pytest
import yaml

from mu2edaq_diskwatcher.config import entries_from_config
from mu2edaq_diskwatcher.poller import poll_entry
from mu2edaq_diskwatcher.state import STORE
from mu2edaq_diskwatcher.web.api import LEGACY_ENTRY_KEYS

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
