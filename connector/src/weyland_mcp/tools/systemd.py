"""Systemd verbs — status, restart, install units."""
from __future__ import annotations

import subprocess
from pathlib import Path

from fastmcp import FastMCP


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def register(app: FastMCP) -> None:
    @app.tool()
    def systemctl_status(unit: str) -> dict:
        """Return `systemctl status <unit>` output."""
        proc = _run("systemctl", "status", unit, "--no-pager")
        return {
            "ok": True,
            "unit": unit,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    @app.tool()
    def systemctl_restart(unit: str) -> dict:
        """Restart a systemd unit via passwordless sudo."""
        proc = _run("sudo", "-n", "systemctl", "restart", unit)
        return {
            "ok": proc.returncode == 0,
            "unit": unit,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    @app.tool()
    def install_unit(name: str, contents: str, enable: bool = True,
                     start: bool = True) -> dict:
        """Write a unit file to /etc/systemd/system/<name> and (optionally) enable + start it."""
        if "/" in name or ".." in name:
            return {"ok": False, "error": "invalid unit name"}
        path = Path("/etc/systemd/system") / name
        # Write via sudo to be permission-agnostic.
        write_proc = subprocess.run(
            ["sudo", "-n", "tee", str(path)],
            input=contents, text=True, capture_output=True, check=False,
        )
        if write_proc.returncode != 0:
            return {"ok": False, "error": write_proc.stderr.strip() or "write failed"}
        _run("sudo", "-n", "systemctl", "daemon-reload")
        if enable:
            _run("sudo", "-n", "systemctl", "enable", name)
        if start:
            _run("sudo", "-n", "systemctl", "restart", name)
        return {"ok": True, "path": str(path), "enabled": enable, "started": start}
