"""Bearer-gated HTML consent form for the OAuth dance.

Mounted at `/weyland-consent` (GET + POST). The OAuth provider redirects
the user here after `/authorize`; the user pastes the pre-shared bearer;
on match we 302 back to claude.ai's callback with the issued auth code.

GET:  render the form with `pending_id` in a hidden field.
POST: validate bearer (sha256 + hmac.compare_digest against
      `cfg.bearer_token_hash`), then look up + pop the pending consent,
      issue an auth code, redirect to the original `redirect_uri` with
      `?code=...&state=...`. Wrong bearer → re-render the form with a
      generic error (no info leak about which field was wrong); the
      pending entry is NOT popped, so the user can retry within the
      60s TTL window.
"""
from __future__ import annotations

from html import escape
from string import Template
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .auth import token_matches
from .config import Config
from .oauth import WeylandOAuthProvider

# string.Template ($name) not str.format() — CSS braces below would break
# str.format. This is the same wrinkle argos-mcp's consent route hits.
_CONSENT_FORM_TEMPLATE = Template("""\
<!doctype html>
<html><head><meta charset="utf-8"><title>weyland-mcp consent</title>
<style>
body{font-family:system-ui,sans-serif;max-width:26em;margin:4em auto;padding:0 1em;color:#111}
h1{font-size:1.2rem;margin:.2em 0}
p{margin:.4em 0;color:#555;font-size:.9rem}
input,button{font-size:1rem;padding:.6em;width:100%;box-sizing:border-box;margin:.3em 0;border:1px solid #ccc;border-radius:4px}
button{background:#111;color:#fff;border:0;cursor:pointer}
button:hover{background:#000}
.err{color:#a00;margin:.5em 0;font-size:.9rem}
</style></head><body>
<h1>weyland-mcp consent</h1>
<p>Paste the weyland-mcp bearer to authorize the connector.</p>
<form method="POST" action="/weyland-consent">
<input type="hidden" name="pending_id" value="$pending_id">
<input type="password" name="bearer" placeholder="bearer token" autocomplete="off" autofocus required>
<button type="submit">Approve</button>
</form>
$error_block
</body></html>
""")


def _consent_html(pending_id: str, *, error: str | None = None) -> str:
    err = f'<p class="err">{escape(error)}</p>' if error else ""
    return _CONSENT_FORM_TEMPLATE.substitute(
        pending_id=escape(pending_id), error_block=err,
    )


async def consent_route(
    request: Request,
    cfg: Config,
    provider: WeylandOAuthProvider,
) -> Response:
    """Async Starlette handler for GET + POST /weyland-consent."""
    if request.method == "GET":
        pending_id = request.query_params.get("pending_id", "")
        if not pending_id:
            return HTMLResponse(
                "<p>Missing pending_id in URL.</p>", status_code=400,
            )
        return HTMLResponse(_consent_html(pending_id))

    # POST
    form = await request.form()
    pending_id = str(form.get("pending_id", "")).strip()
    bearer = str(form.get("bearer", "")).strip()

    if not pending_id:
        return HTMLResponse(
            _consent_html(pending_id, error="Missing or invalid pending_id."),
            status_code=400,
        )

    # Constant-time bearer check (token_matches uses hmac.compare_digest).
    if not token_matches(bearer, cfg.bearer_token_hash):
        # Don't pop the pending entry on a wrong bearer — let the user retry.
        return HTMLResponse(
            _consent_html(pending_id, error="Invalid bearer. Try again."),
            status_code=401,
        )

    pending = provider.take_pending(pending_id)
    if pending is None:
        return HTMLResponse(
            "<p>Consent request expired or already used. "
            "Restart the connector flow.</p>",
            status_code=400,
        )

    code = provider.issue_code(pending.client_id, pending.params)
    target_qs = {"code": code, "state": pending.params.state}
    target = f"{pending.params.redirect_uri}?{urlencode(target_qs)}"
    return RedirectResponse(target, status_code=302)
