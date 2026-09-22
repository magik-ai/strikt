# The evals

Two flows, both run against a real model: **the turn** (21 cases, `cases.json`) - one user message
in, one reply and its database writes out - and **the proactive check-in** (8 cases,
`proactive_cases.json`) - the messages the bot sends first, graded on the text, because the text
is all the user sees.

Everything in `docs/AGENT.md` before this was a measurement of the *request*: tokens, tool counts,
prompt size. None of it says whether the bot answers well. This does.

## Running it

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # the eval calls the real model; it costs real money
make eval                                # the turn flow, programmatic grading only
make eval-judge                          # also asks each case's rubric question (Haiku)
make eval-proactive                      # the check-ins the bot sends first
uv run python -m evals.run --only log,fibre        # one tag or a list of case ids
uv run python -m evals.run --variant v1 --reps 2   # a second variant, two runs per case
```

A pass writes `.claude/hillclimb/turn/<variant>/results.jsonl` (one row per case, with the tools
called, the reply, the failures and the token usage) and `traces/<id>_rep<k>.json` (the whole
conversation, including every tool call). Read the traces of the failures first; that is where the
next change comes from.

## What a case is

One entry in `cases.json`:

- `seed` - the day the turn starts from: profile, targets, meals (today or `days_ago`), earlier
  conversation rows, an open proactive check-in, day flags. Each case gets its own in-memory
  database, so cases never see each other.
- `message` - what the user sends.
- `expect` - what must be true afterwards. `must_call` / `must_not_call` / `must_call_any` name
  tools; `meals_added`, `items_added`, `rows_added`, `fiber_added_at_least`,
  `kcal_added_between`, `profile_equals`, `today_untouched` read the database; `reply_matches`,
  `reply_not_matches`, `reply_has_number`, `max_rounds` read the answer.
- `judge` - one rubric question for the things a check cannot see ("does the reply name a concrete
  dinner with an amount, rather than asking the user what they want?"). Only with `--judge`.

**Every case also carries one check nobody has to write: a turn may not change a row from an
earlier day.** That is the failure that started this work - a fibre correction landing on a
watermelon logged eleven days before.

A case passes only if every check passes. The headline number is cases passed, so a case is worth
adding when you can say exactly what "right" means for it.

## Where the cases came from

Most are the owner's own chat of 22 September 2026, including the ones the bot failed: psyllium
refused as "not really food", vegetables logged at 0 g fibre, a correction landing on another
day's row, a check-in the coach denied having sent, a question answered with a log. Their `note`
field says which. The rest are the ordinary turns that must keep working: a portion correction, a
deletion, a weight, a workout, a reminder, a sick day, an acknowledgement that must write nothing.

They are not sacred. If a case does not match what the product should do, change it in
`cases.json` and say why in the commit; an eval nobody argues with is an eval nobody reads.

## The proactive flow

`--flow proactive` seeds the same kind of day, hands the decider a trigger fire with the facts its
precondition would have produced, and grades the message: does it send at all (a covered day must
stay silent), does it open like a person rather than with a clock, is it at most two or three
lines with one question, does it avoid facts it was never given. The two failures from the real
chat are cases: the wake time the bot assumed and quoted, and the training session it invented in
a dinner nudge.

## What it does not cover

Photos and voice (the harness sends text only), onboarding, and anything requiring several turns
in a row. The judge is a single yes/no per case, not a quality score. With 21 cases, a difference
smaller than about ten points is noise: run `--reps 3` before believing a small win.
