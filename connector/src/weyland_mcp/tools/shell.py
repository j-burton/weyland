"""Shell-execution verbs. No allowlist — runs whatever you ask."""
from __future__ import annotations

import shlex
import subprocess

from mcp.server.fastmcp import FastMCP

from ..config import Config

STDOUT_CAP = 64 * 1024  # 64 KiB
TIMEOUT_DEFAULT = 60


def _truncate(s: str, cap: int) -> tuple[str, bool]:
    if len(s) <= cap:
        return s, False
    return s[:cap], True


def register(app: FastMCP, cfg: Config) -> None:
    @app.tool()
    def run_command(cmd: str, args: list[str] | None = None,
                    timeout: int = TIMEOUT_DEFAULT) -> dict:
        """Run an executable + args. Captures stdout/stderr (64 KiB each)."""
        argv = [cmd, *(args or [])]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return {"ok": False, "error": f"command not found: {cmd}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timeout after {timeout}s"}
        stdout, stdout_trunc = _truncate(proc.stdout, STDOUT_CAP)
        stderr, stderr_trunc = _truncate(proc.stderr, STDOUT_CAP)
        return {
            "ok": True,
            "argv": argv,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_trunc,
            "stderr_truncated": stderr_trunc,
        }

    @app.tool()
    def run_shell(cmdline: str, timeout: int = TIMEOUT_DEFAULT) -> dict:
        """Run a `bash -c '<cmdline>'`. Captures stdout/stderr (64 KiB each)."""
        try:
            proc = subprocess.run(
                ["bash", "-c", cmdline],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timeout after {timeout}s"}
        stdout, stdout_trunc = _truncate(proc.stdout, STDOUT_CAP)
        stderr, stderr_trunc = _truncate(proc.stderr, STDOUT_CAP)
        return {
            "ok": True,
            "cmdline": cmdline,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_trunc,
            "stderr_truncated": stderr_trunc,
        }
