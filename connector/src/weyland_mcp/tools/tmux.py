"""Tmux verbs — drive any session, capture any pane."""
from __future__ import annotations

import shlex
import subprocess

from mcp.server.fastmcp import FastMCP

from ..config import Config


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def register(app: FastMCP, cfg: Config) -> None:
    @app.tool()
    def tmux_send_keys(session: str, keys: str, enter: bool = True,
                       window: int = 0, pane: int = 0) -> dict:
        """Inject keystrokes into a tmux pane."""
        target = f"{session}:{window}.{pane}"
        proc = _tmux("send-keys", "-t", target, keys)
        if proc.returncode != 0:
            return {"ok": False, "error": proc.stderr.strip() or "send-keys failed"}
        if enter:
            _tmux("send-keys", "-t", target, "C-m")
        return {"ok": True, "target": target, "keys_chars": len(keys), "enter": enter}

    @app.tool()
    def tmux_capture_pane(session: str, lines: int = 50,
                          window: int = 0, pane: int = 0) -> dict:
        """Capture the last N lines from a tmux pane."""
        target = f"{session}:{window}.{pane}"
        proc = _tmux("capture-pane", "-t", target, "-p", "-S", f"-{lines}")
        if proc.returncode != 0:
            return {"ok": False, "error": proc.stderr.strip() or "capture-pane failed"}
        return {"ok": True, "target": target, "content": proc.stdout}

    @app.tool()
    def tmux_list() -> dict:
        """List sessions, windows, and panes."""
        sessions_p = _tmux("list-sessions", "-F",
                           "#{session_name}|#{session_windows}|#{session_created}")
        if sessions_p.returncode != 0:
            return {"ok": True, "sessions": []}
        sessions = []
        for line in sessions_p.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                sessions.append({
                    "name": parts[0],
                    "windows": int(parts[1]),
                    "created": int(parts[2]),
                })
        return {"ok": True, "sessions": sessions}
