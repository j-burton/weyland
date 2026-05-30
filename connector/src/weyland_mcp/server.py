"""FastMCP server entry. Wires OAuth provider, consent route, tools.

Uses the official Anthropic MCP Python SDK (`mcp.server.fastmcp.FastMCP`).
Auth: OAuth 2.1 via `WeylandOAuthProvider`. The bearer pasted into the
`/weyland-consent` form is the actual gate; PKCE is the public-client
substitute for a client_secret. See oauth.py for the design notes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from .config import Config, load_config
from .consent import consent_route
from .oauth import WeylandOAuthProvider
from .tools import fs as fs_tools
from .tools import github as github_tools
from .tools import net as net_tools
from .tools import shell as shell_tools
from .tools import systemd as systemd_tools
from .tools import tmux as tmux_tools


def _transport_security(cfg: Config) -> TransportSecuritySettings:
    """Derive allowed Host/Origin lists from the configured public URL +
    bind address.

    DNS rebinding protection stays on (recommended). Without an explicit
    `allowed_hosts` list, the SDK rejects every request whose Host header
    isn't `127.0.0.1` — Claude Desktop's POST /mcp arrives with the public
    hostname and gets 421 Misdirected Request. We enumerate the public
    hostname plus loopback variants so both the Cloudflare tunnel's
    public-host requests and local debug probes work.
    """
    public_host = urlparse(cfg.public_url).hostname or ""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            public_host,
            f"{cfg.bind_host}:{cfg.bind_port}",
            cfg.bind_host,
            "127.0.0.1",
            "localhost",
            f"localhost:{cfg.bind_port}",
        ],
        allowed_origins=[
            cfg.public_url,
            f"http://{cfg.bind_host}:{cfg.bind_port}",
        ],
    )


def build_server(cfg: Config) -> FastMCP:
    """Construct the FastMCP app, wire OAuth + consent + tools, return it."""
    log_path = Path(cfg.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    provider = WeylandOAuthProvider(cfg)

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
        transport_security=_transport_security(cfg),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(cfg.public_url),
            resource_server_url=AnyHttpUrl(cfg.public_url),
            required_scopes=[],
            # DCR enabled — Claude Desktop's connector flow (as of 2026-05)
            # requires `registration_endpoint` in the OAuth discovery doc and
            # aborts the connect with `oauth_error=registration_endpoint_missing`
            # otherwise. The SDK auto-mounts /register, which calls our
            # provider's register_client() and then advertises the endpoint
            # in the discovery doc. No expiry, no scope restrictions.
            client_registration_options=ClientRegistrationOptions(enabled=True),
        ),
    )

    # Bearer-gated consent form — claude.ai's OAuth /authorize redirects here.
    # We wrap consent_route in a closure so the cfg + provider are bound at
    # mount time; the actual handler signature stays (request) -> Response,
    # which is what FastMCP.custom_route expects.
    async def _consent_handler(request):
        return await consent_route(request, cfg, provider)

    app.custom_route("/weyland-consent", methods=["GET", "POST"])(_consent_handler)

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

    _force_tools_changed(app)

    return app


def _force_tools_changed(app: FastMCP) -> None:
    """Force tools.listChanged=true on the underlying low-level Server.

    Lifted verbatim from argos-mcp's server.py. FastMCP 1.27.x calls the
    low-level server's create_initialization_options() with no arguments,
    which defaults to NotificationOptions() with tools_changed=False — the
    cached value gets advertised forever and MCP clients (notably
    claude.ai's connector) never re-poll tools/list when the server
    restarts with a different tool set. There's no constructor knob on
    FastMCP for this; we patch the method directly on the underlying
    _mcp_server instance to inject tools_changed=True.

    Without this, every new MCP tool shipped silently fails to appear in
    chat-Claude until the user manually re-registers the connector in
    claude.ai.
    """
    _orig_create_init_opts = app._mcp_server.create_initialization_options

    def _create_init_opts_with_tools_changed(
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict | None = None,
    ):
        # Honour an explicit NotificationOptions if one is passed (forward-
        # compat: a future FastMCP version might start supplying its own),
        # but force tools_changed=True regardless so the cap never goes back
        # to False through this path.
        if notification_options is None:
            notification_options = NotificationOptions(tools_changed=True)
        else:
            notification_options.tools_changed = True
        return _orig_create_init_opts(notification_options, experimental_capabilities)

    app._mcp_server.create_initialization_options = _create_init_opts_with_tools_changed


def run(app: FastMCP) -> None:
    """Run the FastMCP app over streamable-http. Host/port set on the constructor."""
    app.run(transport="streamable-http")
