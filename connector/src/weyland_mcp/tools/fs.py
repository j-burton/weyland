"""Filesystem verbs."""
from __future__ import annotations

import glob as _glob
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..config import Config
from ..denylist import is_denied

MAX_BYTES = 1 * 1024 * 1024  # 1 MiB


def register(app: FastMCP, cfg: Config) -> None:
    @app.tool()
    def read_file(path: str) -> dict:
        """Read a UTF-8 text file. Refuses denied paths. Caps at 1 MiB."""
        if is_denied(path):
            return {"ok": False, "error": f"path is denied: {path}"}
        try:
            data = Path(path).expanduser().read_bytes()
        except FileNotFoundError:
            return {"ok": False, "error": "file not found"}
        except PermissionError:
            return {"ok": False, "error": "permission denied"}
        if len(data) > MAX_BYTES:
            return {"ok": False, "error": f"file too large ({len(data)} > {MAX_BYTES})"}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "file is not valid UTF-8"}
        return {"ok": True, "content": text, "bytes": len(data)}

    @app.tool()
    def write_file(path: str, content: str) -> dict:
        """Write a UTF-8 text file. Creates parent dirs. Caps at 1 MiB."""
        if is_denied(path):
            return {"ok": False, "error": f"path is denied: {path}"}
        if len(content.encode("utf-8")) > MAX_BYTES:
            return {"ok": False, "error": "content too large"}
        p = Path(path).expanduser()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except PermissionError:
            return {"ok": False, "error": "permission denied"}
        return {"ok": True, "path": str(p), "bytes": len(content)}

    @app.tool()
    def list_dir(path: str) -> dict:
        """List entries in a directory."""
        if is_denied(path):
            return {"ok": False, "error": f"path is denied: {path}"}
        p = Path(path).expanduser()
        if not p.exists():
            return {"ok": False, "error": "not found"}
        if not p.is_dir():
            return {"ok": False, "error": "not a directory"}
        entries = []
        for child in sorted(p.iterdir()):
            if is_denied(str(child)):
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": stat.st_size if child.is_file() else None,
            })
        return {"ok": True, "path": str(p), "entries": entries}

    @app.tool()
    def glob(pattern: str) -> dict:
        """Glob a pattern (supports `**` with recursive=True)."""
        matches = _glob.glob(os.path.expanduser(pattern), recursive=True)
        matches = [m for m in matches if not is_denied(m)]
        return {"ok": True, "pattern": pattern, "matches": sorted(matches)}
