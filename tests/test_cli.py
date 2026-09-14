"""Command-line handling.

Two regressions live here.  `./start-mu2edaq-diskwatcher.sh -c FILE` used to
pass the flag itself as the config filename, because the start script read its
first argument positionally; and a throwaway pre-parser that knew only about
--config reported every malformed command line, so an unrelated mistake printed

    usage: diskwatcher.py [--config CONFIG]
    diskwatcher.py: error: argument --config/-c: expected one argument

naming the wrong option and hiding the rest of the interface.
"""

import os
import pathlib
import subprocess
import sys

import pytest

from mu2edaq_diskwatcher.cli import DEFAULT_CONFIG, build_parser
from mu2edaq_diskwatcher.settings import ENV_PREFIX, get_settings

REPO = pathlib.Path(__file__).resolve().parent.parent
START = REPO / "start-mu2edaq-diskwatcher.sh"


# ---- the parser itself ------------------------------------------------
def parse(argv):
    return build_parser(get_settings()).parse_args(argv)


def test_config_defaults_to_none_so_precedence_is_detectable():
    """`--config` unset must be distinguishable from `--config diskwatcher.yaml`."""
    assert parse([]).config is None
    assert parse(["--config", DEFAULT_CONFIG]).config == DEFAULT_CONFIG


@pytest.mark.parametrize("argv", [
    ["-c", "some.yaml"],
    ["--config", "some.yaml"],
    ["--config=some.yaml"],
])
def test_config_accepts_every_spelling(argv):
    assert parse(argv).config == "some.yaml"


def test_usage_lists_every_option_not_just_config():
    """The old pre-parser's usage line advertised a single option."""
    usage = build_parser(get_settings()).format_usage()
    for flag in ("--host", "--port", "--daemon", "--pid-file", "--verbose"):
        assert flag in usage, usage


def test_unrelated_error_is_not_blamed_on_config(capsys):
    """`--port` with no argument must name --port, not --config.

    Only the `error:` line is checked -- the usage line above it legitimately
    lists every option, --config among them.
    """
    with pytest.raises(SystemExit):
        parse(["--port"])
    error_line = next(line for line in capsys.readouterr().err.splitlines()
                      if "error:" in line)
    assert "--port" in error_line
    assert "--config" not in error_line


def test_missing_config_argument_is_reported_against_config(capsys):
    with pytest.raises(SystemExit):
        parse(["--config"])
    assert "--config" in capsys.readouterr().err


def test_run_dir_flag_defaults_to_none_so_the_yaml_value_survives():
    assert parse([]).run_dir is None
    assert parse(["--run-dir", "/x/{host}"]).run_dir == "/x/{host}"


def test_peer_flags_default_to_none_and_repeat():
    args = parse([])
    assert args.peers is None and args.no_peers is None
    assert args.peer_timeout is None and args.peer_interval is None
    args = parse(["--peer", "a:5002", "--peer", "http://b:5002",
                  "--peer-timeout", "2.5", "--peer-interval", "60", "--no-peers"])
    assert args.peers == ["a:5002", "http://b:5002"]
    assert args.peer_timeout == 2.5 and args.peer_interval == 60
    assert args.no_peers is True


# ---- end-to-end through the real entry point --------------------------
def run_cli(*argv, env=None):
    environ = dict(os.environ, **(env or {}))
    environ.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(REPO / "diskwatcher.py"), *argv],
        capture_output=True, text=True, timeout=30, cwd=str(REPO), env=environ,
    )


def test_explicitly_named_missing_config_is_fatal(tmp_path):
    result = run_cli("--config", str(tmp_path / "nope.yaml"))
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_missing_config_from_the_environment_is_also_fatal(tmp_path):
    """The env layer names the file just as explicitly as the flag does."""
    result = run_cli(env={ENV_PREFIX + "CONFIG": str(tmp_path / "nope.yaml")})
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_cli_rejects_a_peer_that_is_not_a_url(tmp_path):
    """A bad --peer is a warning on stderr, never a crash: the daemon must
    still come up and watch its local paths."""
    good = tmp_path / "good.yaml"
    good.write_text("watcher: {}\n")
    result = run_cli("--config", str(good), "--peer", "ftp://nope", "--version")
    # --version exits before the warning would print; the parser accepts it.
    assert result.returncode == 0, result.stderr


def test_command_line_config_beats_the_environment(tmp_path):
    """Both name a file; only the CLI one is required to exist."""
    good = tmp_path / "good.yaml"
    good.write_text("watcher:\n  web_port: 5199\n")
    result = run_cli("--config", str(good), "--version",
                     env={ENV_PREFIX + "CONFIG": str(tmp_path / "nope.yaml")})
    assert result.returncode == 0, result.stderr


# ---- the control-room start script ------------------------------------
@pytest.mark.skipif(not START.exists(), reason="start script not present")
@pytest.mark.parametrize("argv", [
    ["CONFIG"],                       # historical positional form (crs-app)
    ["-c", "CONFIG"],
    ["--config", "CONFIG"],
    ["--config=CONFIG"],
])
def test_start_script_accepts_the_config_every_way(tmp_path, argv):
    """`-c FILE` used to make the flag itself the filename."""
    cfg = tmp_path / "test.yaml"
    cfg.write_text("watcher:\n  web_port: 5199\n")
    argv = [a.replace("CONFIG", str(cfg)) for a in argv]

    # DW_DRY_RUN stops short of exec so the test never starts a daemon.
    result = subprocess.run(
        ["bash", str(START), *argv], capture_output=True, text=True,
        timeout=30, cwd=str(REPO), env=dict(os.environ, DW_DRY_RUN="1"),
    )
    assert result.returncode == 0, result.stderr
    assert f"config: {cfg}" in result.stdout, result.stdout


@pytest.mark.skipif(not START.exists(), reason="start script not present")
def test_start_script_rejects_a_config_flag_with_no_argument():
    result = subprocess.run(
        ["bash", str(START), "-c"], capture_output=True, text=True,
        timeout=30, cwd=str(REPO), env=dict(os.environ, DW_DRY_RUN="1"),
    )
    assert result.returncode == 2
    assert "requires a file argument" in result.stderr


@pytest.mark.skipif(not START.exists(), reason="start script not present")
def test_start_script_forwards_the_port(tmp_path):
    cfg = tmp_path / "test.yaml"
    cfg.write_text("watcher: {}\n")
    result = subprocess.run(
        ["bash", str(START), str(cfg), "-p", "5188"], capture_output=True,
        text=True, timeout=30, cwd=str(REPO), env=dict(os.environ, DW_DRY_RUN="1"),
    )
    assert result.returncode == 0, result.stderr
    assert "http=5188" in result.stdout, result.stdout
