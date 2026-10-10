# oura-sync

The Oura Ring service: one Quart app that

- serves a **Model Context Protocol server** exposing your Oura data, read-only,
  over Streamable HTTP -- any MCP client (Claude Code, ChatGPT, anything that
  speaks the protocol) can ask about your sleep, readiness, activity, stress
  and workouts. Clients that only offer OAuth, such as ChatGPT, are supported
  too -- see [MCP OAuth](#mcp-oauth);
- **archives every Oura resource into Mongo**, back-filled to the first ring
  and deduplicated, plus a change log of what Oura only reports the current
  value of (ring configuration and firmware, profile), so the MCP surface can
  answer "when did my firmware update", "chart my battery level this month"
  or "how has my sleep changed since 2021" -- see [Snapshot sync](#snapshot-sync);
- handles the Oura OAuth connection, webhooks and a small admin API.

```
MCP client ──▶ https://oura.example.com/mcp ─┐
                                              ├─▶ oura-sync ──▶ Oura API v2
external cron ──▶ POST /internal/sync/run ───┘        │
                                              Mongo: tokens, snapshots, audit
```

This used to be three projects -- `oura-mcp`, `oura-sync` and a shared
`oura-common` source tree. They are now one service: the sync service is the
main app and MCP is an extension of it, built on `framework`'s
`MCPBlueprint`, so tools and routes share one container, one Oura client, one
token refresh lock and one response cache. Snapshot history is a function
call, not an Azure AD-authenticated HTTP hop between two pods.

---

## Contents

1. [Architecture](#architecture)
2. [Snapshot sync](#snapshot-sync)
3. [Ring wear status](#ring-wear-status)
4. [The Oura client](#the-oura-client)
5. [Local setup](#local-setup)
6. [Registering an Oura OAuth application](#registering-an-oura-oauth-application)
7. [Scopes](#scopes)
8. [Connecting an account](#connecting-an-account)
9. [Configuration](#configuration)
10. [Running locally](#running-locally)
11. [Connecting an MCP client](#connecting-an-mcp-client)
12. [MCP tools](#mcp-tools)
13. [Docker](#docker)
14. [Helm](#helm)
15. [Ingress considerations](#ingress-considerations)
16. [Webhooks](#webhooks)
17. [Token refresh and rotation](#token-refresh-and-rotation)
18. [Testing](#testing)
19. [Security](#security)
20. [MCP OAuth](#mcp-oauth)
21. [Migrating from oura-mcp + oura-sync](#migrating-from-oura-mcp--oura-sync)
22. [Limitations](#limitations)

---

## Architecture

One Quart app serves everything. The MCP endpoint is an extension of it:
`framework.mcp.MCPBlueprint` serves the SDK's Streamable HTTP app from a Quart
route, with the blueprint's own bearer/OAuth auth in front of it.

| Path                                           | Served by                                   | Auth                                       |
| ---------------------------------------------- | ------------------------------------------- | ------------------------------------------ |
| `/mcp`                                         | MCP Streamable HTTP, via `MCPBlueprint`     | `mcp.api_key` **or** an OAuth access token |
| `/health`                                      | Liveness                                    | none                                       |
| `/ready`                                       | Readiness                                   | none                                       |
| `/internal/sync/run`                           | Snapshot sync, called by the cron scheduler | Azure AD bearer token                      |
| `/api/snapshots/<resource>`                    | Stored snapshots                            | Azure AD bearer token                      |
| `/oauth/authorize`, `/oauth/callback`          | Our OAuth2 flow **as a client of Oura**     | `state` nonce                              |
| `/authorize`, `/token`, `/register`, `/revoke` | Us **as an OAuth2 server** for MCP clients  | PKCE, DCR, consent                         |
| `/.well-known/oauth-*`                         | OAuth discovery metadata                    | none (public by design)                    |
| `/consent`                                     | Approval screen for an MCP authorization    | `mcp.oauth.owner_password`                 |
| `/webhooks/oura`                               | Oura webhooks                               | Oura challenge + HMAC signature            |
| `/api/*` (the rest)                            | Admin status, audit, request log, webhooks  | `Authorization: Bearer <mcp.api_key>`      |

Two OAuth roles coexist and are easy to confuse. Under `/oauth/*` we are a
_client_ of Oura, holding a token for your ring data. At the root
(`/authorize`, `/token`, ...) we are a _server_, issuing tokens to MCP clients.
The prefixes keep them apart; only the latter appears when
`mcp.oauth.owner_password` is set.

```
app/
  app.py              create_app(): Quart app, blueprints, the MCP extension, startup/shutdown
  clients/            OuraApiClient, WebhookSubscriptionClient, OuraOAuthClient
  services/           OuraService, TokenService, CacheService, OAuthService, AuditService,
                      SummaryService, WebhookService, McpAuthService,
                      SnapshotService, SyncService, HistoryService, WearService,
                      JobScheduler (APScheduler: sync, back-sync, wear poll)
                      (+ wear_detection, its pure rules)
  repositories/       Mongo/Redis persistence: tokens, audit, oauth, webhooks,
                      request log, snapshots, wear
  routes/             Quart blueprints: health, admin, oauth, webhooks, consent, sync,
                      wear, home
  mcp/
    app.py            MCPServer + MCPBlueprint, endpoint auth (with_oauth / with_bearer)
    tools/            one module per area: summary, sleep, readiness, stress, activity,
                      heart, battery, profile, query, history, wear; common.py holds
                      the audited runner
  models/             config.py (typed settings), Oura domain types, tool result shapes
  utilities/          provider.py (DI composition root), middleware (correlation id,
                      access + request log), auth guards, logging, crypto, Mongo, Redis,
                      Azure AD token validation
  static/             landing page
tests/                one suite for everything
```

`app/` is a plain source tree imported as `app.*` from this directory --
there is no package to build or install, only `requirements.txt`. pytest puts
this directory on `sys.path` (see `pytest.ini`); hypercorn does the same for
its working directory.

Entry points stay thin — accept input, call an injected service, return the
result. Dependencies are wired once in `utilities/provider.py` using
[pydepi](https://github.com/danleonard-nj/depi) and reach their consumers
through ordinary constructors. Views get theirs from depi's `QuartInjector`
(`pydepi-quart`, autowired): a view declares `tokens: TokenService` and the
injector supplies it. Blueprints are built by `create_*_blueprint(injector)`
factories for that reason. MCP tools are called by the SDK rather than Quart,
so they resolve through the `ToolRunner` they are handed. `Configuration` is
registered as a singleton and each config section is a factory reading its
key from it.

Three composition details are load-bearing:

**The MCP endpoint is a Quart route, not a mounted sub-app.** `MCPBlueprint`
presents each request to the SDK's ASGI app on its own `/mcp` path, so there
is no prefix stripping and no `307` to `/mcp/`, which MCP clients would not
follow. There is a test pinning this.

**The session manager runs for as long as Quart is serving.** A nested ASGI
app never has its own lifespan run; `MCPBlueprint` registers a
`while_serving` hook that runs `session_manager.run()` instead.

**The DI provider is created at assembly but built in `before_serving`.**
The injector only needs to know what is registered, so views are wired when
the app is assembled. Building constructs the factory registrations, and
`AsyncMongoClient` and `redis.asyncio.Redis` bind to the loop that creates
them -- so `build_async()` waits for the serving loop.

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

**Mongo** holds `oauth_tokens` (your encrypted Oura tokens), `audit_events`,
`webhook_events`, `request_log` (a week, TTL-pruned), the synced copy of your
Oura data in `oura_records` and `resource_snapshots` with its progress in
`sync_state` (see [Snapshot sync](#snapshot-sync)), `wear_days`,
`wear_status` and `wear_transitions` (see [Ring wear status](#ring-wear-status)), plus
`oauth_clients` and `oauth_refresh_tokens` when MCP OAuth is on. The database
is still named `oura_mcp`, so nothing had to move when the services merged.

**MCP tools still read Oura live.** The synced copy exists so data Oura
forgets (battery samples, firmware history) is not lost, and so long-range
questions need no Oura calls; the tools answer from
Oura, through the Redis cache, except `oura_resource_history`, which reads the
copy.

**Redis** does four jobs: the distributed Oura-token refresh lock (mandatory),
response caching (optional), webhook deduplication (optional), and the
short-lived halves of MCP OAuth — pending authorizations, authorization codes
and access tokens. Only the refresh lock is required for correctness.

---

## Snapshot sync

Oura does not keep everything forever -- ring battery samples in particular
only stick around briefly -- so every few hours the service copies what Oura
has into Mongo:

- **Record archive** (`oura_records`): every collection with records in time
  -- daily scores, sleep periods, workouts, sessions, tags, heart rate and
  battery samples. One document per Oura record, upserted by its `id` (or its
  timestamp, for time series), so overlapping runs never duplicate anything
  and a record Oura revises later (a sleep score finalised the next morning)
  is updated in place.
- **Change log** (`resource_snapshots`): `ring_configuration` and
  `personal_info`, which Oura reports only the current value of. A new
  document is stored only when the value changes, so it answers "when did my
  firmware update".

The `oura_resource_history` MCP tool and `GET /api/history/<resource>` read
both: archived records for every collection, the change log for
configuration and profile.

**When it runs.** The [scheduler](#scheduling) runs a sync every
`sync.interval_hours` (6 by default; 0 turns it off). Each run fetches from
the last day that resource synced successfully (`sync_state.synced_through`)
or the last `sync.lookback_days` (2 by default), whichever reaches further
back -- so an outage of any length leaves no gap, and revised records keep
being re-read. `sync.resources`
defaults to every resource; one whose scope is not granted just fails on its
own.

**Windows.** A range wider than `sync.chunk_days` (90) is fetched a window
at a time; heart rate is capped at `oura.max_heartrate_days` (14). A window
that needs more than `MAX_PAGES` pages raises rather than archiving a partial
result, and the sync halves it until it fits.

### Scheduling

Periodic work runs in-process on [APScheduler](https://apscheduler.readthedocs.io/en/3.x/)
(`services/scheduler.py`, `JobScheduler`), started in `before_serving` and
stopped in `after_serving`:

| Job        | Every                                  | Off when                             |
| ---------- | -------------------------------------- | ------------------------------------ |
| `sync`     | `sync.interval_hours` (6)              | `sync.interval_hours` is 0           |
| `backfill` | hour                                   | `sync.backfill_enabled` is false     |
| `wear`     | `wear.poll_interval_minutes` (15)      | `wear.poll_interval_minutes` is 0    |

APScheduler decides *when*: each job runs once at startup, then on interval
boundaries counted from the epoch, so every replica ticks at the same
instants -- the ring check at :00, :15, :30 and :45, the sync at 00:00,
06:00, 12:00 and 18:00 UTC. Redis decides *who*: each interval is claimed
by the first replica to reach it (`oura:<job>:schedule:<account>:<n>`), so
however many replicas run and however often a pod restarts, a job runs at
most once per interval -- and a startup run never pushes the next scheduled
one back. Without Redis a claimed job is skipped rather than risked twice.
The back-sync is unclaimed; it holds its own lease for the length of a
walk. Overruns coalesce instead of queueing (`max_instances=1`), and a job's
failure is logged without stopping the schedule.

### Back-sync

The regular sync only reads forward from its first run. The back-sync fills
in everything older: for each archived resource it walks back from today one
window at a time, newest first, until it reaches the floor:

1. `sync.backfill_start`, if configured;
2. otherwise the setup day of the earliest ring in `ring_configuration` --
   there is no ring data from before a ring existed;
3. failing both, it stops after `sync.backfill_empty_chunks` (4) empty
   windows in a row. It never walks past 2015-01-01.

Progress is saved to `sync_state` after every window, so a restart or a
failure resumes where it stopped, and a finished resource costs no Oura calls
on later passes. A Redis lease, refreshed after every window, keeps replicas
from walking at the same time. A rate limit ends the pass; any other error
only skips that resource until the next one. The [scheduler](#scheduling)
runs it at startup and then hourly (`sync.backfill_enabled`, on by default). Current-state resources
have no history in Oura's API, so they are not walked. Battery samples older
than Oura's own retention are gone upstream and cannot be recovered -- the
archive only keeps them from here on.

| Route                       | Method | Auth                  | Purpose                                            |
| --------------------------- | ------ | --------------------- | -------------------------------------------------- |
| `/internal/sync/run`        | POST   | Azure AD bearer token | Run a sync now (for the external scheduler)        |
| `/internal/sync/backfill`   | GET    | Azure AD bearer token | Back-sync progress per resource                    |
| `/internal/sync/backfill`   | POST   | Azure AD bearer token | Start a back-sync in the background (202)          |
| `/api/history/<resource>`   | GET    | Azure AD bearer token | Stored history of any synced resource, oldest first |
| `/api/snapshots/<resource>` | GET    | Azure AD bearer token | Change-log snapshots of one resource, oldest first |

`POST /internal/sync/run` takes an optional JSON body,
`{"resources": ["ring_battery_level"]}`, to override `sync.resources`. It
returns one result per resource -- `{"resource", "start_date", "end_date",
"fetched", "inserted", "updated"}` for archived ones, `{"resource",
"changed"}` for the change log -- with an `"error"` key instead of stopping
the whole run if one resource fails. Once the external scheduler calls it,
set `sync.interval_hours` to 0; running both is harmless, just redundant.

`POST /internal/sync/backfill` takes `{"resources": [...], "restart": true}`,
both optional; `restart` forgets saved progress so the walk starts again from
today. It answers 409 if this replica is already walking.

`GET /api/history/<resource>` takes `since`/`until` (ISO-8601 dates or
datetimes, inclusive; a bare `until` date covers the whole day) and `limit`
(default 1000, max 5000; the most recent records win when the window holds
more). `GET /api/snapshots/<resource>` takes `since`/`until` (filtering on
when a snapshot was first seen) and `limit` (default 100, max 1000).

### Gateway auth

Both routes are gated by `require_gateway_token` (`utilities/auth.py`): an
Azure AD-issued JWT, validated against the tenant's JWKS (signature, issuer,
audience, expiry, and optionally a required `roles` claim entry) -- the same
client-credentials pattern the other internal services here use to call each
other through the API gateway. With `auth.tenant_id`/`auth.audiences` unset both
routes always return `503 not_configured`; there is no unauthenticated
fallback, and `mcp.api_key` is not accepted on them.

---

## Ring wear status

Every `wear.poll_interval_minutes` (15) the service judges whether the ring
is on a finger -- `WORN`, `NOT_WORN` or `UNKNOWN` -- and keeps the history
of it, so "when did I take it off" and "how many hours did I wear it this
week" have answers. Code: `services/wear_detection.py` (the rules, pure) and
`services/wear_service.py` (fetching and persisting), run by the
[scheduler](#scheduling) at :00, :15, :30 and :45.

**This is not live device status.** Oura's API only holds what the ring last
synced through the phone, in the background, sometimes hours late. Every
answer is as of `latest_evidence_at`; once that is older than
`wear.max_evidence_age_minutes` (60) the status reads `UNKNOWN` -- checked
again on every read, so an outage cannot leave a stale `WORN` on display.

### Evidence, as verified against the live API

- **`daily_activity.class_5_min`** -- one character per 5 minutes:
  `0` non-wear, `1` rest, `2` inactive, `3` low, `4` medium, `5` high
  activity (Oura's OpenAPI spec, v1.41). The activity day is "a 24-hour
  period starting at 4 a.m." local; `timestamp` is that 04:00 with its UTC
  offset, and slot *i* covers `timestamp + 5i` minutes. **Today's record is
  published while the day is in progress**: the string ends at the last
  synced slot (70 characters at 09:50) while `met` is padded to 1440, so its
  length is a sync-freshness signal. `'0'` count x 300 s tracks
  `non_wear_time`, and `'0'` runs line up with gaps in heart rate.
- **`heartrate`** -- positive evidence only. No pulse is read off a finger,
  but the ring does not sample continuously while worn either (hour-long
  daytime gaps are normal), so a missing sample never means it is off.
- **`daily_activity` treats `end_date` as exclusive** (unlike `daily_sleep`):
  `start_date=end_date=D` returns nothing. The poller asks for today + 2.

### Rules

| Newest evidence (all within `max_evidence_age_minutes`)                          | Status     | Confidence |
| -------------------------------------------------------------------------------- | ---------- | ---------- |
| Nothing recent, or no data at all                                                | `UNKNOWN`  | low        |
| Heart rate newer than every classified slot                                      | `WORN`     | medium     |
| Trailing worn slots, with heart rate during them and both fresh                  | `WORN`     | high       |
| Trailing worn slots without heart rate, or heart rate with no activity data      | `WORN`     | medium     |
| Trailing non-wear run of at least `min_non_wear_minutes` (15), no pulse since    | `NOT_WORN` | high from twice the threshold with heart rate checked, else medium |
| Trailing non-wear run with heart rate inside it (beyond its first slot)          | `UNKNOWN`  | low        |
| Trailing non-wear run shorter than the threshold                                 | `UNKNOWN`  | low        |

`since` is when the current period began: the first non-wear slot of a
removal, the first worn slot (or first new pulse) after one. Non-wear blips
shorter than the threshold between worn stretches do not reset it.

### Persistence and idempotency

- `wear_days` -- each activity day's 04:00 start and `class_5_min`, the
  whole wear history. A day is only replaced by one covering at least as many
  slots, so a short or partial response never erases stored evidence.
- `wear_status` -- the current judgement: status, confidence, reason,
  source, `since`, `last_worn_at`, `latest_evidence_at`,
  `activity_through`, `heart_rate_through`, `checked_at`, `last_error`.
- `wear_transitions` -- each change between `WORN` and `NOT_WORN`, at the
  evidence time it happened, unique per (status, time). Flickers through
  `UNKNOWN` are not transitions, and re-polling the same evidence adds none.

Judgement always runs over the *stored* days plus the last heart-rate time
already known, so a failed or partial poll can add evidence but never take
it away. Each source is fetched on its own: a failing one is recorded in
`last_error` and lowers confidence (heart rate) or falls back to stored days
(activity). A failure is logged once when it starts and once when it
recovers, not every poll. Each 15-minute interval is claimed in Redis, so
replicas never poll twice.

### Delayed sync and backfill

Each poll re-reads `wear.lookback_days` (3) of activity, so a ring that
syncs late fills in the days it missed. For anything older, `POST
/internal/wear/backfill` derives wear days from the record archive (which
the [back-sync](#back-sync) fills to the first ring) -- no Oura calls,
safe to repeat.

| Route                     | Method | Auth                  | Purpose                                                                   |
| ------------------------- | ------ | --------------------- | ------------------------------------------------------------------------- |
| `/api/wear/status`        | GET    | Azure AD bearer token | Current status, with evidence age and freshness                           |
| `/api/wear/history`       | GET    | Azure AD bearer token | Intervals, totals and transitions; `since`/`until` (default: last 7 days) |
| `/internal/wear/poll`     | POST   | Azure AD bearer token | Poll now                                                                  |
| `/internal/wear/backfill` | POST   | Azure AD bearer token | Optional `{"since", "until"}`; derive history from the archive            |

MCP: `oura_wear_status` and `oura_wear_intervals`.

To check it end to end, poll and read it back:

```bash
curl -s -X POST -H "Authorization: Bearer $GATEWAY_TOKEN" https://oura.example.com/internal/wear/poll | jq
curl -s -H "Authorization: Bearer $GATEWAY_TOKEN" "https://oura.example.com/api/wear/history?since=2026-10-01" | jq .intervals
```

Take the ring off for 20 minutes, open the Oura app so it syncs, and poll
again: `NOT_WORN` with `since` at the removal. Put it back on, sync, poll:
`WORN`, and two new rows in `transitions`.

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
per worker thread. `oura.executor_workers` survives as the `max_connections`
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

`framework` comes from the private `py-packages` feed:

```bash
cd oura-sync
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt \
  --extra-index-url "https://py-packages:$ARTIFACT_FEED_TOKEN@pkgs.dev.azure.com/dcl525/_packaging/py-packages/pypi/simple/"
cp config.example.json config.dev.json              # then fill it in
```

Working on `framework` itself, or before the release `requirements.txt` pins
is published? Install your checkout editable first --
`pip install -e "../../framework[mcp]"` -- with its `manifest.json` at that
version, so the pin is already satisfied.

`config.dev.json` is gitignored and is what `Configuration` reads when
`FLASK_ENV` (or `QUART_ENV`) is `Development`. Fill it in as described in
[Configuration](#configuration) -- [generating the secrets](#generating-the-secrets)
is there too -- and point it at your local or port-forwarded Mongo and Redis,
with `service.public_scheme` `http` and `service.public_host` empty (OAuth
metadata then falls back to `http://localhost:8000`).

> **Port-forwarded Mongo replica set?** Add `?directConnection=true` to
> `mongo.connection_string`. Without it the driver reads the replica set
> config, discovers internally-advertised hostnames like
> `mongo-internal.default.svc.cluster.local`, and cannot resolve them from
> your machine.

---

## Registering an Oura OAuth application

**This part you have to do by hand.** At
<https://cloud.ouraring.com/oauth/applications>:

1. Create an application. Note the **Client ID** and **Client Secret**.
2. Add a **Redirect URI** for each environment you will use. These must match
   `oura.redirect_uri` byte for byte:
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

If an account was connected before these were added to `oura.scopes`, four
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

## Configuration

Configuration works the way it does in kube-tools-api: one JSON file, read by
`framework`'s `Configuration`, registered in DI, and never committed.

| `FLASK_ENV` / `QUART_ENV` | File read from the working directory |
| ------------------------- | ------------------------------------ |
| unset / `Production`      | `config.json`                        |
| `Development`             | `config.dev.json`                    |
| `Local`                   | `config.local.json`                  |

`FLASK_ENV` is checked first, so a machine-wide `FLASK_ENV=Development` wins
over any `QUART_ENV` you set. The image sets neither and reads `config.json`.

All three are gitignored. `config.example.json` is the committed template:
every section and setting, with `REPLACE_ME` where a secret goes and the
defaults everywhere else. A test fails if the template and the models drift
apart.

In production `config.json` is **baked into the image**: CI/CD drops it into
the build context, the Dockerfile copies it to `/srv/config.json`, and the
build fails without one. Locally, `deploy.bat` does the same from your own
`oura-sync/config.json`. There is no ConfigMap, no Secret and no environment
variable carrying settings -- the only place a value lives is that file.

Each top-level key is a section, validated into a typed model at startup
(`app/models/config.py`) and registered in DI, so a service takes exactly the
section it reads -- `OuraConfig`, `McpConfig`, ... A missing or malformed
setting stops the process before it serves anything, naming the key (never the
value).

| Section    | Required keys                     | Notable optional keys                                                                                  |
| ---------- | --------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `service`  | —                                 | `public_host` (**required in production**; see [Ingress](#ingress-considerations)), `public_scheme`, `log_level` |
| `oura`     | `client_id`, `client_secret`, `redirect_uri` | `scopes`, `account_id` (namespaces tokens, cache keys, locks, audit), retry and range limits |
| `tokens`   | `encryption_key`                  | expiry skew, refresh-lock timings                                                                      |
| `mongo`    | `connection_string`               | `database` (`oura_mcp`)                                                                                |
| `redis`    | `connection_string`               | —                                                                                                      |
| `mcp`      | `api_key`                         | `path`, `stateless`, `extra_allowed_hosts`, `request_state_secrets` (needed with >1 replica; see [Scaling](#scaling)), `oauth.owner_password` (enables [MCP OAuth](#mcp-oauth)) |
| `webhooks` | —                                 | `verification_token` (required to accept webhook subscriptions)                                        |
| `cache`    | —                                 | `enabled`, TTLs                                                                                        |
| `sync`     | —                                 | `resources` (default: all), `interval_hours`, `lookback_days`, `chunk_days`, `backfill_enabled`, `backfill_start`, `backfill_empty_chunks` -- see [Back-sync](#back-sync) |
| `wear`     | —                                 | `poll_interval_minutes` (0 turns it off), `lookback_days`, `heart_rate_lookback_hours`, `max_evidence_age_minutes`, `min_non_wear_minutes` -- see [Ring wear status](#ring-wear-status) |
| `auth`     | —                                 | `tenant_id`, `audiences`, `required_role` -- gateway auth on the sync routes; unset, they always refuse |

`auth` has the same shape as framework's own `auth` section, so the file reads
like every other service's.

### Generating the secrets

None of these are issued by anyone; you generate them once and keep them.

| Key                          | Generate with                                                                                  |
| ---------------------------- | ---------------------------------------------------------------------------------------------- |
| `tokens.encryption_key`      | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`   |
| `mcp.api_key`                | `python -c "import secrets; print(secrets.token_urlsafe(48))"`                                |
| `mcp.oauth.owner_password`   | `python -c "import secrets; print(secrets.token_urlsafe(48))"`                                |
| `webhooks.verification_token`| `python -c "import secrets; print(secrets.token_urlsafe(32))"`                                |
| `mcp.request_state_secrets`  | `python -c "import secrets; print(secrets.token_urlsafe(48))"` (one entry per secret)         |

The rest come from elsewhere: `oura.client_id`/`client_secret` from
<https://cloud.ouraring.com/oauth/applications> (see
[Registering an Oura OAuth application](#registering-an-oura-oauth-application)),
`auth.tenant_id`/`audiences` from this service's Azure AD app registration,
and the Mongo/Redis connection strings from the cluster.

What rotating each one costs -- worth knowing before you regenerate any of
them:

- **`tokens.encryption_key`**: every stored Oura token becomes undecryptable
  and the account has to reconnect (`oura-auth`, or `/oauth/authorize`). The
  service says exactly that rather than failing obscurely. It must also be
  byte-identical to the key `oura-auth` writes with.
- **`mcp.api_key`**: every MCP client configured with the header has to be
  updated.
- **`mcp.oauth.owner_password`**: blocks new authorizations; existing OAuth
  tokens keep working until they expire or are revoked.
- **`webhooks.verification_token`**: existing Oura webhook subscriptions were
  verified with the old one; re-create them.
- **`mcp.request_state_secrets`**: list the new one first and keep the old one
  until in-flight round trips have expired (ten minutes).

Changing any value means rebuilding the image with the new `config.json` --
it is read once, at startup.

## Running locally

From `oura-sync/`:

```bash
FLASK_ENV=Development hypercorn "app.app:create_app()" --bind 0.0.0.0:8000 --reload
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
> That single value goes in exactly two places and must match in both:
> `mcp.api_key` in `config.json`, and the `Authorization` header your MCP
> client sends. The server compares them with `hmac.compare_digest` on every
> `/mcp` and admin `/api/*` request; there is no registration step or handshake.
> Rotating it means rebuilding the image with the new `config.json` and
> updating each client.

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
or _no auth_, with no field for a static header, so set `mcp.oauth.owner_password`
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

Nineteen tools, all read-only. There are no write operations to Oura anywhere in
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
| `oura_resource_history`   | Stored history of any resource from the synced archive — see below                  | all history    |
| `oura_wear_status`        | Whether the ring is worn now, with confidence and evidence age                      | —              |
| `oura_wear_intervals`     | Worn / not-worn intervals, totals and detected removals                             | 7 days         |
| `oura_query`              | Any collection by name, for the long tail                                           | 7 days         |
| `oura_clear_cache`        | Drop cached responses (all, or named resources) so the next call reads live         | —              |

`oura_summary` is the one to reach for first: it fans out across six
collections concurrently and joins them on the calendar day, so "how did I
sleep this week and did it track with my training" is one call rather than
seven. A resource the account cannot see — ungranted scope, or a metric behind
a subscription — is reported in `unavailable` rather than failing the summary.

`oura_query` covers the collections without a dedicated tool: `sleep` (raw
per-period sessions), `sleep_time`, `tag` (legacy format), `rest_mode_period`
and `ring_battery_level`. Keeping them behind one tool avoids five more
schemas for rarely-asked questions.

`oura_resource_history` reads the [synced archive](#snapshot-sync) rather
than Oura: every collection's records, back-filled to the first ring, plus the
change log for ring configuration/firmware and the profile, which Oura has no
history endpoint for. It reaches what Oura no longer returns -- battery
samples above all -- and spans years without Oura's range limits (see
`services/history_service.py`). `until` is inclusive: a bare date covers that
whole day.

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

Build context is `oura-sync/`, and it must contain a `config.json` -- the
image carries its configuration (see [Configuration](#configuration)).
`framework` installs from the private `py-packages` feed, whose token goes in
as a BuildKit secret so it never lands in an image layer:

```bash
docker build --secret id=artifact_feed_token,env=ARTIFACT_FEED_TOKEN -t oura-sync:0.1.0 .
```

```bash
docker run --rm -p 8000:8000 oura-sync:0.1.0
```

Multi-stage, `python:3.12-slim`, dependencies installed in their own cached
layer, runs as non-root uid/gid `10002`, no development server. Hypercorn is
PID 1 via `exec`, so it receives `SIGTERM` directly from the kubelet and drains
connections instead of being killed at the end of the grace period. The
container listens on `$PORT` (default 8000).

Tag and push to your registry:

```bash
docker tag oura-sync:0.1.0 azureks.azurecr.io/oura/oura-sync:0.1.0
docker push azureks.azurecr.io/oura/oura-sync:0.1.0
```

---

## Helm

The chart **does not deploy MongoDB or Redis**; it expects the ones already in
your cluster, at the connection strings in `config.json`. It carries no
application settings at all -- no ConfigMap, no Secret.

```bash
helm upgrade --install oura-sync ./helm/oura-sync \
  --namespace oura \
  --set image.tag=0.1.0 \
  --set ingress.enabled=true \
  --set ingress.hostname=oura.example.com \
  --set ingress.tls.enabled=true \
  --set ingress.tls.email=you@example.com
```

Or just run `deploy.bat` (with `ARTIFACT_FEED_TOKEN` set and `config.json` in
place), which tags the image with a timestamp plus the short commit hash, pushes both that tag and `latest`, and upgrades the release in the
`oura` namespace using `values.prod.yaml`.

Dry run first:

```bash
helm template oura-sync ./helm/oura-sync --set ingress.enabled=true \
  --set ingress.hostname=oura.example.com | less
```

Rollback:

```bash
helm rollback oura-sync
```

`service.public_host` and `oura.redirect_uri` in `config.json` must agree with
`ingress.hostname` -- the chart no longer derives them. RollingUpdate with
`maxUnavailable: 0`, liveness on `/health`, readiness on `/ready`, resource
requests and limits, pod and container security contexts, and an optional HPA
and PodDisruptionBudget are all in `values.yaml`.

### api-gateway

The admin endpoints are also mapped through the shared gateway
(`api-gateway/services/gateway/mapping/oura-mcp.json`, which must now target
the `oura-sync` service) under `/api/oura/*`, so
`https://api.dan-leonard.com/api/oura/status` works alongside the other
services. The gateway forwards request headers, so `mcp.api_key` still applies
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
  mcpExtraAllowedHosts: oura-sync.oura.svc.cluster.local
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

- The MCP transport runs stateless (`mcp.stateless`, on by default), so no session state
  lives in a replica's memory and any replica can serve any request.
- **Sticky sessions are not required.** The Streamable HTTP spec permits
  server-assigned session ids, which would need affinity; stateless mode does
  not use them. The cost is no server-initiated SSE streams between calls,
  which these tools do not use.
- State that must survive a replica lives in Mongo (tokens, audit, webhook
  history) or Redis (locks, cache, dedupe).
- OAuth refresh is serialised across replicas by a Redis lock.
- MCP request state (input-required round trips) is sealed with a per-process
  key unless `mcp.request_state_secrets` is set; set it before running more
  than one replica so a round trip can land on any pod.
- The sync is triggered externally, so it runs once per call, not once per
  replica.

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

`service.public_host` must be set to the ingress hostname. The MCP SDK's
DNS-rebinding protection validates the `Host` header against an allowlist and
defaults to localhost only; without it every production request gets a `421`.
`service.public_scheme` (`https`) is what OAuth metadata URLs are built from.

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
`webhooks.verification_token` and echoes `{"challenge": ...}`.

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
fills in the callback URL from `service.public_host` and the verification
token from `webhooks.verification_token`:

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

From `oura-sync/`:

```bash
pytest -q
```

One suite for everything, no Oura account, Mongo or Redis required — every
external system is faked at the constructor boundary.

The integration tests (`tests/test_app.py`) build the **real** app through
`create_app()` and drive it as a plain ASGI app with Starlette's
`TestClient`, lifespan included -- so `before_serving`, the MCP session
manager and framework's `MCPBlueprint` all run for real. Only Mongo, Redis
and the Oura client are swapped at the container. If a service stops taking a
dependency through its constructor, they break.

Covered, among others: every HTTP status maps to the right domain error;
refresh-and-retry-once on 401; bounded retries; `Retry-After` handling; token
rotation persisted atomically; the Redis refresh lock; ten concurrent
refreshes consuming one refresh token; safe failure when Redis is down; tokens
absent from logs, reprs, audit records and error messages; encryption at
rest; webhook signature accept/reject and tampering; deduplication; cache
hit/miss/bypass/invalidation; MCP bearer auth; the full MCP OAuth flow
(register, authorize, consent, token, call); the `/mcp` no-redirect
guarantee; DNS-rebinding rejection; a sync run storing a snapshot that
`oura_resource_history` then returns; ring removal and replacement,
heart-rate gaps, stale and late-syncing data, the 04:00 activity-day
boundary, repeated polls, backfill and conflicting evidence; Azure AD token
validation; and health and readiness behaviour.

---

## Security

- **`/mcp` requires a bearer credential** — either `mcp.api_key`, compared
  with `hmac.compare_digest`, or an OAuth access token this server issued.
  That check is `MCPBlueprint`'s `BearerAuth` on the MCP route only, so health
  probes, the Oura OAuth callback and the webhook keep their own verification
  models.
- **Health endpoints are unauthenticated** so probes work; they expose no data.
- **The admin `/api/*` routes require the same `mcp.api_key`.** The sync
  routes (`/internal/sync/run`, `/api/snapshots/*`) require an Azure AD token
  instead and do not accept the key.
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
| `Authorization: Bearer <mcp.api_key>`       | Clients that let you set a header (Claude Code) |
| An OAuth access token issued by this server | Clients that only do OAuth discovery (ChatGPT)  |

The static key is checked first and costs nothing; an OAuth token falls through
to a store lookup. Adding OAuth did not change the header path.

### Enabling it

Set one secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # -> mcp.oauth.owner_password
```

With `mcp.oauth.owner_password` unset, none of the OAuth routes are mounted and the
server behaves exactly as before. With it set, these appear:

```
/.well-known/oauth-protected-resource/mcp    RFC 9728 resource metadata
/.well-known/oauth-authorization-server      RFC 8414 AS metadata
/authorize   /token   /register   /revoke    OAuth 2.1 + RFC 7591
/consent                                     the approval screen
```

`service.public_host` and `service.public_scheme` must be right: every URL in those metadata
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
4. You enter `mcp.oauth.owner_password`. Only then is an authorization code minted and
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
after `mcp.oauth.consent_max_attempts`. The counter is keyed on the transaction,
not the source IP — behind an ingress every request shares one address, so
per-IP counting would lock out everyone at once or nobody.

### Revoking access

```bash
curl -sX POST https://oura.dan-leonard.com/revoke -d "token=<access-or-refresh-token>"
```

Or drop the client's record from `oauth_clients` and delete its refresh tokens
from `oauth_refresh_tokens`. Rotating `mcp.oauth.owner_password` prevents new
authorizations but does not revoke existing tokens.

## Migrating from oura-mcp + oura-sync

The data needs no migration: both services already shared the `oura_mcp`
database and the Redis instance. What changes is what runs, and where its
settings come from.

1. **Build `config.json`** from `config.example.json`. The values that matter
   most are the ones already in use -- reuse them rather than generating new
   ones (see what rotating each costs in [Generating the secrets](#generating-the-secrets)).
   Where each old variable goes:

   | Old environment variable                         | config.json key                       |
   | ------------------------------------------------ | ------------------------------------- |
   | `OURA_CLIENT_ID` / `OURA_CLIENT_SECRET`          | `oura.client_id` / `oura.client_secret` |
   | `OURA_REDIRECT_URI` / `OURA_SCOPES` / `OURA_ACCOUNT_ID` | `oura.redirect_uri` / `oura.scopes` / `oura.account_id` |
   | `TOKEN_ENCRYPTION_KEY`                           | `tokens.encryption_key`               |
   | `MONGODB_URI` / `MONGODB_DATABASE`               | `mongo.connection_string` / `mongo.database` |
   | `REDIS_URL`                                      | `redis.connection_string`             |
   | `MCP_API_KEY`                                    | `mcp.api_key`                         |
   | `MCP_OWNER_PASSWORD`                             | `mcp.oauth.owner_password`            |
   | `MCP_EXTRA_ALLOWED_HOSTS` (comma-separated)      | `mcp.extra_allowed_hosts` (a list)    |
   | `OURA_WEBHOOK_VERIFICATION_TOKEN`                | `webhooks.verification_token`         |
   | `PUBLIC_HOST` / `PUBLIC_SCHEME` / `LOG_LEVEL`    | `service.public_host` / `service.public_scheme` / `service.log_level` |
   | `SYNC_RESOURCES` (comma-separated)               | `sync.resources` (a list)             |
   | `AAD_TENANT_ID` / `AAD_AUDIENCE` / `AAD_REQUIRED_ROLE` | `auth.tenant_id` / `auth.audiences` (a list) / `auth.required_role` |
   | `SYNC_SERVICE_BASE_URL`, `AAD_CLIENT_ID`, `AAD_CLIENT_SECRET`, `AAD_SYNC_SCOPE` | gone -- MCP now reads snapshots in-process |

   The running `oura-mcp` deployment's values are in the `oura-mcp` Secret in
   the `oura` namespace. Put the finished file where CI/CD picks it up, and
   in `oura-sync/` for `deploy.bat`.

2. **Free the hostname and the Issuer.** The old `oura-mcp` release owns the
   `oura.dan-leonard.com` Ingress and the `letsencrypt-prod` Issuer. Helm will
   refuse to adopt an Issuer another release owns, and two Ingresses on one
   host route unpredictably, so remove it first:

   ```bash
   helm uninstall oura-mcp -n oura
   ```

   The MCP endpoint is down from here until step 3 finishes, a minute or so.

3. **Deploy this service**: `deploy.bat`. Same release name, same Service
   name, so the cron scheduler's `/internal/sync/run` target does not change.
4. **Repoint the api-gateway** `/api/oura/*` mapping from
   `oura-mcp.oura.svc.cluster.local` to `oura-sync.oura.svc.cluster.local`.
5. **Clean up** once it is healthy: the `oura-mcp` and (if one was ever
   created) `oura-shared` Secrets, and the local `.env.prod` files -- nothing
   reads them any more. The Azure AD app registration `oura-mcp` used to call
   `oura-sync` can be retired too.

MCP clients keep working unchanged: the URL, the API key and any issued OAuth
tokens (stored in Mongo and Redis, not in the pod) all carry over.

---

## Limitations

- **OAuth authorizations are approved with one shared owner secret**, not
  per-user accounts. Correct for a single-person deployment; it is not a
  multi-tenant authorization model.
- **Single account per deployment.** `oura.account_id` namespaces every store,
  cache key and lock, so multi-account support means routing an account id
  through the tool layer — not a rewrite, but not done.
- **No write operations to Oura.** Deliberate. Oura's API is read-only for
  these collections anyway.
- **Sandbox endpoints are not exposed.** Oura mirrors every collection under
  `/v2/sandbox/`; this server always talks to live data.
- **Document-by-id lookups are not surfaced as a tool.** `OuraService.get_document`
  exists and works, but no tool calls it — webhooks carry an `object_id`, and
  nothing yet needs to fetch by it.
- **The synced copy has no retention policy.** It grows with your data --
  heart rate is a few hundred samples a day -- and nothing prunes it.
- **The back-sync can only reach what Oura still has.** Battery samples Oura
  had already dropped before the first run are gone.
- **Only `oura_resource_history` reads the copy.** The other tools read Oura
  live.
- **Ring wear status lags the ring.** It is only as fresh as the ring's last
  sync through the phone; with the phone away it reads `UNKNOWN`, by design.
  A `daily_activity` webhook could trigger a poll sooner; nothing wires that
  up yet. Charging is non-wear, as Oura classifies it.
- **Webhook events are not proactively fetched.** A notification invalidates
  the cache and is recorded; the next tool call fetches fresh data, and the
  next sync archives it.
- **Webhook subscriptions are not auto-renewed.** They expire; the renew
  endpoint exists but nothing calls it on a schedule. One more
  `JobScheduler` job would close this.
- **`x-oura-timestamp` is not checked for freshness.** Oura does not document a
  tolerance window, and rejecting on clock skew would drop valid events. Redis
  deduplication already blocks replays within its TTL.
