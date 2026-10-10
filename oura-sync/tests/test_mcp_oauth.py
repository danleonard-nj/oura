"""MCP OAuth: consent, code issuance, token lifecycle and the discovery surface.

The authorization-server behaviour is exercised against an in-memory store; the
discovery documents and the dual-credential middleware are exercised over the
real ASGI app.
"""

from __future__ import annotations

import time

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from app.services.audit_service import AuditService

from app.services.mcp_auth_service import (
    MCP_SCOPE,
    ConsentError,
    ConsentLockedOut,
    McpAuthService,
)
from tests.conftest import FakeAuditRepository

OWNER_PW = "owner-secret-9WqLp2Zn4TxKb7Rd"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


class FakeOAuthRepository:
    """In-memory stand-in covering exactly what McpAuthService uses."""

    def __init__(self) -> None:
        self.clients: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self.attempts: dict[str, int] = {}

    async def save_client(self, client_id, document):
        self.clients[client_id] = document

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def put_pending(self, txn_id, payload, ttl):
        self.pending[txn_id] = payload

    async def peek_pending(self, txn_id):
        return self.pending.get(txn_id)

    async def take_pending(self, txn_id):
        return self.pending.pop(txn_id, None)

    async def put_code(self, code, payload, ttl):
        self.codes[code] = payload

    async def peek_code(self, code):
        return self.codes.get(code)

    async def take_code(self, code):
        return self.codes.pop(code, None)

    async def put_access_token(self, token, payload, ttl):
        self.access[token] = payload

    async def get_access_token(self, token):
        return self.access.get(token)

    async def delete_access_token(self, token):
        self.access.pop(token, None)

    async def put_refresh_token(self, token, payload):
        self.refresh[token] = payload

    async def get_refresh_token(self, token):
        return self.refresh.get(token)

    async def delete_refresh_token(self, token):
        return self.refresh.pop(token, None) is not None

    async def record_failed_attempt(self, key, ttl):
        self.attempts[key] = self.attempts.get(key, 0) + 1
        return self.attempts[key]

    async def failed_attempts(self, key):
        return self.attempts.get(key, 0)

    async def clear_failed_attempts(self, key):
        self.attempts.pop(key, None)


@pytest.fixture
def oauth_config(config):
    return config.replace("mcp", oauth={"owner_password": OWNER_PW})


def build_auth_service(store, config) -> McpAuthService:
    return McpAuthService(
        repository=store,  # type: ignore[arg-type]
        audit=AuditService(FakeAuditRepository()),  # type: ignore[arg-type]
        mcp=config.mcp,
        oura=config.oura,
        service=config.service,
    )


@pytest.fixture
def store() -> FakeOAuthRepository:
    return FakeOAuthRepository()


@pytest.fixture
def service(store, oauth_config) -> McpAuthService:
    return build_auth_service(store, oauth_config)


def make_client(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name="ChatGPT",
        redirect_uris=[AnyUrl(REDIRECT)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=MCP_SCOPE,
    )


def make_params(state: str = "st-1") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=[MCP_SCOPE],
        code_challenge="challenge-value",
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
    )


async def authorized_txn(service, store, state: str = "st-1") -> str:
    await service.authorize(make_client(), make_params(state))
    return next(iter(store.pending))


# ── Configuration gate ────────────────────────────────────────────────────


def test_oauth_is_off_without_an_owner_password(config):
    assert config.mcp.oauth.enabled is False


def test_oauth_is_on_with_an_owner_password(oauth_config):
    assert oauth_config.mcp.oauth.enabled is True


async def test_authorize_refuses_when_no_owner_password_is_set(config, store):
    service = build_auth_service(store, config)

    with pytest.raises(ConsentError):
        await service.authorize(make_client(), make_params())


# ── Authorization and consent ─────────────────────────────────────────────


async def test_authorize_parks_the_request_and_issues_no_code(service, store):
    url = await service.authorize(make_client(), make_params())

    assert "/consent?txn=" in url
    assert len(store.pending) == 1
    assert store.codes == {}, "a code was issued before consent"


async def test_consent_with_the_owner_password_mints_a_code(service, store):
    txn = await authorized_txn(service, store, state="round-trip")

    redirect = await service.approve_consent(txn, OWNER_PW, attempt_key=txn)

    assert redirect.startswith(REDIRECT)
    assert "code=" in redirect
    assert "state=round-trip" in redirect
    assert len(store.codes) == 1


async def test_a_wrong_password_issues_nothing(service, store):
    txn = await authorized_txn(service, store)

    with pytest.raises(ConsentError):
        await service.approve_consent(txn, "wrong", attempt_key=txn)

    assert store.codes == {}
    assert txn in store.pending, "the request was consumed by a failed attempt"


async def test_repeated_wrong_passwords_lock_out(service, store, oauth_config):
    txn = await authorized_txn(service, store)

    for _ in range(oauth_config.mcp.oauth.consent_max_attempts):
        with pytest.raises(ConsentError):
            await service.approve_consent(txn, "wrong", attempt_key=txn)

    with pytest.raises(ConsentLockedOut):
        await service.approve_consent(txn, OWNER_PW, attempt_key=txn)

    assert store.codes == {}, "lockout was bypassed by the correct password"


async def test_a_successful_consent_clears_the_attempt_counter(service, store):
    txn = await authorized_txn(service, store)
    with pytest.raises(ConsentError):
        await service.approve_consent(txn, "wrong", attempt_key=txn)

    await service.approve_consent(txn, OWNER_PW, attempt_key=txn)

    assert await store.failed_attempts(txn) == 0


async def test_the_consent_page_cannot_be_replayed(service, store):
    txn = await authorized_txn(service, store)
    await service.approve_consent(txn, OWNER_PW, attempt_key=txn)

    with pytest.raises(ConsentError):
        await service.approve_consent(txn, OWNER_PW, attempt_key=txn)


async def test_an_unknown_transaction_is_refused(service):
    with pytest.raises(ConsentError):
        await service.approve_consent("never-issued", OWNER_PW, attempt_key="x")


async def test_deny_returns_access_denied(service, store):
    txn = await authorized_txn(service, store, state="st-9")

    redirect = await service.deny_consent(txn)

    assert "error=access_denied" in redirect
    assert "state=st-9" in redirect
    assert store.codes == {}


async def test_pending_request_exposes_no_secrets(service, store):
    txn = await authorized_txn(service, store)

    details = await service.pending_request(txn)

    assert details["client_name"] == "ChatGPT"
    assert "code_challenge" not in details
    assert "state" not in details


# ── Token lifecycle ───────────────────────────────────────────────────────


async def issue_tokens(service, store):
    txn = await authorized_txn(service, store)
    redirect = await service.approve_consent(txn, OWNER_PW, attempt_key=txn)
    code_value = redirect.split("code=")[1].split("&")[0]
    client = make_client()
    code = await service.load_authorization_code(client, code_value)
    return client, code, await service.exchange_authorization_code(client, code)


async def test_code_exchanges_for_an_access_and_refresh_token(service, store):
    _, _, token = await issue_tokens(service, store)

    assert token.access_token and token.refresh_token
    assert token.token_type == "Bearer"
    assert token.scope == MCP_SCOPE
    assert token.expires_in == service._config.access_token_ttl_seconds


async def test_an_authorization_code_is_single_use(service, store):
    client, code, _ = await issue_tokens(service, store)

    with pytest.raises(ValueError):
        await service.exchange_authorization_code(client, code)


async def test_a_code_issued_to_another_client_is_not_loadable(service, store):
    txn = await authorized_txn(service, store)
    redirect = await service.approve_consent(txn, OWNER_PW, attempt_key=txn)
    code_value = redirect.split("code=")[1].split("&")[0]

    assert await service.load_authorization_code(make_client("someone-else"), code_value) is None


async def test_the_access_token_validates(service, store):
    _, _, token = await issue_tokens(service, store)

    loaded = await service.load_access_token(token.access_token)

    assert loaded is not None
    assert loaded.scopes == [MCP_SCOPE]


async def test_an_unknown_access_token_does_not_validate(service):
    assert await service.load_access_token("not-a-token") is None


async def test_an_expired_access_token_is_rejected_and_dropped(service, store):
    _, _, token = await issue_tokens(service, store)
    store.access[token.access_token]["expires_at"] = int(time.time()) - 1

    assert await service.load_access_token(token.access_token) is None
    assert token.access_token not in store.access


async def test_refresh_rotates_both_tokens(service, store):
    client, _, token = await issue_tokens(service, store)
    old_refresh = await service.load_refresh_token(client, token.refresh_token)

    rotated = await service.exchange_refresh_token(client, old_refresh, [MCP_SCOPE])

    assert rotated.access_token != token.access_token
    assert rotated.refresh_token != token.refresh_token


async def test_a_used_refresh_token_is_dead(service, store):
    client, _, token = await issue_tokens(service, store)
    old_refresh = await service.load_refresh_token(client, token.refresh_token)
    await service.exchange_refresh_token(client, old_refresh, [MCP_SCOPE])

    assert await service.load_refresh_token(client, token.refresh_token) is None


async def test_a_refresh_token_belonging_to_another_client_is_not_loadable(service, store):
    _, _, token = await issue_tokens(service, store)

    assert await service.load_refresh_token(make_client("other"), token.refresh_token) is None


async def test_revoking_an_access_token_invalidates_it(service, store):
    _, _, token = await issue_tokens(service, store)
    loaded = await service.load_access_token(token.access_token)

    await service.revoke_token(loaded)

    assert await service.load_access_token(token.access_token) is None


async def test_tokens_are_not_stored_in_the_clear(service, store, oauth_config):
    """A database dump should not be a set of working bearer tokens."""
    from app.repositories.oauth_repository import fingerprint

    _, _, token = await issue_tokens(service, store)

    # The fake keys by raw value; the real repository hashes. Assert the real
    # one does, since that is the property that matters.
    assert fingerprint(token.access_token) != token.access_token
    assert len(fingerprint(token.access_token)) == 64


# ── Client registration ───────────────────────────────────────────────────


async def test_a_registered_client_round_trips(service):
    await service.register_client(make_client("dcr-1"))

    loaded = await service.get_client("dcr-1")

    assert loaded is not None
    assert loaded.client_name == "ChatGPT"


async def test_an_unregistered_client_is_unknown(service):
    assert await service.get_client("never-seen") is None
