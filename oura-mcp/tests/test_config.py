"""Configuration validation and the MCP transport allowlist."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from oura_mcp.config import Config

BASE = dict(
    oura_client_id="cid",
    oura_client_secret="csecret",
    oura_redirect_uri="https://oura.test/oauth/callback",
    mcp_api_key="key",
    mongodb_uri="mongodb://mongo.invalid:27017",
    redis_url="redis://redis.invalid:6379/0",
)


def build(**overrides) -> Config:
    return Config(**BASE, token_encryption_key=Fernet.generate_key().decode(), **overrides)


def test_public_host_is_on_the_allowlist():
    config = build(public_host="oura.dan-leonard.com")

    assert "oura.dan-leonard.com" in config.allowed_hosts
    assert "oura.dan-leonard.com:*" in config.allowed_hosts
    assert "https://oura.dan-leonard.com" in config.allowed_origins


def test_a_scheme_on_public_host_is_stripped():
    """PUBLIC_HOST is a Host header value, so a pasted URL must be normalised."""
    assert build(public_host="https://oura.test/").public_host == "oura.test"


def test_extra_allowed_hosts_are_added():
    """A proxy that rewrites Host needs its target on the allowlist.

    The api-gateway forwards to the cluster-internal service name, so that
    name must be accepted or the DNS-rebinding guard returns 421.
    """
    config = build(
        public_host="oura.dan-leonard.com",
        mcp_extra_allowed_hosts="oura-mcp.oura.svc.cluster.local, api.dan-leonard.com",
    )

    assert "oura-mcp.oura.svc.cluster.local" in config.allowed_hosts
    assert "oura-mcp.oura.svc.cluster.local:*" in config.allowed_hosts
    assert "api.dan-leonard.com" in config.allowed_hosts
    # The public host is still there.
    assert "oura.dan-leonard.com" in config.allowed_hosts


def test_extra_allowed_hosts_default_to_nothing():
    """The allowlist must not widen unless it is asked to."""
    default = build(public_host="oura.test")
    widened = build(public_host="oura.test", mcp_extra_allowed_hosts="  ,  ")

    assert default.allowed_hosts == widened.allowed_hosts
    assert not any("svc.cluster.local" in host for host in default.allowed_hosts)


def test_localhost_is_always_allowed_for_development():
    hosts = build().allowed_hosts

    assert "localhost:*" in hosts
    assert "127.0.0.1:*" in hosts


def test_a_malformed_encryption_key_is_rejected_at_startup():
    with pytest.raises(ValidationError) as excinfo:
        Config(**BASE, token_encryption_key="not-a-fernet-key")

    assert "TOKEN_ENCRYPTION_KEY" in str(excinfo.value)


def test_an_unknown_log_level_is_rejected():
    with pytest.raises(ValidationError):
        build(log_level="chatty")


def test_mcp_path_is_normalised():
    assert build(mcp_path="mcp/").mcp_path == "/mcp"


def test_mcp_path_may_not_be_the_application_root():
    """/mcp at the root would swallow the health and webhook routes."""
    with pytest.raises(ValidationError):
        build(mcp_path="/")
