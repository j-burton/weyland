"""FastMCP server entry. Wires all the tool modules together."""
from __future__ import annotations

import logging
from pathlib import Path

from fastmcp import FastMCP

from .config import load_config
from .tools import fs as fs_tools
from .tools import github as github_tools
from .tools import net as net_tools
from .tools import shell as shell_tools
from .tools import systemd as systemd_tools
from .tools import tmux as tmux_tools


def build_app() -> FastMCP:
    cfg = load_config()

    log_path = Path(cfg.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = FastMCP(name=f"weyland-mcp/{cfg.pi_name}")

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

    fs_tools.register(app)
    shell_tools.register(app)
    tmux_tools.register(app)
    systemd_tools.register(app)
    net_tools.register(app)
    github_tools.register(app, repo_path=cfg.pi_dir)

    return app


def run() -> None:
    cfg = load_config()
    app = build_app()
    app.run(
        transport="streamable-http",
        host=cfg.bind_host,
        port=cfg.bind_port,
    )
