"""Integration copy reads in every language the bot speaks.

The WHOOP, Withings and Apple Health texts used to live in each module in en and ru only, while
``resolve_lang`` returns any of twenty languages: a German user's "connect WHOOP" raised
``KeyError: 'de'``. The strings now come from ``telegram/locales`` like every other one.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock
from strikt.db.crypto import TokenCipher
from strikt.db.models import User
from strikt.integrations import apple_health
from strikt.integrations.base import PROVIDER_NAMES, link_copy
from strikt.integrations.whoop import WhoopIntegration
from strikt.integrations.withings import WithingsIntegration
from strikt.telegram.copy import STRINGS, t
from tests.test_integrations_fakes import Router, make_settings


async def test_connect_and_callback_copy_in_every_language(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    factory = Router().client_factory()
    integrations = [
        WhoopIntegration(settings, cipher=cipher, clock=clock, client_factory=factory),
        WithingsIntegration(settings, cipher=cipher, clock=clock, client_factory=factory),
    ]
    for lang in STRINGS:
        user.language = lang
        for integration in integrations:
            info = await integration.connect(session, user)
            assert info.kind == "oauth" and info.instructions, (lang, integration.provider)
            assert PROVIDER_NAMES[integration.provider] in info.instructions
            denied_user, denied = await integration.handle_callback(
                session, {"error": "access_denied", "state": info.extra["state"]}
            )
            assert denied_user is not None and denied == link_copy(
                lang, "denied", integration.provider
            )
        text = apple_health.instructions_text(lang, url="https://x/y", token="tok")
        assert "https://x/y" in text


def test_link_copy_names_the_provider_and_falls_back_to_english() -> None:
    assert "MyFitnessPal" in link_copy("ru", "lost", "myfitnesspal")
    assert link_copy(None, "expired", "whoop") == t("en", "link.expired", provider="WHOOP")
    assert "{provider}" not in link_copy("ja", "failed", "withings")
