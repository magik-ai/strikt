"""telegram/voice: the OpenAI transcriber call shape, fallback, and the null transcriber."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from strikt.config import Settings
from strikt.telegram.voice import (
    FOOD_PROMPT,
    MAX_AUDIO_BYTES,
    NullTranscriber,
    OpenAITranscriber,
    TranscriptionError,
    build_transcriber,
    language_candidates,
    upload_name,
    uses_plural_languages,
)


class _Result:
    def __init__(self, text: str) -> None:
        self.text = text


class _Transcriptions:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Audio:
    def __init__(self, transcriptions: _Transcriptions) -> None:
        self.transcriptions = transcriptions


class _Client:
    def __init__(self, *outcomes: Any) -> None:
        self.transcriptions = _Transcriptions(list(outcomes))
        self.audio = _Audio(self.transcriptions)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.transcriptions.calls


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


# ------------------------------------------------------------------------------- helpers


def test_language_candidates() -> None:
    assert language_candidates("ru") == ["ru", "en"]
    assert language_candidates("ru-RU") == ["ru", "en"]
    assert language_candidates("en_US") == ["en"]
    assert language_candidates(None) == ["en"]
    assert language_candidates("") == ["en"]
    assert language_candidates("zzz") == ["en"]


def test_model_language_parameter_shape() -> None:
    assert uses_plural_languages("gpt-transcribe")
    assert uses_plural_languages("gpt-transcribe-2026-01-01")
    assert not uses_plural_languages("whisper-1")
    assert not uses_plural_languages("gpt-4o-transcribe")


def test_upload_name_by_mime() -> None:
    assert upload_name("audio/ogg") == "voice.ogg"
    assert upload_name("audio/ogg; codecs=opus") == "voice.ogg"
    assert upload_name("audio/mpeg") == "voice.mp3"
    assert upload_name("audio/x-m4a") == "voice.m4a"
    assert upload_name(None) == "voice.ogg"
    assert upload_name("application/octet-stream") == "voice.ogg"


# ---------------------------------------------------------------------------- transcribe


async def test_null_transcriber_returns_empty() -> None:
    assert await NullTranscriber().transcribe(b"ogg", mime="audio/ogg", language_hint="ru") == ""


async def test_openai_transcriber_primary_call_shape() -> None:
    client = _Client(_Result("  двести грамм творога  "))
    transcriber = OpenAITranscriber(_settings(), client=client)
    text = await transcriber.transcribe(b"OggS...", mime="audio/ogg", language_hint="ru")
    assert text == "двести грамм творога"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "gpt-transcribe"
    assert call["file"] == ("voice.ogg", b"OggS...", "audio/ogg")
    assert call["languages"] == ["ru", "en"]
    assert "language" not in call
    assert call["prompt"] == FOOD_PROMPT


async def test_openai_transcriber_falls_back_with_singular_language() -> None:
    client = _Client(RuntimeError("boom"), _Result("fallback text"))
    transcriber = OpenAITranscriber(_settings(), client=client)
    text = await transcriber.transcribe(b"OggS", mime="audio/ogg", language_hint="ru-RU")
    assert text == "fallback text"
    assert [c["model"] for c in client.calls] == ["gpt-transcribe", "whisper-1"]
    assert client.calls[1]["language"] == "ru"
    assert "languages" not in client.calls[1]


async def test_openai_transcriber_raises_when_both_fail() -> None:
    client = _Client(RuntimeError("one"), RuntimeError("two"))
    transcriber = OpenAITranscriber(_settings(), client=client)
    with pytest.raises(TranscriptionError, match="two"):
        await transcriber.transcribe(b"OggS", mime="audio/ogg", language_hint=None)


async def test_openai_transcriber_no_fallback_when_same_model() -> None:
    client = _Client(RuntimeError("one"))
    settings = _settings(openai_transcription_fallback_model="gpt-transcribe")
    transcriber = OpenAITranscriber(settings, client=client)
    with pytest.raises(TranscriptionError, match="one"):
        await transcriber.transcribe(b"OggS")
    assert len(client.calls) == 1


async def test_openai_transcriber_accepts_string_results_and_other_mimes() -> None:
    client = _Client("plain string result")
    transcriber = OpenAITranscriber(_settings(), client=client)
    text = await transcriber.transcribe(b"ID3", mime="audio/mpeg", language_hint="en")
    assert text == "plain string result"
    assert client.calls[0]["file"] == ("voice.mp3", b"ID3", "audio/mpeg")
    assert client.calls[0]["languages"] == ["en"]


async def test_openai_transcriber_input_guards() -> None:
    client = _Client()
    transcriber = OpenAITranscriber(_settings(), client=client)
    with pytest.raises(TranscriptionError):
        await transcriber.transcribe(b"")
    with pytest.raises(TranscriptionError):
        await transcriber.transcribe(b"x" * (MAX_AUDIO_BYTES + 1))
    assert client.calls == []


async def test_openai_transcriber_needs_a_key_to_build_a_client() -> None:
    transcriber = OpenAITranscriber(_settings())
    with pytest.raises(TranscriptionError, match="OPENAI_API_KEY"):
        await transcriber.transcribe(b"OggS")


def test_openai_transcriber_builds_real_client_lazily() -> None:
    transcriber = OpenAITranscriber(_settings(openai_api_key=SecretStr("sk-test")))
    client = transcriber._get_client()
    assert client is transcriber._get_client()
    assert type(client).__name__ == "AsyncOpenAI"


def test_build_transcriber_picks_by_key() -> None:
    assert isinstance(build_transcriber(_settings()), NullTranscriber)
    assert isinstance(
        build_transcriber(_settings(openai_api_key=SecretStr("sk-test"))), OpenAITranscriber
    )
