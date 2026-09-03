# Strikt — working in this repo

Strikt is a one-window Telegram health coach on Claude Sonnet 5 (`claude-sonnet-5`). Python
package `strikt`, `src/` layout, GitHub repo `magik-ai/bomiso` (to be renamed).

## Laws

1. **The brief is product law**: `/root/.claude/uploads/.../b739469c-coachbotbrief.md` (copied
   into the planning scratchpad). Voice, food method, proactivity and onboarding come from it.
2. **PLAN.md is engineering law** (scratchpad `PLAN.md`): module contracts, data model, tools.
   Where they disagree, the brief wins and you say so in your report.
3. Nothing hard-codes the first user's numbers. Everything personal enters via onboarding/import.

## Dev loop

```
uv sync                       # deps (uv.lock committed; Python 3.13/3.14)
make fmt                      # ruff --fix + ruff format
make lint                     # ruff check + format --check
make type                     # mypy --strict src
make test                     # pytest (SQLite + aiosqlite, no network)
make check                    # all of the above + PROMPTS.md sync
make prompts                  # regenerate PROMPTS.md from src/strikt/agent/prompts/*.md
make migrate / make revision m="…"
make keygen                   # TOKEN_ENCRYPTION_KEY
```

Dependencies are pinned exactly (research/09 §1.1). Local Python is pinned to 3.13 in
`.python-version`: either 3.13 or 3.14 is acceptable, but the locally installed 3.14.0rc2 fails with
pydantic 2.13.5 (`typing._eval_type(..., prefer_fwd_module=)` is 3.14-final only). Docker uses
`python:3.14-slim` (every compiled dep has cp314 wheels) and CI runs 3.14 and 3.13.

## Layout

```
src/strikt/
  config.py       pydantic-settings; every var documented in .env.example
  logging.py      structlog (json/pretty, secrets redacted)
  events.py       EventBus + Workout/Sleep/Recovery/Measurement/DayStateChanged/UserReplied
  privacy.py      delete_everything(session, user_id) → counts per table
  core/types.py   Macros, FoodItemIn, DayState, Incoming/Outgoing, Button, views, Flag
  core/clock.py   Clock/SystemClock/FakeClock, local_date/local_now/local_day_bounds
  db/models.py    every table (PLAN §3), StrEnum columns as VARCHAR, JSON→JSONB variant
  db/repo.py      all reads/writes; every user-owned query filters by user_id
  db/engine.py    make_engine / make_session_factory / init_sqlite_for_tests
  db/crypto.py    Fernet TokenCipher
  agent/client.py LLM (AsyncAnthropic) + FakeLLM; LLMResult; usage recording
  agent/usage.py  cost from the price table
  agent/tools/    registry (strict schemas, dispatch), schemas (one model per tool), handlers
  agent/prompts/  coach, onboarding, proactive, verify, summarize, import (→ PROMPTS.md)
  telegram/       messenger (Protocol + aiogram + Fake), copy (ru/en), render (card), keyboards, queue
migrations/       alembic (async env); 0001_initial matches Base.metadata (tested)
tests/            conftest: sqlite engine, session, FakeClock, FakeLLM, FakeMessenger, seeded user
```

Modules still owned by later build stages (they raise `NotImplementedError("implemented by build
stage")`): `app.py` and every handler in `agent/tools/{food,training,body,state,profile,research,
memory}.py`. Not yet created: `nutrition/`, `memory/`, `agent/{loop,context,verify,
proactive_decide}.py`, `telegram/{bot,handlers,media,voice,daycard}.py`, `proactive/`,
`integrations/`, `web/`, `onboarding/`.

## Conventions (PLAN §14)

- Async everywhere; no blocking IO in handlers (PIL via `asyncio.to_thread`).
- Complete type hints, `from __future__ import annotations`, pydantic models for all tool IO.
- `structlog.get_logger()`; never `print`. Never log a secret.
- Time: store UTC, compute local with `zoneinfo` via `core/clock.py`; SQLite returns naive
  datetimes — normalise with `ensure_utc`.
- Copy: model-written replies; code-rendered strings live in `telegram/copy.py` (ru/en).
- Tool schemas: docstring = tool description, `Field(description=…)` on every field,
  `extra="forbid"`, no free `dict` fields, no numeric constraints (strict mode subset).
- Prompt caching: the coach prompt is static; the tool list is sorted and byte-stable; anything
  volatile goes at the end of the user message.
- Repo functions `flush`; callers `commit`.
- Tests: no network, SQLite only, every fixture in `tests/conftest.py`.
