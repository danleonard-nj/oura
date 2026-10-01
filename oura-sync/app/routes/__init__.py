"""HTTP route blueprints. The MCP endpoint is not here -- see `app.mcp`."""

from app.routes.admin import admin_bp
from app.routes.consent import consent_bp
from app.routes.health import health_bp
from app.routes.home import home_bp
from app.routes.oauth import oauth_bp
from app.routes.sync import sync_bp
from app.routes.webhooks import webhooks_bp

__all__ = [
    "admin_bp",
    "consent_bp",
    "health_bp",
    "home_bp",
    "oauth_bp",
    "sync_bp",
    "webhooks_bp",
]
