# AGENTS.md — for any agent working on Strikt

Read in this order: this file → `CLAUDE.md` → the brief (product law) → `PLAN.md` (engineering
law) → the module you own. Keep changes inside your module's contract; if the contract must
change, change PLAN.md in the same commit and say why.

## Ground rules

- Run `make check` before you finish. Green means: ruff clean, ruff format clean, `mypy --strict
  src` clean, pytest green, PROMPTS.md in sync.
- Do not add dependencies without a reason written in pyproject.toml comments.
- Do not touch `migrations/versions/0001_initial.py`; add a new revision with `make revision`.
- Keep `Registry.definitions()` byte-stable: add tools only through `agent/tools/schemas.py`
  + `agent/tools/__init__.py`; the test suite asserts the exact PLAN §6.4 set.
- Every DB query filters by `user_id`. Every timestamp is UTC. Every local date is computed in the
  user's timezone via `core/clock.py`; a meal's date is its coaching day (`coaching_day`: rollover
  at max(03:00, bed + 1 h), never past 06:00), not the calendar date.
- The brief's voice rules are as important as the code. Prompts live in `agent/prompts/*.md`;
  regenerate `PROMPTS.md` with `make prompts`.
- Never say the bot "lacks context": if the DB has it, look it up.
- Every model call for a user is billed to that user's own Anthropic key: get the client from
  `llm_factory.for_user(session, user)` (never a process-wide `LLM`); `None` means no key —
  reply `key.needed` or skip with `llm_key_missing`. Never log a key; `logging.py` masks
  `sk-ant-…` strings as a last line of defence, not as permission.
- No real network in tests. Use `FakeLLM`, `FakeLLMFactory`, `FakeKeyValidator`,
  `FakeMessenger`, `FakeClock`.

## Module ownership

| Module | Owner |
|---|---|
| `app.py` main wiring | integration agent |
| `agent/tools/food.py` | nutrition agent (`nutrition/math.py`, `sanity.py`, `units.py`, `resolve.py`, `off.py`, `usda.py`) |
| `agent/tools/training.py` | training/integrations agent |
| `agent/tools/body.py` | body/labs agent |
| `agent/tools/state.py` | day-state/memory agent (`memory/daystate.py`, `summaries.py`) |
| `agent/tools/profile.py` | onboarding agent (`onboarding/checklist.py`, `importer.py`) |
| `agent/tools/research.py` | research agent (server-side web_search/web_fetch) |
| `agent/tools/memory.py` | memory agent (`memory/notes.py`, `retrieval.py`) |

## How to add a tool

1. Add the input model to `agent/tools/schemas.py` (docstring = description) and its name to
   `TOOL_NAMES` and `SCHEMAS`.
2. Implement `async def <name>(ctx: ToolContext, args: <Model>) -> ToolResult` in the owning
   module and wire it in `agent/tools/__init__.py`.
3. `make check`. `tests/test_registry.py` verifies strictness and completeness.

## Reporting

Finish with: what you built, the commands you ran and their status, deviations from PLAN.md or
the brief (and why), and anything you left unfinished.
