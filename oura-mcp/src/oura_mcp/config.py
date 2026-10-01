"""Typed application configuration, loaded once at startup and injected."""

from __future__ import annotations

from functools import cached_property

from pydantic import Field, SecretStr, field_validator

from oura_common.config import DEFAULT_SCOPES, CommonConfig

__all__ = ["Config", "DEFAULT_SCOPES", "load_config"]


class Config(CommonConfig):
    """Every environment-specific value the service needs, on top of the
    shared Oura/Mongo/Redis fields in `CommonConfig`.

    Validation happens at construction, so a missing or malformed value fails
    the process at startup rather than on the first request that needs it.
    """

    service_name: str = "oura-mcp"

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

    # ── HTTP ──────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    public_host: str = ""
    # Scheme the service is reached on from outside. Only ever http for
    # local development; OAuth metadata URLs are built from it, and an
    # http issuer would have clients sending tokens in the clear.
    public_scheme: str = "https"

    # Extra Host header values the MCP transport will accept, comma-separated.
    # Needed when something in front of the service rewrites Host -- a reverse
    # proxy forwarding to the cluster-internal name, for example. Leave empty
    # unless a proxy actually needs it; every entry widens the DNS-rebinding
    # allowlist.
    mcp_extra_allowed_hosts: str = ""

    # ── Webhooks ──────────────────────────────────────────────────────────
    oura_webhook_verification_token: SecretStr | None = None
    webhook_dedupe_ttl_seconds: int = 3600

    # ── Calling oura-sync (historical snapshots oura-mcp cannot fetch live) ─
    sync_service_base_url: str | None = None
    # This deployment's own Azure AD app registration -- used as the *caller*
    # identity when requesting a token scoped to oura-sync.
    aad_tenant_id: str | None = None
    aad_client_id: str | None = None
    aad_client_secret: SecretStr | None = None
    # oura-sync's Application ID URI, e.g. "api://<oura-sync-app-id>/.default".
    aad_sync_scope: str | None = None

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
