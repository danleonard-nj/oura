"""HTTP route blueprints. The MCP endpoint is not here -- see `app.mcp`.

Blueprints are built by factories taking the app's `QuartInjector`, so each
view declares its dependencies by type and the injector supplies them.
"""

from app.routes.admin import create_admin_blueprint
from app.routes.consent import create_consent_blueprint
from app.routes.health import create_health_blueprint
from app.routes.home import home_bp
from app.routes.oauth import create_oauth_blueprint
from app.routes.sync import create_sync_blueprint
from app.routes.wear import create_wear_blueprint
from app.routes.webhooks import create_webhooks_blueprint

__all__ = [
    "create_admin_blueprint",
    "create_consent_blueprint",
    "create_health_blueprint",
    "create_oauth_blueprint",
    "create_sync_blueprint",
    "create_wear_blueprint",
    "create_webhooks_blueprint",
    "home_bp",
]
