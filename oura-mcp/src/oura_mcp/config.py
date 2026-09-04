"""Typed application configuration, loaded once at startup and injected."""

from __future__ import annotations

from functools import cached_property

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SCOPES = "email personal daily heartrate workout tag session spo2"


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
    log_level: str = "INFO"

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
        return hosts

    @cached_property
    def allowed_origins(self) -> list[str]:
        origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
        if self.public_host:
            origins.extend([f"https://{self.public_host}", f"http://{self.public_host}"])
        return origins


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
