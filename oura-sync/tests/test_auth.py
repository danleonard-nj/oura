"""require_gateway_token: the Starlette-response shape wrapped around
oura_common.azure_ad.verify_bearer_token (unit-tested there in full)."""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import oura_sync.api.auth as auth_module

TENANT = "22222222-2222-2222-2222-222222222222"
AUDIENCE = "api://oura-sync-app-id"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


class FakeRequest:
    def __init__(self, authorization: str | None) -> None:
        self.headers = {"authorization": authorization} if authorization else {}


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

    import oura_common.azure_ad as azure_ad

    monkeypatch.setattr(azure_ad, "_jwks_clients", {TENANT: FakeJwksClient()})


def make_token(keypair) -> str:
    private_pem, _public_pem = keypair
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "iat": now - 10, "exp": now + 3600}
    return pyjwt.encode(claims, private_pem, algorithm="RS256")


def resolve_config(monkeypatch, config):
    monkeypatch.setattr(auth_module, "resolve", lambda _request, _type: config)


def test_an_unconfigured_deployment_always_refuses(monkeypatch, config):
    resolve_config(monkeypatch, config)  # aad_tenant_id/aad_audience are None by default

    response = auth_module.require_gateway_token(FakeRequest("Bearer whatever"))

    assert response is not None
    assert response.status_code == 503


def test_a_valid_token_is_accepted(monkeypatch, config, keypair):
    config = config.model_copy(update={"aad_tenant_id": TENANT, "aad_audience": AUDIENCE})
    resolve_config(monkeypatch, config)
    token = make_token(keypair)

    response = auth_module.require_gateway_token(FakeRequest(f"Bearer {token}"))

    assert response is None


def test_a_missing_token_is_refused(monkeypatch, config):
    config = config.model_copy(update={"aad_tenant_id": TENANT, "aad_audience": AUDIENCE})
    resolve_config(monkeypatch, config)

    response = auth_module.require_gateway_token(FakeRequest(None))

    assert response is not None
    assert response.status_code == 401
