"""Typed application configuration, loaded once at startup and injected."""

from __future__ import annotations

from functools import cached_property

from oura_common.config import CommonConfig
from oura_common.models.resources import Resource

DEFAULT_SYNC_RESOURCES = "ring_configuration,ring_battery_level,personal_info"


class Config(CommonConfig):
    """Every environment-specific value oura-sync needs, on top of the
    shared Oura/Mongo/Redis fields in `CommonConfig`.
    """

    service_name: str = "oura-sync"

    host: str = "0.0.0.0"
    port: int = 8000

    # Resources polled by `POST /internal/sync/run` when the caller does not
    # override the list. Comma-separated, not derived from `ResourceSpec.static`
    # -- that flag drives oura-mcp's cache-TTL choice, a different concern, and
    # would silently exclude ring_battery_level (see oura-common's resources.py).
    sync_resources: str = DEFAULT_SYNC_RESOURCES

    # ── Inbound gateway auth ──────────────────────────────────────────────
    # Validates the Azure AD bearer token on /internal/sync/run and
    # /api/snapshots/*. All three unset means those routes always refuse --
    # there is no unauthenticated fallback for them.
    aad_tenant_id: str | None = None
    # This service's own Application ID URI (the audience callers request a
    # token for), e.g. "api://<oura-sync-app-id>".
    aad_audience: str | None = None
    # Optional: require this value in the token's `roles` claim.
    aad_required_role: str | None = None

    @cached_property
    def sync_resource_list(self) -> list[Resource]:
        return [Resource(value.strip()) for value in self.sync_resources.split(",") if value.strip()]


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
