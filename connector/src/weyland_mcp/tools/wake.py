"""Wake-system verbs — re-arm the watcher between tasks."""
from __future__ import annotations

import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..config import Config

WAKE_MODE_FILE = Path("/etc/weyland/wake-mode")
WATCHER_UNIT = "weyland-watcher.service"


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _read_mode() -> str:
    try:
        return WAKE_MODE_FILE.read_text().strip().lower() or "on"
    except OSError:
        return "unknown"


def _write_mode(value: str) -> tuple[bool, str]:
    # wake-mode is root-owned; the connector runs as the service user, so go
    # through passwordless sudo (same pattern as the systemd/shell verbs).
    proc = subprocess.run(
        ["sudo", "-n", "tee", str(WAKE_MODE_FILE)],
        input=value + "\n", text=True, capture_output=True, check=False,
    )
    return proc.returncode == 0, (proc.stderr.strip() if proc.returncode else "")


def register(app: FastMCP, cfg: Config) -> None:
    @app.tool()
    def restart_wake() -> dict:
        """Re-arm the wake watcher between tasks (canonical 'between-tasks' action).

        Flips /etc/weyland/wake-mode off → on (the re-arm signal), then reports
        whether the weyland-watcher service is alive. Run this after every
        dispatch so the watcher is freshly armed for the next idle period.

        Returns:
          ok            — both writes succeeded
          previous_mode — wake-mode before the flip ('on' / 'off' / 'unknown')
          current_mode  — wake-mode after the flip (normally 'on')
          watcher_alive — is weyland-watcher.service active
          watcher_pid   — main PID of the watcher if alive, else None
        """
        previous_mode = _read_mode()

        # Flip off → on. Two sequential writes; the brief 'off' is the re-arm
        # edge. Tiny single-write files, so each write is effectively atomic.
        ok = True
        err = ""
        for value in ("off", "on"):
            wrote, write_err = _write_mode(value)
            if not wrote:
                ok = False
                err = write_err or "write failed"
                break

        active = _run("systemctl", "is-active", WATCHER_UNIT)
        watcher_alive = active.stdout.strip() == "active"
        watcher_pid = None
        if watcher_alive:
            show = _run("systemctl", "show", "-p", "MainPID", "--value",
                        WATCHER_UNIT)
            pid_str = show.stdout.strip()
            if pid_str.isdigit() and int(pid_str) > 0:
                watcher_pid = int(pid_str)

        result = {
            "ok": ok,
            "previous_mode": previous_mode,
            "current_mode": _read_mode() if ok else previous_mode,
            "watcher_alive": watcher_alive,
            "watcher_pid": watcher_pid,
        }
        if not ok:
            result["error"] = err
        return result
