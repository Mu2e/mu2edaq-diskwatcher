import time

import pytest
import yaml

from mu2edaq_diskwatcher import poller
from mu2edaq_diskwatcher.config import entries_from_config
from mu2edaq_diskwatcher.poller import STATE_KEYS, do_poll, poll_entry
from mu2edaq_diskwatcher.ssh import RemoteProbe
from mu2edaq_diskwatcher.state import STORE

def entry(text):
    entries, _ = entries_from_config(yaml.safe_load(text))
    return entries[0]


def poll(text, now=None):
    # `now` defaults to call time, not import time: the fixture files are
    # created after this module loads, so a fixed timestamp would make every
    # age negative.
    return poll_entry(entry(text), time.time() if now is None else now)


# --------------------------------------------------------------- key parity
# The old code patched the error branch by hand, so a key added to only one
# path silently became `undefined` in the browser.  Both paths must agree.

def test_key_parity_between_success_and_failure(tmp_tree):
    good = poll(f"files:\n  - path: {tmp_tree['big']}\n    delay: 60\n")
    bad  = poll(f"files:\n  - path: {tmp_tree['missing']}\n    delay: 60\n")
    assert set(good) == set(bad) == set(STATE_KEYS)


def test_key_parity_with_every_feature_enabled(tmp_tree):
    sized = poll(f"files:\n  - path: {tmp_tree['big']}\n    size:\n      warning: 1MiB\n")
    spaced = poll(f"paths:\n  - path: {tmp_tree['root']}\n    space:\n      warning: 1GiB\n")
    assert set(sized) == set(spaced) == set(STATE_KEYS)


# ------------------------------------------------------------ watch states
def test_fresh_file_is_ok(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['big']}\n    delay: 3600\n")
    assert (state["watch_state"], state["stale"], state["missing"]) == ("ok", False, False)


def test_old_file_is_stale(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['big']}\n    delay: 0\n")
    assert state["watch_state"] == "stale"
    assert state["stale"] is True


def test_missing_file(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['missing']}\n    delay: 60\n")
    assert state["watch_state"] == "missing"
    assert state["missing"] is True
    assert state["stale"] is True          # historical behaviour, preserved
    assert state["error"]


def test_entry_without_delay_is_unmonitored(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['big']}\n    size:\n      warning: 1B\n")
    assert state["watch_state"] == "unmonitored"
    assert state["stale"] is False
    assert state["delay"] is None


def test_missing_path_without_delay_is_not_stale(tmp_tree):
    # No staleness threshold configured => no staleness alarm, even absent.
    state = poll(f"files:\n  - path: {tmp_tree['missing']}\n    size:\n      warning: 1B\n")
    assert state["missing"] is True
    assert state["stale"] is False
    assert state["watch_state"] == "missing"


# -------------------------------------------------------------- file sizes
def test_size_is_reported_for_files(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['big']}\n    delay: 60\n")
    assert state["size"] == 5 * 1024 ** 2
    assert "MiB" in state["size_str"]


def test_directories_report_no_size(tmp_tree):
    # A directory's st_size is an allocation figure, not a data size.
    state = poll(f"paths:\n  - path: {tmp_tree['root']}\n    delay: 60\n")
    assert state["size"] is None


def test_empty_file_is_flagged(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['empty']}\n    size:\n      warning: 1MiB\n")
    assert state["size_state"] == "EMPTY"
    assert state["size_trigger"] == "empty"
    assert state["size"] == 0


def test_allow_empty_suppresses_the_alarm(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['empty']}\n    size:\n"
                 f"      allow_empty: true\n      warning: 1MiB\n")
    assert state["size_state"] == "GOOD"


def test_size_thresholds_fire(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['big']}\n    size:\n"
                 f"      warning: 1MiB\n      critical: 4MiB\n      full: 100MiB\n")
    assert state["size_state"] == "CRITICAL"
    assert state["size_trigger"] == "critical"
    assert state["size_rank"] > 0
    assert state["size_limits"]["critical"] == 4 * 1024 ** 2
    assert state["size_limits_str"]["critical"] == "4MiB"


def test_size_pct_is_measured_against_the_largest_limit(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['big']}\n    size:\n"
                 f"      warning: 1MiB\n      full: 10MiB\n")
    assert state["size_pct_ref"] == "full"
    assert state["size_pct"] == 50.0


def test_unmonitored_file_has_null_size_state(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['big']}\n    delay: 60\n")
    assert state["size_monitored"] is False
    assert state["size_state"] is None


def test_missing_monitored_file_reports_missing(tmp_tree):
    state = poll(f"files:\n  - path: {tmp_tree['missing']}\n    size:\n      warning: 1MiB\n")
    assert state["size_state"] == "MISSING"


# ------------------------------------------------------------- disk space
def test_disk_usage_is_collected_for_directories(tmp_tree):
    state = poll(f"paths:\n  - path: {tmp_tree['root']}\n    delay: 60\n")
    assert state["disk_total"] > 0
    assert state["disk_free"] >= 0
    assert 0 <= state["disk_pct"] <= 100
    assert 0 <= state["disk_free_pct"] <= 100
    # disk_pct is *used*, disk_free_pct is *free* — they must not be the same.
    assert state["disk_used_str"] and state["disk_free_str"]


def test_space_state_good_on_a_generous_threshold(tmp_tree):
    state = poll(f"paths:\n  - path: {tmp_tree['root']}\n    space:\n      warning: 1B\n")
    assert state["space_state"] == "GOOD"
    assert state["space_monitored"] is True


def test_space_state_full_on_an_impossible_threshold(tmp_tree):
    state = poll(f"paths:\n  - path: {tmp_tree['root']}\n    space:\n      full: 100PiB\n")
    assert state["space_state"] == "FULL"
    assert state["space_trigger"] == "full"


def test_percent_thresholds_resolve_against_capacity(tmp_tree):
    state = poll(f"paths:\n  - path: {tmp_tree['root']}\n    space:\n      warning: 50%\n")
    assert state["space_limits"]["warning"] == int(round(state["disk_total"] * 0.5))
    assert state["space_limits_str"]["warning"] == "50%"


def test_mixed_ordering_is_checked_at_poll_time(tmp_tree):
    # 1% of any real filesystem is far below 100 PiB, so these resolve out of
    # order — something only measurable once the capacity is known.
    state = poll(f"paths:\n  - path: {tmp_tree['root']}\n    space:\n"
                 f"      warning: 1%\n      critical: 100PiB\n")
    assert state["space_limits_ordered"] is False


def test_unmonitored_directory_has_null_space_state(tmp_tree):
    state = poll(f"paths:\n  - path: {tmp_tree['root']}\n    delay: 60\n")
    assert state["space_monitored"] is False
    assert state["space_state"] is None


# ------------------------------------------------------------------ remote
def test_remote_success(tmp_tree, monkeypatch):
    monkeypatch.setattr(poller, "remote_probe",
                        lambda *a, **k: RemoteProbe(time.time() - 10, 2048, 1000, 400, 600, None))
    state = poll("paths:\n  - path: /remote\n    delay: 3600\n"
                 "    ssh:\n      host: node01\n"
                 "    space:\n      warning: 500\n")
    assert state["remote"] is True
    assert state["ssh_host"] == "node01"
    assert state["disk_free"] == 600
    assert state["space_state"] == "GOOD"      # 600 free > 500 warning
    assert state["watch_state"] == "ok"


def test_remote_error_is_missing(monkeypatch):
    monkeypatch.setattr(poller, "remote_probe",
                        lambda *a, **k: RemoteProbe(None, None, None, None, None,
                                                    "ssh: connect failed"))
    state = poll("paths:\n  - path: /remote\n    delay: 60\n"
                 "    ssh:\n      host: node01\n"
                 "    space:\n      warning: 500\n")
    assert state["missing"] is True
    assert state["space_state"] == "MISSING"
    assert "connect failed" in state["error"]


def test_remote_without_disk_usage_is_unknown_not_missing(monkeypatch):
    # The path exists, but the filesystem could not be measured — a different
    # problem from the path being gone, and the states must reflect that.
    monkeypatch.setattr(poller, "remote_probe",
                        lambda *a, **k: RemoteProbe(time.time(), 0, None, None, None, None))
    state = poll("paths:\n  - path: /remote\n    delay: 3600\n"
                 "    ssh:\n      host: node01\n"
                 "    space:\n      warning: 500\n")
    assert state["missing"] is False
    assert state["space_state"] == "UNKNOWN"


# -------------------------------------------------------------- do_poll
def test_do_poll_publishes_to_the_store(tmp_tree, settings):
    settings.entries, _ = entries_from_config(yaml.safe_load(
        f"files:\n  - path: {tmp_tree['big']}\n    delay: 60\n"
        f"paths:\n  - path: {tmp_tree['root']}\n    delay: 60\n"))
    do_poll()
    snapshot = STORE.snapshot()
    assert len(snapshot) == 2
    assert STORE.meta()["last_poll"] is not None
    assert STORE.meta()["poll_duration_s"] is not None


def test_do_poll_preserves_config_order(tmp_tree, settings):
    settings.entries, _ = entries_from_config(yaml.safe_load(
        f"files:\n  - path: {tmp_tree['big']}\n  - path: {tmp_tree['empty']}\n"
        f"  - path: {tmp_tree['missing']}\n"))
    do_poll()
    assert [e["path"] for e in STORE.snapshot()] == \
           [str(tmp_tree['big']), str(tmp_tree['empty']), str(tmp_tree['missing'])]


def test_do_poll_with_no_entries(settings):
    settings.entries = []
    do_poll()
    assert STORE.snapshot() == []
