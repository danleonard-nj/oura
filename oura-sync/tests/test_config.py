"""Configuration: the config.json sections, their validation, and the MCP allowlist."""

from __future__ import annotations

import json

import pytest
from framework.configuration.configuration import Configuration

from app.models.config import (
    ConfigurationError,
    McpConfig,
    OuraConfig,
    ServiceConfig,
    bind_sections,
)
from app.models.resources import Resource
from tests.conftest import Sections, base_config_values


def hosts(config: Sections) -> list[str]:
    return config.mcp.allowed_hosts(config.service)


# ── Loading ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "filename"),
    [(None, "config.json"), ("Development", "config.dev.json"), ("Local", "config.local.json")],
)
def test_the_file_is_chosen_by_quart_env(env, filename, tmp_path, monkeypatch):
    """framework's Configuration picks the file; production needs no variable at all."""
    (tmp_path / filename).write_text(json.dumps(base_config_values()), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    if env:
        monkeypatch.setenv("QUART_ENV", env)
    else:
        monkeypatch.delenv("QUART_ENV", raising=False)

    sections = bind_sections(Configuration().values)

    assert sections[OuraConfig].client_id == "test-client-id"


def test_optional_sections_take_their_defaults():
    sections = bind_sections(base_config_values())

    assert sections[ServiceConfig].name == "oura-sync"
    assert Sections(base_config_values()).sync.resources == [
        Resource.RING_CONFIGURATION,
        Resource.RING_BATTERY_LEVEL,
        Resource.PERSONAL_INFO,
    ]
    assert Sections(base_config_values()).auth.configured is False


def test_a_missing_required_section_names_the_key():
    values = base_config_values()
    del values["mongo"]

    with pytest.raises(ConfigurationError) as excinfo:
        bind_sections(values)

    assert "mongo.connection_string" in str(excinfo.value)


def test_validation_errors_never_echo_secret_values():
    values = base_config_values()
    values["tokens"]["encryption_key"] = "definitely-not-a-fernet-key"

    with pytest.raises(ConfigurationError) as excinfo:
        bind_sections(values)

    assert "tokens.encryption_key" in str(excinfo.value)
    assert "definitely-not-a-fernet-key" not in str(excinfo.value)


def test_unknown_keys_are_ignored():
    values = base_config_values()
    values["oura"]["some_future_setting"] = True
    values["unrelated_section"] = {"x": 1}

    assert bind_sections(values)[OuraConfig].client_id == "test-client-id"


def test_an_empty_api_key_is_refused(config):
    with pytest.raises(ConfigurationError):
        config.replace("mcp", api_key="   ")


def test_short_request_state_secrets_are_refused(config):
    with pytest.raises(ConfigurationError):
        config.replace("mcp", request_state_secrets=["too-short"])


def test_an_unknown_sync_resource_is_refused(config):
    with pytest.raises(ConfigurationError):
        config.replace("sync", resources=["made_up"])


def test_an_unknown_log_level_is_rejected(config):
    with pytest.raises(ConfigurationError):
        config.replace("service", log_level="chatty")


def test_mcp_path_is_normalised(config):
    assert config.replace("mcp", path="mcp/").mcp.path == "/mcp"


def test_mcp_path_may_not_be_the_application_root(config):
    """/mcp at the root would swallow the health and webhook routes."""
    with pytest.raises(ConfigurationError):
        config.replace("mcp", path="/")


def test_oauth_is_enabled_only_with_an_owner_password(config):
    assert config.mcp.oauth.enabled is False
    assert config.replace("mcp", oauth={"owner_password": "pw"}).mcp.oauth.enabled is True


# ── MCP transport allowlist ───────────────────────────────────────────────


def test_public_host_is_on_the_allowlist(config):
    config = config.replace("service", public_host="oura.dan-leonard.com")

    assert "oura.dan-leonard.com" in hosts(config)
    assert "oura.dan-leonard.com:*" in hosts(config)
    assert "https://oura.dan-leonard.com" in config.mcp.allowed_origins(config.service)


def test_a_scheme_on_public_host_is_stripped(config):
    """public_host is a Host header value, so a pasted URL must be normalised."""
    assert config.replace("service", public_host="https://oura.test/").service.public_host == "oura.test"


def test_extra_allowed_hosts_are_added(config):
    """A proxy that rewrites Host needs its target on the allowlist.

    The api-gateway forwards to the cluster-internal service name, so that
    name must be accepted or the DNS-rebinding guard returns 421.
    """
    config = config.replace(
        "mcp", extra_allowed_hosts=["oura-sync.oura.svc.cluster.local", " api.dan-leonard.com "]
    )

    assert "oura-sync.oura.svc.cluster.local" in hosts(config)
    assert "oura-sync.oura.svc.cluster.local:*" in hosts(config)
    assert "api.dan-leonard.com" in hosts(config)
    # The public host is still there.
    assert "oura.test" in hosts(config)


def test_extra_allowed_hosts_default_to_nothing(config):
    """The allowlist must not widen unless it is asked to."""
    widened = config.replace("mcp", extra_allowed_hosts=["  ", ""])

    assert hosts(config) == hosts(widened)
    assert not any("svc.cluster.local" in host for host in hosts(config))


def test_localhost_is_always_allowed_for_development():
    mcp = McpConfig(api_key="key")

    assert "localhost:*" in mcp.allowed_hosts(ServiceConfig())
    assert "127.0.0.1:*" in mcp.allowed_hosts(ServiceConfig())


def test_public_base_url_falls_back_to_localhost():
    assert ServiceConfig(port=9000).public_base_url == "http://localhost:9000"
    assert ServiceConfig(public_host="oura.test").public_base_url == "https://oura.test"


def test_the_example_file_covers_every_section_and_validates_once_filled():
    """config.example.json is the template CI/CD config is built from; keep it honest."""
    from pathlib import Path

    from cryptography.fernet import Fernet

    from app.models.config import SECTIONS

    example = json.loads(
        (Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8")
    )
    assert set(example) == set(SECTIONS.values())

    filled = json.loads(
        json.dumps(example).replace('"REPLACE_ME"', '"filled-in-value"')
    )
    filled["tokens"]["encryption_key"] = Fernet.generate_key().decode()

    bind_sections(filled)
