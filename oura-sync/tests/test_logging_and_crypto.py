"""Redaction and encryption: the two things that must never leak a credential."""

from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet

from app.utilities.crypto import TokenCipher, TokenDecryptionError
from app.utilities.logging import (
    REDACTED,
    StructuredFormatter,
    configure_logging,
    get_correlation_id,
    new_correlation_id,
    redact,
    set_correlation_id,
)

SECRET = "iBPzMv7xR4tKq2WdN8sLhY6cJ3fA5gU1"  # noqa: S105 - a dummy value, not a real token


def format_record(**extra) -> str:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=extra.pop("msg", "hello"), args=(), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return StructuredFormatter().format(record)


# ── Redaction ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field",
    [
        "access_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "api_key",
        "token_encryption_key",
        "password",
        "code",
        "x_oura_signature",
    ],
)
def test_credential_fields_are_redacted(field):
    assert redact({field: SECRET})[field] == REDACTED


def test_redaction_reaches_nested_structures():
    payload = {"outer": {"items": [{"refresh_token": SECRET}]}, "safe": "keep-me"}

    cleaned = redact(payload)

    assert cleaned["outer"]["items"][0]["refresh_token"] == REDACTED
    assert cleaned["safe"] == "keep-me"


def test_authorization_header_in_a_message_is_redacted():
    formatted = format_record(msg=f"calling oura with Bearer {SECRET}")

    assert SECRET not in formatted
    assert REDACTED in formatted


def test_assigned_secret_in_a_message_is_redacted():
    formatted = format_record(msg=f"using access_token={SECRET} now")

    assert SECRET not in formatted


def test_extra_fields_are_redacted_in_the_emitted_line():
    formatted = format_record(access_token=SECRET, resource="daily_sleep")

    assert f"access_token='{REDACTED}'" in formatted
    assert "resource='daily_sleep'" in formatted


def test_structured_output_carries_the_expected_fields():
    set_correlation_id("corr-1")
    formatted = format_record(tool="oura_sleep", duration_ms=12.5, cache="hit")

    assert " INFO     test [corr-1] hello" in formatted
    assert "tool='oura_sleep'" in formatted
    assert "duration_ms=12.5" in formatted


def test_token_models_do_not_expose_secrets_in_their_repr():
    from tests.conftest import make_token

    token = make_token(access="access-plaintext", refresh="refresh-plaintext")

    assert "access-plaintext" not in repr(token)
    assert "refresh-plaintext" not in repr(token)
    assert "access-plaintext" not in str(token)


def test_a_logged_token_model_leaks_nothing():
    from tests.conftest import make_token

    token = make_token(access="access-plaintext", refresh="refresh-plaintext")
    formatted = format_record(msg=f"stored {token}")

    assert "access-plaintext" not in formatted


def test_correlation_id_round_trips():
    value = new_correlation_id()
    set_correlation_id(value)

    assert get_correlation_id() == value
    assert len(value) == 32


def test_configure_logging_installs_exactly_one_handler():
    configure_logging("DEBUG")
    configure_logging("INFO")

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, StructuredFormatter)


# ── Encryption ────────────────────────────────────────────────────────────


def test_tokens_round_trip_through_encryption(config):
    cipher = TokenCipher(config.tokens)

    ciphertext = cipher.encrypt(SECRET)

    assert SECRET not in ciphertext
    assert cipher.decrypt(ciphertext) == SECRET


def test_a_rotated_key_produces_a_clear_error(config):
    original = TokenCipher(config.tokens)
    ciphertext = original.encrypt(SECRET)
    rotated = TokenCipher(config.replace("tokens", encryption_key=Fernet.generate_key().decode()).tokens)

    with pytest.raises(TokenDecryptionError) as excinfo:
        rotated.decrypt(ciphertext)

    assert "tokens.encryption_key" in str(excinfo.value)


def test_configuration_rejects_a_malformed_encryption_key(config):
    from app.models.config import ConfigurationError

    with pytest.raises(ConfigurationError) as excinfo:
        config.replace("tokens", encryption_key="not-a-key")

    assert "tokens.encryption_key" in str(excinfo.value)
    assert "not-a-key" not in str(excinfo.value)
