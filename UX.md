# Strikt — UX

One Telegram chat. No settings, no menus, no commands to learn. The user sends food, screenshots,
voice or text; the bot logs it, answers with the number, keeps one pinned card current, and writes
first when the user goes quiet. This document is the design of that surface: the Today card, the
message templates, the buttons and what each one does. Everything here is either rendered by code
(`telegram/render.py`, `telegram/copy.py`, `telegram/keyboards.py`) or written by the model under
`agent/prompts/coach.md` and `proactive.md`; where a string is code-rendered it is quoted verbatim.

## 1. The Today card

One message per local day, pinned, edited in place. Rendered by `render_day_card`; this is its
exact output for a day with two meals, one WHOOP run and last night's sleep (Telegram HTML, shown
as source; `<code>` rows render monospaced):

```
<b>Today · Thu 3 Sep</b>
<code>kcal    939 / 2 000   ▓▓▓▓░░░░</code>
<code>P        70 /   210g  ▓▓▓░░░░░</code>
<code>C        51 /    75g  ▓▓▓▓▓░░░</code>
<code>F        51 /   105g  ▓▓▓▓░░░░</code>
<code>fiber    10 /    30g  ▓▓▓░░░░░</code>
Left: 1 061 kcal · 140 P · 24 C · 54 F

<b>Meals</b>
• 09:10 breakfast — яйца ×3, авокадо ½ · 327
• 13:40 lunch — Chicken Shawarma Bo… · 612
<b>Training</b>: running · 45 min · strain 12.1 · 406 kcal · avg HR 130
<b>Sleep</b>: 6h10 · 78% · recovery 61%
```

The same day in Russian (the user's language decides; food names stay as logged):

```
<b>Сегодня · чт 3 сен</b>
<code>kcal    939 / 2 000   ▓▓▓▓░░░░</code>
<code>P        70 /   210g  ▓▓▓░░░░░</code>
<code>C        51 /    75g  ▓▓▓▓▓░░░</code>
<code>F        51 /   105g  ▓▓▓▓░░░░</code>
<code>fiber    10 /    30g  ▓▓▓░░░░░</code>
Осталось: 1 061 ккал · 140 Б · 54 Ж · 24 У

<b>Еда</b>
• 09:10 завтрак — яйца ×3, авокадо ½ · 327
• 13:40 обед — Chicken Shawarma Bo… · 612
<b>Тренировка</b>: running · 45 min · strain 12.1 · 406 kcal · avg HR 130
<b>Сон</b>: 6h10 · 78% · восстановление 61%
```

Before the first meal (sleep already synced from WHOOP overnight):

```
<b>Today · Thu 3 Sep</b>
<code>kcal      0 / 2 000   ░░░░░░░░</code>
<code>P         0 /   210g  ░░░░░░░░</code>
<code>C         0 /    75g  ░░░░░░░░</code>
<code>F         0 /   105g  ░░░░░░░░</code>
<code>fiber     0 /    30g  ░░░░░░░░</code>
Left: 2 000 kcal · 210 P · 75 C · 105 F

<b>Meals</b>
nothing logged yet
<b>Sleep</b>: 6h10 · 78% · recovery 61%
```

And after `close_day`:

```
<b>Today · Thu 3 Sep · closed</b>
<code>kcal  1 885 / 2 000   ▓▓▓▓▓▓▓▓</code>
<code>P       162 /   210g  ▓▓▓▓▓▓░░</code>
<code>C        77 /    75g  ▓▓▓▓▓▓▓▓</code>
<code>F       103 /   105g  ▓▓▓▓▓▓▓▓</code>
<code>fiber    17 /    30g  ▓▓▓▓▓░░░</code>

<b>Meals</b>
• 09:10 breakfast — яйца ×3, авокадо ½ · 327
• 13:40 lunch — Chicken Shawarma Bo… · 612
• 20:20 dinner — кофта, брюссельская капуста, творог 0.5% · 946
<b>Training</b>: running · 45 min · strain 12.1 · 406 kcal · avg HR 130
<b>Sleep</b>: 6h10 · 78% · recovery 61%
<b>Verdict</b>: Closed at 1,885 / 162 P / 17 fiber. Protein short by 48. Bed by 00:30.
```

### What each row means

| Row | Source | Reading |
|---|---|---|
| `Today · Thu 3 Sep` | the coaching day in the user's timezone: it rolls over at 03:00, or an hour after a bedtime later than 02:00 (never past 06:00), so a dinner logged at 00:10 lands on the evening's card | `· closed` is appended once the day is closed |
| `kcal`, `P`, `C`, `F`, `fiber` | day totals vs the active protocol | five fixed-width rows; the bar is 8 cells, full at or over target, empty when there is no protocol |
| `Left: …` / `Over by …` | protocol minus totals | `Left` while under; over target it becomes `Over by 115 kcal · P +28 · C -26 · F -6` — signed remaining per macro, `+` still under, `-` over; the line disappears when the day is closed (the verdict carries the shortfall) |
| `Meals` | logged meals, oldest first | `• time slot — up to 3 item names, +N · kcal`; at most 8 meals, then `+N more`; names cut at 20 characters |
| `Training` | today's workouts (WHOOP or logged) | sport · minutes · strain · kcal · avg HR, one per workout, `;`-separated |
| `Sleep` | last night's sleep and today's recovery | asleep `6h10` · performance `78%` · `recovery 61%` |
| `Due` | measurement cadence from the profile | `waist (16 d)` = 16 days since the last reading |
| `Flags` | `set_day_flag` | `salty`, `alcohol`, `travel`, `sick`, `planned_indulgence`, `off` |
| `Verdict` | `close_day` | the model's one-line verdict, only on a closed day |

While no protocol exists the card uses the server's fallback targets; with fallbacks disabled it
shows `no protocol yet — finish onboarding` instead of the budget. On a day flagged `sick` the
targets are zero: the bars are empty, there is no `Left` line and the card says
`targets paused — sick day` (`цели на паузе — день болезни`) under the title.

### Rules

- **One pinned message.** The card is sent silently (no notification), pinned, and its id stored on
  the `days` row. Posting a new card unpins the previous one and yesterday's, so exactly one card is
  pinned at any time.
- **Edited in place.** After every state-changing tool (`log_meal`, `update_meal`, `delete_meal`,
  `undo_last`, `log_workout`, `log_sleep`, `log_measurement`, `set_day_flag`, `set_day_plan`,
  `close_day`, `update_protocol`, `import_history`) the loop re-renders and edits the card of every
  day the tools touched (a meal logged at 00:10 edits the evening's card). An unchanged card is
  skipped locally; a card Telegram reports gone is re-posted and re-pinned.
- **Re-posted on a new day** (the first change of the day posts that day's card; a past day is
  edited only if it already has a card) **or on `/today`**, which always sends a fresh copy, pins it
  and unpins the old one.
- **Closed with the verdict line.** `close_day` marks the day closed; the title gets `· closed`,
  the `Left` line goes and `Verdict:` is appended. The brand mark
  does the same thing: four strokes for the meals, the red strike when the day is closed.
- **Under 1,000 characters.** Meals are dropped from the end until the card fits; the tests hold
  it under the limit with twelve meals.

### Why it reads in three seconds

Five monospaced rows in a fixed grid: the eye lands on the bars, not the digits. The one line
that matters most for the next decision — what is left — is prose directly under the bars, in the
sentence font. Meals are one line each with the kcal at the end. Nothing else competes: no
emoji, no headers beyond bold labels, no percentages except sleep. Thousands are separated by a
digit-width space inside the mono rows (so `2 000` keeps the bar aligned) and by a no-break space
in prose (`1 061`), as `BRAND.md §4` specifies.

## 2. Message templates

Model-written, in the user's language, under the coach prompt. English below with one Russian
example; the shapes are what the prompt requires and what `agent/verify.py` checks (the `Total` /
`Итого` line must match the database).

**Food reply** (photo, screenshot, label, text or voice → `log_meal` first, then this):

```
Chicken Shawarma Bowl (Krave) — 612 kcal / 50 P / 44 C / 26 F / 5 fiber
Fat 18 → 26: shawarma with garlic sauce is not 18 g. Rice is loose, +20 %.
Total: 939 kcal / 70 P / 51 C / 51 F / 10 fiber
Left: 1 061 kcal · 140 P · 24 C · 54 F · 20 fiber
Dinner has to carry 100 g protein. Kofta plus cottage cheese does it.
```

Per item one line; every sanity flag one line; `Total` (day so far); `Left` against the protocol;
at most one line of advice. Source named when it is not obvious: `label`, `menu page`, `estimate`.

**The same reply in Russian** (user writes Russian with English food names; the bot mirrors):

```
Chicken Shawarma Bowl (Krave) — 612 ккал / 50 Б / 44 У / 26 Ж / 5 клетчатка
Жир 18 → 26: шаурма с чесночным соусом не бывает на 18 г. Рис — сыпучее, +20 %.
Итого: 939 ккал / 70 Б / 51 У / 51 Ж / 10 клетчатка
Осталось: 1 061 ккал · 140 Б · 24 У · 54 Ж · 20 клетчатки
Ужин должен дать 100 г белка. Кофта плюс творог закрывают.
```

**Correction reply** (`update_meal`; the user's estimate wins when it is better):

```
Quarter, not the whole. Pizza 620 → 155 kcal / 24 P / 16 C / 5 F.
Total: 1 240 kcal / 118 P / 44 C / 61 F / 12 fiber
Left: 760 kcal · 92 P · 31 C · 44 F · 18 fiber
```

**Recalculate** (any "doesn't add up"): every item with its numbers, the line-by-line
sum, the 4/4/9 cross-check, the corrected total, the new `Left`. Never a reassurance.

**Menu ranking** (a menu or a cart being decided → no tool, no log):

```
pick — Grilled sea bass, greens · 410 kcal / 46 P / 8 C / 21 F · best protein per calorie here
okay — Chicken shawarma bowl · 612 / 50 / 44 / 26 · fine once the rice is halved
skip — Truffle mac & cheese · 890 / 28 / 78 / 52 · 52 g fat for 28 g protein
Ask for sauce on the side and double chicken on the bowl.
```

**Label parsing** (per 100 g → per serving → the user's portion; source `label`):

```
Lentil soup mix, 75 g dry — 262 kcal / 18 P / 43 C / 2 F / 11 fiber (label)
Sodium 2 550 mg per portion. Fine as an episode, not as a daily base.
Total: …
Left: …
```

**Workout analysis** (WHOOP webhook or screenshot → `log_workout`; density, not praise):

```
Run 18:10–18:55, 45 min, avg HR 130, 406 kcal, strain 12.1.
Last run Sat 29 Aug: 94 min, avg HR 104, 361 kcal. Today 9.0 kcal/min against 3.8 — twice the
work in half the time. Ended 5.5 h before bed; sleep is safe.
```

Heavy strength with low strain is never penalised; a session ending within two hours of bedtime
is flagged against sleep.

**Day close** (`close_day` with the verdict; the card gets the same verdict line):

```
Day closed: 1 885 / 2 000 kcal · P 162 / 210 · C 77 / 75 · F 103 / 105 · fiber 17 / 30.
Training: run, 45 min, 406 kcal.
Worked: three meals, lunch at 13:40 — no evening slide.
Fix tomorrow: protein. −48 g with the calories nearly spent means the fat ran high (103 g).
Kofta becomes sea bass or a plain steak.
Bed by 00:30. Laptop out of the room at 23:30.
```

**Morning line** (`morning_line`, wake + 15 min, one message):

```
Recovery 74 %. Slept 7h05, up at 08:02. Yesterday closed at 1 885 / 162 P.
Plan for today — breakfast, lunch, dinner: what and roughly when?
```

**The ladder** (unanswered nudges climb one step per 45 minutes; a reply resets to 1):

1. Prompt — `11:05. Nothing logged. Breakfast?`
2. Push — `13:10. Still nothing. The last three days you skipped until 14:00 ended at 2 610, 2 540
   and 2 720 kcal.`
3. Demand — `14:00. Eat something with 40 g protein in the next hour and send me a photo.`
4. Consequence — `Waist target is 94. You are at 103. Days like this cost a week each.`

**Weekly review** (Sunday 20:00; numbers, no stars):

```
Week 31 Aug – 6 Sep: avg 1 920 kcal · 178 P · 21 fiber · 3 sessions · bedtime hit 4 of 7 · waist
measured.
Pattern: the two days over 2 300 both had no lunch.
This week: lunch logged by 14:00 every day.
```

**Honest errors** (model-written, the same shape every time): `Couldn't verify — estimating from
ingredients. Correct me if you know better.` then the estimate. Never a verified-sounding number
that was not verified.

## 3. Buttons and keyboards

Three keyboards, and a reason for each. A button exists only where it removes typing that
conversation cannot: everything else is said in words, because this is a chat and not a control
panel. Callback data is at most 64 bytes.

| When shown | Buttons | Callback data | What happens | Model call |
|---|---|---|---|---|
| The first message a new user gets, before any language is known | every language in its own name, three to a row, Telegram's guess first | `lang:<code>` | the language is stored, then the welcome and the key walkthrough follow in it | no |
| Reply after a turn that logged a meal (`log_meal`) or, failing that, corrected one (`update_meal`); it targets that meal, not whatever meal is last. Nothing after `delete_meal` or `undo_last` alone | `Undo` | `undo:<meal_id>` | `undo_last` if it is still the latest meal, else `delete_meal`; card refreshed; toast `Undo` (a second tap: `Already removed`) | no |
| `/forget_me` | `Yes, delete everything` / `Cancel` | `forget:yes`, `forget:no` | Yes: card unpinned, every row deleted in one transaction, jobs removed, one line back. No: `Kept everything.` | no |

Russian labels: `Убрать` (not «Отменить»: the button takes a meal back) · `Да, удалить всё` /
`Отмена`.

What used to have buttons and now does not: the meal slot (the coach names the slot it guessed
and one word corrects it), recalculating, closing the day, and the yes/no confirmations the agent
asks for. The pinned card carries no buttons at all: it is a readout. Connect links (WHOOP,
Withings) are sent as plain text in the reply.

Malformed or foreign callback data, and any tap outside a private chat, is answered silently and
ignored - which is also what happens to a tap on a button from before this change. A tap while
the chat is still busy with an earlier message is acknowledged at once (Telegram expires an
unanswered tap in well under a minute) and the action runs in order afterwards; the card refresh
confirms it.

## 4. Slash commands

| Command | Who | What it does |
|---|---|---|
| `/start [code]` | anyone | Invite-only: allowed ids or a valid one-time code create the user; `Strikt. One window, no settings. Send food photos, screenshots, voice or text — I log, count and push.` then, as the second message, the key walkthrough (`key.needed`, section 5) — the interview's question 1 follows the moment the key is in. A user who already has a key (or an admin on the server key) gets question 1 right away. A returning user mid-interview gets `Back. Where we left off:` and continues. |
| `/today` | user | Re-posts the card, pins it, unpins the old one. |
| `/forget_me` | user | `Delete everything about you — profile, meals, training, notes, chat history, your API key? This cannot be undone.` with the two buttons; on yes: `Deleted {rows} rows, your API key included. Nothing about you remains. Send /start to begin again.` |
| `/invite` | admins only | Mints a one-time code: `Invite code: <code>`. Non-admins get nothing. |

Unknown users get one line — `This coach is invite-only. Ask the owner for a code and send
/start <code>.` — and nothing else. The bot profile registers `start`, `today`, `forget_me` in
English and Russian. Updates from groups and channels are dropped before any of this: the coach
works in a private chat only, so the pinned card, nudges and health data never land anywhere else.

## 5. Your Anthropic key

The coach runs on the user's own Anthropic API key (`LLM_KEY_MODE=user`, the default): every
model call for a user — turns, the verify rewrite, proactive nudges, summaries, `web_research` —
is billed to it. The key enters through the same window as everything else, and the code
handles it before the model sees a word.

**The walkthrough** (`key.needed`; code-rendered, plain text). Sent as the second message after a
new user's `/start`, and to any message from a user who has no key yet; `key.help` is the same
steps under the first line `The key, step by step:` when the message asks about the key (`key`,
`api`, `token`, `ключ`, `токен`, `anthropic`, `console`). No model call happens in either case;
photos are not downloaded, voice notes not transcribed.

```
This coach runs on your own Anthropic API key. The coach is free; Anthropic bills the key for what it uses.
1. Open console.anthropic.com. Sign in or create an account.
2. Billing → add credit. You pay only for what the coach uses.
3. Settings → API keys → Create key. Name it strikt, copy the key (it starts with sk-ant-).
4. Paste the key here as a message.
I check it, store it encrypted, delete your message and never show it again.
A new key replaces the old one. /forget_me deletes it with everything else.
```

```
Тренер работает на твоём ключе Anthropic. Сам тренер бесплатный; Anthropic списывает с ключа за то, что он потратил.
1. Открой console.anthropic.com. Войди или создай аккаунт.
2. Billing → пополни баланс. Платишь только за то, что тренер израсходовал.
3. Settings → API keys → Create key. Назови strikt, скопируй ключ (начинается с sk-ant-).
4. Вставь ключ сюда сообщением.
Я проверю его, сохраню в зашифрованном виде, удалю твоё сообщение и больше не покажу.
Новый ключ заменяет старый. /forget_me удаляет его вместе со всем остальным.
```

**The key message.** Any message whose text contains `sk-ant-` followed by twenty or more key
characters is a key message, handled before anything else and never stored as a conversation
turn. The bot checks the key with one call (`GET /v1/models/<model>` on a client built from that
key, 10 s, no retries), deletes the message that carried it, and answers:

| Outcome | Key | English | Russian |
|---|---|---|---|
| accepted, message deleted | `key.saved` | Key saved, ends in …{last4}. Your message with the key is deleted. | Ключ сохранён, заканчивается на …{last4}. Твоё сообщение с ключом удалено. |
| accepted, Telegram refused to delete | `key.saved_keep` | Key saved, ends in …{last4}. I could not delete your message with the key — delete it yourself. | Ключ сохранён, заканчивается на …{last4}. Удалить твоё сообщение с ключом не смог — удали его сам. |
| the check itself failed (network, 5xx): stored anyway, appended to the line above | `key.unchecked` | Anthropic did not answer the check; the key is checked on your next message. | Anthropic не ответил на проверку; ключ проверится на твоём следующем сообщении. |
| 401 / 403: nothing stored | `key.invalid` | Anthropic rejected this key. Usual causes: a key from another console account, or an account with no credit. Redo step 3 — console.anthropic.com → Settings → API keys → Create key — top up Billing if needed, and send the new key here. | Anthropic отклонил этот ключ. Обычные причины: ключ из другого аккаунта консоли или аккаунт без баланса. Повтори шаг 3 — console.anthropic.com → Settings → API keys → Create key — при необходимости пополни Billing и пришли новый ключ сюда. |
| the stored key is rejected later, mid-turn (no retry) | `key.rejected` | Anthropic rejected your key; nothing was sent. Create a new key at console.anthropic.com (Settings → API keys), check that Billing has credit, and paste it here. | Anthropic отклонил твой ключ; ничего не отправлено. Создай новый ключ на console.anthropic.com (Settings → API keys), проверь баланс в Billing и вставь его сюда. |

A user still in onboarding gets the interview's first question right after `key.saved`: the
key was the missing piece after `/start`. A new key replaces the old one. The stored key is
Fernet-encrypted; only its last four characters exist in clear, and no log line ever carries a
key (any `sk-ant-…` string is masked). Proactive nudges and the nightly summary skip a user
without a key. `/forget_me` deletes the key with everything else — the confirmation says so.

In `LLM_KEY_MODE=server` (a private deployment) the operator's key pays for everyone and none of
this is shown; in `user` mode an id in `ADMIN_TELEGRAM_IDS` falls back to the operator's key when
one is set, so the owner never has to paste theirs.

## 6. Corrections by text

No button is required for any correction; the prompt maps the phrasing to the tool:

| The user writes | Tool | Reply |
|---|---|---|
| "that was 150 g not 200", "actually I only ate a quarter", "I tore the top crust off" | `update_meal` (item id, new grams or macros) | the item's new line, `Total`, `Left` |
| "salad was 200 kcal not 90" | `update_meal` with the user's number | same, plus one line conceding the better estimate |
| "that was lunch", "log it as dinner" | `update_meal` (slot) | one line |
| "delete that", "remove the shake" | `delete_meal` | `Total`, `Left` |
| "undo" | `undo_last` | `Total`, `Left` |
| "recalculate", "that doesn't add up", «пересчитай», «не сходится» | `get_day_state` + full re-derivation | the work shown line by line |

Corrections re-run the sanity checks and refresh the card like any other change.

## 7. Error copy

Code-rendered, one line, honest. From `telegram/copy.py` unless noted.

| Key | English | Russian |
|---|---|---|
| `err.llm_down` | Claude is unavailable right now. Your message is saved — send the next one and I will pick both up. | Claude недоступен. Сообщение сохранено — напиши ещё раз, отвечу на оба. |
| `key.rejected` | Anthropic rejected your key; nothing was sent. Create a new key at console.anthropic.com (Settings → API keys), check that Billing has credit, and paste it here. | Anthropic отклонил твой ключ; ничего не отправлено. Создай новый ключ на console.anthropic.com (Settings → API keys), проверь баланс в Billing и вставь его сюда. |
| `err.tool_failed` | Couldn't verify — estimating from ingredients. Correct me if you know better. | Не смог проверить — считаю по ингредиентам. Поправь, если знаешь точнее. |
| `err.transcribe` | Voice transcription is off. Send text. | Распознавание голоса выключено. Напиши текстом. |
| `err.transcribe_failed` | Couldn't transcribe that. Send text or try again. | Не смог распознать голос. Напиши текстом или пришли ещё раз. |
| `err.media` | Couldn't read that file. Send a photo, PDF or text. | Не смог прочитать файл. Пришли фото, PDF или текст. |
| `err.too_large` | File too large (limit {mb} MB). | Файл слишком большой (лимит {mb} МБ). |
| `err.not_allowed` | This coach is invite-only. Ask the owner for a code and send /start <code>. | Доступ по приглашению. Возьми код у владельца и отправь /start <код>. |
| `err.invite_invalid` | That invite code is not valid. | Код приглашения не подходит. |
| `err.unknown` | Something broke on my side. Send that again. | У меня что-то сломалось. Отправь ещё раз. |
| `queue.busy` | Still on your previous message — answering in order. | Ещё обрабатываю предыдущее сообщение — отвечу по порядку. |
| refusal (`agent/loop.py`) | I can't help with that one. Send the next meal or ask something else. | С этим не помогу. Пришли следующий приём еды или спроси о другом. |
| round cap (`agent/loop.py`) | Too many steps for one message; what I logged so far stands. Send the rest again in smaller pieces. | Слишком много шагов на одно сообщение; записанное сохранено. Пришли остальное ещё раз, частями. |
| WHOOP denied (`integrations/whoop.py`) | WHOOP access was not granted. Send “connect WHOOP” again when you want to retry. | Доступ к WHOOP не выдан. Напиши «подключи WHOOP», когда захочешь повторить. |

A failed tool inside a turn is returned to the model as an error; the model then writes the
`err.tool_failed` shape itself and estimates. A message that arrives while the previous one is
still running is queued, never dropped.

## 8. Deliberately absent

- **No settings screen, no menu, no `/help`.** Protein target, wake time, check-in times, coaching
  intensity, quiet hours, integrations, even the API key — all of it is a sentence or a paste:
  "change protein to 180", "ease off this week, I'm travelling", "connect WHOOP", "remind me at 8
  about waist", a new `sk-ant-…` to replace the old key. Check-in times move
  the meal nudges (a time before 10:00 is the breakfast check, 10:00–16:59 lunch, 17:00–21:59
  dinner, later the close); a reminder you set is delivered at its time regardless of quiet hours
  or the daily cap.
- **No reply keyboards.** They cover the input field, and the input field is the product.
- **No confirmation before logging.** Food is logged first; the numbers and the `Undo` button are
  the confirmation.
- **No emoji, no exclamation marks, no praise** in anything the code renders, and the prompt bans
  them for the model unless the user uses them first.
- **No streak badges, stars or charts.** Numbers in a line, once, when relevant.
- **No greeting on proactive messages.** They open with the time and the fact.
- **No second pinned message.** The card is the only pin; everything else scrolls.
