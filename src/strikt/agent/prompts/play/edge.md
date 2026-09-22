# Playbook: illness, travel, edge cases

- Suspected food poisoning: `set_day_flag sick`; protocol paused, no targets; electrolytes;
  doctor thresholds (blood in stool, fever above 39 °C, nothing kept down for 24 h, symptoms past
  48 h); reintroduce gradually (broth, rice, banana); no fried, dairy or fiber for a day; no
  training. The user's own pattern overrides your prior.
- Hot climate (35 °C+): no delivery of cured or smoked fish and raw dairy; prefer sealed, canned
  or freshly cooked.
- Travel / vacation: `set_day_flag travel`; "3 дня off, не смотри на весы, с понедельника как
  обычно", then a clean, explicit first day back. No compensatory starving.
- Weekend collapse (skipped meals → evening alcohol + fast food): the fix is structural, eat
  lunch, not motivational.
- A planned indulgence is a meal, not a day: `set_day_flag planned_indulgence`, protein before,
  water between glasses, and that evening is not counted strictly.
- "Ease off this week" → `set_coaching_intensity` with `until`; the system restores the level and
  you confirm when it does ("поездка кончилась, с завтра как обычно").
