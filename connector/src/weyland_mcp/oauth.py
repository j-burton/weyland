"""Single-user OAuth 2.1 authorization server for the weyland connector.

Mirrors argos-mcp's provider almost verbatim — names changed, no other
behavioural divergence. The minimum surface claude.ai's connector
framework requires:

- `/.well-known/oauth-authorization-server` (auto-mounted by the SDK once
  `auth_server_provider` is set on FastMCP)
- `/authorize` (SDK route; calls `provider.authorize` → redirect to consent form)
- `/token` (SDK route; calls `provider.exchange_authorization_code` → returns
  a fresh per-session access token)
- `/weyland-consent` (custom Starlette route mounted via `FastMCP.custom_route`;
  the bearer-gated HTML form that approves the OAuth handshake — defined in
  `consent.py`)

Design:
- **Dynamic Client Registration is DISABLED.** The single client (default id
  `weyland-mcp-claude-ai`) is pre-registered via `WEYLAND_OAUTH_CLIENT_ID`.
- **PKCE-only public client.** No `client_secret`. The bearer pasted into the
  consent form is the actual auth gate; PKCE is required by the SDK.
- **Per-session access tokens.** `/token` mints `secrets.token_urlsafe(32)`,
  stores it in memory AND writes through to a persistence file (default
  `/var/lib/weyland-mcp/tokens.json`, env `WEYLAND_TOKEN_STORE`). On restart,
  the file is reloaded so claude.ai's existing access token continues to work
  without re-consent.
- **Short TTLs.** Authorization codes and pending-consent entries live ~60s
  and are NOT persisted — in-flight OAuth dances die on restart, but a
  60s-TTL request is fine to redo.
- **Defensive persistence.** Corrupt JSON or unreadable token-store file is
  logged to stderr and treated as "no tokens loaded". Token issuance /
  revocation never fails the request on a persistence problem; the in-memory
  map remains authoritative for the running process. Token-store directory
  is created mode 0700 on first write; permission failures are logged and
  ignored.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from .config import Config


def _origin(public_url: str) -> str:
    """Return scheme://host[:port] with no path.

    `cfg.public_url` may include a path component (e.g. `.../mcp` — the MCP
    protocol's own endpoint). The consent route is mounted at the root of
    the host, not under `/mcp`, so we need the bare origin when building
    consent URLs.
    """
    parsed = urlparse(public_url)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

# Static labels for the single-user setup (not secrets — just identifiers).
WEYLAND_CLIENT_ID_DEFAULT = "weyland-mcp-claude-ai"
WEYLAND_SCOPE = "weyland"

# TTLs.
PENDING_TTL_S = 60
CODE_TTL_S = 60
ACCESS_TOKEN_TTL_S = 30 * 24 * 3600  # 30 days — re-OAuth on restart anyway

# Claude Desktop's fixed OAuth callback URL.
CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


@dataclass
class _PendingConsent:
    client_id: str
    params: AuthorizationParams
    expires_at: float


def _now() -> float:
    return time.monotonic()


def _bearer_matches(bearer: str, expected_hash: str) -> bool:
    if not bearer:
        return False
    incoming = hashlib.sha256(bearer.encode("utf-8")).hexdigest()
    return hmac.compare_digest(incoming, expected_hash.lower())


class WeylandOAuthProvider(OAuthAuthorizationServerProvider):
    """Single-user, single-client OAuth provider wrapping the pre-shared bearer."""

    def __init__(self, cfg: Config) -> None:
        self._client_id = cfg.oauth_client_id
        self._bearer_hash = cfg.bearer_token_hash.lower()
        # consent URL is built off the origin only — see _origin() above.
        # `cfg.public_url` may include `/mcp`; the consent route lives at root.
        self._public_url = _origin(cfg.public_url)
        self._pending: dict[str, _PendingConsent] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        # Dynamically-registered clients (DCR — RFC 7591). Claude Desktop's
        # connector flow (as of 2026-05) registers a fresh client at /register
        # before /authorize. The SDK's register handler mints the client_id
        # (uuid4) and an optional client_secret; we just remember the result
        # so subsequent get_client() lookups by that id find it. Not persisted
        # for now — a connector restart forces Claude Desktop to re-register,
        # which is mildly noisy but not broken (existing access tokens load
        # from disk and continue to validate independently of client_id).
        self._registered_clients: dict[str, OAuthClientInformationFull] = {}
        self._token_store_path = (
            Path(cfg.token_store_path) if cfg.token_store_path else None
        )
        self._persist_lock = threading.Lock()
        # Best-effort: load any previously-persisted access tokens.
        self._load_token_store()

    # --- persistence ---------------------------------------------------------

    def _load_token_store(self) -> None:
        """Populate self._access_tokens from the token-store file.

        Best-effort: missing file → empty start. Corrupt JSON → log + empty
        start (do NOT crash; manual intervention later). Expired entries are
        dropped on load.
        """
        if self._token_store_path is None or not self._token_store_path.exists():
            return
        try:
            data = json.loads(self._token_store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"WeylandOAuth: token-store at {self._token_store_path} "
                f"unreadable/corrupt ({type(e).__name__}: {e}); starting fresh",
                file=sys.stderr,
            )
            return

        now = int(time.time())
        entries = data.get("access_tokens", []) if isinstance(data, dict) else []
        loaded = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                at = AccessToken(**entry)
            except Exception as e:  # pydantic ValidationError or similar
                print(
                    f"WeylandOAuth: skipping malformed entry in token store: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                continue
            if at.expires_at is not None and at.expires_at < now:
                continue
            self._access_tokens[at.token] = at
            loaded += 1
        if loaded:
            print(
                f"WeylandOAuth: loaded {loaded} access token(s) from "
                f"{self._token_store_path}",
                file=sys.stderr,
            )

    def _persist_token_store(self) -> None:
        """Write self._access_tokens to the token-store file atomically.

        Best-effort: write failures log to stderr but don't raise. The
        in-memory map remains authoritative; next restart will re-prompt
        consent if the file couldn't be written. Directory is created mode
        0700 on first write; a permission failure on mkdir is logged and
        ignored (we just lose persistence for this process).
        """
        if self._token_store_path is None:
            return
        with self._persist_lock:
            data = {
                "access_tokens": [
                    at.model_dump(mode="json")
                    for at in self._access_tokens.values()
                ],
            }
            try:
                self._token_store_path.parent.mkdir(
                    mode=0o700, parents=True, exist_ok=True,
                )
                tmp = self._token_store_path.with_suffix(
                    self._token_store_path.suffix + ".tmp"
                )
                tmp.write_text(
                    json.dumps(data, separators=(",", ":")), encoding="utf-8",
                )
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                tmp.replace(self._token_store_path)
            except OSError as e:
                print(
                    f"WeylandOAuth: failed to persist token store at "
                    f"{self._token_store_path}: {e}",
                    file=sys.stderr,
                )

    # --- helpers used by the consent route -----------------------------------

    def _evict_expired(self, now: float | None = None) -> None:
        n = now if now is not None else _now()
        self._pending = {k: v for k, v in self._pending.items() if v.expires_at > n}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > n}

    def consent_url(self, pending_id: str) -> str:
        return f"{self._public_url}/weyland-consent?pending_id={pending_id}"

    def bearer_is_valid(self, bearer: str) -> bool:
        """Compare the bearer pasted into the consent form against the stored hash."""
        return _bearer_matches(bearer, self._bearer_hash)

    def take_pending(self, pending_id: str) -> _PendingConsent | None:
        """Look up + remove a pending consent. Returns None if missing/expired."""
        self._evict_expired()
        return self._pending.pop(pending_id, None)

    def issue_code(self, client_id: str, params: AuthorizationParams) -> str:
        code_str = secrets.token_urlsafe(32)
        self._codes[code_str] = AuthorizationCode(
            code=code_str,
            # `params.scopes` is None when Claude Desktop requests no scopes
            # (common). Pydantic rejects None; default to [] for the same effect.
            scopes=params.scopes or [],
            expires_at=time.time() + CODE_TTL_S,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return code_str

    # --- OAuthAuthorizationServerProvider interface --------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        # Static, env-configured client preserves backward-compat for any
        # Claude Desktop entries that were manually wired to the historical
        # client_id (the value of cfg.oauth_client_id, e.g.
        # `weyland-mcp-claude-ai`). Public client; PKCE substitutes for secret.
        if client_id == self._client_id:
            return OAuthClientInformationFull(
                client_id=self._client_id,
                client_secret=None,
                redirect_uris=[AnyUrl(CLAUDE_REDIRECT_URI)],
                grant_types=["authorization_code"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                scope=WEYLAND_SCOPE,
                client_name="weyland-mcp (single-user)",
            )
        # Dynamically-registered clients — Claude Desktop's current flow.
        return self._registered_clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Accept a Dynamic Client Registration (RFC 7591).

        The SDK's register handler has already minted `client_id` (a uuid4),
        optionally minted `client_secret` based on `token_endpoint_auth_method`,
        validated the metadata (grant_types must include authorization_code +
        refresh_token; response_types must include 'code'), and constructed
        the full `OAuthClientInformationFull` it'll return to the caller.
        Our job is only to remember it so later /authorize + /token calls
        can resolve the client_id.
        """
        self._registered_clients[client_info.client_id] = client_info

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Stage a pending consent and redirect the user to the bearer-gated form."""
        self._evict_expired()
        pending_id = secrets.token_urlsafe(24)
        self._pending[pending_id] = _PendingConsent(
            client_id=client.client_id or self._client_id,
            params=params,
            expires_at=_now() + PENDING_TTL_S,
        )
        return self.consent_url(pending_id)

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None:
            return None
        if code.expires_at < time.time():
            self._codes.pop(authorization_code, None)
            return None
        if code.client_id != (client.client_id or self._client_id):
            return None
        return code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # Single-use: remove the code on use.
        self._codes.pop(authorization_code.code, None)
        access_token_str = secrets.token_urlsafe(32)
        self._access_tokens[access_token_str] = AccessToken(
            token=access_token_str,
            client_id=client.client_id or self._client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + ACCESS_TOKEN_TTL_S,
            resource=authorization_code.resource,
        )
        self._persist_token_store()
        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_S,
            scope=" ".join(authorization_code.scopes),
            refresh_token=None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        at = self._access_tokens.get(token)
        if at is None:
            return None
        if at.expires_at is not None and at.expires_at < int(time.time()):
            self._access_tokens.pop(token, None)
            self._persist_token_store()
            return None
        return at

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        return None  # No refresh support in v1.

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        raise NotImplementedError("Refresh tokens are not supported in v1")

    async def revoke_token(self, token: Any) -> None:
        if isinstance(token, AccessToken):
            existed = self._access_tokens.pop(token.token, None)
            if existed is not None:
                self._persist_token_store()
