"""Typed application configuration, loaded once at startup and injected."""

from __future__ import annotations

from functools import cached_property

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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


class Config(BaseSettings):
    """Every environment-specific value the service needs.

    Validation happens at construction, so a missing or malformed value fails
    the process at startup rather than on the first request that needs it.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Oura OAuth application ────────────────────────────────────────────
    oura_client_id: str = Field(min_length=1)
    oura_client_secret: SecretStr
    oura_redirect_uri: str = Field(min_length=1)
    oura_scopes: str = DEFAULT_SCOPES

    # Identifies the Oura account this deployment serves. Single account today;
    # every store, cache key and lock is already namespaced by it so adding
    # more later is a routing change, not a rewrite.
    oura_account_id: str = "default"

    # ── MCP ───────────────────────────────────────────────────────────────
    mcp_api_key: SecretStr
    mcp_path: str = "/mcp"
    mcp_stateless: bool = True
    mcp_server_name: str = "oura"

    # ── MCP OAuth (for clients that cannot send a static header) ──────────
    # Leave unset to run with bearer-token auth only. Setting it turns this
    # service into an OAuth 2.1 authorization server as well, which is what
    # ChatGPT and other discovery-only clients require.
    #
    # This is the secret you type on the consent screen to approve an
    # authorization. Generate it like an API key, not like a memorable
    # password -- it is compared directly, so its entropy is its strength.
    mcp_owner_password: SecretStr | None = None
    oauth_access_token_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    oauth_refresh_token_ttl_seconds: int = Field(default=2592000, ge=3600)
    oauth_authorization_code_ttl_seconds: int = Field(default=60, ge=30, le=600)
    oauth_consent_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    oauth_consent_max_attempts: int = Field(default=5, ge=1, le=50)
    oauth_consent_lockout_seconds: int = Field(default=900, ge=60)

    # ── Secrets ───────────────────────────────────────────────────────────
    token_encryption_key: SecretStr

    # ── Infrastructure ────────────────────────────────────────────────────
    mongodb_uri: SecretStr
    mongodb_database: str = "oura_mcp"
    redis_url: SecretStr

    # ── HTTP ──────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    public_host: str = ""
    # Scheme the service is reached on from outside. Only ever http for
    # local development; OAuth metadata URLs are built from it, and an
    # http issuer would have clients sending tokens in the clear.
    public_scheme: str = "https"
    log_level: str = "INFO"

    # Extra Host header values the MCP transport will accept, comma-separated.
    # Needed when something in front of the service rewrites Host -- a reverse
    # proxy forwarding to the cluster-internal name, for example. Leave empty
    # unless a proxy actually needs it; every entry widens the DNS-rebinding
    # allowlist.
    mcp_extra_allowed_hosts: str = ""

    # ── Webhooks ──────────────────────────────────────────────────────────
    oura_webhook_verification_token: SecretStr | None = None
    webhook_dedupe_ttl_seconds: int = 3600

    # ── Behaviour tuning ──────────────────────────────────────────────────
    oura_executor_workers: int = Field(default=8, ge=1, le=64)
    oura_request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    oura_max_attempts: int = Field(default=3, ge=1, le=6)
    oura_max_range_days: int = Field(default=180, ge=1, le=1000)
    oura_max_heartrate_days: int = Field(default=14, ge=1, le=120)

    cache_enabled: bool = True
    cache_ttl_recent_seconds: int = Field(default=300, ge=0)
    cache_ttl_historical_seconds: int = Field(default=21600, ge=0)
    cache_ttl_static_seconds: int = Field(default=86400, ge=0)

    refresh_lock_ttl_seconds: int = Field(default=30, ge=5, le=300)
    refresh_lock_wait_seconds: int = Field(default=15, ge=1, le=120)
    token_expiry_skew_seconds: int = Field(default=120, ge=0, le=3600)
    oauth_state_ttl_seconds: int = Field(default=600, ge=60, le=3600)

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"log_level must be a standard logging level, got {value!r}")
        return level

    @field_validator("mcp_path")
    @classmethod
    def _normalise_mcp_path(cls, value: str) -> str:
        path = "/" + value.strip("/")
        if path == "/":
            raise ValueError("mcp_path must not be the application root")
        return path

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

    @model_validator(mode="after")
    def _validate_encryption_key(self) -> "Config":
        from cryptography.fernet import Fernet

        try:
            Fernet(self.token_encryption_key.get_secret_value().encode())
        except Exception as exc:  # noqa: BLE001 - startup validation boundary
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY must be a urlsafe base64-encoded 32-byte key; "
                "generate one with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            ) from exc
        return self

    @property
    def mcp_oauth_enabled(self) -> bool:
        """OAuth is served only when an owner password is configured.

        Without one there is no way to approve an authorization, so the
        endpoints would exist but never issue a token.
        """
        return self.mcp_owner_password is not None

    @property
    def public_base_url(self) -> str:
        """The externally reachable origin, used for OAuth metadata URLs."""
        if self.public_host:
            return f"{self.public_scheme}://{self.public_host}"
        return f"http://localhost:{self.port}"

    @cached_property
    def scope_list(self) -> list[str]:
        return self.oura_scopes.split()

    @cached_property
    def allowed_hosts(self) -> list[str]:
        """Host header values the MCP transport will accept.

        Production traffic arrives through the ingress hostname, so that must be
        present or the SDK's DNS-rebinding guard rejects every request.
        """
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost"]
        if self.public_host:
            hosts.extend([self.public_host, f"{self.public_host}:*"])
        for extra in self._split(self.mcp_extra_allowed_hosts):
            hosts.extend([extra, f"{extra}:*"])
        return hosts

    @cached_property
    def allowed_origins(self) -> list[str]:
        origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
        for host in [self.public_host, *self._split(self.mcp_extra_allowed_hosts)]:
            if host:
                origins.extend([f"https://{host}", f"http://{host}"])
        return origins

    @staticmethod
    def _split(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]


def load_config() -> Config:
    """Build the configuration, translating validation failures into a clear error."""
    from pydantic import ValidationError

    try:
        return Config()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing = [
            ".".join(str(p) for p in err["loc"]).upper()
            for err in exc.errors()
            if err["type"] == "missing"
        ]
        detail = f" Missing required environment variables: {', '.join(missing)}." if missing else ""
        raise RuntimeError(f"Invalid configuration.{detail}\n{exc}") from exc
