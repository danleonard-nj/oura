"""Tests for the scope reporting that misled us once already."""

from __future__ import annotations

import oura_auth


def test_the_extapi_prefix_is_stripped():
    """Oura echoes `extapi:daily` for the `daily` we asked for."""
    assert oura_auth.normalize_scopes("extapi:daily extapi:spo2") == {"daily", "spo2"}


def test_unprefixed_scopes_are_left_alone():
    assert oura_auth.normalize_scopes("daily spo2") == {"daily", "spo2"}


def test_mixed_forms_compare_equal():
    """The bug: granted came back prefixed, requested did not, so every
    granted scope was reported as missing."""
    requested = oura_auth.normalize_scopes("daily spo2 stress")
    granted = oura_auth.normalize_scopes("extapi:daily extapi:spo2")

    assert requested - granted == {"stress"}, "only the genuinely missing scope"


def test_empty_scopes():
    assert oura_auth.normalize_scopes("") == set()


def test_the_default_scope_set_covers_every_endpoint():
    """The three undocumented scopes must stay in the default request."""
    scopes = oura_auth.normalize_scopes(oura_auth.DEFAULT_SCOPES)

    assert {"stress", "heart_health", "ring_configuration"} <= scopes
    assert {"daily", "heartrate", "workout", "tag", "session", "spo2", "personal"} <= scopes
