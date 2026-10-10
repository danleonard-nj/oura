"""Consent screen for the MCP OAuth authorization flow.

The only interactive page this service serves. A client sends the browser here
via `/authorize`; approving it with the owner secret mints the authorization
code and redirects back.

Deliberately plain HTML with no external assets: it handles a secret, so it
should load nothing it does not control.
"""

from __future__ import annotations

import html

from depi_quart import QuartInjector
from quart import Blueprint, Response, redirect, request

from app.models.config import McpConfig
from app.services.mcp_auth_service import (
    CONSENT_PATH,
    ConsentError,
    ConsentLockedOut,
    McpAuthService,
)
from app.utilities.logging import get_logger

logger = get_logger(__name__)


def create_consent_blueprint(injector: QuartInjector) -> Blueprint:
    consent_bp = Blueprint("consent", __name__)

    @consent_bp.route(CONSENT_PATH, methods=["GET"])
    @injector.inject
    async def show_consent(mcp: McpConfig, service: McpAuthService):
        if not mcp.oauth.enabled:
            return _page("Not available", "MCP OAuth is not enabled on this server.", status=404)

        txn = request.args.get("txn", "")
        pending = await service.pending_request(txn)

        if pending is None:
            return _page(
                "Request expired",
                "This authorization request has expired or was already completed. "
                "Start again from your MCP client.",
                status=400,
            )

        return _page(
            "Authorize access",
            None,
            status=200,
            body=_form(txn, pending),
        )

    @consent_bp.route(CONSENT_PATH, methods=["POST"])
    @injector.inject
    async def submit_consent(mcp: McpConfig, service: McpAuthService):
        if not mcp.oauth.enabled:
            return _page("Not available", "MCP OAuth is not enabled on this server.", status=404)

        form = await request.form
        txn = str(form.get("txn", ""))

        if form.get("action") == "deny":
            target = await service.deny_consent(txn)
            if target is None:
                return _page("Request expired", "Nothing to deny; the request had already gone.", status=400)
            return redirect(target, 302)

        # Rate limiting is keyed on the transaction, not the client address: behind
        # an ingress every request shares a source IP, so per-IP counting would
        # either lock out everyone at once or nobody.
        try:
            target = await service.approve_consent(
                txn_id=txn,
                password=str(form.get("password", "")),
                attempt_key=txn,
            )
        except ConsentLockedOut as exc:
            return _page("Locked out", str(exc), status=429)
        except ConsentError as exc:
            pending = await service.pending_request(txn)
            if pending is None:
                return _page("Request expired", str(exc), status=400)
            return _page("Authorize access", None, status=401, body=_form(txn, pending, error=str(exc)))

        return redirect(target, 302)

    return consent_bp



def _form(txn: str, pending: dict, error: str | None = None) -> str:
    client = html.escape(pending.get("client_name") or pending["client_id"])
    redirect_uri = html.escape(pending["redirect_uri"])
    scopes = ", ".join(html.escape(s) for s in pending["scopes"])
    banner = f'<p class="error">{html.escape(error)}</p>' if error else ""

    return f"""
      {banner}
      <p><strong>{client}</strong> is asking to read your Oura Ring data.</p>
      <dl>
        <dt>Redirect</dt><dd><code>{redirect_uri}</code></dd>
        <dt>Scope</dt><dd><code>{scopes}</code></dd>
      </dl>
      <p class="warn">
        Approving gives this client read access to your sleep, readiness,
        activity and other health data until you revoke it. Only continue if
        you started this from a client you trust.
      </p>
      <form method="post" action="{CONSENT_PATH}">
        <input type="hidden" name="txn" value="{html.escape(txn)}">
        <label for="password">Owner password</label>
        <input type="password" id="password" name="password" autocomplete="current-password"
               autofocus required>
        <div class="actions">
          <button type="submit" name="action" value="approve">Approve</button>
          <button type="submit" name="action" value="deny" class="secondary">Deny</button>
        </div>
      </form>
    """


def _page(title: str, message: str | None, *, status: int, body: str = "") -> Response:
    content = body or f"<p>{html.escape(message or '')}</p>"
    return Response(
        f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)} - Oura MCP</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         max-width: 34rem; margin: 4rem auto; padding: 0 1.25rem; }}
  h1 {{ font-size: 1.35rem; margin-bottom: 1.25rem; }}
  dl {{ display: grid; grid-template-columns: auto 1fr; gap: .35rem 1rem; margin: 1.25rem 0; }}
  dt {{ font-weight: 600; }}
  dd {{ margin: 0; overflow-wrap: anywhere; }}
  code {{ font-size: .9em; }}
  label {{ display: block; font-weight: 600; margin: 1.5rem 0 .35rem; }}
  input[type=password] {{ width: 100%; padding: .6rem; font-size: 1rem; box-sizing: border-box; }}
  .actions {{ display: flex; gap: .75rem; margin-top: 1.25rem; }}
  button {{ padding: .6rem 1.25rem; font-size: 1rem; cursor: pointer; }}
  .secondary {{ opacity: .75; }}
  .warn {{ font-size: .9rem; opacity: .8; }}
  .error {{ padding: .6rem .8rem; border-left: 3px solid currentColor; font-weight: 600; }}
</style>
</head><body>
<h1>{html.escape(title)}</h1>
{content}
</body></html>""",
        status=status,
        content_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )
