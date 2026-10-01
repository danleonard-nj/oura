"""Azure AD-backed API gateway auth: inbound bearer-token validation.

The gateway-auth pattern the other internal services use: callers (the
external cron scheduler) present a client-credentials token issued for this
service's Application ID URI, validated here against the tenant's JWKS.

Transport-agnostic: `utilities.auth.require_gateway_token` wraps it into a
route decorator.
"""

from __future__ import annotations

from typing import Any

import jwt
from jwt import PyJWKClient

from app.utilities.logging import get_logger

logger = get_logger(__name__)

JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
ISSUER_TEMPLATES = (
    "https://login.microsoftonline.com/{tenant_id}/v2.0",
    "https://sts.windows.net/{tenant_id}/",
)

# One JWKS client per tenant, reused across requests: PyJWKClient caches the
# signing keys it fetches, so this avoids a discovery round trip per request.
_jwks_clients: dict[str, PyJWKClient] = {}


class AzureAdError(Exception):
    """Validating an Azure AD token failed.

    `message` is safe to show a caller: it never contains a client secret or
    the token itself.
    """


def verify_bearer_token(
    authorization_header: str | None,
    *,
    tenant_id: str,
    audiences: list[str],
    required_role: str | None = None,
) -> dict[str, Any]:
    """Validate an inbound `Authorization: Bearer <jwt>` header.

    Checks the signature against the tenant's JWKS, the issuer, the audience,
    and expiry; when `required_role` is set, also requires it in the token's
    `roles` claim -- app-only client-credentials tokens carry app roles
    (Application permissions, consented by a tenant admin), never delegated
    `scp` scopes, which only ever appear on user-delegated tokens. Returns the
    verified claims on success. Raises `AzureAdError` -- with a message safe
    to log or return -- on any failure.
    """
    scheme, _, credential = (authorization_header or "").partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        raise AzureAdError("A Bearer token is required.")
    token = credential.strip()

    try:
        signing_key = _jwks_client(tenant_id).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audiences,
            issuer=[template.format(tenant_id=tenant_id) for template in ISSUER_TEMPLATES],
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        raise AzureAdError(f"Bearer token failed validation: {exc}") from exc

    if required_role is not None and required_role not in claims.get("roles", []):
        raise AzureAdError(f"Bearer token is missing the required app role {required_role!r}.")

    return claims


def _jwks_client(tenant_id: str) -> PyJWKClient:
    client = _jwks_clients.get(tenant_id)
    if client is None:
        client = PyJWKClient(JWKS_URL_TEMPLATE.format(tenant_id=tenant_id), cache_keys=True, lifespan=3600)
        _jwks_clients[tenant_id] = client
    return client
