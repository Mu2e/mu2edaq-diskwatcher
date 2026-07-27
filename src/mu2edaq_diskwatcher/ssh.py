"""Remote path probing over SSH.

One :func:`remote_probe` call returns mtime, size and (optionally) disk usage
in a single round trip.  Previously a remote directory cost two SSH
invocations per poll cycle — one ``stat``, one ``python3 -c`` — which on a slow
link doubled the poll time for every remote entry.

The primary probe runs a small ``python3`` snippet, so it behaves identically on
Linux, macOS and BSD remotes.  Hosts without ``python3`` fall back to ``stat``,
whose flags *are* platform-specific (GNU ``-c '%Y %s'`` vs BSD ``-f '%m %z'``);
the fallback tries both.  The choice is cached per host for the life of the
process, so the extra round trip is paid once, not once per poll.
"""

import os
import shlex
import subprocess
import sys
from collections import namedtuple
from typing import List, Optional

#: mtime/size/total/used/free are None when unavailable; error is None on success.
RemoteProbe = namedtuple(
    "RemoteProbe", "mtime size total used free error")

#: host -> "python3" | "stat".  Populated on first fallback for that host.
_PROBE_MODE = {}

_PY_PROBE = (
    "import os,shutil,sys\n"
    "p=sys.argv[1]\n"
    "s=os.stat(p)\n"
    "d=(-1,-1,-1)\n"
    "if len(sys.argv)>2:\n"
    "    try:\n"
    "        u=shutil.disk_usage(p); d=(u.total,u.used,u.free)\n"
    "    except OSError:\n"
    "        pass\n"
    "print(s.st_mtime, s.st_size, d[0], d[1], d[2])\n"
)


def build_ssh_cmd(ssh_cfg: dict, remote_argv: Optional[List[str]] = None,
                  remote_shell: Optional[str] = None) -> list:
    """Return a subprocess argument list for an SSH command.

    Exactly one of *remote_argv* or *remote_shell* must be given.  Elements of
    *remote_argv* are shell-quoted before being appended, because SSH joins the
    remote arguments into a single string and hands it to the remote shell.
    *remote_shell* is passed through verbatim, for the rare command that needs
    shell syntax such as ``||`` — the caller is responsible for quoting.
    """
    timeout = int(ssh_cfg.get("timeout", 10))
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",           # never prompt for a password
        "-o", f"ConnectTimeout={timeout}",
    ]
    if ssh_cfg.get("port"):
        cmd += ["-p", str(ssh_cfg["port"])]
    if ssh_cfg.get("key"):
        cmd += ["-i", os.path.expanduser(str(ssh_cfg["key"]))]
    extra = ssh_cfg.get("options", "")
    if isinstance(extra, list):
        cmd += extra
    elif extra:
        cmd += shlex.split(str(extra))
    cmd.append(str(ssh_cfg["host"]))
    if remote_shell is not None:
        cmd.append(remote_shell)
    else:
        cmd += [shlex.quote(a) for a in (remote_argv or [])]
    return cmd


def _run(cmd: list, timeout: int):
    """Run *cmd*, returning ``(returncode, stdout, stderr)`` or raising nothing.

    Returns ``(None, "", message)`` when the command could not be run at all.
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout + 2)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return None, "", f"SSH timed out after {timeout} s"
    except FileNotFoundError:
        return None, "", "ssh executable not found in PATH"
    except OSError as exc:
        return None, "", str(exc)


def _no_python3(returncode, stderr: str) -> bool:
    """True when the remote failed because ``python3`` is not installed."""
    if returncode == 127:
        return True
    lowered = stderr.lower()
    return "python3" in lowered and (
        "not found" in lowered or "no such file" in lowered)


def remote_probe(path: str, ssh_cfg: dict, want_disk: bool = False) -> RemoteProbe:
    """Stat *path* on a remote host, optionally including filesystem usage.

    A non-zero exit means the path could not be statted (MISSING).  Disk-usage
    sentinels of ``-1`` mean the path exists but ``shutil.disk_usage`` failed
    (UNKNOWN) — the two cases must stay distinguishable.
    """
    timeout = int(ssh_cfg.get("timeout", 10))
    host = str(ssh_cfg.get("host", ""))
    empty = RemoteProbe(None, None, None, None, None, None)

    if _PROBE_MODE.get(host) != "stat":
        argv = ["python3", "-c", _PY_PROBE, path]
        if want_disk:
            argv.append("disk")
        rc, out, err = _run(build_ssh_cmd(ssh_cfg, argv), timeout)
        if rc is None:
            return empty._replace(error=err)
        if rc == 0:
            return _parse_python_probe(out, err)
        if not _no_python3(rc, err):
            return empty._replace(
                error=err.strip() or f"remote probe exited {rc}")
        _PROBE_MODE[host] = "stat"
        print(f"[SSH] {host}: python3 unavailable, falling back to stat",
              file=sys.stderr)

    return _stat_fallback(path, ssh_cfg, timeout)


def _parse_python_probe(stdout: str, stderr: str) -> RemoteProbe:
    parts = stdout.strip().split()
    if len(parts) != 5:
        return RemoteProbe(None, None, None, None, None,
                           f"unparseable probe output: {stdout.strip()!r}")
    try:
        mtime = float(parts[0])
        size  = int(parts[1])
        total, used, free = (int(v) for v in parts[2:5])
    except ValueError:
        return RemoteProbe(None, None, None, None, None,
                           f"unparseable probe output: {stdout.strip()!r}")
    if total < 0:                       # disk usage not requested, or it failed
        total = used = free = None
    return RemoteProbe(mtime, size, total, used, free, None)


def _stat_fallback(path: str, ssh_cfg: dict, timeout: int) -> RemoteProbe:
    """GNU-then-BSD ``stat``.  Yields mtime and size only — never disk usage."""
    quoted = shlex.quote(path)
    shell = (f"stat -c '%Y %s' {quoted} 2>/dev/null || "
             f"stat -f '%m %z' {quoted}")
    rc, out, err = _run(build_ssh_cmd(ssh_cfg, remote_shell=shell), timeout)
    if rc is None:
        return RemoteProbe(None, None, None, None, None, err)
    if rc != 0:
        return RemoteProbe(None, None, None, None, None,
                           err.strip() or f"stat exited {rc}")
    parts = out.strip().split()
    if len(parts) != 2:
        return RemoteProbe(None, None, None, None, None,
                           f"unparseable stat output: {out.strip()!r}")
    try:
        return RemoteProbe(float(parts[0]), int(parts[1]), None, None, None, None)
    except ValueError:
        return RemoteProbe(None, None, None, None, None,
                           f"unparseable stat output: {out.strip()!r}")


# ---------------------------------------------------------------------------
# Backwards-compatible wrappers over remote_probe()
# ---------------------------------------------------------------------------
def remote_stat(path: str, ssh_cfg: dict) -> tuple:
    """Return ``(mtime_float, None)`` or ``(None, error_str)``."""
    probe = remote_probe(path, ssh_cfg, want_disk=False)
    if probe.error:
        return None, probe.error
    return probe.mtime, None


def remote_disk_usage(path: str, ssh_cfg: dict) -> tuple:
    """Return ``(total, used, free, None)`` or ``(None, None, None, error_str)``."""
    probe = remote_probe(path, ssh_cfg, want_disk=True)
    if probe.error:
        return None, None, None, probe.error
    if probe.total is None:
        return None, None, None, "remote disk usage unavailable"
    return probe.total, probe.used, probe.free, None
