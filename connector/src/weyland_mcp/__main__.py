"""CLI entry: `weyland-mcp`.

Loads config from env (populated by systemd EnvironmentFile=/etc/weyland/mcp.env
in production), builds the FastMCP app, runs it on streamable-http transport.
"""
from __future__ import annotations

import sys

from .config import load_config
from .server import build_server, run


def main() -> int:
    try:
        cfg = load_config()
        app = build_server(cfg)
        run(app)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"weyland-mcp: fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
