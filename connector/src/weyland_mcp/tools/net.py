"""Network verbs — outbound HTTP only."""
from __future__ import annotations

import httpx

from fastmcp import FastMCP

DEFAULT_TIMEOUT = 15.0


def register(app: FastMCP) -> None:
    @app.tool()
    def http_get(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
        """GET a URL. Returns body (text) or error."""
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "url": str(r.url),
            "status": r.status_code,
            "body": r.text[:64 * 1024],
            "truncated": len(r.text) > 64 * 1024,
        }

    @app.tool()
    def http_post(url: str, body: str | None = None,
                  json_body: dict | None = None,
                  timeout: float = DEFAULT_TIMEOUT) -> dict:
        """POST a URL. One of body / json_body."""
        try:
            r = httpx.post(url, content=body, json=json_body,
                           timeout=timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "url": str(r.url),
            "status": r.status_code,
            "body": r.text[:64 * 1024],
            "truncated": len(r.text) > 64 * 1024,
        }
