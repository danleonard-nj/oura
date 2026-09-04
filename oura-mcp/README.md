# oura-mcp

A Model Context Protocol server that exposes your Oura Ring data, read-only,
over Streamable HTTP. Deploy it behind a Kubernetes ingress and any MCP client
— Claude Code, Claude Desktop, ChatGPT, or anything else that speaks the
protocol — can ask questions about your sleep, readiness, activity, stress and
workouts.

```
you ──▶ Claude / ChatGPT ──▶ https://oura.example.com/mcp ──▶ Oura API v2
                                    (this service)
```

---

## Contents

1. [Architecture](#architecture)
2. [Why `oura-ring`](#why-oura-ring)
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
19. [Limitations](#limitations)

---

## Architecture

One Starlette application serves everything. There is no second web framework:
the MCP SDK already brings Starlette, the HTTP surface is eight routes, and a
single app means a single lifespan with no cross-framework bridging.

| Path | Served by | Auth |
| --- | --- | --- |
| `/mcp` | Official MCP Streamable HTTP app | `Authorization: Bearer $MCP_API_KEY` |
| `/health` | Liveness | none |
| `/ready` | Readiness | none |
| `/oauth/authorize`, `/oauth/callback` | OAuth2 flow | OAuth `state` nonce |
| `/webhooks/oura` | Oura webhooks | Oura challenge + HMAC signature |
| `/api/*` | Admin status, audit, webhook history | `Authorization: Bearer $MCP_API_KEY` |

```
src/oura_mcp/
  app.py              ASGI composition root, lifespan, route table
  config.py           typed configuration, validated at startup
  container.py        DEPI composition root — every registration lives here
  api/                thin route handlers, auth middleware, correlation ids
  mcp/                MCP server construction and the tool surface
  services/           all business and integration logic
  repositories/       Mongo persistence
  clients/            blocking Oura HTTP clients
  infrastructure/     logging, crypto, Mongo, Redis, the thread pool
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
     └─ OuraService          cache lookup → token → offload → translate errors
        ├─ CacheService      Redis, generation-counter invalidation
        ├─ TokenService      Redis lock → re-read → refresh → atomic persist
        └─ ThreadOffloader   bounded pool; oura-ring is synchronous
     └─ AuditService         Mongo row + structured stdout, never fatal
```

### What is stored where

**Mongo** holds durable operational data only: `oauth_tokens` (encrypted),
`audit_events` and `webhook_events`.

**Health data is deliberately not copied into Mongo.** Oura is the system of
record, Redis already absorbs repeat reads, and webhooks keep that cache
honest. Mirroring the dataset would add a sync problem, a correctness problem
and a pile of personal health information at rest, in exchange for nothing this
service needs. If `oura-sync` later wants a local historical store, that is a
separate concern with its own schema and its own justification.

**Redis** does three jobs: the distributed OAuth refresh lock (mandatory),
response caching (optional), and webhook deduplication (optional). Only the
lock is required for correctness.

---

## Why `oura-ring`

[`hedgertronic/oura-ring`](https://github.com/hedgertronic/oura-ring) is a
small, readable client that covers most of Oura's v2 collections, follows
`next_token` pagination properly, and has no heavy dependencies. Using it means
not maintaining a hand-rolled HTTP client for fifteen endpoints.

It is **synchronous**, built on `requests`. Calling it from a coroutine would
block the event loop for an entire Oura round trip and stall every other MCP
session on the replica. Every call therefore goes through
`infrastructure/executor.py`, a bounded `ThreadPoolExecutor`. Bounded rather
than `asyncio.to_thread` on purpose: the default executor is unbounded, and
Oura enforces per-token and per-application rate limits, so a hard ceiling on
concurrent upstream requests is the useful behaviour.

That detail is confined to `clients/oura_client.py` and the offloader. Nothing
in the MCP tools knows the client is synchronous, so replacing it with a native
async client later means deleting an indirection, not rewriting callers. There
is a test asserting no Oura call ever executes on the event-loop thread.

### Where the library falls short

Four collections exist in Oura's published OpenAPI spec but are absent from
`oura-ring` 0.3.0:

| Collection | Exposed as |
| --- | --- |
| `daily_resilience` | `oura_resilience` |
| `daily_cardiovascular_age` | `oura_cardiovascular_age` |
| `vO2_max` | `oura_vo2_max` |
| `ring_battery_level` | `oura_query` |

These are issued directly in `clients/oura_client.py` through the library's own
authenticated session, reusing its connection pool and pagination shape. The
webhook subscription routes (`/v2/webhook/subscription`) are likewise absent
and live in `WebhookSubscriptionClient`. Nothing else is hand-rolled.

One further gap: `oura-ring` calls `raise_for_status()` and returns parsed
JSON, so the `Response` never reaches the caller. Rate-limit headers are
recovered from `requests.HTTPError.response` in the translation layer.

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
| `daily` | Sleep, readiness, activity, stress, resilience, cardiovascular age, VO2 max | `oura_summary`, `oura_sleep`, `oura_readiness`, `oura_activity`, `oura_stress`, `oura_resilience`, `oura_cardiovascular_age`, `oura_vo2_max` |
| `heartrate` | Heart rate time series | `oura_heart_rate` |
| `workout` | Workouts | `oura_workouts` |
| `session` | Guided and unguided sessions | `oura_sessions` |
| `tag` | User-entered tags | `oura_tags` |
| `spo2` | Blood oxygen during sleep | `oura_spo2` |
| `personal` | Age, sex, height, weight | `oura_personal_info` |
| `email` | Email address | — |

Default: all of them. Users can deselect scopes individually at the consent
screen, so `oura-auth` reports what was actually granted. A missing scope
surfaces as a readable permission error naming the scope, not a crash.

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

| Variable | Purpose |
| --- | --- |
| `OURA_CLIENT_ID` | OAuth application client id |
| `OURA_CLIENT_SECRET` | OAuth client secret; also the webhook HMAC key |
| `OURA_REDIRECT_URI` | Must match the Oura application exactly |
| `MCP_API_KEY` | Bearer token for `/mcp` and `/api/*` |
| `TOKEN_ENCRYPTION_KEY` | Fernet key; urlsafe base64, 32 bytes |
| `MONGODB_URI` | Existing cluster |
| `REDIS_URL` | Existing instance |

Important optional ones:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PUBLIC_HOST` | — | Ingress hostname, no scheme. **Required in production**; see [Ingress](#ingress-considerations). |
| `MONGODB_DATABASE` | `oura_mcp` | |
| `OURA_SCOPES` | all read scopes | Space-separated |
| `OURA_ACCOUNT_ID` | `default` | Namespaces tokens, cache keys, locks, audit |
| `MCP_STATELESS` | `true` | Stateless transport; no sticky sessions |
| `MCP_PATH` | `/mcp` | |
| `OURA_WEBHOOK_VERIFICATION_TOKEN` | — | Required to accept webhook subscriptions |
| `LOG_LEVEL` | `INFO` | |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | |

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

**ChatGPT** — Settings → Connectors → Custom, same URL and header. Note that
ChatGPT's deep-research connectors expect `search`/`fetch` tools specifically;
this server exposes Oura tools instead, so use it as a general connector.

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

| Tool | Returns | Default range |
| --- | --- | --- |
| `oura_summary` | Sleep + readiness + activity + stress + resilience + SpO2 + workouts, joined by day | 7 days |
| `oura_sleep` | Daily sleep summaries and scores | 7 days |
| `oura_readiness` | Daily readiness scores and contributors | 7 days |
| `oura_activity` | Steps, calories, MET minutes, activity score | 7 days |
| `oura_stress` | High-stress vs recovery minutes | 7 days |
| `oura_spo2` | Blood oxygen during sleep | 7 days |
| `oura_resilience` | Resilience level and contributors | 14 days |
| `oura_cardiovascular_age` | Estimated vascular age | 30 days |
| `oura_vo2_max` | VO2 max readings | 90 days |
| `oura_heart_rate` | Heart rate samples | 1 day (max 14) |
| `oura_workouts` | Workouts | 14 days |
| `oura_sessions` | Meditation, breathing, naps | 14 days |
| `oura_tags` | User-entered tags | 14 days |
| `oura_ring_info` | Ring model, size, firmware | 30 days |
| `oura_personal_info` | Age, sex, height, weight | — |
| `oura_query` | Any collection by name, for the long tail | 7 days |

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

```bash
kubectl create secret generic oura-mcp \
  --namespace default \
  --from-literal=OURA_CLIENT_ID='...' \
  --from-literal=OURA_CLIENT_SECRET='...' \
  --from-literal=MCP_API_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  --from-literal=TOKEN_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
  --from-literal=MONGODB_URI='mongodb://user:pass@mongo-internal.default.svc.cluster.local:27017/?authSource=admin' \
  --from-literal=REDIS_URL='redis://redis-internal.default.svc.cluster.local:6379/0' \
  --from-literal=OURA_WEBHOOK_VERIFICATION_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

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
nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
nginx.ingress.kubernetes.io/proxy-buffering: "off"
nginx.ingress.kubernetes.io/proxy-request-buffering: "off"
nginx.ingress.kubernetes.io/proxy-body-size: "4m"
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
The chart derives it from `ingress.hostname` automatically.

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
Oura's *subscription* resource — never to health data. The MCP surface stays
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

| Upstream | Behaviour |
| --- | --- |
| 400 | `OuraValidationError`, no retry |
| 401 | Refresh under lock, retry **once** |
| 403 | `OuraPermissionError` naming the required scope, no retry |
| 404 | `OuraNotFoundError`, no retry |
| 429 | Retry honouring `Retry-After`, within the attempt budget |
| 5xx | Exponential backoff with jitter, capped at 8s |
| Timeout / connection | Same as 5xx |

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

- **`/mcp` requires a bearer token**, compared with `hmac.compare_digest`. The
  middleware wraps only the MCP app, so health probes, the OAuth callback and
  the webhook keep their own verification models. It is one self-contained
  class — swapping in standards-based MCP OAuth later means replacing it with
  the SDK's `token_verifier` and touching nothing else.
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

## Limitations

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
- **`oura-ring` is 0.3.0, not 1.x.** The four missing collections are covered
  here; if the library adds them, the direct calls can be deleted.
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
