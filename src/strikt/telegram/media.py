"""Inbound Telegram media: download, HEIC → JPEG, resize, hash, base64; PDFs; album debounce.

Facts this module is built on (research/03 §3–6, research/02 §8–9, research/09 §2.10):

- Bots can download files of at most 20 MB (``getFile``); ``file_id`` is persistent.
- Claude vision accepts JPEG/PNG/GIF/WebP only — never HEIC — at most 10 MB of base64 per image,
  and every image must stay within 2000 px per side once a request carries more than 20 images.
  Sonnet 5 downscales to a 2576 px long edge anyway, so 2000 px loses nothing that matters.
- PDFs go in as ``document`` blocks: at most 32 MB per request and 100 pages when the context
  window is under 1M tokens (PLAN §7 pins 100).
- iPhones send HEIC only as a *document* (``image/heic`` mime or ``.heic`` filename, both
  sender-defined); ``pillow-heif`` opens it once ``register_heif_opener`` has run.
- Every item of an album is its own update with the same ``media_group_id``; nothing marks the
  last one, so the collector debounces (PLAN §7: 1.2 s) and caps at 10 parts.

PIL work runs in ``asyncio.to_thread`` (PLAN §14). Nothing here talks to the model or the DB.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol

import structlog
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from strikt.core.types import Attachment

if TYPE_CHECKING:
    from aiogram import Bot

log = structlog.get_logger(__name__)

register_heif_opener()

MAX_TELEGRAM_DOWNLOAD = 20 * 1024 * 1024  # Bot API getFile hard cap
MAX_PDF_BYTES = 32 * 1024 * 1024  # Claude request cap for documents
MAX_PDF_PAGES = 100  # PLAN §7 (600 on 1M-context requests; 100 is the safe floor)
MAX_IMAGE_B64 = 10 * 1024 * 1024  # Claude per-image base64 cap
MAX_IMAGE_EDGE = 2000  # px; keeps many-image requests valid and stays under Sonnet 5's 2576
JPEG_QUALITY = 85
JPEG_QUALITY_STEPS = (85, 70, 55, 40)  # retried when the encoded image would exceed the cap
ALBUM_DEBOUNCE_S = 1.2
ALBUM_MAX_PARTS = 10  # sendMediaGroup allows 2–10 items

HEIC_MIMES = frozenset({"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"})
HEIC_EXTENSIONS = frozenset({".heic", ".heif", ".hif"})
HEIC_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"}
)
IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif"} | HEIC_EXTENSIONS
)
PDF_MIME = "application/pdf"
_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page(?![s\w])")


class MediaError(Exception):
    """The file cannot be turned into something the model can read."""


class MediaTooLargeError(MediaError):
    """The file exceeds a hard limit (Telegram download or Claude request)."""

    def __init__(self, size: int, limit: int, what: str = "file") -> None:
        super().__init__(f"{what} is {size} bytes, limit {limit}")
        self.size = size
        self.limit = limit
        self.limit_mb = limit // (1024 * 1024)


# ------------------------------------------------------------------------------ downloading


class Downloader(Protocol):
    async def download(self, file_id: str) -> bytes:
        """Fetch the bytes behind a Telegram ``file_id``; raises ``MediaTooLargeError`` over 20 MB."""
        ...


class AiogramDownloader:
    """``getFile`` + ``download_file`` into memory with the 20 MB guard applied twice."""

    def __init__(self, bot: Bot, *, max_bytes: int = MAX_TELEGRAM_DOWNLOAD) -> None:
        self._bot = bot
        self._max_bytes = max_bytes

    async def download(self, file_id: str) -> bytes:
        info = await self._bot.get_file(file_id)
        if info.file_size is not None and info.file_size > self._max_bytes:
            raise MediaTooLargeError(info.file_size, self._max_bytes)
        if not info.file_path:
            raise MediaError("telegram returned no file_path (file too large or expired)")
        buffer = BytesIO()
        await self._bot.download_file(info.file_path, destination=buffer)
        data = buffer.getvalue()
        if len(data) > self._max_bytes:
            raise MediaTooLargeError(len(data), self._max_bytes)
        log.debug("media_downloaded", file_id=file_id, size=len(data))
        return data


# ------------------------------------------------------------------------------- detection


def _extension(filename: str | None) -> str:
    return PurePosixPath(filename).suffix.lower() if filename else ""


def is_heic(data: bytes, mime: str | None = None, filename: str | None = None) -> bool:
    """HEIC/HEIF by mime, by extension or by the ISO-BMFF ``ftyp`` brand in the first bytes."""
    if mime and mime.lower() in HEIC_MIMES:
        return True
    if _extension(filename) in HEIC_EXTENSIONS:
        return True
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12].lower() in HEIC_BRANDS


def is_pdf(data: bytes, mime: str | None = None, filename: str | None = None) -> bool:
    if mime and mime.lower() == PDF_MIME:
        return True
    return _extension(filename) == ".pdf" or data[:5] == b"%PDF-"


def is_image(data: bytes, mime: str | None = None, filename: str | None = None) -> bool:
    """Anything Pillow can plausibly open: ``image/*`` mime, a known extension or magic bytes."""
    if mime and mime.lower().startswith("image/"):
        return True
    if _extension(filename) in IMAGE_EXTENSIONS:
        return True
    return (
        data[:3] == b"\xff\xd8\xff"
        or data[:8] == b"\x89PNG\r\n\x1a\n"
        or data[:6] in {b"GIF87a", b"GIF89a"}
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or is_heic(data)
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pdf_page_estimate(data: bytes) -> int:
    """Count ``/Type /Page`` objects. Under-counts compressed object streams, never over-counts."""
    return len(_PDF_PAGE_RE.findall(data))


# -------------------------------------------------------------------------------- images


@dataclass(frozen=True)
class EncodedImage:
    data: bytes
    width: int
    height: int
    quality: int


def encode_jpeg(
    data: bytes, *, max_edge: int = MAX_IMAGE_EDGE, max_b64: int = MAX_IMAGE_B64
) -> EncodedImage:
    """Blocking: open (HEIC included), EXIF-transpose, flatten to RGB, fit the long edge, JPEG.

    Retries at lower quality until the base64 form fits Claude's per-image cap.
    """
    try:
        with Image.open(BytesIO(data)) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened) or opened
            image = _flatten(image)
            if max(image.size) > max_edge:
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            for quality in JPEG_QUALITY_STEPS:
                out = BytesIO()
                image.save(out, format="JPEG", quality=quality, optimize=True)
                encoded = out.getvalue()
                if len(encoded) * 4 // 3 + 4 <= max_b64:
                    return EncodedImage(encoded, image.width, image.height, quality)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise MediaError(f"cannot decode image: {exc}") from exc
    raise MediaTooLargeError(len(encoded) * 4 // 3, max_b64, "encoded image")


def _flatten(image: Image.Image) -> Image.Image:
    """RGB with transparency composited onto white (JPEG has no alpha; black would hide labels)."""
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


async def prepare_image(data: bytes, mime: str | None, filename: str | None) -> Attachment:
    """Bytes from Telegram (photo or image document) → a JPEG ``Attachment`` for the model.

    ``sha256`` is the hash of the *original* bytes: it identifies what the user sent, so the
    ``[image: <hash>]`` stub in ``conversation_turns`` and ``meals.raw_ref`` stay stable even if
    the encoder changes.
    """
    heic = is_heic(data, mime, filename)
    encoded = await asyncio.to_thread(encode_jpeg, data)
    log.debug(
        "image_prepared",
        heic=heic,
        source_bytes=len(data),
        jpeg_bytes=len(encoded.data),
        width=encoded.width,
        height=encoded.height,
        quality=encoded.quality,
    )
    return Attachment(
        kind="image",
        mime="image/jpeg",
        bytes_b64=base64.b64encode(encoded.data).decode("ascii"),
        sha256=sha256_hex(data),
        filename=_jpeg_name(filename),
    )


def _jpeg_name(filename: str | None) -> str:
    stem = PurePosixPath(filename).stem if filename else ""
    return f"{stem or 'photo'}.jpg"


# ----------------------------------------------------------------------------- documents


async def prepare_document(data: bytes, mime: str | None, filename: str | None) -> Attachment:
    """A Telegram *document* → PDF document block, image (HEIC included) or an honest stub.

    Unsupported types are not dropped: the attachment carries ``text="unsupported: <mime>"`` so
    the model can tell the user what to send instead.
    """
    if is_pdf(data, mime, filename):
        if len(data) > MAX_PDF_BYTES:
            raise MediaTooLargeError(len(data), MAX_PDF_BYTES, "pdf")
        pages = pdf_page_estimate(data)
        if pages > MAX_PDF_PAGES:
            raise MediaError(f"pdf has about {pages} pages, limit {MAX_PDF_PAGES}")
        log.debug("pdf_prepared", size=len(data), pages=pages)
        return Attachment(
            kind="document",
            mime=PDF_MIME,
            bytes_b64=base64.b64encode(data).decode("ascii"),
            sha256=sha256_hex(data),
            filename=filename or "document.pdf",
        )
    if is_image(data, mime, filename):
        return await prepare_image(data, mime, filename)
    label = (mime or "unknown").lower()
    log.info("document_unsupported", mime=label, filename=filename, size=len(data))
    return Attachment(
        kind="document",
        mime=mime,
        text=f"unsupported: {label}",
        sha256=sha256_hex(data),
        filename=filename,
    )


# -------------------------------------------------------------------------------- albums


@dataclass
class _Album[T]:
    parts: list[tuple[int, T]] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: list[T] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None


class AlbumCollector[T]:
    """Gather the parts of one media group and hand the complete, ordered list to one caller.

    Every update of an album calls ``collect``; all of them wait until the debounce timer (reset
    on each arrival) expires or ``max_parts`` have arrived. The *first* caller receives the
    ordered parts and continues into the handler; the others receive ``None`` and stop. Parts
    are ordered by ``order`` (use the Telegram ``message_id``) and by arrival when it is absent.
    """

    def __init__(
        self, *, debounce_s: float = ALBUM_DEBOUNCE_S, max_parts: int = ALBUM_MAX_PARTS
    ) -> None:
        self._debounce_s = debounce_s
        self._max_parts = max_parts
        self._pending: dict[str, _Album[T]] = {}

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def collect(
        self, media_group_id: str, part: T, *, order: int | None = None
    ) -> list[T] | None:
        album = self._pending.get(media_group_id)
        first = album is None
        if album is None:
            album = _Album[T]()
            self._pending[media_group_id] = album
        seq = order if order is not None else len(album.parts)
        album.parts.append((seq, part))
        if len(album.parts) >= self._max_parts:
            self._resolve(media_group_id)
        else:
            if album.timer is not None:
                album.timer.cancel()
            loop = asyncio.get_running_loop()
            album.timer = loop.call_later(self._debounce_s, self._resolve, media_group_id)
        await album.done.wait()
        return album.result if first else None

    def _resolve(self, media_group_id: str) -> None:
        album = self._pending.pop(media_group_id, None)
        if album is None:
            return
        if album.timer is not None:
            album.timer.cancel()
            album.timer = None
        album.result = [part for _, part in sorted(album.parts, key=lambda pair: pair[0])]
        log.debug("album_collected", media_group_id=media_group_id, parts=len(album.result))
        album.done.set()
