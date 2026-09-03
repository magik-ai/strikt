"""One cheap request to tell a working optional key from a typo.

The Anthropic key has its own validator in ``agent/client.py`` (it is the one key the coach
cannot run without). These two are the optional ones the user may hand over in the chat: an
OpenAI key so voice notes get transcribed, and a USDA key so the food database answers faster
and more often.

The verdict is deliberately three-valued. ``invalid`` means the service looked at the key and
said no, and the user is asked for another. ``unknown`` means the service did not answer at
all - a network blip, a 500, a timeout - and the key is stored anyway, because refusing a good
key over someone else's outage is worse than finding out on the next real call.
"""

from __future__ import annotations

from typing import Literal

import httpx
import structlog

log = structlog.get_logger(__name__)

KeyCheck = Literal["valid", "invalid", "unknown"]

#: Wall-clock cap for one check. The user is watching the chat while it runs.
CHECK_TIMEOUT_S = 10.0
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"


def _verdict(status: int, service: str) -> KeyCheck:
    if status in (401, 403):
        return "invalid"
    if 200 <= status < 300:
        return "valid"
    log.info("key_check_inconclusive", service=service, status=status)
    return "unknown"


async def check_openai_key(key: str, *, timeout_s: float = CHECK_TIMEOUT_S) -> KeyCheck:
    """``GET /v1/models`` with the key. Free, and the smallest authenticated call there is."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(
                OPENAI_MODELS_URL, headers={"Authorization": f"Bearer {key}"}
            )
    except httpx.HTTPError as exc:
        log.info("key_check_failed", service="openai", error=type(exc).__name__)
        return "unknown"
    return _verdict(response.status_code, "openai")


async def check_usda_key(key: str, *, timeout_s: float = CHECK_TIMEOUT_S) -> KeyCheck:
    """One one-result search. api.data.gov answers 403 for a key it does not know."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(
                USDA_SEARCH_URL, params={"api_key": key, "query": "egg", "pageSize": 1}
            )
    except httpx.HTTPError as exc:
        log.info("key_check_failed", service="usda", error=type(exc).__name__)
        return "unknown"
    return _verdict(response.status_code, "usda")


async def check_secret(service: str, key: str, *, timeout_s: float = CHECK_TIMEOUT_S) -> KeyCheck:
    """Dispatch by service name; an unknown service is not something to reject a key over."""
    if service == "openai":
        return await check_openai_key(key, timeout_s=timeout_s)
    if service == "usda":
        return await check_usda_key(key, timeout_s=timeout_s)
    return "unknown"
