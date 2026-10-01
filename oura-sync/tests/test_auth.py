"""The route guards in app.utilities.auth, exercised on a minimal Quart app.

`verify_bearer_token` itself is unit-tested in full in test_azure_ad.py; this
covers the decorator shape around it and the MCP_API_KEY guard."""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from quart import Quart

from app.models.config import bind_sections
from app.utilities.auth import require_api_key, require_gateway_token
from app.utilities.provider import EXTENSION_KEY

TENANT = "22222222-2222-2222-2222-222222222222"
AUDIENCE = "api://oura-sync-app-id"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


class SectionsContainer:
    """Resolves config sections only -- all the guards need."""

    def __init__(self, config) -> None:
        self._sections = bind_sections(config.values)

    def resolve(self, service_type):
        return self._sections[service_type]


def guarded_app(config) -> Quart:
    app = Quart(__name__)
    app.extensions[EXTENSION_KEY] = SectionsContainer(config)

    @app.route("/gateway")
    @require_gateway_token
    async def gateway():
        return {"ok": True}

    @app.route("/admin")
    @require_api_key
    async def admin():
        return {"ok": True}

    return app


async def get(app: Quart, path: str, authorization: str | None):
    headers = {"Authorization": authorization} if authorization else {}
    return await app.test_client().get(path, headers=headers)


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture(autouse=True)
def fake_jwks(monkeypatch, keypair):
    _private_pem, public_pem = keypair

    class FakeSigningKey:
        def __init__(self, key) -> None:
            self.key = key

    class FakeJwksClient:
        def get_signing_key_from_jwt(self, _token: str) -> FakeSigningKey:
            return FakeSigningKey(public_pem)

    import app.utilities.azure_ad as azure_ad

    monkeypatch.setattr(azure_ad, "_jwks_clients", {TENANT: FakeJwksClient()})


def make_token(keypair) -> str:
    private_pem, _public_pem = keypair
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "iat": now - 10, "exp": now + 3600}
    return pyjwt.encode(claims, private_pem, algorithm="RS256")


async def test_an_unconfigured_deployment_always_refuses(config):
    # No `auth` section at all
    response = await get(guarded_app(config), "/gateway", "Bearer whatever")

    assert response.status_code == 503
    assert (await response.get_json())["error"] == "not_configured"


async def test_a_valid_token_is_accepted(config, keypair):
    config = config.replace("auth", tenant_id=TENANT, audiences=[AUDIENCE])

    response = await get(guarded_app(config), "/gateway", f"Bearer {make_token(keypair)}")

    assert response.status_code == 200
    assert await response.get_json() == {"ok": True}


async def test_a_missing_token_is_refused(config):
    config = config.replace("auth", tenant_id=TENANT, audiences=[AUDIENCE])

    response = await get(guarded_app(config), "/gateway", None)

    assert response.status_code == 401


async def test_the_api_key_is_accepted_on_admin_routes(config):
    response = await get(guarded_app(config), "/admin", "Bearer test-mcp-api-key")

    assert response.status_code == 200


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic test-mcp-api-key"])
async def test_admin_routes_refuse_anything_but_the_api_key(config, authorization):
    response = await get(guarded_app(config), "/admin", authorization)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
