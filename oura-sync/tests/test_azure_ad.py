"""Inbound Azure AD bearer-token validation.

No network calls: `_jwks_client` is monkeypatched to a stand-in whose signing
key is the public half of the keypair the test tokens are signed with, so
this exercises the actual `jwt.decode` validation (signature, issuer,
audience, expiry, role) without hitting a real JWKS endpoint.
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.utilities.azure_ad import AzureAdError, verify_bearer_token

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "api://oura-sync-app-id"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


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


def make_token(keypair, *, claims_override=None, expired=False) -> str:
    private_pem, _public_pem = keypair
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now - 10,
        "exp": now - 3600 if expired else now + 3600,
        "roles": ["Sync.Execute"],
    }
    if claims_override:
        claims.update(claims_override)
    return pyjwt.encode(claims, private_pem, algorithm="RS256")


def test_a_valid_token_is_accepted(keypair):
    token = make_token(keypair)

    claims = verify_bearer_token(
        f"Bearer {token}", tenant_id=TENANT, audiences=[AUDIENCE]
    )

    assert claims["aud"] == AUDIENCE


def test_a_missing_authorization_header_is_refused():
    with pytest.raises(AzureAdError):
        verify_bearer_token(None, tenant_id=TENANT, audiences=[AUDIENCE])


def test_a_non_bearer_scheme_is_refused(keypair):
    token = make_token(keypair)

    with pytest.raises(AzureAdError):
        verify_bearer_token(f"Basic {token}", tenant_id=TENANT, audiences=[AUDIENCE])


def test_an_expired_token_is_refused(keypair):
    token = make_token(keypair, expired=True)

    with pytest.raises(AzureAdError):
        verify_bearer_token(f"Bearer {token}", tenant_id=TENANT, audiences=[AUDIENCE])


def test_the_wrong_audience_is_refused(keypair):
    token = make_token(keypair, claims_override={"aud": "api://someone-else"})

    with pytest.raises(AzureAdError):
        verify_bearer_token(f"Bearer {token}", tenant_id=TENANT, audiences=[AUDIENCE])


def test_the_wrong_issuer_is_refused(keypair):
    token = make_token(keypair, claims_override={"iss": "https://evil.example/v2.0"})

    with pytest.raises(AzureAdError):
        verify_bearer_token(f"Bearer {token}", tenant_id=TENANT, audiences=[AUDIENCE])


def test_a_required_role_is_enforced(keypair):
    token = make_token(keypair, claims_override={"roles": ["SomethingElse"]})

    with pytest.raises(AzureAdError):
        verify_bearer_token(
            f"Bearer {token}",
            tenant_id=TENANT,
            audiences=[AUDIENCE],
            required_role="Sync.Execute",
        )


def test_a_present_required_role_is_accepted(keypair):
    token = make_token(keypair)

    claims = verify_bearer_token(
        f"Bearer {token}",
        tenant_id=TENANT,
        audiences=[AUDIENCE],
        required_role="Sync.Execute",
    )

    assert "Sync.Execute" in claims["roles"]
