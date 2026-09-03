"""Token usage and cost computation from the price table in ``Settings``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strikt.config import ModelPrice, Settings

_MILLION = 1_000_000


@dataclass(frozen=True)
class LLMUsage:
    """Token counts for one API call (all four buckets the API bills separately)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

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
        )


def usage_from_message(message: Any) -> LLMUsage:
    """Read ``response.usage`` from an SDK ``Message`` (Optional fields default to 0)."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return LLMUsage()
    return LLMUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )


def compute_cost(price: ModelPrice | None, usage: LLMUsage) -> float:
    """USD for one call; 0.0 when the model has no price row (logged by the caller)."""
    if price is None:
        return 0.0
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    ) / _MILLION


def cost_for(settings: Settings, model: str, usage: LLMUsage) -> float:
    return compute_cost(settings.price_for(model), usage)
