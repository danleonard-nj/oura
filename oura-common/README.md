# oura-common

Shared Oura-integration code for `oura-mcp` and `oura-sync`: everything about
talking to the Oura API on a user's behalf that both services need
identically -- token storage/refresh, the Oura HTTP client, Mongo/Redis
infrastructure, domain models, and the Azure AD gateway-auth code both
services' inbound/outbound sides are built on.

**Not an installable package.** No `pyproject.toml`, no version, nothing to
`pip install`. It's a plain shared source tree at `src/oura_common/`, reached
by putting `oura-common/src` on the consuming service's import path:

- **pytest**: each service's `pyproject.toml` sets
  `[tool.pytest.ini_options] pythonpath = ["../oura-common/src"]`.
- **Running a service directly**: `PYTHONPATH=../oura-common/src hypercorn ...`
  (see that service's README).
- **Docker**: each service's Dockerfile copies `src/oura_common/` into the
  image and drops a `.pth` file into the venv's site-packages pointing at it
  -- the same mechanism `pip install -e` uses under the hood, without making
  this a real distribution.

`tests/` lives outside `src/` specifically so that putting `oura-common/src`
on `sys.path` never also exposes a top-level `tests` package that would
collide with the consuming service's own `tests/` directory.

## Layout

```
src/oura_common/
  config.py           CommonConfig -- shared env-driven settings; each
                       service subclasses it and adds its own fields
  container.py         register_common(services, config) -- shared DI
                       wiring; each service calls it once, then registers
                       its own additions on top
  azure_ad.py           AzureAdTokenClient (outbound client-credentials token
                       fetch, cached) + verify_bearer_token (inbound JWT
                       validation) -- the gateway-auth pattern the other
                       internal services here use, reimplemented directly
                       against httpx2/redis.asyncio/PyJWT rather than the
                       private `framework` package kube-tools-api uses (this
                       project uses pydepi for DI instead)
  clients/              OuraApiClient, OuraOAuthClient, WebhookSubscriptionClient
  services/              TokenService, CacheService, OuraService, OAuthService, AuditService
  repositories/          TokenRepository (oauth_tokens), AuditRepository (audit_events)
  infrastructure/        crypto (Fernet), logging (redaction, correlation id),
                       Mongo/Redis client factories
  models/                OuraToken/TokenGrant, Resource/ResourceSpec, DateRange,
                       the OuraError hierarchy, AuditEvent
```

What's deliberately **not** here: anything MCP-specific (the tool surface,
MCP OAuth, webhooks, the admin HTTP API) or sync-specific (the snapshot
schema, the sync orchestration). Those stay in each service, since nothing
else needs them -- see `oura-mcp`'s and `oura-sync`'s own READMEs.

## Why oauth_tokens is a shared contract

`TokenRepository` defines the `oauth_tokens` document shape (encrypted
access/refresh token pair, `account_id`, `version`, atomic single-write
rotation). `oura-auth` (a separate one-off CLI, kept intentionally outside
this shared tree since it has to run before either service is deployed)
mirrors this shape by hand; `oura-mcp` and `oura-sync` both import this
module directly, so they can never drift from each other.

## Testing

```bash
pytest -q
```

Runs standalone (`pytest.ini` sets `pythonpath = src`) without either
consuming service's own dependencies installed, since oura-common's own
`pyproject`-free test run only needs what's in this repo.
