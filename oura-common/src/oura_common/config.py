"""Configuration shared by every service that talks to the Oura API.

Each service defines its own `Config(CommonConfig)` subclass adding the
fields only it needs (MCP settings, sync-job settings, AAD gateway settings)
-- see that service's own `config.py`.
"""

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


class CommonConfig(BaseSettings):
    """Every environment-specific value the shared Oura integration layer needs.

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

    # ── Secrets ───────────────────────────────────────────────────────────
    token_encryption_key: SecretStr

    # ── Infrastructure ────────────────────────────────────────────────────
    mongodb_uri: SecretStr
    mongodb_database: str = "oura_mcp"
    redis_url: SecretStr

    # This service's own name -- used as the Mongo connection appname and the
    # outbound HTTP User-Agent, so both are traceable back to the right
    # service. Each service overrides the default in its own Config subclass.
    service_name: str = "oura-common"

    log_level: str = "INFO"

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

    @model_validator(mode="after")
    def _validate_encryption_key(self) -> "CommonConfig":
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
