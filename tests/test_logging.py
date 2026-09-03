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


def test_token_counters_survive_redaction() -> None:
    """``input_tokens`` matches the ``token`` marker, and every turn logs it. Numbers are never
    credentials, so the usage and cost line stays readable while the keys beside it do not."""
    out = redact_secrets(
        None,
        "info",
        {
            "event": "turn_done",
            "input_tokens": 4211,
            "output_tokens": 318,
            "cache_read_tokens": 12_000,
            "max_tokens": 8192,
            "cost_usd": 0.021,
            "api_key": "sk-ant-api03-abcdefgh",
            "telegram_bot_token": "8949408198:AAH",
        },
    )
    assert out["input_tokens"] == 4211 and out["output_tokens"] == 318
    assert out["cache_read_tokens"] == 12_000 and out["max_tokens"] == 8192
    assert out["cost_usd"] == 0.021
    assert out["api_key"] == "***" and out["telegram_bot_token"] == "***"
