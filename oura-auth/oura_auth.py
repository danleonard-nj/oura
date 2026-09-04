#!/usr/bin/env python3
"""One-time Oura OAuth2 bootstrap.

Runs the Authorization Code flow on your machine and writes the resulting token
pair, encrypted, into the shared `oauth_tokens` collection. oura-mcp and
oura-sync both read that document, so this only needs to be run once per
account -- after that the services rotate the token themselves.

Standalone on purpose: it depends only on pymongo, cryptography and requests,
so it can be run before either service is deployed, and from anywhere that can
reach the database.

    python oura_auth.py connect
    python oura_auth.py status
    python oura_auth.py refresh

The document schema is the contract between the three programs. See README.md.
"""

from __future__ import annotations

import argparse
import http.server
import logging
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from pymongo import MongoClient

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
DEFAULT_SCOPES = "email personal daily heartrate workout tag session spo2"
COLLECTION = "oauth_tokens"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("oura-auth")


class ConfigError(Exception):
    """A required setting is missing or malformed."""


class Settings:
    """Configuration, read once from the environment."""

    def __init__(self) -> None:
        self.client_id = _require("OURA_CLIENT_ID")
        self.client_secret = _require("OURA_CLIENT_SECRET")
        self.redirect_uri = _require("OURA_REDIRECT_URI")
        self.encryption_key = _require("TOKEN_ENCRYPTION_KEY")
        self.mongodb_uri = _require("MONGODB_URI")
        self.mongodb_database = os.getenv("MONGODB_DATABASE", "oura_mcp")
        self.account_id = os.getenv("OURA_ACCOUNT_ID", "default")
        self.scopes = os.getenv("OURA_SCOPES", DEFAULT_SCOPES)

        try:
            self.fernet = Fernet(self.encryption_key.encode())
        except Exception as exc:
            raise ConfigError(
                "TOKEN_ENCRYPTION_KEY must be a urlsafe base64-encoded 32-byte key. "
                'Generate one with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from exc

        parsed = urllib.parse.urlparse(self.redirect_uri)
        self.callback_host = parsed.hostname or "localhost"
        self.callback_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.callback_path = parsed.path or "/"
        self.is_local_callback = self.callback_host in ("localhost", "127.0.0.1")


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            "or export it in your shell."
        )
    return value


# ── Token storage ─────────────────────────────────────────────────────────


class TokenStore:
    """Reads and writes the shared, encrypted `oauth_tokens` document.

    Deliberately mirrors oura-mcp's TokenRepository: same collection, same
    field names, same Fernet encryption, same single-update rotation.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000)
        self._collection = self._client[settings.mongodb_database][COLLECTION]

    def check(self) -> None:
        self._client.admin.command("ping")
        self._collection.create_index("account_id", unique=True)

    def save(self, grant: dict[str, Any]) -> dict[str, Any]:
        """Write both token halves in one atomic update."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=int(grant["expires_in"]))
        scopes = grant.get("scope", "").split()

        return self._collection.find_one_and_update(
            {"account_id": self._settings.account_id},
            {
                "$set": {
                    "access_token": self._encrypt(grant["access_token"]),
                    "refresh_token": self._encrypt(grant["refresh_token"]),
                    "expires_at": expires_at,
                    "scopes": scopes,
                    "token_type": grant.get("token_type", "Bearer"),
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "account_id": self._settings.account_id,
                    "created_at": now,
                },
                "$inc": {"version": 1},
            },
            upsert=True,
            return_document=True,
        )

    def status(self) -> dict[str, Any] | None:
        return self._collection.find_one(
            {"account_id": self._settings.account_id},
            projection={"_id": 0, "access_token": 0, "refresh_token": 0},
        )

    def refresh_token(self) -> str:
        document = self._collection.find_one({"account_id": self._settings.account_id})
        if document is None:
            raise ConfigError(
                f"No token stored for account '{self._settings.account_id}'. "
                "Run `python oura_auth.py connect` first."
            )
        try:
            return self._settings.fernet.decrypt(document["refresh_token"].encode()).decode()
        except InvalidToken as exc:
            raise ConfigError(
                "The stored token cannot be decrypted with this TOKEN_ENCRYPTION_KEY. "
                "The key was rotated; reconnect with `python oura_auth.py connect`."
            ) from exc

    def _encrypt(self, value: str) -> str:
        return self._settings.fernet.encrypt(value.encode()).decode()

    def close(self) -> None:
        self._client.close()


# ── OAuth ─────────────────────────────────────────────────────────────────


def exchange(settings: Settings, payload: dict[str, str]) -> dict[str, Any]:
    """Call Oura's token endpoint. Errors never echo the submitted grant."""
    response = requests.post(
        TOKEN_URL,
        data=payload | {"client_id": settings.client_id, "client_secret": settings.client_secret},
        timeout=30,
    )
    if response.status_code >= 400:
        error = "unknown_error"
        try:
            error = str(response.json().get("error", error))
        except ValueError:
            pass
        raise SystemExit(
            f"Oura rejected the request (HTTP {response.status_code}, error={error}).\n"
            "Check that OURA_REDIRECT_URI matches the value registered on the "
            "application at https://cloud.ouraring.com/oauth/applications exactly."
        )
    return response.json()


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot handler that captures the authorization code."""

    result: dict[str, str] = {}
    expected_state = ""
    expected_path = "/"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.expected_path:
            self.send_error(404)
            return

        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if "error" in params:
            CallbackHandler.result = {"error": params["error"]}
            self._respond("Authorization was declined. You can close this tab.")
            return
        if not secrets.compare_digest(params.get("state", ""), self.expected_state):
            CallbackHandler.result = {"error": "state_mismatch"}
            self._respond("State mismatch. Nothing was saved. You can close this tab.")
            return
        if "code" not in params:
            CallbackHandler.result = {"error": "missing_code"}
            self._respond("No authorization code returned. You can close this tab.")
            return

        CallbackHandler.result = {"code": params["code"]}
        self._respond("Oura account connected. You can close this tab.")

    def _respond(self, message: str) -> None:
        body = f"<!doctype html><meta charset=utf-8><title>oura-auth</title><p>{message}</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args) -> None:
        """Silence the default access log; the query string carries the code."""


def authorize_url(settings: Settings, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "scope": settings.scopes,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def capture_code_locally(settings: Settings, state: str) -> str:
    """Serve the redirect URI locally until Oura calls back."""
    CallbackHandler.expected_state = state
    CallbackHandler.expected_path = settings.callback_path
    CallbackHandler.result = {}

    try:
        server = http.server.HTTPServer((settings.callback_host, settings.callback_port), CallbackHandler)
    except OSError as exc:
        raise SystemExit(
            f"Could not listen on {settings.callback_host}:{settings.callback_port} ({exc}).\n"
            "Something else is using the port, or OURA_REDIRECT_URI points elsewhere."
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = authorize_url(settings, state)
    logger.info("\nOpening your browser to authorize with Oura.")
    logger.info("If it does not open, paste this URL:\n\n  %s\n", url)
    webbrowser.open(url)
    logger.info("Waiting for the callback on %s ...", settings.redirect_uri)

    try:
        while not CallbackHandler.result:
            thread.join(0.25)
    except KeyboardInterrupt:
        raise SystemExit("\nCancelled. Nothing was saved.") from None
    finally:
        server.shutdown()
        server.server_close()

    result = CallbackHandler.result
    if "error" in result:
        raise SystemExit(f"Authorization failed: {result['error']}. Nothing was saved.")
    return result["code"]


def prompt_for_code(settings: Settings, state: str) -> str:
    """Fallback for a non-local redirect URI: paste the code by hand."""
    url = authorize_url(settings, state)
    logger.info("\nOURA_REDIRECT_URI points at %s, which this script cannot serve.", settings.callback_host)
    logger.info("\n1. Open this URL and approve the requested scopes:\n\n  %s\n", url)
    logger.info("2. You will be redirected. Copy the full redirect URL from the address bar.")
    pasted = input("\nPaste the redirect URL (or just the code): ").strip()

    if pasted.startswith("http"):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        returned_state = params.get("state", [""])[0]
        if returned_state and not secrets.compare_digest(returned_state, state):
            raise SystemExit("State mismatch. Nothing was saved.")
        codes = params.get("code")
        if not codes:
            raise SystemExit("That URL has no `code` parameter. Nothing was saved.")
        return codes[0]
    if not pasted:
        raise SystemExit("Nothing pasted. Nothing was saved.")
    return pasted


# ── Commands ──────────────────────────────────────────────────────────────


def cmd_connect(settings: Settings, store: TokenStore) -> int:
    state = secrets.token_urlsafe(32)
    code = (
        capture_code_locally(settings, state)
        if settings.is_local_callback
        else prompt_for_code(settings, state)
    )

    logger.info("\nExchanging the authorization code ...")
    grant = exchange(settings, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.redirect_uri,
    })

    document = store.save(grant)
    _report(settings, document, grant.get("scope", ""))
    logger.info(
        "\nStored encrypted in %s.%s for account '%s'.",
        settings.mongodb_database, COLLECTION, settings.account_id,
    )
    logger.info("oura-mcp and oura-sync will pick it up and rotate it from here.")
    return 0


def cmd_refresh(settings: Settings, store: TokenStore) -> int:
    """Rotate the token once by hand.

    Only for recovering a stuck connection. The services refresh on their own,
    under a Redis lock; this does not take that lock, so do not run it while
    they are actively serving traffic.
    """
    logger.info("Refreshing the stored token ...")
    grant = exchange(settings, {
        "grant_type": "refresh_token",
        "refresh_token": store.refresh_token(),
    })
    document = store.save(grant)
    _report(settings, document, grant.get("scope", ""))
    return 0


def cmd_status(settings: Settings, store: TokenStore) -> int:
    document = store.status()
    if document is None:
        logger.info("No Oura account connected for '%s'.", settings.account_id)
        logger.info("Run: python oura_auth.py connect")
        return 1

    expires_at = document.get("expires_at")
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    remaining = (expires_at - datetime.now(UTC)) if expires_at else None

    logger.info("account_id   %s", document.get("account_id"))
    logger.info("scopes       %s", " ".join(document.get("scopes", [])) or "(none)")
    logger.info("version      %s", document.get("version"))
    logger.info("updated_at   %s", document.get("updated_at"))
    logger.info("expires_at   %s", expires_at)
    if remaining is not None:
        logger.info(
            "state        %s",
            "valid" if remaining.total_seconds() > 0 else "EXPIRED (a service will rotate it)",
        )
    return 0


def _report(settings: Settings, document: dict[str, Any], scope: str) -> None:
    granted = set(scope.split())
    requested = set(settings.scopes.split())
    logger.info("\nConnected. Token version %s.", document.get("version"))
    logger.info("Granted scopes: %s", " ".join(sorted(granted)) or "(none)")
    if missing := requested - granted:
        logger.warning(
            "\nNot granted: %s\nTools needing those scopes will return a permission error. "
            "Re-run `connect` and approve them to fix it.",
            " ".join(sorted(missing)),
        )


COMMANDS = {"connect": cmd_connect, "refresh": cmd_refresh, "status": cmd_status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oura_auth.py",
        description="One-time Oura OAuth2 bootstrap. Writes an encrypted token to MongoDB.",
    )
    parser.add_argument("command", choices=sorted(COMMANDS), help="the action to perform")
    parser.add_argument("--env-file", default=".env", help="dotenv file to load (default: .env)")
    args = parser.parse_args(argv)

    load_dotenv(args.env_file)

    try:
        settings = Settings()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 2

    store = TokenStore(settings)
    try:
        store.check()
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.error("Cannot reach MongoDB at the configured URI: %s", type(exc).__name__)
        logger.error(
            "If it is a port-forwarded replica set, add ?directConnection=true to MONGODB_URI."
        )
        store.close()
        return 2

    try:
        return COMMANDS[args.command](settings, store)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2
    except requests.RequestException as exc:
        logger.error("Could not reach Oura: %s", type(exc).__name__)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
