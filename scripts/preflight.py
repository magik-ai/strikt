"""One real call to Anthropic with the coach's actual tools, to prove the request is accepted.

Two outages in one evening were the same shape: a request this code builds is rejected by the
API for a reason no local test can see, and the user is told "Claude is unavailable". `FakeLLM`
answers whatever it is asked; only the real endpoint knows about per-request budgets.

    ANTHROPIC_API_KEY=sk-ant-... uv run python scripts/preflight.py
    uv run python scripts/preflight.py --key sk-ant-...      # not saved anywhere

It sends one 16-token message carrying the whole tool list and the coach system prompt, prints
what came back, and exits non-zero on any rejection with the API's own words. Cost is a fraction
of a cent, and the key is never written to disk or logged.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strikt.agent.context import load_prompt
from strikt.agent.tools import build_registry
from strikt.config import get_settings

PROBE = "Reply with the single word: ready."


async def probe(api_key: str, model: str | None) -> int:
    import anthropic

    settings = get_settings()
    registry = build_registry()
    tools = registry.definitions()
    system = [
        {"type": "text", "text": load_prompt("coach")},
        {"type": "text", "text": "<probe>preflight check, answer in one word</probe>"},
    ]
    optional = sum(
        len(
            [
                p
                for p in d["input_schema"].get("properties", {})
                if p not in set(d["input_schema"].get("required", []))
            ]
        )
        for d in tools
    )
    print(
        f"tools: {len(tools)}, optional parameters: {optional}, strict: "
        f"{sum(1 for d in tools if d.get('strict'))}"
    )
    print(f"system prompt: {sum(len(block['text']) for block in system)} characters")

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=60.0, max_retries=1)
    try:
        message = await client.messages.create(
            model=model or settings.anthropic_model,
            max_tokens=16,
            system=system,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            messages=[{"role": "user", "content": PROBE}],
        )
    except anthropic.APIStatusError as exc:
        print(f"\nREJECTED ({exc.status_code}): {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()

    text = "".join(block.text for block in message.content if block.type == "text")
    print(f"\nOK: {message.model} answered {text.strip()!r}")
    print(f"tokens in/out: {message.usage.input_tokens}/{message.usage.output_tokens}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", default=os.environ.get("ANTHROPIC_API_KEY"))
    parser.add_argument("--model", default=None, help="override ANTHROPIC_MODEL")
    args = parser.parse_args(argv)
    if not args.key:
        print("no key: pass --key or set ANTHROPIC_API_KEY", file=sys.stderr)
        return 2
    return asyncio.run(probe(args.key, args.model))


if __name__ == "__main__":
    raise SystemExit(main())
