"""Azure AD-backed API gateway auth: outbound client-credentials tokens and
inbound bearer-token validation.

Mirrors the *shape* of kube-tools-api's `IdentityClient`/`AzureGatewayClient`
(client-credentials token fetch, cached, used to call another gateway-fronted
service; inbound JWT validation gating a route) but is implemented directly
against `httpx2`/`redis.asyncio`/`PyJWT`, with no dependency on that private
`framework` package -- this project uses `pydepi` for DI instead.

Deliberately transport-agnostic: nothing here knows about Starlette. Each
service's own `api/auth.py` wraps `verify_bearer_token` into whatever guard
shape its routes use (see oura-mcp's `admin_guard` for the pattern).
"""

from __future__ import annotations

from typing import Any

import jwt
from httpx2 import AsyncClient
from jwt import PyJWKClient
from redis.asyncio import Redis
from redis.exceptions import RedisError

from oura_common.infrastructure.logging import get_logger

logger = get_logger(__name__)

TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
ISSUER_TEMPLATES = (
    "https://login.microsoftonline.com/{tenant_id}/v2.0",
    "https://sts.windows.net/{tenant_id}/",
)

TOKEN_CACHE_PREFIX = "aad:token"
# Cache slightly short of the token's real lifetime so a request never races
# expiry between the cache read and the call it authenticates.
TOKEN_CACHE_SKEW_SECONDS = 60

# One JWKS client per tenant, reused across requests: PyJWKClient caches the
# signing keys it fetches, so this avoids a discovery round trip per request.
_jwks_clients: dict[str, PyJWKClient] = {}


class AzureAdError(Exception):
    """Acquiring or validating an Azure AD token failed.

    `message` is safe to show a caller: it never contains a client secret or
    the token itself.
    """


class AzureAdTokenClient:
    """Client-credentials token fetch for calling another gateway-fronted service.

    Same shape as kube-tools-api's `IdentityClient.get_token()`: request a
    token scoped to the target service's Application ID URI, cache it in
    Redis for slightly less than its lifetime, and refetch on expiry or cache
    miss.
    """

    def __init__(
        self,
        http: AsyncClient,
        redis: Redis,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._http = http
        self._redis = redis
        self._token_url = TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id)
        self._client_id = client_id
        self._client_secret = client_secret

    async def get_token(self, scope: str) -> str:
        """Return a bearer token valid for `scope` (an `api://<app-id>/.default` URI)."""
        cache_key = f"{TOKEN_CACHE_PREFIX}:{self._client_id}:{scope}"
        try:
            cached = await self._redis.get(cache_key)
        except RedisError as exc:
            logger.warning("azure ad token cache read failed", extra={"error": str(exc)})
            cached = None
        if cached:
            return cached

        response = await self._http.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": scope,
            },
        )
        if response.status_code >= 400:
            # Never echo the response body: a misconfigured request can have
            # the client secret reflected back in Azure AD's error detail.
            raise AzureAdError(
                f"Azure AD rejected the client-credentials request for scope {scope!r} "
                f"(HTTP {response.status_code})."
            )

        body = response.json()
        token = body["access_token"]
        expires_in = int(body.get("expires_in", 3600))

        try:
            await self._redis.set(
                cache_key, token, ex=max(30, expires_in - TOKEN_CACHE_SKEW_SECONDS)
            )
        except RedisError as exc:
            logger.warning("azure ad token cache write failed", extra={"error": str(exc)})

        return token


def verify_bearer_token(
    authorization_header: str | None,
    *,
    tenant_id: str,
    audience: str,
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
            audience=audience,
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
