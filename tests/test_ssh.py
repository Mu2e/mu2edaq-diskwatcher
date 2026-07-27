"""SSH command construction and probe parsing.  No real SSH is ever run."""

import subprocess
from collections import namedtuple

import pytest

from mu2edaq_diskwatcher import ssh
from mu2edaq_diskwatcher.ssh import build_ssh_cmd, remote_disk_usage, remote_probe, remote_stat

Completed = namedtuple("Completed", "returncode stdout stderr")


@pytest.fixture(autouse=True)
def clear_probe_mode():
    ssh._PROBE_MODE.clear()
    yield
    ssh._PROBE_MODE.clear()


def fake_run(monkeypatch, *responses):
    """Queue up responses for successive subprocess.run calls; record the argv."""
    calls = []
    queue = list(responses)

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return queue.pop(0) if queue else Completed(0, "", "")

    monkeypatch.setattr(subprocess, "run", runner)
    return calls


# ------------------------------------------------------------ command build
def test_minimal_command():
    cmd = build_ssh_cmd({"host": "node01"}, ["stat", "/a"])
    assert cmd[0] == "ssh"
    assert "BatchMode=yes" in cmd
    assert "ConnectTimeout=10" in cmd
    assert cmd[-3:] == ["node01", "stat", "/a"]


def test_port_key_and_timeout():
    cmd = build_ssh_cmd(
        {"host": "n", "port": 2222, "key": "~/.ssh/id_ed25519", "timeout": 30}, ["ls"])
    assert cmd[cmd.index("-p") + 1] == "2222"
    assert not cmd[cmd.index("-i") + 1].startswith("~")     # expanduser applied
    assert "ConnectTimeout=30" in cmd


def test_options_as_list_and_string():
    as_list = build_ssh_cmd({"host": "n", "options": ["-o", "StrictHostKeyChecking=no"]}, ["ls"])
    assert "StrictHostKeyChecking=no" in as_list
    as_string = build_ssh_cmd({"host": "n", "options": "-o StrictHostKeyChecking=no"}, ["ls"])
    assert "StrictHostKeyChecking=no" in as_string


def test_remote_arguments_are_quoted():
    cmd = build_ssh_cmd({"host": "n"}, ["stat", "/path with spaces/a;rm -rf /"])
    assert cmd[-1].startswith("'") and cmd[-1].endswith("'")


def test_remote_shell_is_passed_through_verbatim():
    # The `||` fallback needs shell syntax, which per-argument quoting destroys.
    cmd = build_ssh_cmd({"host": "n"}, remote_shell="a || b")
    assert cmd[-1] == "a || b"


# ---------------------------------------------------------------- probing
def test_probe_parses_mtime_size_and_disk(monkeypatch):
    fake_run(monkeypatch, Completed(0, "1700000000.5 2048 1000 400 600\n", ""))
    probe = remote_probe("/a", {"host": "n"}, want_disk=True)
    assert probe.error is None
    assert (probe.mtime, probe.size) == (1700000000.5, 2048)
    assert (probe.total, probe.used, probe.free) == (1000, 400, 600)


def test_disk_sentinels_become_none(monkeypatch):
    # -1 means "the path exists but disk usage failed" -> UNKNOWN, not MISSING.
    fake_run(monkeypatch, Completed(0, "1700000000.0 2048 -1 -1 -1\n", ""))
    probe = remote_probe("/a", {"host": "n"})
    assert probe.error is None
    assert probe.mtime == 1700000000.0
    assert (probe.total, probe.used, probe.free) == (None, None, None)


def test_want_disk_flag_is_forwarded(monkeypatch):
    calls = fake_run(monkeypatch, Completed(0, "1 2 3 4 5\n", ""),
                     Completed(0, "1 2 -1 -1 -1\n", ""))
    remote_probe("/a", {"host": "n"}, want_disk=True)
    remote_probe("/a", {"host": "n"}, want_disk=False)
    assert calls[0][-1] == "disk"
    assert calls[1][-1] != "disk"


def test_nonzero_exit_is_an_error(monkeypatch):
    fake_run(monkeypatch, Completed(1, "", "No such file or directory\n"))
    probe = remote_probe("/gone", {"host": "n"})
    assert probe.error == "No such file or directory"
    assert probe.mtime is None


def test_unparseable_output_is_an_error(monkeypatch):
    fake_run(monkeypatch, Completed(0, "not numbers at all\n", ""))
    assert "unparseable" in remote_probe("/a", {"host": "n"}).error


def test_timeout(monkeypatch):
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 12)
    monkeypatch.setattr(subprocess, "run", boom)
    assert "timed out" in remote_probe("/a", {"host": "n"}).error


def test_missing_ssh_binary(monkeypatch):
    def boom(cmd, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", boom)
    assert "not found in PATH" in remote_probe("/a", {"host": "n"}).error


# ---------------------------------------------------------------- fallback
def test_falls_back_to_stat_when_python3_is_missing(monkeypatch):
    calls = fake_run(monkeypatch,
                     Completed(127, "", "bash: python3: command not found"),
                     Completed(0, "1700000000 4096\n", ""))
    probe = remote_probe("/a", {"host": "oldnode"})
    assert probe.error is None
    assert (probe.mtime, probe.size) == (1700000000.0, 4096)
    assert "stat -c" in calls[1][-1] and "stat -f" in calls[1][-1]   # GNU then BSD


def test_fallback_choice_is_cached_per_host(monkeypatch):
    calls = fake_run(monkeypatch,
                     Completed(127, "", "python3: command not found"),
                     Completed(0, "1 2\n", ""),
                     Completed(0, "3 4\n", ""))
    remote_probe("/a", {"host": "oldnode"})       # 2 calls: probe + fallback
    remote_probe("/b", {"host": "oldnode"})       # 1 call: straight to stat
    assert len(calls) == 3
    assert ssh._PROBE_MODE["oldnode"] == "stat"


def test_other_hosts_are_unaffected_by_a_fallback(monkeypatch):
    fake_run(monkeypatch,
             Completed(127, "", "python3: command not found"),
             Completed(0, "1 2\n", ""),
             Completed(0, "1 2 3 4 5\n", ""))
    remote_probe("/a", {"host": "oldnode"})
    probe = remote_probe("/a", {"host": "newnode"})
    assert probe.total == 3                       # used the python3 probe
    assert "newnode" not in ssh._PROBE_MODE


def test_real_failure_does_not_trigger_the_fallback(monkeypatch):
    calls = fake_run(monkeypatch, Completed(1, "", "Permission denied"))
    probe = remote_probe("/a", {"host": "n"})
    assert probe.error == "Permission denied"
    assert len(calls) == 1                        # no pointless retry


# ------------------------------------------------------- compat wrappers
def test_remote_stat_wrapper(monkeypatch):
    fake_run(monkeypatch, Completed(0, "1700000000.0 2048 -1 -1 -1\n", ""))
    assert remote_stat("/a", {"host": "n"}) == (1700000000.0, None)


def test_remote_stat_wrapper_on_error(monkeypatch):
    fake_run(monkeypatch, Completed(1, "", "gone"))
    assert remote_stat("/a", {"host": "n"}) == (None, "gone")


def test_remote_disk_usage_wrapper(monkeypatch):
    fake_run(monkeypatch, Completed(0, "1.0 2 1000 400 600\n", ""))
    assert remote_disk_usage("/a", {"host": "n"}) == (1000, 400, 600, None)


def test_remote_disk_usage_wrapper_when_unmeasurable(monkeypatch):
    fake_run(monkeypatch, Completed(0, "1.0 2 -1 -1 -1\n", ""))
    total, used, free, err = remote_disk_usage("/a", {"host": "n"})
    assert (total, used, free) == (None, None, None)
    assert err
