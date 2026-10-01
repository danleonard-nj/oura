"""Public landing page; no service dependencies or account data."""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def index(_: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")
