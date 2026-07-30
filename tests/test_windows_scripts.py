"""Windows PowerShell launch-script coverage.

Added in the windows-compat sweep. The DAQ nodes' bash launch scripts have
PowerShell ports for Windows control-room hosts; these tests lock in the parity
(every .sh has a .ps1) and exercise the start script's argument handling through
a real PowerShell interpreter, mirroring the bash-driven cases in test_cli.py.
"""
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# Standardized control-room scripts that must ship both forms.
SCRIPT_STEMS = [
    "start-mu2edaq-diskwatcher",
    "stop-mu2edaq-diskwatcher",
]

PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _pwsh(*args, env=None):
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", *args],
        capture_output=True, text=True, timeout=60, cwd=str(REPO), env=env,
    )


def test_every_launch_script_has_both_forms():
    for stem in SCRIPT_STEMS:
        assert (REPO / f"{stem}.sh").is_file(), f"missing bash script: {stem}.sh"
        assert (REPO / f"{stem}.ps1").is_file(), f"missing PowerShell port: {stem}.ps1"
    # bootstrap and the shared proc lib too.
    assert (REPO / "bootstrap_diskwatcher.sh").is_file()
    assert (REPO / "bootstrap-diskwatcher.ps1").is_file()
    assert (REPO / "lib" / "diskwatcher-proc.sh").is_file()
    assert (REPO / "lib" / "diskwatcher-proc.ps1").is_file()


@pytest.mark.skipif(not PWSH, reason="PowerShell not available")
@pytest.mark.parametrize("script", [
    "start-mu2edaq-diskwatcher.ps1",
    "stop-mu2edaq-diskwatcher.ps1",
    "bootstrap-diskwatcher.ps1",
    "lib/diskwatcher-proc.ps1",
])
def test_powershell_scripts_parse(script):
    path = (REPO / script).as_posix()
    code = (
        "$e=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$null,[ref]$e)|Out-Null;"
        "if($e){$e|ForEach-Object{Write-Error $_};exit 1}else{exit 0}"
    )
    result = _pwsh("-Command", code)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not PWSH, reason="PowerShell not available")
def test_start_script_forwards_the_port(tmp_path):
    cfg = tmp_path / "test.yaml"
    cfg.write_text("watcher: {}\n")
    import os
    env = dict(os.environ, DW_DRY_RUN="1")
    result = _pwsh("-File", str(REPO / "start-mu2edaq-diskwatcher.ps1"),
                   str(cfg), "-Port", "5188", env=env)
    assert result.returncode == 0, result.stderr
    assert "http=5188" in result.stdout, result.stdout


@pytest.mark.skipif(not PWSH, reason="PowerShell not available")
def test_start_script_reports_missing_config(tmp_path):
    import os
    env = dict(os.environ, DW_DRY_RUN="1")
    result = _pwsh("-File", str(REPO / "start-mu2edaq-diskwatcher.ps1"),
                   str(tmp_path / "nope.yaml"), env=env)
    assert result.returncode == 1
    assert "not found" in (result.stdout + result.stderr).lower()
