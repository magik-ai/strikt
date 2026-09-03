"""Token usage and cost computation from the price table in ``Settings``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strikt.config import ModelPrice, Settings

_MILLION = 1_000_000


@dataclass(frozen=True)
class LLMUsage:
    """Counts for one API call: the four token buckets the API bills separately, how many of the
    cache-write tokens were 1-hour writes (a subset of ``cache_write_tokens``), and the number of
    server-side web searches (billed per request, on top of the tokens)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    web_search_requests: int = 0

    @property
    def total_input(self) -> int:
        """Full prompt size: uncached + cache reads + cache writes."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens + other.cache_write_1h_tokens,
            web_search_requests=self.web_search_requests + other.web_search_requests,
        )


def _int(obj: Any, name: str) -> int:
    return int(getattr(obj, name, 0) or 0) if obj is not None else 0


def usage_from_message(message: Any) -> LLMUsage:
    """Read ``response.usage`` from an SDK ``Message`` (Optional fields default to 0)."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return LLMUsage()
    cache_write = _int(usage, "cache_creation_input_tokens")
    creation = getattr(usage, "cache_creation", None)
    one_hour = min(_int(creation, "ephemeral_1h_input_tokens"), cache_write)
    return LLMUsage(
        input_tokens=_int(usage, "input_tokens"),
        output_tokens=_int(usage, "output_tokens"),
        cache_read_tokens=_int(usage, "cache_read_input_tokens"),
        cache_write_tokens=cache_write,
        cache_write_1h_tokens=one_hour,
        web_search_requests=_int(getattr(usage, "server_tool_use", None), "web_search_requests"),
    )


def compute_cost(price: ModelPrice | None, usage: LLMUsage) -> float:
    """USD for one call; 0.0 when the model has no price row (logged by the caller)."""
    if price is None:
        return 0.0
    write_1h_price = price.cache_write_1h if price.cache_write_1h is not None else price.cache_write
    write_5m = usage.cache_write_tokens - usage.cache_write_1h_tokens
    tokens = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + write_5m * price.cache_write
        + usage.cache_write_1h_tokens * write_1h_price
    ) / _MILLION
    searches = usage.web_search_requests * price.web_search_per_1000 / 1000
    return tokens + searches


def cost_for(settings: Settings, model: str, usage: LLMUsage) -> float:
    return compute_cost(settings.price_for(model), usage)
