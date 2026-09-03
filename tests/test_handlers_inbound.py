"""``telegram/handlers``: aiogram ``Message``/``CallbackQuery`` → the transport-free inbound types."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram.types import (
    Audio,
    CallbackQuery,
    Chat,
    Document,
    Message,
    MessageOriginChannel,
    MessageOriginHiddenUser,
    MessageOriginUser,
    PhotoSize,
    User,
    VideoNote,
    Voice,
)

from strikt.telegram.handlers import (
    InboundMessage,
    MediaRef,
    from_callback,
    from_message,
    merge_album,
    parse_command,
)

WHEN = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
SENDER = User(id=77, is_bot=False, first_name="Ilya", last_name="C", language_code="ru")
CHAT = Chat(id=77, type="private")


def _message(**kwargs: object) -> Message:
    base: dict[str, object] = {
        "message_id": 5,
        "date": WHEN,
        "chat": CHAT,
        "from_user": SENDER,
    }
    base.update(kwargs)
    return Message.model_validate(base)


def test_parse_command_variants() -> None:
    assert parse_command("/start") == ("start", None)
    assert parse_command("/start  ab12 ") == ("start", "ab12")
    assert parse_command("/START@StriktBot code") == ("start", "code")
    assert parse_command("/forget_me") == ("forget_me", None)
    assert parse_command("hello /start") == (None, None)
    assert parse_command("") == (None, None)
    assert parse_command(None) == (None, None)
    assert parse_command("/") == (None, None)


def test_text_command_message() -> None:
    inbound = from_message(_message(text="/start WELCOME1"))
    assert inbound.telegram_id == 77 and inbound.chat_id == 77 and inbound.message_id == 5
    assert inbound.command == "start" and inbound.command_args == "WELCOME1"
    assert inbound.text == "/start WELCOME1" and inbound.language_code == "ru"
    assert inbound.received_at == WHEN and inbound.media == [] and inbound.lang == "ru"


def test_photo_takes_the_largest_size_and_the_caption() -> None:
    sizes = [
        PhotoSize(file_id="small", file_unique_id="s", width=90, height=60, file_size=1000),
        PhotoSize(file_id="big", file_unique_id="b", width=1280, height=960, file_size=90_000),
    ]
    inbound = from_message(
        _message(photo=sizes, caption="/start is not a command in a caption", media_group_id="g7")
    )
    assert inbound.media == [MediaRef("photo", "big", mime="image/jpeg", size=90_000)]
    assert inbound.text == "/start is not a command in a caption"
    assert inbound.command is None and inbound.media_group_id == "g7"


def test_document_voice_audio_and_video_note() -> None:
    doc = Document(
        file_id="d1",
        file_unique_id="d",
        file_name="IMG_1.HEIC",
        mime_type="image/heic",
        file_size=5,
    )
    assert from_message(_message(document=doc)).media == [
        MediaRef("document", "d1", mime="image/heic", filename="IMG_1.HEIC", size=5)
    ]
    voice = Voice(file_id="v1", file_unique_id="v", duration=3, mime_type=None, file_size=9)
    assert from_message(_message(voice=voice)).media == [
        MediaRef("voice", "v1", mime="audio/ogg", size=9)
    ]
    audio = Audio(
        file_id="a1", file_unique_id="a", duration=3, mime_type="audio/mpeg", file_name="x.mp3"
    )
    assert from_message(_message(audio=audio)).media == [
        MediaRef("audio", "a1", mime="audio/mpeg", filename="x.mp3", size=None)
    ]
    note = VideoNote(file_id="n1", file_unique_id="n", length=240, duration=4, file_size=77)
    assert from_message(_message(video_note=note)).media == [
        MediaRef("video_note", "n1", mime="video/mp4", size=77)
    ]


def test_forward_origins() -> None:
    origin_user = MessageOriginUser(
        type="user",
        date=WHEN,
        sender_user=User(id=9, is_bot=False, first_name="Anna", last_name="K"),
    )
    assert from_message(_message(text="x", forward_origin=origin_user)).forwarded_from == "Anna K"
    hidden = MessageOriginHiddenUser(type="hidden_user", date=WHEN, sender_user_name="Coach")
    assert from_message(_message(text="x", forward_origin=hidden)).forwarded_from == "Coach"
    channel = MessageOriginChannel(
        type="channel",
        date=WHEN,
        chat=Chat(id=-100, type="channel", title="Kinoya Menu"),
        message_id=3,
    )
    assert from_message(_message(text="x", forward_origin=channel)).forwarded_from == "Kinoya Menu"
    assert from_message(_message(text="x")).forwarded_from is None


def test_received_at_override_and_missing_sender() -> None:
    now = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
    inbound = from_message(_message(text="hi", from_user=None), received_at=now)
    assert inbound.received_at == now and inbound.telegram_id == CHAT.id
    assert inbound.language_code is None and inbound.lang == "en"


def test_merge_album_keeps_order_first_caption_and_origin() -> None:
    def part(
        mid: int, file_id: str, text: str | None = None, fwd: str | None = None
    ) -> InboundMessage:
        return InboundMessage(
            telegram_id=77,
            chat_id=77,
            message_id=mid,
            received_at=WHEN,
            text=text,
            language_code="ru",
            media=[MediaRef("photo", file_id)],
            media_group_id="g",
            forwarded_from=fwd,
        )

    merged = merge_album([part(10, "a"), part(11, "b", text="  "), part(12, "c", "cart", "Krave")])
    assert [m.file_id for m in merged.media] == ["a", "b", "c"]
    assert merged.text == "cart" and merged.forwarded_from == "Krave"
    assert merged.message_id == 10 and merged.media_group_id is None and merged.command is None


def test_from_callback() -> None:
    query = CallbackQuery(
        id="q1",
        from_user=SENDER,
        chat_instance="ci",
        data="s:7:lunch",
        message=_message(text="Омлет 420"),
    )
    cb = from_callback(query)
    assert cb.telegram_id == 77 and cb.chat_id == 77 and cb.message_id == 5
    assert cb.callback_id == "q1" and cb.data == "s:7:lunch" and cb.language_code == "ru"
    orphan = from_callback(CallbackQuery(id="q2", from_user=SENDER, chat_instance="ci"))
    assert orphan.chat_id == 77 and orphan.message_id is None and orphan.data is None
