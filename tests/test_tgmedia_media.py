"""telegram/media: image preparation (HEIC, EXIF, resize), documents, download guard, albums."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass
from io import BytesIO
from typing import BinaryIO

import pytest
from PIL import Image

from strikt.telegram import media
from strikt.telegram.media import (
    MAX_IMAGE_EDGE,
    AiogramDownloader,
    AlbumCollector,
    MediaError,
    MediaTooLargeError,
    encode_jpeg,
    is_heic,
    is_image,
    is_pdf,
    pdf_page_estimate,
    prepare_document,
    prepare_image,
)

# ------------------------------------------------------------------------------ fixtures


def _png(width: int = 40, height: int = 30, mode: str = "RGB") -> bytes:
    image = Image.new(mode, (width, height), (200, 30, 30, 255) if mode == "RGBA" else "red")
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _jpeg(width: int = 40, height: int = 30, *, orientation: int | None = None) -> bytes:
    image = Image.new("RGB", (width, height), "blue")
    out = BytesIO()
    if orientation is None:
        image.save(out, format="JPEG")
    else:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


def _heic(width: int = 40, height: int = 30) -> bytes:
    image = Image.new("RGB", (width, height), "green")
    out = BytesIO()
    image.save(out, format="HEIF")
    return out.getvalue()


def _pdf(pages: int = 1) -> bytes:
    body = b"%PDF-1.4\n"
    for n in range(pages):
        body += f"{n + 1} 0 obj << /Type /Page /Parent 99 0 R >> endobj\n".encode()
    body += b"99 0 obj << /Type /Pages /Count 1 >> endobj\n%%EOF\n"
    return body


def _decode(attachment_b64: str | None) -> Image.Image:
    assert attachment_b64 is not None
    return Image.open(BytesIO(base64.b64decode(attachment_b64)))


# ----------------------------------------------------------------------------- detection


def test_is_heic_by_mime_extension_and_magic() -> None:
    assert is_heic(b"", "image/heic", None)
    assert is_heic(b"", "IMAGE/HEIF", None)
    assert is_heic(b"", None, "IMG_0001.HEIC")
    assert is_heic(b"", None, "x.heif")
    assert is_heic(_heic(), None, None)
    assert not is_heic(_jpeg(), None, "photo.jpg")
    assert not is_heic(b"short", None, None)


def test_is_image_and_is_pdf() -> None:
    assert is_image(_jpeg())
    assert is_image(_png())
    assert is_image(b"GIF89a....")
    assert is_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    assert is_image(b"", "image/webp")
    assert is_image(b"", None, "scan.PNG")
    assert not is_image(b"hello", "text/plain", "a.txt")
    assert is_pdf(_pdf())
    assert is_pdf(b"", "application/pdf")
    assert is_pdf(b"", None, "menu.pdf")
    assert not is_pdf(_png(), "image/png", "a.png")


def test_pdf_page_estimate_counts_pages_not_pages_node() -> None:
    assert pdf_page_estimate(_pdf(3)) == 3
    assert pdf_page_estimate(b"%PDF-1.4 /Type /Pages only") == 0


# --------------------------------------------------------------------------------- images


async def test_prepare_image_jpeg_passthrough_keeps_size_and_hashes_original() -> None:
    data = _jpeg(40, 30)
    attachment = await prepare_image(data, None, None)
    assert attachment.kind == "image"
    assert attachment.mime == "image/jpeg"
    assert attachment.sha256 == hashlib.sha256(data).hexdigest()
    assert attachment.filename == "photo.jpg"
    image = _decode(attachment.bytes_b64)
    assert image.format == "JPEG"
    assert image.size == (40, 30)
    assert image.mode == "RGB"


async def test_prepare_image_resizes_long_edge() -> None:
    attachment = await prepare_image(_png(4000, 1000), "image/png", "wide.png")
    image = _decode(attachment.bytes_b64)
    assert image.size == (MAX_IMAGE_EDGE, MAX_IMAGE_EDGE // 4)
    assert attachment.filename == "wide.jpg"


async def test_prepare_image_flattens_rgba_onto_white() -> None:
    rgba = Image.new("RGBA", (10, 10), (0, 0, 0, 0))  # fully transparent
    out = BytesIO()
    rgba.save(out, format="PNG")
    attachment = await prepare_image(out.getvalue(), "image/png", "t.png")
    image = _decode(attachment.bytes_b64)
    r, g, b = image.getpixel((5, 5))  # type: ignore[misc]
    assert min(r, g, b) > 240


async def test_prepare_image_applies_exif_orientation() -> None:
    attachment = await prepare_image(_jpeg(40, 20, orientation=6), "image/jpeg", "rot.jpg")
    image = _decode(attachment.bytes_b64)
    assert image.size == (20, 40)


async def test_prepare_image_converts_heic_document() -> None:
    data = _heic(60, 40)
    for mime, name in (("image/heic", "IMG_1.HEIC"), (None, "IMG_1.heic"), (None, None)):
        attachment = await prepare_image(data, mime, name)
        assert attachment.mime == "image/jpeg"
        image = _decode(attachment.bytes_b64)
        assert image.format == "JPEG"
        assert image.size == (60, 40)


async def test_prepare_image_rejects_garbage() -> None:
    with pytest.raises(MediaError):
        await prepare_image(b"not an image at all", "image/jpeg", "x.jpg")


def test_encode_jpeg_lowers_quality_to_fit_cap() -> None:
    noisy = Image.effect_noise((600, 600), 80).convert("RGB")
    out = BytesIO()
    noisy.save(out, format="PNG")
    at_85 = encode_jpeg(out.getvalue())
    cap = len(at_85.data) * 4 // 3 + 4 - 1  # just under what q85 needs
    smaller = encode_jpeg(out.getvalue(), max_b64=cap)
    assert smaller.quality < at_85.quality
    assert len(smaller.data) < len(at_85.data)


def test_encode_jpeg_raises_when_nothing_fits() -> None:
    with pytest.raises(MediaTooLargeError):
        encode_jpeg(_png(200, 200), max_b64=10)


# ------------------------------------------------------------------------------ documents


async def test_prepare_document_pdf() -> None:
    data = _pdf(2)
    attachment = await prepare_document(data, "application/pdf", "menu.pdf")
    assert attachment.kind == "document"
    assert attachment.mime == "application/pdf"
    assert attachment.bytes_b64 is not None
    assert base64.b64decode(attachment.bytes_b64) == data
    assert attachment.sha256 == hashlib.sha256(data).hexdigest()
    assert attachment.filename == "menu.pdf"


async def test_prepare_document_pdf_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media, "MAX_PDF_BYTES", 16)
    with pytest.raises(MediaTooLargeError) as too_large:
        await prepare_document(_pdf(1), "application/pdf", "big.pdf")
    assert too_large.value.limit == 16
    monkeypatch.setattr(media, "MAX_PDF_BYTES", 10_000_000)
    monkeypatch.setattr(media, "MAX_PDF_PAGES", 2)
    with pytest.raises(MediaError):
        await prepare_document(_pdf(3), None, "long.pdf")


async def test_prepare_document_routes_images_and_stubs_the_rest() -> None:
    image = await prepare_document(_heic(), "image/heic", "IMG.HEIC")
    assert image.kind == "image"
    assert image.mime == "image/jpeg"

    magic_only = await prepare_document(_png(), "application/octet-stream", "blob")
    assert magic_only.kind == "image"

    other = await prepare_document(b"a,b,c\n", "text/csv", "data.csv")
    assert other.kind == "document"
    assert other.text == "unsupported: text/csv"
    assert other.bytes_b64 is None
    assert other.sha256 == hashlib.sha256(b"a,b,c\n").hexdigest()

    unknown = await prepare_document(b"\x00\x01", None, None)
    assert unknown.text == "unsupported: unknown"


# ------------------------------------------------------------------------------- download


@dataclass
class _FakeFile:
    file_id: str
    file_size: int | None
    file_path: str | None


class _FakeBot:
    def __init__(self, payload: bytes, *, size: int | None = None, path: str | None = "p") -> None:
        self.payload = payload
        self.size = size
        self.path = path
        self.downloaded: list[str] = []

    async def get_file(self, file_id: str) -> _FakeFile:
        return _FakeFile(file_id, self.size, self.path)

    async def download_file(self, file_path: str, destination: BinaryIO) -> BinaryIO:
        self.downloaded.append(file_path)
        destination.write(self.payload)
        return destination


async def test_downloader_returns_bytes() -> None:
    bot = _FakeBot(b"abc", size=3, path="photos/1.jpg")
    downloader = AiogramDownloader(bot)  # type: ignore[arg-type]
    assert await downloader.download("fid") == b"abc"
    assert bot.downloaded == ["photos/1.jpg"]


async def test_downloader_guards_size_before_and_after() -> None:
    declared = AiogramDownloader(_FakeBot(b"abc", size=50), max_bytes=10)  # type: ignore[arg-type]
    with pytest.raises(MediaTooLargeError) as exc:
        await declared.download("fid")
    assert exc.value.limit == 10

    actual = AiogramDownloader(_FakeBot(b"x" * 20, size=None), max_bytes=10)  # type: ignore[arg-type]
    with pytest.raises(MediaTooLargeError):
        await actual.download("fid")

    no_path = AiogramDownloader(_FakeBot(b"abc", size=3, path=None))  # type: ignore[arg-type]
    with pytest.raises(MediaError):
        await no_path.download("fid")


def test_media_too_large_carries_mb() -> None:
    err = MediaTooLargeError(30 * 1024 * 1024, 20 * 1024 * 1024)
    assert err.limit_mb == 20
    assert "limit" in str(err)


# --------------------------------------------------------------------------------- albums


async def test_album_collector_resolves_once_in_order() -> None:
    collector: AlbumCollector[str] = AlbumCollector(debounce_s=0.05)

    async def part(name: str, order: int, delay: float) -> list[str] | None:
        await asyncio.sleep(delay)
        return await collector.collect("g1", name, order=order)

    results = await asyncio.gather(part("a", 10, 0), part("c", 12, 0.02), part("b", 11, 0.01))
    assert list(results) == [["a", "b", "c"], None, None]
    assert collector.pending == 0


async def test_album_collector_debounce_resets_on_each_part() -> None:
    collector: AlbumCollector[int] = AlbumCollector(debounce_s=0.3)
    started = asyncio.get_running_loop().time()

    async def late(n: int, delay: float) -> list[int] | None:
        await asyncio.sleep(delay)
        return await collector.collect("g", n)

    results = await asyncio.gather(late(1, 0), late(2, 0.1), late(3, 0.2))
    elapsed = asyncio.get_running_loop().time() - started
    assert results[0] == [1, 2, 3]
    assert elapsed >= 0.45  # 0.2 arrival + a full 0.3 debounce after the last part


async def test_album_collector_caps_parts_without_waiting() -> None:
    collector: AlbumCollector[int] = AlbumCollector(debounce_s=5.0, max_parts=3)
    results = await asyncio.wait_for(
        asyncio.gather(*(collector.collect("g", n) for n in range(3))), timeout=1.0
    )
    assert list(results) == [[0, 1, 2], None, None]


async def test_album_collector_keeps_groups_apart() -> None:
    collector: AlbumCollector[str] = AlbumCollector(debounce_s=0.05)
    results = await asyncio.gather(
        collector.collect("x", "x1"), collector.collect("y", "y1"), collector.collect("x", "x2")
    )
    assert list(results) == [["x1", "x2"], ["y1"], None]
