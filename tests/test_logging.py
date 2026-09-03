"""``strikt.logging``: secrets are masked at any depth, ordinary keys pass through."""

from __future__ import annotations

import pytest

from strikt.logging import _SECRET_MARKERS, redact_secrets


@pytest.mark.parametrize("marker", _SECRET_MARKERS)
def test_every_marker_is_masked_at_the_top_level(marker: str) -> None:
    event = {"event": "x", f"my_{marker}_value": "s3cr3t", "user_id": 7}
    out = redact_secrets(None, "info", event)
    assert out[f"my_{marker}_value"] == "***"
    assert out["user_id"] == 7 and out["event"] == "x"


def test_case_insensitive_and_empty_values_untouched() -> None:
    out = redact_secrets(None, "info", {"Authorization": "Bearer x", "token": "", "TOKEN_COUNT": 0})
    assert out["Authorization"] == "***"
    assert out["token"] == "" and out["TOKEN_COUNT"] == 0  # falsy values are not secrets


def test_nested_credentials_are_masked() -> None:
    event = {
        "event": "http",
        "headers": {"Cookie": "sid=1", "Accept": "json"},
        "body": [{"api_key": "k", "name": "n"}, {"nested": {"client_secret": "s"}}],
        "note": "keep",
    }
    out = redact_secrets(None, "info", event)
    assert out["headers"] == {"Cookie": "***", "Accept": "json"}
    assert out["body"][0] == {"api_key": "***", "name": "n"}
    assert out["body"][1] == {"nested": {"client_secret": "***"}}
    assert out["note"] == "keep"
