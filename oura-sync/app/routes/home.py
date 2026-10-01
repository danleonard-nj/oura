"""Public landing page; no service dependencies or account data."""

from quart import Blueprint, current_app

home_bp = Blueprint("home", __name__)


@home_bp.route("/", methods=["GET"])
async def index():
    return await current_app.send_static_file("index.html")
