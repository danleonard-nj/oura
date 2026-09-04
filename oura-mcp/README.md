# oura-mcp

A Model Context Protocol server that exposes your Oura Ring data, read-only,
over Streamable HTTP. Deploy it behind a Kubernetes ingress and any MCP client
— Claude Code, ChatGPT, or anything else that speaks the protocol — can
ask questions about your sleep, readiness, activity, stress and workouts.
Clients that only offer OAuth, such as ChatGPT, are supported too --
see [MCP OAuth](#mcp-oauth).

```
MCP client ──▶ https://oura.example.com/mcp ──▶ Oura API v2
                     (this service)
```

---

## Contents

1. [Architecture](#architecture)
2. [The Oura client](#the-oura-client)
3. [Local setup](#local-setup)
4. [Registering an Oura OAuth application](#registering-an-oura-oauth-application)
5. [Scopes](#scopes)
6. [Connecting an account](#connecting-an-account)
7. [Environment variables](#environment-variables)
8. [Running locally](#running-locally)
9. [Connecting an MCP client](#connecting-an-mcp-client)
10. [MCP tools](#mcp-tools)
11. [Docker](#docker)
12. [Helm](#helm)
13. [Kubernetes secrets](#kubernetes-secrets)
14. [Ingress considerations](#ingress-considerations)
15. [Webhooks](#webhooks)
16. [Token refresh and rotation](#token-refresh-and-rotation)
17. [Testing](#testing)
18. [Security](#security)
19. [MCP OAuth](#mcp-oauth)
20. [Limitations](#limitations)

---

## Architecture

One Starlette application serves everything. There is no second web framework:
the MCP SDK already brings Starlette, the HTTP surface is small, and a single
app means a single lifespan with no cross-framework bridging.

| Path                                           | Served by                                  | Auth                                       |
| ---------------------------------------------- | ------------------------------------------ | ------------------------------------------ |
| `/mcp`                                         | Official MCP Streamable HTTP app           | `MCP_API_KEY` **or** an OAuth access token |
| `/health`                                      | Liveness                                   | none                                       |
| `/ready`                                       | Readiness                                  | none                                       |
| `/oauth/authorize`, `/oauth/callback`          | Our OAuth2 flow **as a client of Oura**    | `state` nonce                              |
| `/authorize`, `/token`, `/register`, `/revoke` | Us **as an OAuth2 server** for MCP clients | PKCE, DCR, consent                         |
| `/.well-known/oauth-*`                         | OAuth discovery metadata                   | none (public by design)                    |
| `/consent`                                     | Approval screen for an MCP authorization   | `MCP_OWNER_PASSWORD`                       |
| `/webhooks/oura`                               | Oura webhooks                              | Oura challenge + HMAC signature            |
| `/api/*`                                       | Admin status, audit, webhook subscriptions | `Authorization: Bearer $MCP_API_KEY`       |

Two OAuth roles coexist and are easy to confuse. Under `/oauth/*` we are a
_client_ of Oura, holding a token for your ring data. At the root
(`/authorize`, `/token`, ...) we are a _server_, issuing tokens to MCP clients.
The prefixes keep them apart; only the latter appears when
`MCP_OWNER_PASSWORD` is set.

```
src/oura_mcp/
  app.py              ASGI composition root, lifespan, route table
  config.py           typed configuration, validated at startup
  container.py        DEPI composition root — every registration lives here
  api/                thin route handlers, auth middleware, consent screen
  mcp/                MCP server construction and the tool surface
  services/           all business and integration logic
  repositories/       Mongo persistence (tokens, audit, webhooks, oauth)
  clients/            async Oura HTTP clients (httpx2)
  infrastructure/     logging, crypto, Mongo, Redis
  models/             domain types, date validation, result shapes
```

Entry points stay thin — accept input, call an injected service, return the
result. Dependencies are wired once in `container.py` using
[pydepi](https://github.com/danleonard-nj/depi) and reach their consumers
through ordinary constructors.

Three composition details are load-bearing:

**The MCP app is attached with `Route`, not `Mount`.** `Mount("/mcp", app)`
strips the prefix, leaving the sub-app matching an empty path, which Starlette
answers with a `307` redirect to `/mcp/`. Browsers follow that; MCP clients do
not. `Route` passes the path through unchanged so the SDK's own `/mcp` route
matches directly. There is a test pinning this.

**The session manager is started from the root lifespan.** A nested ASGI app
never has its own lifespan run, and `streamable_http_app()` puts the session
manager there. `app.py` calls `server.session_manager.run()` itself.

**The DI provider is built inside the lifespan, not at import.**
`AsyncMongoClient` and `redis.asyncio.Redis` bind to the loop that creates
them; building at import time binds them to a throwaway loop.

### Data flow for one tool call

```
tools/call
  └─ tool fn (thin)          resolve range → call service → return typed model
     └─ OuraService          cache lookup → token → await → translate errors
        ├─ CacheService      Redis, generation-counter invalidation
        ├─ TokenService      Redis lock → re-read → refresh → atomic persist
        └─ OuraApiClient     httpx2, pooled and shared across the app
     └─ AuditService         Mongo row + structured stdout, never fatal
```

### What is stored where

**Mongo** holds durable operational data only: `oauth_tokens` (your encrypted
Oura tokens), `audit_events`, `webhook_events`, plus `oauth_clients` and
`oauth_refresh_tokens` when MCP OAuth is on.

**Health data is deliberately not copied into Mongo.** Oura is the system of
record, Redis already absorbs repeat reads, and webhooks keep that cache
honest. Mirroring the dataset would add a sync problem, a correctness problem
and a pile of personal health information at rest, in exchange for nothing this
service needs. If `oura-sync` later wants a local historical store, that is a
separate concern with its own schema and its own justification.

**Redis** does four jobs: the distributed Oura-token refresh lock (mandatory),
response caching (optional), webhook deduplication (optional), and the
short-lived halves of MCP OAuth — pending authorizations, authorization codes
and access tokens. Only the refresh lock is required for correctness.

---

## The Oura client

Written directly on `httpx2`, the same async client the MCP SDK already brings.
Every Oura collection is the same shape -- `GET /v2/usercollection/{segment}`
with `start_date`/`end_date` and `next_token` pagination -- so the whole API
surface is one map plus one pagination loop, about eighty lines in
`clients/oura_client.py`.

It previously used [`hedgertronic/oura-ring`](https://github.com/hedgertronic/oura-ring),
which was dropped because it is built on synchronous `requests`. That cost:

- a `ThreadPoolExecutor` to keep blocking calls off the event loop, plus
  thread-confined session handling so a token refresh could not race a request;
- four collections it does not implement (`daily_resilience`,
  `daily_cardiovascular_age`, `vO2_max`, `ring_battery_level`), already
  hand-rolled alongside it;
- no access to the `Response`, since it calls `raise_for_status()` internally --
  so rate-limit headers had to be recovered from the exception.

All of that is gone. One pooled `AsyncClient` is a DI singleton, so connection
reuse and the connection ceiling apply across the whole application instead of
per worker thread. `OURA_EXECUTOR_WORKERS` survives as the `max_connections`
limit -- Oura rate-limits per token and per application, so a ceiling on
concurrent upstream requests is still the useful behaviour, just without the
threads.

Errors reach `OuraService` as `httpx2.HTTPStatusError` (or a transport error)
and are translated there, which keeps this module a pure transport concern.

### VO2 max is spelled two ways

The collection path is `vO2_max`; the webhook `data_type` for the same data is
`vo2_max`. Both spellings are in the codebase deliberately -- `RESOURCE_PATHS`
holds the former, `WEBHOOK_DATA_TYPE_RESOURCES` the latter.

---

## Local setup

Requires Python 3.12+, plus a reachable MongoDB and Redis.

```bash
cd oura-mcp
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Generate the two secrets:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The first is `TOKEN_ENCRYPTION_KEY`, the second `MCP_API_KEY` (and a third for
`OURA_WEBHOOK_VERIFICATION_TOKEN`).

> **Port-forwarded Mongo replica set?** Add `?directConnection=true` to
> `MONGODB_URI`. Without it the driver reads the replica set config, discovers
> internally-advertised hostnames like `mongo-internal.default.svc.cluster.local`,
> and cannot resolve them from your machine.

---

## Registering an Oura OAuth application

**This part you have to do by hand.** At
<https://cloud.ouraring.com/oauth/applications>:

1. Create an application. Note the **Client ID** and **Client Secret**.
2. Add a **Redirect URI** for each environment you will use. These must match
   `OURA_REDIRECT_URI` byte for byte:
   - local bootstrap: `http://localhost:8000/oauth/callback`
   - production: `https://<your-ingress-host>/oauth/callback`
3. Select the scopes below.
4. After deploying, register webhook subscriptions — see
   [Webhooks](#webhooks). There is no console UI for this; it is an API call.

Two things worth knowing:

- **Personal access tokens were deprecated in December 2025.** OAuth2 is the
  only option, which is why this service carries a full token lifecycle.
- **An unapproved application is limited to 10 users.** Fine for personal use;
  request approval from Oura if that changes.

---

## Scopes

| Scope | Grants | Tools |
| --- | --- | --- |
| `daily` | Sleep, readiness, activity, stress | `oura_summary`, `oura_sleep`, `oura_readiness`, `oura_activity`, `oura_stress` |
| `heartrate` | Heart rate time series | `oura_heart_rate` |
| `workout` | Workouts | `oura_workouts` |
| `session` | Guided and unguided sessions | `oura_sessions` |
| `tag` | User-entered tags | `oura_tags` |
| `spo2` | Blood oxygen during sleep | `oura_spo2` |
| `personal` | Age, sex, height, weight | `oura_personal_info` |
| `email` | Email address | — |
| **`stress`** | Resilience | `oura_resilience` |
| **`heart_health`** | Cardiovascular age, VO2 max | `oura_cardiovascular_age`, `oura_vo2_max` |
| **`ring_configuration`** | Ring model, firmware, battery | `oura_ring_info` |

Default: all of them. Users can deselect scopes individually at the consent
screen, so `oura-auth` reports what was actually granted.

### Three of these are undocumented

The last three appear **nowhere in Oura's OpenAPI spec**, which advertises only
`email personal daily heartrate workout tag session spo2Daily`. They are real
and required, and Oura signals a missing one with **`401`, not `403`**:

```json
{ "detail": "Token is not authorized access stress scope." }
```

That matters twice over. A `401` normally means an expired token, so without
special handling the service would refresh and retry on every call to an
endpoint it can never reach. `OuraService` therefore inspects the body and
turns a scope-shaped `401` into a permission error naming the scope — no
refresh, no retry, and a message that says what to do.

### Ungranted scopes are refused locally

Because a missing scope is a `401`, an ungranted collection would otherwise
cost a round trip on every call and surface as an auth failure. `oura_summary`
fans out over six collections, so one ungranted scope taxed every summary.

`OuraService` now compares the resource's required scope against what the
stored token records Oura actually granted, and refuses locally -- no request,
no retry, and `oura_summary` reports it in `unavailable` alongside the data
that did come back.

Two deliberate properties:

- **Unknown means no opinion.** A token record with no scopes (an older
  connection) blocks nothing; the request goes out and Oura decides.
- **The map is empirical.** Every entry was verified by probing the live API,
  because the spec is wrong about three of them. If Oura ever relaxes a
  requirement, the preflight would refuse something now permitted -- reconnect
  with the wider scope set, or clear the map entry.

If an account was connected before these were added to `OURA_SCOPES`, four
tools return a permission error until it reconnects. Check what was granted:

```bash
curl -s -H "Authorization: Bearer $MCP_API_KEY"   https://oura.dan-leonard.com/api/status | jq .scopes
```

---

## Connecting an account

Either way works; both write the same encrypted document.

**Bootstrap tool** (recommended, works before deployment):

```bash
cd ../oura-auth && python oura_auth.py connect
```

**Through the running service:**

```bash
open https://oura.example.com/oauth/authorize
```

Verify:

```bash
curl -s -H "Authorization: Bearer $MCP_API_KEY" https://oura.example.com/api/status | jq
```

The token is stored encrypted and rotated automatically from then on. Neither
the API nor any MCP tool ever returns token material.

---

## Environment variables

Required — the process refuses to start without them, naming what is missing:

| Variable               | Purpose                                        |
| ---------------------- | ---------------------------------------------- |
| `OURA_CLIENT_ID`       | OAuth application client id                    |
| `OURA_CLIENT_SECRET`   | OAuth client secret; also the webhook HMAC key |
| `OURA_REDIRECT_URI`    | Must match the Oura application exactly        |
| `MCP_API_KEY`          | Bearer token for `/mcp` and `/api/*`           |
| `TOKEN_ENCRYPTION_KEY` | Fernet key; urlsafe base64, 32 bytes           |
| `MONGODB_URI`          | Existing cluster                               |
| `REDIS_URL`            | Existing instance                              |

Important optional ones:

| Variable                          | Default            | Purpose                                                                                          |
| --------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------ |
| `PUBLIC_HOST`                     | —                  | Ingress hostname, no scheme. **Required in production**; see [Ingress](#ingress-considerations). |
| `MONGODB_DATABASE`                | `oura_mcp`         |                                                                                                  |
| `OURA_SCOPES`                     | all read scopes    | Space-separated                                                                                  |
| `OURA_ACCOUNT_ID`                 | `default`          | Namespaces tokens, cache keys, locks, audit                                                      |
| `MCP_STATELESS`                   | `true`             | Stateless transport; no sticky sessions                                                          |
| `MCP_PATH`                        | `/mcp`             |                                                                                                  |
| `OURA_WEBHOOK_VERIFICATION_TOKEN` | —                  | Required to accept webhook subscriptions                                                         |
| `LOG_LEVEL`                       | `INFO`             |                                                                                                  |
| `HOST` / `PORT`                   | `0.0.0.0` / `8000` |                                                                                                  |

Tuning knobs (executor size, timeouts, retry budget, range caps, cache TTLs,
lock timings) are listed with their defaults in `.env.example`.

---

## Running locally

```bash
hypercorn "oura_mcp.app:create_app()" --bind 0.0.0.0:8000 --reload
```

Then:

```bash
curl -s localhost:8000/health | jq
curl -s localhost:8000/ready | jq
```

`/ready` reports Mongo and Redis individually, so a red probe says which
dependency is down.

---

## Connecting an MCP client

The server speaks Streamable HTTP with a bearer token — standard for all of
these. Substitute your hostname and key.

> **Where does the API key come from?** You generate it. Nobody issues it —
> it is a shared secret you invent once, and the only thing protecting your
> health data from the public internet.
>
> ```bash
> python -c "import secrets; print(secrets.token_urlsafe(48))"
> ```
>
> That single value goes in exactly three places and must match in all of them:
> the `MCP_API_KEY` key of the Kubernetes Secret, the server's environment
> (read from that Secret at boot), and the `Authorization` header your MCP
> client sends. The server compares them with `hmac.compare_digest` on every
> `/mcp` and `/api/*` request; there is no registration step or handshake.
> Rotating it means updating the Secret, restarting the pods, and updating each
> client.

**Claude Code**

```bash
claude mcp add --transport http oura https://oura.example.com/mcp \
  --header "Authorization: Bearer $MCP_API_KEY"
```

**Config-file clients** (`mcp.json.example` in this repo is a fill-in-the-blanks
copy; `mcp.json` is gitignored so a filled-in one never gets committed):

```json
{
  "mcpServers": {
    "oura": {
      "type": "http",
      "url": "https://oura.example.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_MCP_API_KEY" }
    }
  }
}
```

**Claude Desktop / Claude web** — add it as a Custom Connector with the same
URL and header.

**ChatGPT** — Settings → Connectors → Custom. Its dialog offers only _OAuth_
or _no auth_, with no field for a static header, so set `MCP_OWNER_PASSWORD`
and choose **OAuth**; the server handles discovery and registration itself.
Enter `https://oura.dan-leonard.com/mcp` as the server URL. See
[MCP OAuth](#mcp-oauth).

Check it end to end:

```bash
curl -sS https://oura.example.com/mcp \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
       "protocolVersion":"2025-06-18","capabilities":{},
       "clientInfo":{"name":"curl","version":"1"}}}'
```

---

## MCP tools

Sixteen tools, all read-only. There are no write operations to Oura anywhere in
the MCP surface.

| Tool                      | Returns                                                                             | Default range  |
| ------------------------- | ----------------------------------------------------------------------------------- | -------------- |
| `oura_summary`            | Sleep + readiness + activity + stress + resilience + SpO2 + workouts, joined by day | 7 days         |
| `oura_sleep`              | Daily sleep summaries and scores                                                    | 7 days         |
| `oura_readiness`          | Daily readiness scores and contributors                                             | 7 days         |
| `oura_activity`           | Steps, calories, MET minutes, activity score                                        | 7 days         |
| `oura_stress`             | High-stress vs recovery minutes                                                     | 7 days         |
| `oura_spo2`               | Blood oxygen during sleep                                                           | 7 days         |
| `oura_resilience`         | Resilience level and contributors                                                   | 14 days        |
| `oura_cardiovascular_age` | Estimated vascular age                                                              | 30 days        |
| `oura_vo2_max`            | VO2 max readings                                                                    | 90 days        |
| `oura_heart_rate`         | Heart rate samples                                                                  | 1 day (max 14) |
| `oura_workouts`           | Workouts                                                                            | 14 days        |
| `oura_sessions`           | Meditation, breathing, naps                                                         | 14 days        |
| `oura_tags`               | User-entered tags                                                                   | 14 days        |
| `oura_ring_info`          | Ring model, size, firmware                                                          | 30 days        |
| `oura_personal_info`      | Age, sex, height, weight                                                            | —              |
| `oura_query`              | Any collection by name, for the long tail                                           | 7 days         |

`oura_summary` is the one to reach for first: it fans out across six
collections concurrently and joins them on the calendar day, so "how did I
sleep this week and did it track with my training" is one call rather than
seven. A resource the account cannot see — ungranted scope, or a metric behind
a subscription — is reported in `unavailable` rather than failing the summary.

`oura_query` covers the collections without a dedicated tool: `sleep` (raw
per-period sessions), `sleep_time`, `tag` (legacy format), `rest_mode_period`
and `ring_battery_level`. Keeping them behind one tool avoids five more
schemas for rarely-asked questions.

Every tool returns a typed Pydantic model, so clients get real structured
content and a real output schema — not stringified JSON.

**Schema footprint:** the full `tools/list` payload is ~30 KB (~7.5k tokens),
of which ~17 KB is the `RecordSet` output schema repeated per tool. If that
matters more than discoverability for your client, collapsing the named tools
into `oura_query` and `oura_summary` alone would cut it to roughly 4 KB.

### Dates

`start_date` and `end_date` are ISO-8601 `YYYY-MM-DD` and inclusive. Omit both
for the tool's default window; give only one and the other is derived.

Ranges are validated before anything reaches Oura: inverted ranges, malformed
dates, start dates in the future and over-large spans are all refused with a
message saying what to do instead. The global cap is 180 days
(`OURA_MAX_RANGE_DAYS`); heart rate is capped at 14 (`OURA_MAX_HEARTRATE_DAYS`)
because it returns hundreds of samples per day. A range is always fetched as
one paginated request, never one request per day.

Defaults resolve in **UTC**, so a container's local timezone cannot change
which day a call means. Oura keys daily records by the user's local `day`, so
near midnight a default range may include a day Oura has no data for yet —
harmless, and safer than silently cutting the range short.

---

## Docker

```bash
docker build -t oura-mcp:0.1.0 .
```

```bash
docker run --rm -p 8000:8000 --env-file .env oura-mcp:0.1.0
```

Multi-stage, `python:3.12-slim`, dependencies installed in their own cached
layer, runs as non-root uid/gid `10001`, no development server. Hypercorn is
PID 1 via `exec`, so it receives `SIGTERM` directly from the kubelet and drains
connections instead of being killed at the end of the grace period. The
container listens on `$PORT` (default 8000).

Tag and push to your registry:

```bash
docker tag oura-mcp:0.1.0 azureks.azurecr.io/oura/oura-mcp:0.1.0
docker push azureks.azurecr.io/oura/oura-mcp:0.1.0
```

---

## Helm

The chart **does not deploy MongoDB or Redis**; it expects the ones already in
your cluster and takes their URLs from a Secret.

```bash
helm upgrade --install oura-mcp ./helm/oura-mcp \
  --namespace default \
  --set image.tag=0.1.0 \
  --set existingSecret=oura-mcp \
  --set ingress.enabled=true \
  --set ingress.hostname=oura.example.com \
  --set ingress.tls.enabled=true \
  --set ingress.tls.clusterIssuer=letsencrypt-prod
```

Or just run `deploy.bat`, which tags the image with a timestamp plus the short
commit hash, pushes both that tag and `latest`, and upgrades the release in the
`oura` namespace using `values.prod.yaml`.

Dry run first:

```bash
helm template oura-mcp ./helm/oura-mcp --set ingress.enabled=true \
  --set ingress.hostname=oura.example.com | less
```

Rollback:

```bash
helm rollback oura-mcp
```

`PUBLIC_HOST` and `OURA_REDIRECT_URI` default to values derived from
`ingress.hostname`, so they do not need repeating. RollingUpdate with
`maxUnavailable: 0`, liveness on `/health`, readiness on `/ready`, resource
requests and limits, pod and container security contexts, and an optional HPA
and PodDisruptionBudget are all in `values.yaml`.

### api-gateway

The admin endpoints are also mapped through the shared gateway
(`api-gateway/services/gateway/mapping/oura-mcp.json`) under `/api/oura/*`, so
`https://api.dan-leonard.com/api/oura/status` works alongside the other
services. The gateway forwards request headers, so `MCP_API_KEY` still applies
end to end. Mappings are discovered with `os.listdir('mapping')` — dropping the
file in is the whole registration.

`/mcp` is mapped too, at `/api/oura/mcp`, and marked `"streaming": true`. The
gateway proxy buffers by default — it awaited the whole upstream response and
returned `service_response.content` — which would have stalled every tool call
until it completed and broken incremental SSE delivery. It now supports
opt-in streaming per route; see that repo's notes on the flag.

Because the gateway rewrites `Host` to the cluster-internal service name, that
name has to be on this service's DNS-rebinding allowlist:

```yaml
config:
  mcpExtraAllowedHosts: oura-mcp.oura.svc.cluster.local
```

Without it the transport answers `421`. `values.prod.yaml` sets it.

Either address works for clients — `https://oura.dan-leonard.com/mcp` direct,
or `https://api.dan-leonard.com/api/oura/mcp` through the gateway. The direct
host is one hop shorter; the gateway route keeps everything behind one
hostname.

`/webhooks/oura` and `/oauth/*` stay unmapped, for different reasons: the
webhook signature is an HMAC over the exact request bytes, and the OAuth
redirect URI is registered with Oura against the service's own hostname.

### Scaling

`replicaCount` defaults to 1 but nothing assumes it.

- The MCP transport runs stateless (`MCP_STATELESS=true`), so no session state
  lives in a replica's memory and any replica can serve any request.
- **Sticky sessions are not required.** The Streamable HTTP spec permits
  server-assigned session ids, which would need affinity; stateless mode does
  not use them. The cost is no server-initiated SSE streams between calls,
  which these tools do not use.
- State that must survive a replica lives in Mongo (tokens, audit, webhook
  history) or Redis (locks, cache, dedupe).
- OAuth refresh is serialised across replicas by a Redis lock.

---

## Kubernetes secrets

Create the Secret out of band and point the chart at it with `existingSecret`.
The chart can render its own from inline values, but those end up in the Helm
release history in plain text.

The simplest route is an env file:

```bash
cp .env.prod.example .env.prod    # then fill it in
push-secret.bat                   # validate, create the Secret, restart the pods
push-secret.bat --overwrite       # validate, update the Secret, restart the pods
```

`.env.prod` is gitignored. The script validates before pushing, because
`kubectl create secret --from-env-file` has two behaviours that fail silently:

- **Quotes are not stripped.** `TOKEN_ENCRYPTION_KEY="abc..."` stores the
  quotes as part of the value, and Fernet rejects the key at startup.
- **Trailing whitespace is preserved.** A stray space after `MCP_API_KEY`
  means every request 401s, with nothing in the logs to explain it.

It also rejects ConfigMap keys such as `PUBLIC_HOST`. The deployment lists the
Secret _after_ the ConfigMap in `envFrom`, so anything in this file silently
overrides the chart, and a stale `PUBLIC_HOST` would quietly break OAuth
discovery. Keep the file to secrets only.

Run the check alone at any time:

```bash
python scripts/validate_env_file.py .env.prod
```

Or use explicit flags instead, if you prefer:

```bash
kubectl create secret generic oura-mcp   --namespace oura   --from-literal=OURA_CLIENT_ID='...'   --from-literal=OURA_CLIENT_SECRET='...'   --from-literal=MCP_API_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"   --from-literal=TOKEN_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"   --from-literal=MONGODB_URI='mongodb://user:pass@mongo-internal.default.svc.cluster.local:27017/?authSource=admin'   --from-literal=REDIS_URL='redis://redis-internal.default.svc.cluster.local:6379/0'   --from-literal=OURA_WEBHOOK_VERIFICATION_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"   --from-literal=MCP_OWNER_PASSWORD="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
```

`MCP_OWNER_PASSWORD` is optional: omit it and the server runs bearer-token
only. Include it to enable [MCP OAuth](#mcp-oauth), which is what ChatGPT
needs. Read it back when you need it at the consent screen:

```bash
kubectl get secret oura-mcp -n oura -o jsonpath='{.data.MCP_OWNER_PASSWORD}' | base64 -d
```

A Secret change on its own restarts nothing — `envFrom` values are read at
container start. `push-secret.bat` does the rollout restart for you.

**Keep `TOKEN_ENCRYPTION_KEY` safe.** Rotating it makes every stored token
undecryptable and the account has to reconnect. The service says exactly that
if it happens rather than failing obscurely.

---

## Ingress considerations

Streamable HTTP holds a response open while a tool runs. A proxy that buffers
responses or times out early will cut the stream mid-call, which surfaces as a
client that hangs and then fails.

Two rules, whatever controller you run:

1. **Generous read/send timeouts** — well above your slowest tool call.
2. **Response buffering off** — the proxy must stream, not accumulate.

The chart ships ingress-nginx annotations by default. Replace them wholesale
for another controller:

**ingress-nginx** (chart default)

```yaml
nginx.ingress.kubernetes.io/proxy-read-timeout: '3600'
nginx.ingress.kubernetes.io/proxy-send-timeout: '3600'
nginx.ingress.kubernetes.io/proxy-buffering: 'off'
nginx.ingress.kubernetes.io/proxy-request-buffering: 'off'
nginx.ingress.kubernetes.io/proxy-body-size: '4m'
```

**Traefik** — set `respondingTimeouts.readTimeout` on the entrypoint and use a
`ServersTransport` with a raised `forwardingTimeouts.responseHeaderTimeout`.
Traefik does not buffer responses by default, so leave buffering middleware off.

**HAProxy ingress** — raise `timeout-client` and `timeout-server`.

**Cloud L7 load balancers** (AWS ALB, GCP, Azure Application Gateway) — raise
the backend idle/response timeout; the common 30–60s default is too low.

`PUBLIC_HOST` must be set to the ingress hostname. The MCP SDK's DNS-rebinding
protection validates the `Host` header against an allowlist and defaults to
localhost only; without `PUBLIC_HOST` every production request gets a `421`.
The chart derives it from `ingress.hostname` automatically. `PUBLIC_SCHEME`
follows `ingress.tls.enabled` and is what OAuth metadata URLs are built from.

### TLS

This cluster has **no ClusterIssuers** — each namespace carries its own
cert-manager `Issuer` named `letsencrypt-prod`, and ingresses are annotated
`cert-manager.io/issuer`, not `cert-manager.io/cluster-issuer`. A
`cluster-issuer` annotation here fails silently: the Certificate is created and
simply never becomes ready.

The chart therefore renders the namespaced Issuer itself, matching the existing
ones (ACME v2 production, HTTP-01 solver over the `nginx` class):

```yaml
ingress:
  className: nginx
  tls:
    enabled: true
    issuerName: letsencrypt-prod
    createIssuer: true # false if the namespace already has one
    email: you@example.com
```

Ingresses in this cluster carry both `ingressClassName: nginx` and the legacy
`kubernetes.io/ingress.class` annotation; the chart emits both.

---

## Webhooks

Oura's recommended pattern, which this implements: one historical fetch when an
account connects, then webhooks for ongoing changes.

`GET /webhooks/oura` answers the subscription challenge — it compares
`verification_token` in constant time against
`OURA_WEBHOOK_VERIFICATION_TOKEN` and echoes `{"challenge": ...}`.

`POST /webhooks/oura` accepts events. The signature is
`HMAC-SHA256(client_secret, x-oura-timestamp + raw_body)`, hex, uppercase,
compared in constant time against `x-oura-signature`. Verification uses the
**raw request bytes** — re-serialising the parsed JSON changes them and breaks
the comparison. Invalid signatures get a `401` and an audit record.

A valid event is deduplicated in Redis, the affected cache generations are
bumped, and the event is recorded in Mongo. Oura requires acknowledgement
within 10 seconds; this is a couple of Redis commands and one insert, so
**there is no background worker, task queue, Celery or Kafka** — none is
warranted by the actual work.

Cache invalidation is a per-resource **generation counter**: a webhook does one
`INCR`, every key minted under the old generation becomes unreachable, and
Redis expires them on its own. `O(1)`, versus scanning an unbounded keyspace.

### Managing subscriptions

Register subscriptions once the endpoint is live — Oura verifies the callback
URL before activating. One subscription per `event_type` / `data_type` pair.

The admin API wraps this so you never handle the client secret by hand. It
fills in the callback URL from `PUBLIC_HOST` and the verification token from
the environment:

```bash
# Create — event_type defaults to "update"
curl -sX POST https://oura.example.com/api/webhooks/subscriptions \
  -H "Authorization: Bearer $MCP_API_KEY" -H "Content-Type: application/json" \
  -d '{"data_type":"daily_sleep"}' | jq

# List, with expiry
curl -s -H "Authorization: Bearer $MCP_API_KEY" \
  https://oura.example.com/api/webhooks/subscriptions | jq

# Renew, and cancel
curl -sX PUT    -H "Authorization: Bearer $MCP_API_KEY" \
  https://oura.example.com/api/webhooks/subscriptions/<id>/renew | jq
curl -sX DELETE -H "Authorization: Bearer $MCP_API_KEY" \
  https://oura.example.com/api/webhooks/subscriptions/<id>
```

These are administrative HTTP endpoints, not MCP tools, and they write only to
Oura's _subscription_ resource — never to health data. The MCP surface stays
read-only.

**Subscriptions expire.** Each carries an `expiration_time`, and Oura stops
delivering once it passes. Nothing in this service renews them automatically —
check the list periodically and call the renew endpoint, or drive it from a
CronJob. Stale data served from cache with no invalidation is the symptom of a
lapsed subscription.

Valid `data_type` values (Oura's enum): `tag`, `enhanced_tag`, `workout`,
`session`, `sleep`, `daily_sleep`, `daily_readiness`, `daily_activity`,
`daily_spo2`, `sleep_time`, `rest_mode_period`, `ring_configuration`,
`daily_stress`, `daily_cardiovascular_age`, `daily_resilience`, `vo2_max`,
`meal`. `event_type` is `create`, `update` or `delete`.

Note that the webhook `data_type` is not always the collection path: VO2 max is
`vo2_max` here but `vO2_max` as a collection. `meal` has no v2 collection and
is accepted but maps to nothing.

Recently received events:

```bash
curl -s -H "Authorization: Bearer $MCP_API_KEY" \
  https://oura.example.com/api/webhooks/events | jq
```

Oura retries failures about 10 times over roughly an hour, and a `410` response
cancels a subscription. Only sanitized metadata is logged — event type, data
type, object id, whether a user id was present. Never the user id itself.

---

## Token refresh and rotation

**Oura refresh tokens rotate and are single-use.** Refresh with a token, and
that token is dead. If two replicas refresh from the same stored token, one
result is thrown away and the account can end up with no valid refresh token at
all — requiring a manual reconnect.

`TokenService.refresh` prevents that:

1. **Take a Redis lock** scoped to the account
   (`oura:lock:refresh:{account_id}`), with a bounded wait.
2. **Re-read the persisted token** under the lock — another replica may have
   refreshed while we queued.
3. **If it is now valid, use it** and refresh nothing.
4. **Otherwise call Oura**, on a worker thread.
5. **Persist both halves in one atomic update**, incrementing `version`, before
   releasing the lock. No reader can ever see a new access token beside a dead
   refresh token.
6. **Retry the original operation once** after a successful refresh.

**If Redis is unavailable, the refresh is refused**, not attempted. A failed
request is recoverable; a burned refresh token is not. Callers get a `503`-class
`TokenRefreshUnavailableError` explaining why.

The lock TTL (`REFRESH_LOCK_TTL_SECONDS`, default 30s) bounds how long a
crashed replica can block others — Redis expires it regardless of what happened
to the holder, so a stuck refresh can never become permanent. Tokens are also
refreshed slightly early (`TOKEN_EXPIRY_SKEW_SECONDS`, default 120s) so a token
cannot expire mid-flight.

Ten concurrent callers racing on an expired token result in exactly one call to
Oura. There is a test for that, and for the Redis-outage path.

### Upstream error handling

| Upstream             | Behaviour                                                 |
| -------------------- | --------------------------------------------------------- |
| 400                  | `OuraValidationError`, no retry                           |
| 401                  | Refresh under lock, retry **once**                        |
| 403                  | `OuraPermissionError` naming the required scope, no retry |
| 404                  | `OuraNotFoundError`, no retry                             |
| 429                  | Retry honouring `Retry-After`, within the attempt budget  |
| 5xx                  | Exponential backoff with jitter, capped at 8s             |
| Timeout / connection | Same as 5xx                                               |

Bounded at `OURA_MAX_ATTEMPTS` (default 3). Nothing retries indefinitely.
Errors are translated into caller-safe messages — no tokens, no headers, no
upstream response bodies — before they leave the service layer.

---

## Testing

```bash
pytest -q
```

131 tests, no Oura account, Mongo or Redis required — every external system is
faked at the constructor boundary.

The integration tests build the **real** app through `create_app()` and drive
it over the actual ASGI stack, swapping only Mongo, Redis and the blocking Oura
client at the container. If a service stops taking a dependency through its
constructor, they break.

Covered, among others: blocking Oura calls never run on the event loop; every
HTTP status maps to the right domain error; refresh-and-retry-once on 401;
bounded retries; `Retry-After` handling; token rotation persisted atomically;
the Redis refresh lock; ten concurrent refreshes consuming one refresh token;
safe failure when Redis is down; tokens absent from logs, reprs, audit records
and error messages; encryption at rest; webhook signature accept/reject and
tampering; deduplication; cache hit/miss/bypass/invalidation; MCP bearer auth;
the `/mcp` no-redirect guarantee; DNS-rebinding rejection; and health and
readiness behaviour.

---

## Security

- **`/mcp` requires a bearer credential** — either `MCP_API_KEY`, compared
  with `hmac.compare_digest`, or an OAuth access token this server issued. The
  middleware wraps only the MCP app, so health probes, the Oura OAuth callback
  and the webhook keep their own verification models.
- **Health endpoints are unauthenticated** so probes work; they expose no data.
- **`/api/*` requires the same bearer token.**
- **Tokens are encrypted at rest** with Fernet, and never returned by any
  endpoint or tool.
- **Nothing sensitive is logged.** The structured formatter redacts by field
  name recursively and by pattern in message text — bearer headers, `token=`
  assignments, client secrets, authorization codes, signatures. Token models
  override `__repr__`. There are tests asserting each of these.
- **Oura payloads are never logged by default.** They are personal health
  information; logs carry counts, durations and statuses.
- **Webhook signatures and verification tokens** are compared in constant time.
- **DNS-rebinding protection** is enabled with an explicit host allowlist.
- **Container** runs non-root with a read-only root filesystem, all
  capabilities dropped, and no privilege escalation.
- **Correlation ids** flow from the inbound request through tool execution,
  Oura calls and Mongo audit rows, and come back in `x-correlation-id`.

---

## MCP OAuth

The `/mcp` endpoint accepts **either** credential:

| Credential                                  | For                                             |
| ------------------------------------------- | ----------------------------------------------- |
| `Authorization: Bearer $MCP_API_KEY`        | Clients that let you set a header (Claude Code) |
| An OAuth access token issued by this server | Clients that only do OAuth discovery (ChatGPT)  |

The static key is checked first and costs nothing; an OAuth token falls through
to a store lookup. Adding OAuth did not change the header path.

### Enabling it

Set one secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # -> MCP_OWNER_PASSWORD
```

With `MCP_OWNER_PASSWORD` unset, none of the OAuth routes are mounted and the
server behaves exactly as before. With it set, these appear:

```
/.well-known/oauth-protected-resource/mcp    RFC 9728 resource metadata
/.well-known/oauth-authorization-server      RFC 8414 AS metadata
/authorize   /token   /register   /revoke    OAuth 2.1 + RFC 7591
/consent                                     the approval screen
```

`PUBLIC_HOST` and `PUBLIC_SCHEME` must be right: every URL in those metadata
documents is built from them, and a client follows what it is told.

### The flow

1. ChatGPT fetches `/.well-known/oauth-protected-resource/mcp`, which names
   this server as its own authorization server, and follows it to the AS
   metadata. A `401` from `/mcp` also advertises the metadata URL in its
   `WWW-Authenticate` header, so discovery works from a cold start.
2. It registers itself via `/register` — dynamic client registration, because
   it cannot be pre-registered.
3. It sends your browser to `/authorize`, which issues nothing and redirects to
   `/consent`.
4. You enter `MCP_OWNER_PASSWORD`. Only then is an authorization code minted and
   the browser sent back to the client.
5. The client exchanges the code at `/token` with PKCE and receives an access
   token and a refresh token.

### Why a password rather than accounts

There is one user. Inventing a user table to serve one person would be more
code and more attack surface for no benefit, so the consent screen asks for a
single owner secret instead. It is compared with `hmac.compare_digest`, so its
strength is entirely its entropy — generate it, do not choose it.

Registration being open is per RFC 7591 and safe here for one reason: anyone
can register a client, but registering grants nothing. Every authorization
still has to be approved on the consent screen.

### Token handling

- Access tokens: opaque random values, 1 hour, in Redis, stored **hashed** —
  a database dump is not a set of working tokens.
- Refresh tokens: 30 days, in Mongo so they survive a Redis flush, **rotated
  on every use** and the old one deleted, the same single-use discipline Oura
  applies to its own.
- Authorization codes: 60 seconds, single-use via an atomic `GETDEL`, so a
  replay finds nothing on any replica.
- Registered clients: Mongo, so a connector never has to re-register.

Opaque rather than JWTs on purpose: a JWT saves a lookup, but revocation then
needs a denylist that costs the same lookup, and an opaque token cannot leak
claims.

Failed consent attempts are counted per authorization request and lock out
after `OAUTH_CONSENT_MAX_ATTEMPTS`. The counter is keyed on the transaction,
not the source IP — behind an ingress every request shares one address, so
per-IP counting would lock out everyone at once or nobody.

### Revoking access

```bash
curl -sX POST https://oura.dan-leonard.com/revoke -d "token=<access-or-refresh-token>"
```

Or drop the client's record from `oauth_clients` and delete its refresh tokens
from `oauth_refresh_tokens`. Rotating `MCP_OWNER_PASSWORD` prevents new
authorizations but does not revoke existing tokens.

## Limitations

- **OAuth authorizations are approved with one shared owner secret**, not
  per-user accounts. Correct for a single-person deployment; it is not a
  multi-tenant authorization model.
- **Single account per deployment.** `OURA_ACCOUNT_ID` namespaces every store,
  cache key and lock, so multi-account support means routing an account id
  through the tool layer — not a rewrite, but not done.
- **No write operations to Oura.** Deliberate. Oura's API is read-only for
  these collections anyway.
- **Sandbox endpoints are not exposed.** Oura mirrors every collection under
  `/v2/sandbox/`; this server always talks to live data.
- **Document-by-id lookups are not surfaced as a tool.** `OuraService.get_document`
  exists and works, but no tool calls it — webhooks carry an `object_id`, and
  nothing yet needs to fetch by it.
- **No local historical store.** By design; see
  [Architecture](#what-is-stored-where). If `oura-sync` needs one, that is its
  own schema and its own justification.
- **Webhook events are not proactively fetched.** A notification invalidates
  the cache and is recorded; the next tool call fetches fresh data. Prefetching
  would mean storing health data locally, which this service does not do.
- **Webhook subscriptions are not auto-renewed.** They expire; the renew
  endpoint exists but nothing calls it on a schedule. A CronJob hitting
  `/api/webhooks/subscriptions/<id>/renew` would close this, but adding a
  scheduler for one periodic call did not seem worth it yet.
- **`x-oura-timestamp` is not checked for freshness.** Oura does not document a
  tolerance window, and rejecting on clock skew would drop valid events. Redis
  deduplication already blocks replays within its TTL.
