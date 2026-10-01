# oura-sync

Polls Oura resources the live Oura API cannot show history for, and stores
only the snapshots that changed. `oura-mcp` calls this service to answer
questions like "when did my ring's firmware update" or "chart my battery
level this month" -- things Oura's API has no way to answer itself.

```
external cron ──▶ POST /internal/sync/run ──▶ oura-sync ──▶ Oura API v2
                                                    │
oura-mcp ──▶ GET /api/snapshots/{resource} ◀────────┘  (Mongo: resource_snapshots)
```

No public endpoint, no ingress, no TLS chart templates: this service is only
ever reached through the internal API gateway or from inside the cluster.

---

## Why this exists

Three resources have no real history behind them even though something in
this codebase can technically ask Oura for them:

| Resource             | What Oura's live API gives you                                    |
| --------------------- | ------------------------------------------------------------------ |
| `ring_configuration`  | Current model, colour, firmware version, setup date -- no history  |
| `ring_battery_level`  | Accepts a date range, but Oura does not durably retain a long history behind it |
| `personal_info`       | Current profile only -- no history                                 |

Polling on a cadence and keeping only the snapshots that actually differ from
the last one gives a genuine change history for all three, without storing
health data Oura already tracks properly (sleep, readiness, activity, ...) --
see `oura-mcp`'s README for why this project deliberately does not mirror
that data.

## Architecture

```
src/oura_sync/
  app.py              ASGI composition root: health/ready + the sync/snapshot API
  config.py           typed configuration, validated at startup
  container.py        DEPI composition root -- registers what this service adds
  api/                route handlers, the gateway-auth guard, correlation-id logging
  services/
    snapshot_service.py   fetch one resource live, hash it, store only if changed
    sync_service.py        orchestrates one run across the configured resources
  repositories/
    snapshot_repository.py   resource_snapshots collection

../oura-common/src/oura_common/   shared with oura-mcp -- see that project's README
  Same TokenService/OuraService/CacheService/OuraApiClient/TokenRepository
  oura-mcp uses, plus oura_common.azure_ad (the gateway-auth code both
  services' inbound/outbound sides are built on).
```

`oura-common` is a plain shared source tree, not an installed package (see
`oura-common/src/oura_common/__init__.py`). Reached via pytest's `pythonpath`
setting for tests and Docker's `.pth`-file mechanism for the built image (see
the Dockerfile). **Running the app directly (not under pytest)** needs the
sibling directory on `PYTHONPATH`:

```bash
PYTHONPATH=../oura-common/src hypercorn "oura_sync.app:create_app()" --bind 0.0.0.0:8000 --reload
```

### Single account per deployment

Same constraint as oura-mcp: `OURA_ACCOUNT_ID` namespaces every store, and
there is one configured account per deployment. `SnapshotService`/`SyncService`
work against `OuraService.account_id` directly rather than taking one as a
parameter -- see oura-mcp's README for why this is a routing change, not a
rewrite, if that ever needs to change.

### No internal scheduler

`/internal/sync/run` is a plain POST endpoint; nothing in this repo decides
when it runs. An external scheduler (a C# cron job, in this deployment) calls
it on whatever cadence is decided later, through the same API gateway the
other internal services sit behind. There is no APScheduler, no Kubernetes
CronJob manifest in this chart -- if one is added later, it would run this
same image with a different command (`curl`-ing the endpoint, or a small CLI
entrypoint), not an in-process timer competing with the web server for the
event loop.

### Storage: change log, not poll log

Each poll fetches a resource live from Oura (bypassing the response cache --
a cache hit would look like "nothing changed" even when it did), hashes the
payload, and compares it to the most recent stored snapshot for that
`(account_id, resource)`. Identical payloads never create a new document --
`last_seen_at` is bumped on the existing one instead. Only a genuine change
inserts a new row. This bounds storage growth regardless of poll frequency
and gives a real "when did this change" answer, not "how many times did we
poll".

For the two date-ranged resources (`ring_configuration`, `ring_battery_level`),
one poll fetches *today's* record(s) and stores the most recent one -- a day
can have more than one entry (battery level sampled repeatedly, for example).

---

## Local setup

Requires Python 3.12+, plus a reachable MongoDB and Redis -- the same ones
oura-mcp uses, since both read and write the same `oauth_tokens` collection.

```bash
cd oura-sync
python -m venv .venv && .venv/Scripts/activate     # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example ../.env
```

**The `.env` lives at the repo root, not in this directory.** oura-mcp and
oura-sync share several values byte-for-byte (`OURA_CLIENT_ID`/`SECRET`,
`TOKEN_ENCRYPTION_KEY`, `MONGODB_URI`, `REDIS_URL`), so there is one file for
both instead of two kept in sync by hand. `load_dotenv()` (called with no
path in `app.py`) finds it automatically by walking **up** from `app.py`'s
own location, not from your shell's current directory -- this works
regardless of where you `cd` from, as long as neither service directory has
its own `.env` shadowing the root one. See oura-mcp's README for the same
note in more detail.

`TOKEN_ENCRYPTION_KEY` must be the **exact same key** oura-mcp uses -- both
decrypt the same encrypted token documents. Sharing one `.env` makes that
automatic instead of something to remember.

Connecting an Oura account happens through oura-mcp or the `oura-auth`
bootstrap tool, not here -- oura-sync only ever reads and refreshes an
existing token, it never drives the authorization-code flow.

---

## Environment variables

Required — the process refuses to start without them:

| Variable               | Purpose                                                          |
| ---------------------- | ----------------------------------------------------------------- |
| `OURA_CLIENT_ID`       | Same Oura OAuth application oura-mcp uses                         |
| `OURA_CLIENT_SECRET`   | Same application's client secret                                  |
| `OURA_REDIRECT_URI`    | Required by shared config validation; never actually used here -- oura-sync only refreshes, it never authorizes |
| `TOKEN_ENCRYPTION_KEY` | Must match oura-mcp's exactly                                      |
| `MONGODB_URI`          | Same database oura-mcp uses                                       |
| `REDIS_URL`            | Same instance oura-mcp uses                                       |

Functionally required, though the process starts without them (every gateway
route just always refuses instead):

| Variable            | Purpose                                                                 |
| -------------------- | ------------------------------------------------------------------------ |
| `AAD_TENANT_ID`      | Azure AD tenant the caller's token must be issued from                  |
| `AAD_AUDIENCE`       | This service's own Application ID URI, e.g. `api://<oura-sync-app-id>` |
| `AAD_REQUIRED_ROLE`  | Optional: also require this value in the token's `roles` claim          |

Optional:

| Variable          | Default                                        | Purpose                        |
| ----------------- | ----------------------------------------------- | ------------------------------- |
| `SYNC_RESOURCES`  | `ring_configuration,ring_battery_level,personal_info` | Default resources for `POST /internal/sync/run` |
| `OURA_ACCOUNT_ID` | `default`                                       | Must match oura-mcp's           |
| `LOG_LEVEL`       | `INFO`                                          |                                  |
| `HOST` / `PORT`   | `0.0.0.0` / `8000`                              |                                  |

Tuning knobs (executor size, timeouts, retry budget, range caps, lock
timings) are the same ones oura-mcp exposes, listed with their defaults in
the repo-root `.env.example`. `CACHE_ENABLED` has no effect on syncing itself -- every poll
already bypasses the cache explicitly -- but the underlying `CacheService` is
still wired, so it's there if a future feature needs it.

---

## Running locally

```bash
PYTHONPATH=../oura-common/src hypercorn "oura_sync.app:create_app()" --bind 0.0.0.0:8000 --reload
```

```bash
curl -s localhost:8000/health | jq
curl -s localhost:8000/ready | jq
```

Trigger a sync run and query what it stored (needs a valid Azure AD bearer
token -- see [Auth](#auth)):

```bash
curl -sX POST localhost:8000/internal/sync/run \
  -H "Authorization: Bearer $TOKEN" | jq

curl -s "localhost:8000/api/snapshots/ring_battery_level?limit=20" \
  -H "Authorization: Bearer $TOKEN" | jq
```

---

## API

| Route                          | Method | Auth                | Purpose                                              |
| ------------------------------- | ------ | -------------------- | ----------------------------------------------------- |
| `/health`                      | GET    | none                 | Liveness, dependency-free                             |
| `/ready`                       | GET    | none                 | Readiness; checks Mongo and Redis                      |
| `/internal/sync/run`           | POST   | Azure AD bearer token | Poll the configured (or requested) resources          |
| `/api/snapshots/{resource}`    | GET    | Azure AD bearer token | Historical snapshots of one resource, oldest first     |

`POST /internal/sync/run` takes an optional JSON body,
`{"resources": ["ring_battery_level"]}`, to override the default list from
`SYNC_RESOURCES`. Returns `{"results": [{"resource": ..., "changed": bool}, ...]}`,
with a per-resource `"error"` key instead of stopping the whole run if one
resource fails -- the same "don't let one failure sink the rest" reasoning
`SummaryService` applies to its fan-out.

`GET /api/snapshots/{resource}` takes `since`/`until` (ISO-8601, filtering on
when a snapshot was first seen) and `limit` (default 100, max 1000). Returns
`{"count": N, "snapshots": [{"resource", "content_hash", "payload",
"first_seen_at", "last_seen_at"}, ...]}`.

## Auth

Both authenticated routes are gated by `oura_common.azure_ad.verify_bearer_token`:
an Azure AD-issued JWT, validated against the tenant's JWKS (signature,
issuer, audience, expiry, and optionally a required `roles` claim entry) --
the same client-credentials pattern the other internal services here use to
call each other through the API gateway. See `oura_common.azure_ad` for the
implementation and oura-mcp's README for the caller side
(`AzureAdTokenClient`, used by its `clients/sync_client.py`).

Unconfigured (`AAD_TENANT_ID`/`AAD_AUDIENCE` unset) both routes always return
`503 not_configured` -- there is no unauthenticated fallback for them, unlike
oura-mcp's static-key admin routes.

---

## Docker

```bash
# from the repo root -- the build context, not this directory
docker build -t oura-sync:0.1.0 -f oura-sync/Dockerfile .
```

```bash
docker run --rm -p 8000:8000 --env-file .env oura-sync:0.1.0   # the repo-root .env
```

Multi-stage, `python:3.12-slim`, non-root uid/gid `10002` (distinct from
oura-mcp's `10001`, so both can run on the same node without an `fsGroup`
collision). `oura-common`'s source is copied into the runtime image and put
on the venv's import path via a `.pth` file — see the Dockerfile comment.

---

## Helm

No Mongo/Redis deployed by this chart either; same expectation as oura-mcp.

```bash
helm upgrade --install oura-sync ./helm/oura-sync \
  --namespace oura \
  --set image.tag=0.1.0 \
  --set existingSecret=oura-shared
```

`oura-shared` is the same Secret oura-mcp's release points at -- see
[Kubernetes secrets](#kubernetes-secrets).

Or run `deploy.bat`. No `ingress.yaml`/`issuer.yaml` in this chart at all --
add them if this service ever needs to be reached from outside the internal
gateway/cluster, but nothing about the design assumes that.

---

## Kubernetes secrets

**Managed from the repo root, not from this directory** -- oura-sync shares
the `oura-shared` Secret with oura-mcp, one push updates both:

```bash
cd ..                          # the repo root
cp .env.example .env.prod      # then fill it in
push-secret.bat                # validate, create the Secret, restart BOTH deployments
push-secret.bat --overwrite    # validate, update the Secret, restart BOTH deployments
```

`../scripts/validate_env_file.py` catches the same `kubectl --from-env-file`
foot-guns as before (quoted values, trailing whitespace, silent duplicate
keys) plus a note if `AAD_TENANT_ID`/`AAD_AUDIENCE` are missing -- see
oura-mcp's README for the full rationale, since the script and the Secret are
both shared.

---

## Testing

```bash
pytest -q
```

Covered: `SnapshotService`'s hash-diff logic (unchanged payload -> no new
document, just a `last_seen_at` bump; changed payload -> a new document; a
date-ranged resource with multiple records in a day stores the most recent
one). The Azure AD token-validation logic itself (signature, issuer,
audience, expiry, required role) is tested in `oura-common/tests/test_azure_ad.py`,
since that's where it lives; this project's `tests/test_auth.py` only covers
the thin Starlette-response wrapper around it.

---

## Limitations

- **Single account per deployment**, same as oura-mcp -- see that project's
  README for what changing this would take.
- **No internal scheduler.** Deliberate; see [above](#no-internal-scheduler).
- **Only three resources tracked today** (`ring_configuration`,
  `ring_battery_level`, `personal_info`). Adding another means adding it to
  `SYNC_RESOURCES` and to `SnapshotService`'s date-ranged-vs-singleton branch
  if it needs the singleton (`fetch_personal_info`-style) path.
- **No retention/pruning policy.** A change log grows only on real changes,
  which for these three resources should be rare -- but nothing here caps it
  yet.
