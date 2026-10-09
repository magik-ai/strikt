# AGENTS.md - for any agent working on Strikt

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
  `llm_factory.for_user(session, user)` (never a process-wide `LLM`); `None` means no key:
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
