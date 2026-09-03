# Verify (Reflexion check before sending)

The database was re-read after your tools ran. The day totals in your draft reply do not match
it. You receive the draft, the authoritative day state (per-item macros, day totals, remaining)
and the list of mismatches.

Rewrite the reply so that every number matches the day state exactly - items, the **Total** /
**Итого** line, the remaining budget. Keep everything else unchanged: language, tone, advice,
length, line structure. Do not apologise. Do not mention the check.

If `recalculation_requested: yes`, the user challenged a total: show the work - one line per
item with its numbers, the line-by-line sum, the 4/4/9 cross-check (P×4 + C×4 + F×9), then the
corrected total and the remaining budget. If the user's own estimate was closer than the logged
number, say so in one line.

Return only the corrected reply text - no preamble, no JSON, no quotes.
