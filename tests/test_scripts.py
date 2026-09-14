"""Start/stop script process handling.

These drive the real scripts against real processes, using `sleep` as a stand-in
daemon where a live diskwatcher is not needed.  The safety property that matters
most is the negative one: a stale pid file naming a *recycled* pid must never
cause an unrelated process to be signalled.
"""

import os
import pathlib
import shutil
import signal
import socket
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
START = REPO / "start-mu2edaq-diskwatcher.sh"
STOP = REPO / "stop-mu2edaq-diskwatcher.sh"
LIB = REPO / "lib" / "diskwatcher-proc.sh"

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not shutil.which("bash"),
    reason="POSIX shell scripts",
)


def sh(script, *argv, env=None, cwd=REPO):
    return subprocess.run(
        ["bash", str(script), *argv], capture_output=True, text=True,
        timeout=60, cwd=str(cwd), env=dict(os.environ, **(env or {})),
    )


@pytest.fixture
def pid_file(tmp_path):
    """A pid file path outside the repo, so a real daemon is never disturbed."""
    return tmp_path / "test.pid"


@pytest.fixture
def victim():
    """A live process that is NOT a diskwatcher. Reaped however the test ends."""
    proc = subprocess.Popen(["sleep", "300"])
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


# ---- syntax -----------------------------------------------------------
@pytest.mark.parametrize("script", [START, STOP, LIB])
def test_scripts_parse(script):
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def test_helper_library_exists():
    """Both scripts source it; a missing file breaks start AND stop at once."""
    assert LIB.is_file()


# ---- the pid-recycling hazard -----------------------------------------
def test_stale_pid_naming_a_live_stranger_is_not_killed(victim, pid_file):
    """The bug this guards against was observed for real during development.

    A pid file left by a SIGKILLed daemon eventually names some unrelated
    process. `kill -0` says it exists, so a pid-file-only check would SIGTERM
    a stranger. The command line must be checked too.
    """
    pid_file.write_text(str(victim.pid))
    result = sh(STOP, str(pid_file))

    assert result.returncode == 0, result.stderr
    time.sleep(1)
    assert victim.poll() is None, "an unrelated process was signalled"
    assert not pid_file.exists(), "stale pid file should be cleaned up"


def test_python_run_from_a_path_naming_the_app_is_not_a_diskwatcher(tmp_path, pid_file):
    """The interpreter path must not count as the command line.

    The checkout is called mu2edaq-diskwatcher, so a `python` binary reached
    through it -- a venv, a worktree's sibling venv -- puts the application
    name into every process it runs.  Before this check the test suite itself,
    run as ../mu2edaq-diskwatcher/venv/bin/python -m pytest, was identified as
    the daemon and SIGTERMed by the stop script it was testing.
    """
    fake_bin = tmp_path / "mu2edaq-diskwatcher" / "venv" / "bin"
    fake_bin.mkdir(parents=True)
    interpreter = fake_bin / "python"
    interpreter.symlink_to(shutil.which("python3"))
    proc = subprocess.Popen([str(interpreter), "-c", "import time; time.sleep(300)"],
                            cwd=str(REPO))
    try:
        pid_file.write_text(str(proc.pid))
        result = sh(STOP, str(pid_file))
        assert result.returncode == 0, result.stderr
        time.sleep(1)
        assert proc.poll() is None, "a python process was mistaken for the daemon"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_the_real_daemon_command_lines_are_still_recognised(tmp_path, pid_file):
    """Tightening the matcher must not lose the three genuine spellings."""
    script = tmp_path / "diskwatcher.py"
    script.write_text("import time\ntime.sleep(300)\n")
    proc = subprocess.Popen(["python3", str(script)], cwd=str(REPO))
    try:
        pid_file.write_text(str(proc.pid))
        result = sh(STOP, str(pid_file), env={"CRS_STOP_TIMEOUT": "5"})
        assert result.returncode == 0, result.stderr
        proc.wait(timeout=15)
        assert proc.returncode is not None, "a real diskwatcher command line was not stopped"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_stop_reports_nothing_running_when_there_is_no_pid_file(pid_file):
    result = sh(STOP, str(pid_file))
    assert result.returncode == 0
    assert "not running" in result.stdout


def test_stop_tolerates_a_garbage_pid_file(pid_file):
    pid_file.write_text("not-a-number\n")
    result = sh(STOP, str(pid_file))
    assert result.returncode == 0, result.stderr
    assert "not running" in result.stdout


# ---- per-node run directory ---------------------------------------------
# The checkout is shared over NFS between DAQ nodes. The pid file must be
# keyed by node or a start on one machine reads the pid another wrote.
def _node():
    return subprocess.run(["bash", "-c", f'source "{LIB}"; dw_node'],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_dw_node_is_the_short_hostname():
    node = _node()
    assert node and "." not in node
    assert node == socket.gethostname().split(".")[0]


def test_start_defaults_the_pid_file_into_the_node_run_dir(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("watcher: {}\n")
    result = sh(START, str(cfg), env={"DW_DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr
    expected = REPO / "run" / _node()
    assert f"run dir: {expected}" in result.stdout, result.stdout
    assert f"pid file: {expected / 'mu2edaq-diskwatcher.pid'}" in result.stdout


@pytest.mark.parametrize("how", ["flag", "env"])
def test_run_dir_can_be_overridden(tmp_path, how):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("watcher: {}\n")
    run_dir = tmp_path / "elsewhere"
    if how == "flag":
        result = sh(START, str(cfg), "--run-dir", str(run_dir), env={"DW_DRY_RUN": "1"})
    else:
        result = sh(START, str(cfg), env={"DW_DRY_RUN": "1", "DW_RUN_DIR": str(run_dir)})
    assert result.returncode == 0, result.stderr
    assert f"pid file: {run_dir / 'mu2edaq-diskwatcher.pid'}" in result.stdout


def test_explicit_pid_file_still_wins_over_the_run_dir(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("watcher: {}\n")
    result = sh(START, str(cfg), "--run-dir", str(tmp_path / "rd"),
                "--pid-file", str(tmp_path / "mine.pid"), env={"DW_DRY_RUN": "1"})
    assert f"pid file: {tmp_path / 'mine.pid'}" in result.stdout


def test_stop_reads_the_pre_1_3_pid_file_too(tmp_path):
    """Upgrading over a daemon started by an older release must still stop it.

    The legacy location is inside the repo, so this only runs when no real
    file is there; it never removes one it did not create.
    """
    legacy = REPO / "diskwatcher.pid"
    if legacy.exists():
        pytest.skip("a real legacy pid file is present")
    fake = subprocess.Popen(["python3", "-c",
                             "import time; time.sleep(300)  # diskwatcher.py"])
    try:
        legacy.write_text(str(fake.pid))
        result = sh(STOP, str(tmp_path / "unused.pid"), env={"CRS_STOP_TIMEOUT": "5"})
        assert result.returncode == 0, result.stderr
        fake.wait(timeout=15)
        assert not legacy.exists()
    finally:
        if fake.poll() is None:
            fake.kill()
        fake.wait(timeout=10)
        if legacy.exists():
            legacy.unlink()


def test_legacy_script_names_are_gone():
    """start_diskwatcher.sh / stop_diskwatcher.sh were removed in 1.3.0; a
    stray copy reappearing would drift from the real scripts again."""
    for name in ("start_diskwatcher.sh", "stop_diskwatcher.sh"):
        assert not (REPO / name).exists(), f"{name} should not exist"


# ---- fleet script -----------------------------------------------------------
# Never touches ssh in tests: everything below is --dry-run or `list`.
FLEET = REPO / "tools" / "diskwatcher-fleet.sh"


def fleet(*argv):
    return subprocess.run(["bash", str(FLEET), *argv], capture_output=True,
                          text=True, timeout=60, cwd=str(REPO))


def test_fleet_script_parses():
    assert subprocess.run(["bash", "-n", str(FLEET)]).returncode == 0


def test_fleet_list_covers_every_node_config_plus_dl_01():
    result = fleet("list")
    assert result.returncode == 0, result.stderr
    rows = dict(line.split(None, 1) for line in result.stdout.splitlines())
    assert len(rows) == 29, sorted(rows)
    assert rows["mu2e-dl-01"] == "config/mu2e-diskwatcher-dl-01.yaml"
    assert rows["mu2e-mgr-01"] == "config/mu2e-diskwatcher-mgr-01.yaml"   # the aggregator
    assert rows["mu2e-trk-14"] == "config/nodes/mu2e-diskwatcher-trk-14.yaml"
    assert rows["mu2e-calo-01"] == "config/nodes/mu2e-diskwatcher-calo-01.yaml"
    assert rows["mu2egateway01"] == "config/nodes/mu2e-diskwatcher-mu2egateway01.yaml"
    assert "(no config file)" not in result.stdout


def test_fleet_dry_run_start_builds_the_right_remote_command():
    result = fleet("-n", "-u", "mu2eshift", "-d", "/home/mu2eshift/mu2edaq-diskwatcher",
                   "-p", "5010", "mu2e-trk-03.fnal.gov")
    assert result.returncode == 0, result.stderr
    (line,) = [l for l in result.stdout.splitlines() if l.startswith("mu2e-trk-03")]
    assert "DRY ssh mu2eshift@mu2e-trk-03.fnal.gov" in line
    assert "cd /home/mu2eshift/mu2edaq-diskwatcher" in line
    assert ("CRS_PORT_HTTP=5010 ./start-mu2edaq-diskwatcher.sh -c "
            "config/nodes/mu2e-diskwatcher-trk-03.yaml") in line
    # Verification: a wait loop, then a final check whose exit status decides,
    # because a bash `for` loop's own status is that of its last `sleep`.
    assert line.count("localhost:5010/api/health") == 2
    assert line.rstrip().endswith("curl -sf --max-time 3 localhost:5010/api/health")
    assert "summary: 1 ok, 0 failed/unreachable, 0 skipped" in result.stdout


def test_fleet_dry_run_stop_and_status_and_flags():
    jump = fleet("status", "-n", "-J", "mu2egateway01.fnal.gov", "mu2e-crv-01", "mu2egateway01")
    assert "DRY ssh -J mu2egateway01.fnal.gov mu2e-crv-01.fnal.gov" in jump.stdout
    # The jump host is a node too; ssh refuses to jump through it to itself.
    assert "DRY ssh mu2egateway01.fnal.gov curl" in jump.stdout
    stop = fleet("stop", "-n", "mu2e-crv-01")
    assert "./stop-mu2edaq-diskwatcher.sh" in stop.stdout and "start-" not in stop.stdout
    status = fleet("status", "-n", "mu2e-crv-01")
    assert "curl -sf --max-time 5 localhost:5002/api/health" in status.stdout
    no_verify = fleet("-n", "--no-verify", "--no-replace", "mu2e-crv-01")
    assert "/api/health" not in no_verify.stdout and "--no-replace" in no_verify.stdout


def test_fleet_exclude_and_unknown_node():
    result = fleet("list", "-x", "mu2e-dl-01", "-x", "mu2e-dl-02.fnal.gov")
    assert "mu2e-dl-01" not in result.stdout and "mu2e-dl-02" not in result.stdout
    assert len(result.stdout.splitlines()) == 27
    result = fleet("-n", "mu2e-nope-99")
    assert result.returncode == 0
    assert "SKIP no config file for mu2e-nope-99" in result.stdout
    assert "1 skipped" in result.stdout


def test_fleet_rejects_bad_usage():
    assert fleet("--bogus").returncode == 1
    assert fleet("-j", "0", "list").returncode == 1
    assert fleet("list", "-x", "mu2e-dl-01", "mu2e-dl-01").returncode == 1   # nothing left


# ---- replace-on-start --------------------------------------------------
def test_start_reports_no_running_copy_when_idle(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("watcher: {}\n")
    result = sh(START, str(cfg), "--pid-file", str(tmp_path / "x.pid"),
                env={"DW_DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr
    assert "Found running" not in result.stdout


def test_start_announces_that_it_will_replace_a_running_copy(tmp_path, pid_file):
    """A dry run stops short of killing, so a fake pid file is enough here."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text("watcher: {}\n")

    # A live process whose command line does look like the app.
    fake = subprocess.Popen(["python3", "-c",
                             "import time; time.sleep(300)  # diskwatcher.py"])
    try:
        pid_file.write_text(str(fake.pid))
        result = sh(START, str(cfg), "--pid-file", str(pid_file),
                    env={"DW_DRY_RUN": "1"})
        assert result.returncode == 0, result.stderr
        assert "Found running" in result.stdout
        assert str(fake.pid) in result.stdout
        # DW_DRY_RUN must not actually kill anything.
        assert fake.poll() is None
    finally:
        fake.kill()
        fake.wait(timeout=10)


def test_no_replace_refuses_while_a_copy_is_running(tmp_path, pid_file):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("watcher: {}\n")
    fake = subprocess.Popen(["python3", "-c",
                             "import time; time.sleep(300)  # diskwatcher.py"])
    try:
        pid_file.write_text(str(fake.pid))
        result = sh(START, str(cfg), "--pid-file", str(pid_file),
                    "--no-replace", env={"DW_DRY_RUN": "1"})
        assert result.returncode == 1
        assert "already running" in result.stderr
        assert fake.poll() is None
    finally:
        fake.kill()
        fake.wait(timeout=10)


def test_no_replace_starts_normally_when_nothing_is_running(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("watcher: {}\n")
    result = sh(START, str(cfg), "--pid-file", str(tmp_path / "x.pid"),
                "--no-replace", env={"DW_DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr


# ---- kill escalation ---------------------------------------------------
def test_kill_escalates_to_sigkill_for_a_process_ignoring_sigterm(pid_file):
    """CRS_STOP_TIMEOUT bounds how long a wedged daemon blocks a restart."""
    ignorer = subprocess.Popen(
        ["python3", "-c",
         "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
         " time.sleep(300)  # diskwatcher.py"])
    try:
        pid_file.write_text(str(ignorer.pid))
        result = sh(STOP, str(pid_file), env={"CRS_STOP_TIMEOUT": "2"})
        assert result.returncode == 0, result.stderr
        assert "SIGKILL" in result.stdout
        ignorer.wait(timeout=10)
        assert ignorer.returncode in (-signal.SIGKILL, 137)
    finally:
        if ignorer.poll() is None:
            ignorer.kill()
            ignorer.wait(timeout=10)
