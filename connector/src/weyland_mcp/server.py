"""FastMCP server entry. Wires all the tool modules together.

Uses the official Anthropic MCP Python SDK (`mcp.server.fastmcp.FastMCP`).
Bearer-token auth is still the active auth path; OAuth layers on top in
a later handoff.
"""
from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import Config, load_config
from .tools import fs as fs_tools
from .tools import github as github_tools
from .tools import net as net_tools
from .tools import shell as shell_tools
from .tools import systemd as systemd_tools
from .tools import tmux as tmux_tools


def build_server(cfg: Config) -> FastMCP:
    """Construct the FastMCP app, wire whoami + the six tool modules, return it."""
    log_path = Path(cfg.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = FastMCP(
        name=f"weyland-mcp/{cfg.pi_name}",
        instructions=(
            "Weyland MCP connector for one minion Pi. Default-allow surface "
            "(no per-call approval); tiny credential-file denylist; full sudo "
            "available via the shell + systemd verbs."
        ),
        host=cfg.bind_host,
        port=cfg.bind_port,
        streamable_http_path="/mcp",
        log_level="INFO",
    )

    # Identity / discovery verb.
    @app.tool()
    def whoami() -> dict:
        """Return this Pi's identity for client orientation."""
        return {
            "pi_name": cfg.pi_name,
            "pi_repo": cfg.pi_repo,
            "pi_dir": cfg.pi_dir,
            "public_url": cfg.public_url,
            "version": "0.0.1",
        }

    fs_tools.register(app, cfg)
    shell_tools.register(app, cfg)
    tmux_tools.register(app, cfg)
    systemd_tools.register(app, cfg)
    net_tools.register(app, cfg)
    github_tools.register(app, cfg)

    return app


def run(app: FastMCP) -> None:
    """Run the FastMCP app over streamable-http. Host/port set on the constructor."""
    app.run(transport="streamable-http")
