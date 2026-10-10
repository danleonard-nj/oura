"""Typed configuration sections, bound from framework's `Configuration`.

`Configuration` reads `config.json` (or `config.dev.json` / `config.local.json`
by `FLASK_ENV`, then `QUART_ENV`) from the working directory. That file is never committed: it
is dropped into the build by CI/CD, the same as kube-tools-api. See
`config.example.json` for its shape and the README for generating the secrets
in it.

Each top-level key of the file is one section below. `Configuration` is
registered in DI, and each section is a factory reading its key from it (see
`utilities/provider.py`), so a service takes exactly the section it needs --
`OuraConfig`, `McpConfig`, ... -- rather than a grab-bag of every setting.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from app.models.resources import Resource

__all__ = [
    "bind_section",
    "CacheConfig",
    "ConfigurationError",
    "DEFAULT_SCOPES",
    "GatewayAuthConfig",
    "McpConfig",
    "McpOAuthConfig",
    "MongoConfig",
    "OuraConfig",
    "RedisConfig",
    "SECTIONS",
    "ServiceConfig",
    "SyncConfig",
    "TokenConfig",
    "WearConfig",
    "WebhookConfig",
    "bind_sections",
]

# Oura's OpenAPI spec lists only: email personal daily heartrate workout tag
# session spo2Daily. Three more exist and are required by real endpoints, and
# Oura returns 401 -- not 403 -- when they are missing:
#   stress             -> daily_resilience
#   heart_health       -> daily_cardiovascular_age, vO2_max
#   ring_configuration -> ring_configuration, ring_battery_level
DEFAULT_SCOPES = (
    "email personal daily heartrate workout tag session spo2 "
    "stress heart_health ring_configuration"
)

#: Everything Oura exposes. Ungranted scopes just fail that resource's sync.
DEFAULT_SYNC_RESOURCES = list(Resource)


class _Section(BaseModel):
    # Unknown keys are ignored, so the file can carry notes or settings for a
    # later version without failing an older one.
    model_config = ConfigDict(extra="ignore", frozen=True)


class ServiceConfig(_Section):
    """`service`: identity and public address of this deployment."""

    # Mongo connection appname and the outbound HTTP User-Agent.
    name: str = "oura-sync"
    log_level: str = "INFO"
    # The ingress hostname, no scheme. Required in production: OAuth metadata
    # URLs and the MCP transport's Host allowlist are built from it.
    public_host: str = ""
    # Only ever http for local development; an http issuer would have OAuth
    # clients sending tokens in the clear.
    public_scheme: str = "https"
    # Used only for the local fallback of `public_base_url`.
    port: int = 8000

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"log_level must be a standard logging level, got {value!r}")
        return level

    @field_validator("public_scheme")
    @classmethod
    def _validate_scheme(cls, value: str) -> str:
        scheme = value.strip().lower()
        if scheme not in {"http", "https"}:
            raise ValueError(f"public_scheme must be http or https, got {value!r}")
        return scheme

    @field_validator("public_host")
    @classmethod
    def _strip_scheme(cls, value: str) -> str:
        return value.strip().removeprefix("https://").removeprefix("http://").rstrip("/")

    @property
    def public_base_url(self) -> str:
        """The externally reachable origin, used for OAuth metadata URLs."""
        if self.public_host:
            return f"{self.public_scheme}://{self.public_host}"
        return f"http://localhost:{self.port}"


class OuraConfig(_Section):
    """`oura`: the Oura OAuth application and how we call its API."""

    client_id: str = Field(min_length=1)
    # Also the webhook HMAC key.
    client_secret: SecretStr
    # Must match the redirect URI registered on the Oura application exactly.
    redirect_uri: str = Field(min_length=1)
    scopes: str = DEFAULT_SCOPES

    # Identifies the Oura account this deployment serves. Single account today;
    # every store, cache key and lock is already namespaced by it so adding
    # more later is a routing change, not a rewrite.
    account_id: str = "default"

    # Ceiling on concurrent upstream connections.
    executor_workers: int = Field(default=8, ge=1, le=64)
    request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_attempts: int = Field(default=3, ge=1, le=6)
    max_range_days: int = Field(default=180, ge=1, le=1000)
    max_heartrate_days: int = Field(default=14, ge=1, le=120)
    # Lifetime of the `state` nonce in our own Oura authorization-code flow.
    oauth_state_ttl_seconds: int = Field(default=600, ge=60, le=3600)

    @property
    def scope_list(self) -> list[str]:
        return self.scopes.split()


class TokenConfig(_Section):
    """`tokens`: encryption at rest and refresh behaviour for the Oura token."""

    # Fernet key. Rotating it makes every stored token undecryptable and the
    # account has to reconnect.
    encryption_key: SecretStr
    expiry_skew_seconds: int = Field(default=120, ge=0, le=3600)
    refresh_lock_ttl_seconds: int = Field(default=30, ge=5, le=300)
    refresh_lock_wait_seconds: int = Field(default=15, ge=1, le=120)

    @field_validator("encryption_key")
    @classmethod
    def _validate_encryption_key(cls, value: SecretStr) -> SecretStr:
        from cryptography.fernet import Fernet

        try:
            Fernet(value.get_secret_value().encode())
        except Exception as exc:  # noqa: BLE001 - startup validation boundary
            raise ValueError(
                "tokens.encryption_key must be a urlsafe base64-encoded 32-byte Fernet key "
                "(see the README for how to generate one)"
            ) from exc
        return value


class MongoConfig(_Section):
    """`mongo`: the existing cluster. Tokens, audit, snapshots and OAuth state live here."""

    connection_string: SecretStr
    # Named for the service that created it; kept so existing data stays put.
    database: str = "oura_mcp"


class RedisConfig(_Section):
    """`redis`: the refresh lock (required for correctness), cache and dedupe."""

    connection_string: SecretStr


class CacheConfig(_Section):
    """`cache`: Redis response caching of Oura reads."""

    enabled: bool = True
    ttl_recent_seconds: int = Field(default=300, ge=0)
    ttl_historical_seconds: int = Field(default=21600, ge=0)
    ttl_static_seconds: int = Field(default=86400, ge=0)


class WebhookConfig(_Section):
    """`webhooks`: Oura webhook subscription verification and deduplication."""

    # Your own random string, echoed back during subscription verification.
    # Unset, webhook subscriptions cannot be created or verified.
    verification_token: SecretStr | None = None
    dedupe_ttl_seconds: int = Field(default=3600, ge=60)


class McpOAuthConfig(_Section):
    """`mcp.oauth`: this service as an OAuth 2.1 authorization server for MCP clients."""

    # Typed on the consent screen to approve an authorization. Leave unset to
    # run with bearer-key auth only. Compared directly, so its entropy is its
    # strength -- generate it like an API key.
    owner_password: SecretStr | None = None
    access_token_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    refresh_token_ttl_seconds: int = Field(default=2592000, ge=3600)
    authorization_code_ttl_seconds: int = Field(default=60, ge=30, le=600)
    consent_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    consent_max_attempts: int = Field(default=5, ge=1, le=50)
    consent_lockout_seconds: int = Field(default=900, ge=60)

    @property
    def enabled(self) -> bool:
        """Without an owner password nothing could ever be approved."""
        return self.owner_password is not None


class McpConfig(_Section):
    """`mcp`: the MCP endpoint and its auth."""

    # Bearer key for /mcp and the admin /api/* routes.
    api_key: SecretStr
    path: str = "/mcp"
    stateless: bool = True
    server_name: str = "oura"

    # Seal MCP request state (input-required round trips) so any replica can
    # open what another issued. Empty means a key generated per process, which
    # is fine for one replica. Each at least 32 characters; list several to
    # rotate -- the first seals, all of them open.
    request_state_secrets: list[SecretStr] = Field(default_factory=list)

    # Extra Host header values the transport accepts. Needed when a proxy
    # rewrites Host (the api-gateway forwards to the cluster-internal service
    # name). Every entry widens the DNS-rebinding allowlist.
    extra_allowed_hosts: list[str] = Field(default_factory=list)

    oauth: McpOAuthConfig = Field(default_factory=McpOAuthConfig)

    @field_validator("api_key")
    @classmethod
    def _require_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("mcp.api_key must be set; refusing to expose MCP unauthenticated")
        return value

    @field_validator("path")
    @classmethod
    def _normalise_path(cls, value: str) -> str:
        path = "/" + value.strip("/")
        if path == "/":
            raise ValueError("mcp.path must not be the application root")
        return path

    @field_validator("request_state_secrets")
    @classmethod
    def _validate_request_state_secrets(cls, value: list[SecretStr]) -> list[SecretStr]:
        if any(len(secret.get_secret_value()) < 32 for secret in value):
            raise ValueError("mcp.request_state_secrets must each be at least 32 characters")
        return value

    @field_validator("extra_allowed_hosts")
    @classmethod
    def _strip_hosts(cls, value: list[str]) -> list[str]:
        return [host.strip() for host in value if host.strip()]

    def allowed_hosts(self, service: ServiceConfig) -> list[str]:
        """Host header values the MCP transport will accept.

        Production traffic arrives through the ingress hostname, so that must be
        present or the SDK's DNS-rebinding guard rejects every request.
        """
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost"]
        for host in [service.public_host, *self.extra_allowed_hosts]:
            if host:
                hosts.extend([host, f"{host}:*"])
        return hosts

    def allowed_origins(self, service: ServiceConfig) -> list[str]:
        origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
        for host in [service.public_host, *self.extra_allowed_hosts]:
            if host:
                origins.extend([f"https://{host}", f"http://{host}"])
        return origins


class SyncConfig(_Section):
    """`sync`: copying Oura data into Mongo before Oura forgets it."""

    # Resources each sync run covers. Defaults to all of them.
    resources: list[Resource] = Field(default_factory=lambda: list(DEFAULT_SYNC_RESOURCES))
    # How often the in-process scheduler runs a sync. 0 turns it off (leaving
    # only POST /internal/sync/run).
    interval_hours: float = Field(default=6, ge=0)
    # How far back each run fetches. Overlapping runs is the point: a record
    # Oura revises (a sleep score finalised the next morning) is picked up
    # again, and a missed run loses nothing as long as the next one lands
    # inside the window.
    lookback_days: int = Field(default=2, ge=1, le=14)
    # Widest window fetched in one request. A wider range -- catching up after
    # an outage, or the backfill -- is split into windows this size. Heart
    # rate is always capped at `oura.max_heartrate_days`.
    chunk_days: int = Field(default=90, ge=1, le=365)

    # Back-sync: walk each archived resource back through Oura's history, one
    # window at a time, until there is nothing older. Resumable across
    # restarts, and finished resources cost nothing on later checks.
    backfill_enabled: bool = True
    # How far back to go. Unset, the earliest ring's setup date (from
    # ring_configuration) is the floor; failing that, the walk stops after
    # `backfill_empty_chunks` empty windows in a row.
    backfill_start: date | None = None
    backfill_empty_chunks: int = Field(default=4, ge=1, le=100)


class WearConfig(_Section):
    """`wear`: polling whether the ring is on a finger. See `services/wear_service.py`."""

    # How often the poller asks Oura for fresh evidence. 0 turns it off
    # (leaving only POST /internal/wear/poll).
    poll_interval_minutes: float = Field(default=15, ge=0)
    # Activity days re-read on every poll, so a ring that syncs late still
    # fills in the days it missed. Longer outages: POST /internal/wear/backfill.
    lookback_days: int = Field(default=3, ge=1, le=14)
    # Heart-rate samples older than this are not fetched or considered.
    heart_rate_lookback_hours: int = Field(default=6, ge=1, le=48)
    # Evidence older than this cannot say what is happening now: the status
    # reads UNKNOWN until the ring syncs again.
    max_evidence_age_minutes: int = Field(default=60, ge=5, le=24 * 60)
    # Consecutive non-wear time needed before calling the ring off. Shorter
    # runs are sensor gaps or a quick hand-wash. Rounded up to 5-minute slots.
    min_non_wear_minutes: int = Field(default=15, ge=5, le=24 * 60)

    @property
    def min_non_wear_slots(self) -> int:
        return -(-self.min_non_wear_minutes // 5)


class GatewayAuthConfig(_Section):
    """`auth`: Azure AD gateway auth on /internal/sync/run and /api/snapshots/*.

    Same shape as framework's `auth` section (`tenant_id`, `audiences`), so the
    file reads like every other service's. Unconfigured, those routes always
    refuse -- there is no unauthenticated fallback.
    """

    tenant_id: str | None = None
    # This service's own Application ID URI(s), e.g. "api://<app-id>" -- the
    # audience callers request a token for.
    audiences: list[str] = Field(default_factory=list)
    # Optional: require this value in the token's `roles` claim.
    required_role: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.tenant_id and self.audiences)


#: Section model -> top-level key in config.json.
SECTIONS: dict[type[_Section], str] = {
    ServiceConfig: "service",
    OuraConfig: "oura",
    TokenConfig: "tokens",
    MongoConfig: "mongo",
    RedisConfig: "redis",
    CacheConfig: "cache",
    WebhookConfig: "webhooks",
    McpConfig: "mcp",
    SyncConfig: "sync",
    WearConfig: "wear",
    GatewayAuthConfig: "auth",
}


class ConfigurationError(RuntimeError):
    """config.json is missing a section or a section is invalid."""


def bind_section(section_type: type[_Section], key: str, raw: dict | None) -> _Section:
    """Validate one section of the configuration, naming the bad key on failure.

    Error messages carry the key and the problem, never the value -- most of
    these are secrets.
    """
    try:
        return section_type.model_validate(raw or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{key}.{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigurationError(f"Invalid configuration -- {problems}") from exc


def bind_sections(values: dict) -> dict[type[_Section], _Section]:
    """Validate every section of a raw configuration dict at once."""
    return {
        section_type: bind_section(section_type, key, values.get(key))
        for section_type, key in SECTIONS.items()
    }
