"""Voice notes → text. ``Transcriber`` protocol, OpenAI implementation, and a null fallback.

Sonnet 5 takes text and images only (research/02 §1), so audio is transcribed first. Telegram
voice notes are OGG/Opus and OpenAI accepts OGG directly (research/09 §2.3–2.4): the bytes are
sent as ``("voice.ogg", data, "audio/ogg")`` without ffmpeg. Primary model ``gpt-transcribe``
takes ``languages=[...]`` (plural); the fallback ``whisper-1`` takes ``language=`` (singular).

The user writes their own language with English food names (brief §3.1), so English is always
among the language candidates and the prompt seeds food vocabulary.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog

from strikt.db import repo
from strikt.db.models import SecretService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from strikt.config import Settings
    from strikt.db.crypto import TokenCipher
    from strikt.db.models import User

log = structlog.get_logger(__name__)

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # OpenAI transcription upload cap
#: Transcription clients kept alive, one per distinct OpenAI key.
MAX_TRANSCRIBERS = 32
DEFAULT_LANGUAGE = "en"

FOOD_PROMPT = (
    "Food and training log for a health coach. Dishes, grams, brands, restaurants and numbers: "
    "200 g cottage cheese 0.5%, 160 g Greek yogurt 0%, kcal, protein, fiber, "
    "brussels sprouts, lentil soup, breadless burger, shawarma, kofta, sea bass, "
    "WHOOP, strain, recovery, HRV, waist, bedtime."
)

_MIME_FILENAMES: dict[str, str] = {
    "audio/ogg": "voice.ogg",
    "audio/oga": "voice.ogg",
    "audio/opus": "voice.ogg",
    "audio/mpeg": "voice.mp3",
    "audio/mp3": "voice.mp3",
    "audio/mp4": "voice.m4a",
    "audio/x-m4a": "voice.m4a",
    "audio/m4a": "voice.m4a",
    "audio/aac": "voice.m4a",
    "audio/wav": "voice.wav",
    "audio/x-wav": "voice.wav",
    "audio/webm": "voice.webm",
    "audio/flac": "voice.flac",
    "video/mp4": "note.mp4",  # video notes: the audio track is transcribed
}


class TranscriptionError(Exception):
    """Both the primary and the fallback model failed (or the input is unusable)."""


class Transcriber(Protocol):
    async def transcribe(
        self, data: bytes, *, mime: str | None = None, language_hint: str | None = None
    ) -> str:
        """Audio bytes → plain text. Empty string means "no transcription available"."""
        ...


class NullTranscriber:
    """Used when no OpenAI key is configured; the handler then asks for text (``err.transcribe``)."""

    async def transcribe(
        self, data: bytes, *, mime: str | None = None, language_hint: str | None = None
    ) -> str:
        log.info("transcription_disabled", size=len(data), mime=mime)
        return ""


# --- the slice of the OpenAI client this module uses, so tests can pass a fake ------------


class _TranscriptionsAPI(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


class _AudioAPI(Protocol):
    @property
    def transcriptions(self) -> _TranscriptionsAPI: ...


class TranscriptionClient(Protocol):
    @property
    def audio(self) -> _AudioAPI: ...


def uses_plural_languages(model: str) -> bool:
    """``gpt-transcribe`` (and dated variants) take ``languages``; every other model ``language``."""
    return model.startswith("gpt-transcribe")


def language_candidates(language_hint: str | None) -> list[str]:
    """``"ru-RU"`` → ``["ru", "en"]``; ``None`` → ``["en"]``. ISO-639-1, English always present."""
    primary = _iso639_1(language_hint)
    if primary and primary != DEFAULT_LANGUAGE:
        return [primary, DEFAULT_LANGUAGE]
    return [DEFAULT_LANGUAGE]


def _iso639_1(code: str | None) -> str | None:
    if not code:
        return None
    base = code.strip().lower().replace("_", "-").split("-")[0]
    return base if len(base) == 2 and base.isalpha() else None


def upload_name(mime: str | None) -> str:
    """Filename with the extension OpenAI uses to identify the container; OGG when unknown."""
    if not mime:
        return "voice.ogg"
    return _MIME_FILENAMES.get(mime.lower().split(";")[0].strip(), "voice.ogg")


def _text_of(result: Any) -> str:
    text = result if isinstance(result, str) else getattr(result, "text", "")
    return str(text or "").strip()


class OpenAITranscriber:
    """``gpt-transcribe`` with a ``whisper-1`` fallback; the client is built on first use."""

    def __init__(
        self,
        settings: Settings,
        client: TranscriptionClient | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._api_key = api_key
        self._model = settings.openai_transcription_model
        self._fallback_model = settings.openai_transcription_fallback_model

    def _get_client(self) -> TranscriptionClient:
        if self._client is None:
            key = self._settings.openai_api_key
            api_key = self._api_key or (key.get_secret_value() if key is not None else "")
            if not api_key:
                raise TranscriptionError("OPENAI_API_KEY is not configured")
            from openai import AsyncOpenAI

            self._client = cast("TranscriptionClient", AsyncOpenAI(api_key=api_key))
        return self._client

    async def transcribe(
        self, data: bytes, *, mime: str | None = None, language_hint: str | None = None
    ) -> str:
        if not data:
            raise TranscriptionError("empty audio")
        if len(data) > MAX_AUDIO_BYTES:
            raise TranscriptionError(f"audio is {len(data)} bytes, limit {MAX_AUDIO_BYTES}")
        client = self._get_client()
        file = (upload_name(mime), data, mime or "audio/ogg")
        candidates = language_candidates(language_hint)
        try:
            result = await client.audio.transcriptions.create(
                file=file,
                model=self._model,
                prompt=FOOD_PROMPT,
                **self._language_kwargs(self._model, candidates),
            )
        except Exception as primary_exc:
            log.warning(
                "transcription_primary_failed",
                model=self._model,
                error=str(primary_exc),
                fallback=self._fallback_model,
            )
            if not self._fallback_model or self._fallback_model == self._model:
                raise TranscriptionError(str(primary_exc)) from primary_exc
            try:
                result = await client.audio.transcriptions.create(
                    file=file,
                    model=self._fallback_model,
                    prompt=FOOD_PROMPT,
                    **self._language_kwargs(self._fallback_model, candidates),
                )
            except Exception as fallback_exc:
                log.error(
                    "transcription_failed", model=self._fallback_model, error=str(fallback_exc)
                )
                raise TranscriptionError(str(fallback_exc)) from fallback_exc
        text = _text_of(result)
        log.info("transcribed", chars=len(text), bytes=len(data), mime=mime)
        return text

    @staticmethod
    def _language_kwargs(model: str, candidates: list[str]) -> dict[str, Any]:
        if uses_plural_languages(model):
            return {"languages": candidates}
        return {"language": candidates[0]}


def build_transcriber(settings: Settings) -> Transcriber:
    """``OpenAITranscriber`` when an OpenAI key is configured, otherwise ``NullTranscriber``."""
    key = settings.openai_api_key
    if key is not None and key.get_secret_value():
        return OpenAITranscriber(settings)
    log.info("transcriber_null", reason="no OPENAI_API_KEY")
    return NullTranscriber()


class TranscriberResolver(Protocol):
    async def for_user(self, session: AsyncSession, user: User) -> Transcriber: ...


class TranscriberFactory:
    """The transcriber a given user's voice notes go through.

    The user's own OpenAI key first (they pasted it into the chat, it is billed to them), the
    server key second when there is one, and ``NullTranscriber`` when there is neither - then
    the handler asks for text instead. One client per distinct key, oldest evicted, because
    building an ``AsyncOpenAI`` per voice note leaks connections.
    """

    def __init__(self, settings: Settings, cipher: TokenCipher | None = None) -> None:
        self._settings = settings
        self._cipher = cipher
        self._server = build_transcriber(settings)
        self._by_key: OrderedDict[str, Transcriber] = OrderedDict()

    async def for_user(self, session: AsyncSession, user: User) -> Transcriber:
        key = None
        if self._cipher is not None:
            key = await repo.get_user_secret(session, user.id, SecretService.openai, self._cipher)
        if not key:
            return self._server
        cached = self._by_key.get(key)
        if cached is not None:
            self._by_key.move_to_end(key)
            return cached
        made: Transcriber = OpenAITranscriber(self._settings, api_key=key)
        self._by_key[key] = made
        self._by_key.move_to_end(key)
        while len(self._by_key) > MAX_TRANSCRIBERS:
            self._by_key.popitem(last=False)
        return made


class FakeTranscriberFactory:
    """Tests hand one transcriber to everybody."""

    def __init__(self, transcriber: Transcriber) -> None:
        self._transcriber = transcriber

    async def for_user(self, session: AsyncSession, user: User) -> Transcriber:
        return self._transcriber
