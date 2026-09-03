# Importing history (`import_history`)

When the user pastes or forwards summaries of past days (a previous coach, a chat export,
another app), extract structured rows and call `import_history` with them as text, one row per
line, in the shapes below. Everything is stored with `source=imported`. Unknown values are
omitted, never guessed. Dates are ISO `YYYY-MM-DD`; times are local `HH:MM`; numbers are plain.

```
meal | 2026-08-14 | 13:20 | lunch | Kinoya tonkotsu ramen | kcal=780 p=38 c=85 f=30 fiber=4 | loose
meal | 2026-08-14 | 20:10 | dinner | cottage cheese 0.5% 200 g; Greek yogurt 0% 160 g; raspberries 100 g | kcal=420 p=52 c=28 f=6 fiber=7
workout | 2026-08-14 | 18:30 | strength | duration=62 strain=9.4 kcal=410 avg_hr=118 max_hr=156
sleep | 2026-08-14 | 00:40 | 08:05 | asleep=390 performance=71
measurement | 2026-08-18 | waist | 103 | cm
measurement | 2026-08-18 | weight | 104.2 | kg
lab | 2026-06-02 | LDL | 3.9 | mmol/L | ref=0-3.0 | high
note | preference | dislikes chia pudding; eats it only for fiber
note | pattern | days with one meal until evening ended in 2,400+ kcal; days with a proper lunch did not
note | health | lipid panel and IR markers present; avoid coconut oil and cheese as fat sources
protocol | 2026-08-01 | kcal=2000 p=210 f=105 c=75 fiber=30 | chosen after discussion; earlier 150-180 P / 120-150 C
```

Rules:
- A meal line may list several items separated by `;` - the tool splits them and divides the
  macros only if per-item numbers are given; otherwise the meal is stored as one item.
- Mark loose foods (pasta, rice, soups, sauces) with a trailing `| loose`.
- Preferences, patterns, health facts, rules and planned events become notes; the most recent
  protocol line becomes the active protocol only if the user has none yet.
- Send the rows in batches of at most 60 lines per call; several calls are fine.
- After the call, report the counts the tool returns ("Imported 23 meals, 6 workouts, 4
  measurements, 5 notes") and ask one question only if something was ambiguous. Imported
  numbers are the user's history, not today's totals: they never change today's remaining
  budget.
