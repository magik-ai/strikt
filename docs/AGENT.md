# The agent: how the turn is built, and why

`docs/PLAN.md` says what the modules are. This file says how the agent inside them is shaped, what
was measured, and which rule each decision comes from. It is engineering law like PLAN, and it
exists because the first shape was wrong in a way the owner felt every day: the bot lost the
thread, mixed up ids, argued with its own messages and gave no advice.

Sources this follows: Anthropic, [Effective context engineering for AI
agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents);
[Writing effective tools for AI
agents](https://www.anthropic.com/engineering/writing-tools-for-agents); [Effective harnesses for
long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents);
the [tool search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
docs (tool-selection accuracy falls off between 30 and 50 tools).

## What was measured (2026-09-22, before the change)

One ordinary turn, empty day, one line of user text:

| Block | Tokens | Share |
| --- | --- | --- |
| tool schemas (28 tools) | 9 793 | 65 % |
| coach system prompt | 5 109 | 34 % |
| profile block | 149 | 1 % |
| the user's day and message | 98 | 0.6 % |
| **total** | **15 149** | |

The agent's attention budget was spent almost entirely on rules and schemas, and 0.6 % of it on
the user. `update_profile` alone cost 1 644 tokens on every turn and is used about once a week.
Every tool also shipped its description twice (once as the tool description, once as the schema
root's, which pydantic copies from the docstring) and a `"default": null` on each of its hundred
optional fields. Both are gone: the full catalogue is 8 078 tokens now, the daily loop 4 262.

## The five decisions

### 1. Two tool tiers, not one catalogue

A turn starts with the daily loop (`agent/tools/__init__.CORE_TOOL_NAMES`: food, the day,
training, history, research, notes) plus `load_tools`. Everything cold (profile, protocol,
reminders, flags, plans, weight, labs, integrations, keys, intensity, onboarding, import) is one
`load_tools` call away; the turn loop then re-sends the same turn with the full catalogue.

- 12 tools and 4 262 tokens on an ordinary turn instead of 28 and 9 793.
- Two stable tool sets, so two cache entries, not a new prefix per turn: the core set is
  byte-identical on every ordinary turn.
- While onboarding is unfinished, the checklist tools join the core set, because then they *are*
  the daily loop.

Server-side tool search (`tool_search_tool_bm25_20251119`) would do this for us, but it is not
available on `claude-sonnet-5`, which is the model the brief runs on. `load_tools` is the same
idea done client side, and it costs one round trip only on the turns that need a cold tool.

### 2. The turn's job is decided before a tool is chosen

The coach prompt opens with three kinds of message: a report of what was eaten (log it), a
question (answer it, log nothing), a fact about the user or the day (store it). A question logged
as a meal is the single most annoying failure this product has, and it came from a prompt whose
first instruction was "intent clear, act". Ambiguity resolves toward answering, not logging: a
wrong guess toward logging costs a correction, a wrong guess toward asking costs one word.

Paired with it: every reply carries the advice. A logging reply ends with the one thing that
changes the rest of the day; a question gets a recommendation with a number and a name, never a
counter-question.

### 3. Playbooks instead of a bigger prompt

Sleep tactics, the scale and labs, illness and travel are not part of a food day. They live in
`agent/prompts/play/*.md` and enter the turn's context block only when the message is about them
(`context.playbooks_for`, keyword matched in ru and en, plus the day's `sick` / `travel` flags).
Just-in-time context beats a prompt that carries every domain on every turn. Photo triage stays in
the core prompt, because a photo carries no keywords.

### 4. The coach can see what the coach sent

Proactive check-ins are written to `conversation_turns` as assistant rows, not only to
`proactive_sends`. Before that, an answered check-in vanished from the model's world and the coach
told the owner it had never written the message on his screen.

### 5. Ids are read, never remembered

The day block carries `meal#<id>` and `item#<id>`. `update_meal` / `delete_meal` take
`expect_name`, required for a row older than yesterday, and a name that does not match the row
fails the call. An id carried over from an earlier day used to rewrite a closed day silently.

## Standing rules for anyone changing this

1. **Measure the turn before and after.** `context_built` logs `tools`, `tool_count`,
   `system_static`, `context`, `history`, `total`. A change that grows the fixed part needs a
   reason in the commit message.
2. **A new tool needs a tier.** Add it to `CORE_TOOL_NAMES` only if an ordinary day needs it;
   otherwise it is discovered through `load_tools`, and its description carries the words a user
   would use, because that is what the model matches against.
3. **No two tools with overlapping purposes.** If a human cannot say which of two tools applies,
   the model cannot either. Prefer one tool with a clear argument over two near-synonyms.
4. **Tool results are the reply's ground truth**, and a result that hides a hole (fibre silently
   0, an id that points elsewhere) is worse than an error: say the hole in the result.
5. **The system prompt is hot content only.** A rule that applies on fewer than one turn in ten
   belongs in a playbook.
6. **Nothing volatile in `system`.** `system[0]` (coach) and `system[1]` (profile block) must
   render the same bytes for the same inputs; everything that changes per turn goes in the
   `<context>` block of the user message.

## Proving it: the eval

`evals/` holds two flows. The turn eval (21 cases) grades what ends up in the database: which
tools ran, what was written, what the reply says, plus the invariant that started this work - a
turn may not change a row from an earlier day. The proactive eval (8 cases) grades the check-ins
the bot sends first: silence on a covered day, no clock opening, one thing per message, and no
fact the decider was never given. `make eval`, `make eval-judge`, `make eval-proactive`, all
against a real model (`ANTHROPIC_API_KEY`).

Numbers in this file that describe the *request* (tokens, tool counts) come from
`context_built`; anything about the *answers* has to come from an eval run, and a claim without
one does not belong here. The set is small enough that a difference under about ten points is
noise: `--reps 3` before believing a small win.

## Known gaps, in the order they are worth fixing

- `log_meal`, `update_meal` and `log_workout` are still 931 / 698 / 666 tokens of schema, which
  is most of what an ordinary turn now carries.
- The proactive decider's per-trigger guidance moved out of its cached prompt (2 044 tokens for
  twenty-five triggers, of which a fire needs one) into `prompts/triggers.md`: the firing
  trigger's entry now travels with its facts. The prompt itself is 1 205 tokens.
- Photos and voice are not in the eval: the harness sends text only.
- `context_max_turns` went from 30 to 60 rows when the fixed part dropped to 9k tokens, because
  the window is where "he does not remember anything" comes from and proactive check-ins are rows
  in it now too. Nothing measures whether 60 is the right number.
