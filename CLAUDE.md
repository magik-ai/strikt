# Strikt - working in this repo

Strikt is a one-window Telegram health coach on Claude Sonnet 5 (`claude-sonnet-5`). Python
package `strikt`, `src/` layout, GitHub repo `magik-ai/bomiso` (to be renamed).

## Laws

1. **The brief is product law**: `docs/BRIEF.md` (the public copy; two lines of the owner's body
   numbers are removed). Voice, food method, proactivity and onboarding come from it.
2. **`docs/PLAN.md` is engineering law**: module contracts, data model, tools.
   Where they disagree, the brief wins and you say so in your report.
3. Nothing hard-codes the first user's numbers. Everything personal enters via onboarding/import.
4. **Bring-your-own-key**: every model call for a user is billed to that user's Anthropic key
   (`LLM_KEY_MODE=user`, the default). Resolve the client with `llm_factory.for_user(session,
   user)`; a `None` means "no key": reply with the walkthrough (`key.needed`) or skip silently
   (`llm_key_missing`). Never call the server key for a keyless non-admin; never log a key.
5. **No long dashes anywhere.** The em dash, the en dash, the horizontal bar and the minus sign
   are banned in this project: in the bot's messages, in prompts, copy and locales, in code,
   comments, docs and commit messages, and in your own replies to the owner in chat. Write a
   hyphen with spaces ( - ) instead; a numeric range is `20-40`. `plain_dashes` in
   `telegram/render.py` strips them from every outgoing message as the last line of defence, and
   `tests/test_no_long_dashes.py` fails the build if one is committed.

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
  core/clock.py   Clock/SystemClock/FakeClock, local_date/local_now/local_day_bounds,
                  coaching_day/day_rollover (the day ends at max(03:00, bed + 1 h), never past 06:00)
  db/models.py    every table (PLAN §3), StrEnum columns as VARCHAR, JSON→JSONB variant
  db/repo.py      all reads/writes; every user-owned query filters by user_id
  db/engine.py    make_engine / make_session_factory / init_sqlite_for_tests
  db/crypto.py    Fernet TokenCipher
  agent/client.py LLM (AsyncAnthropic) + FakeLLM; LLMResult; usage recording; LLMFactory (one
                  LLM per API key, LRU 64, for_user applies LLM_KEY_MODE + admin fallback) +
                  FakeLLMFactory; AnthropicKeyValidator (one models.retrieve on the pasted key)
  agent/usage.py  cost from the price table
  agent/tools/    registry (strict schemas, dispatch), schemas (one model per tool), handlers
  agent/prompts/  coach, onboarding, proactive, verify, summarize, import (→ PROMPTS.md)
  telegram/       messenger (Protocol + aiogram + Fake), copy + locales/ (20 languages), render (card),
                  keyboards, queue,
                  keys (sk-ant-… detection; the key handler itself is handlers.handle_key_message)
migrations/       alembic (async env); 0001_initial + 0002_user_llm_key match Base.metadata (tested)
tests/            conftest: sqlite engine, session, FakeClock, FakeLLM, FakeMessenger, seeded user
```

Every package is built: `nutrition/` (math, units, sanity rules, food resolution: cache → Open Food
Facts → USDA), `memory/` (day state, summaries, notes, retrieval, period parsing), `agent/` (context
assembly, turn loop, Reflexion verify, proactive decider, tools), `onboarding/` (checklist, importer),
`telegram/` (bot, handlers, commands, media, voice, day card), `proactive/` (scheduler, triggers,
ladder, engine), `integrations/` (WHOOP, Withings, Apple Health, OAuth links), `web/` (aiohttp
server: health, OAuth callbacks, webhooks, optional Telegram webhook), `app.py` (wiring).
`scripts/setup_telegram.py` sets the bot's name, descriptions, commands and avatar through the Bot API.

## Conventions (PLAN §14)

- Async everywhere; no blocking IO in handlers (PIL via `asyncio.to_thread`).
- Complete type hints, `from __future__ import annotations`, pydantic models for all tool IO.
- `structlog.get_logger()`; never `print`. Never log a secret.
- Time: store UTC, compute local with `zoneinfo` via `core/clock.py`; SQLite returns naive
  datetimes - normalise with `ensure_utc`. "What day is it" is `coaching_today`, never
  `local_date`: `log_meal` dates food by the coaching day, so anything that disagrees puts the
  card and the triggers on a day the food did not land on.
- Copy: model-written replies come out in the user's language on their own; code-rendered strings
  live in `telegram/locales/<code>.json`, one file per language, English as the fallback. The
  language is asked once on `/start` (button or free text) and stored on the user.
- Tool schemas: docstring = tool description, `Field(description=…)` on every field,
  `extra="forbid"`, no free `dict` fields, no numeric constraints (strict mode subset).
- Prompt caching: the coach prompt is static; the tool list is sorted and byte-stable; anything
  volatile goes at the end of the user message.
- Repo functions `flush`; callers `commit`.
- Tests: no network, SQLite only, every fixture in `tests/conftest.py`.

<!-- murmur:contract -->
Agent work in this repository follows `.murmur/contract.md`.
Read it before the first action, and branch from `main`, never commit to it.
Run the doctor command when anything about the setup looks wrong.

<!-- murmur:farm -->
## The farm

Agents for this repository run on a farm: a separate, always-on machine. The
`fleet` command starts and tracks them. On a laptop, `fleet` is a small script
that runs each command on the farm over ssh. This repository is the farm
project `strikt`.

**Check the farm before every spawn, in every session.** Run these four
commands in order, and stop at the first one that fails:

1. `command -v fleet` finds the command.
2. `fleet capacity` answers `OK` or `BLOCK`. Any other answer, or an ssh
   error, means this computer cannot reach the farm.
3. `fleet projects` lists `strikt`.
4. `fleet accounts pick` prints an account name. It exits 1 when no account
   has room.

If all four pass, start lanes on the farm with
`fleet spawn --project strikt ...`, as the orchestrate skill says. A
`BLOCK` means wait or spawn fewer, never `--force`.

If any check fails, do not spawn anything, and do not switch to local
subagents on your own. Tell Ilya which check failed, with its output and
the fix:

- no `fleet`: follow step 9 of murmur's INSTALL.md on this computer
  (https://github.com/magik-ai/murmur/blob/main/INSTALL.md), or put
  `~/.local/bin` on the `PATH` if the script is there;
- no answer from the farm: ssh to the farm must work with no prompt. This
  prints the ssh host the `fleet` script uses, for `ssh <host> true`:
  `sed -n 's/^exec ssh \(.* \)*\([^ ]*\) ".*"$/\2/p' ~/.local/bin/fleet`;
- no project:
  `fleet add-project --name strikt --repo magik-ai/strikt --branch main`;
- no account with room: wait until one has room.

Run lanes on this computer only when Ilya says so.
<!-- /murmur:farm -->
