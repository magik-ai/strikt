# Strikt — architecture plan and module contracts

This is the build spec. Every build agent reads this file first, then the research files in
`research/`, then the brief at `/root/.claude/uploads/8db7122f-c439-5341-8cf3-2b0544b86168/b739469c-coachbotbrief.md`.
The brief is the product law; this file is the engineering law. Where they disagree, the brief wins
and the agent says so in its report.

Product name: **Strikt** ("strict" with one letter changed: a homophone of strict in English, the real word for strict in German, Dutch and Scandinavian languages).
Repo on GitHub is still `magik-ai/bomiso` (placeholder name); the Python package is `strikt`.
Bot runtime model: `claude-sonnet-5`. Voice transcription: OpenAI (best model per research/09).

## 0. Non-negotiables

- One window, zero settings. No menus, no `/settings`. Everything by message. The only slash commands
  are `/start` (with optional invite code), `/today` (re-post the card), `/forget_me` (delete everything).
- Act, then confirm. Food arrives → log → show totals → offer buttons to correct.
- The number is the product. Every food message: per-item kcal/P/C/F(+fiber), day total, remaining, one
  line of advice at most.
- Infinite memory: the agent must never say it lacks context that exists in the DB.
- Proactive by default (intensity `pushy`), quiet hours 00:00–07:30, max 5 proactive sends per day,
  escalation ladder 1→4, reset on any user reply.
- Honest voice (brief §3.1, §7.4): fact first, no greeting, no emoji by default, one question max.
- Universal: nothing in the code hard-codes Ilya's numbers. His data comes in through onboarding/import.

## 1. Stack (pin exact versions from research/09-python-stack.md)

- Python 3.14 (Docker `python:3.14-slim`), `requires-python = ">=3.13"` so 3.13 runners also work.
- uv for dependency management (`uv.lock` committed), hatchling build backend, `src/` layout.
- aiogram 3.x (Telegram), anthropic 1.x (Claude), openai (transcription only), SQLAlchemy 2.x async +
  asyncpg, Alembic, pydantic v2 + pydantic-settings, APScheduler 3.x `AsyncIOScheduler`, aiohttp
  (web server for OAuth callbacks + webhooks; aiogram already depends on it), pillow + pillow-heif,
  structlog, cryptography (Fernet), httpx (food DB clients), python-dateutil / zoneinfo.
- Dev: pytest, pytest-asyncio, aiosqlite (unit tests run on SQLite; models must be portable: use
  `sa.JSON` with a `JSONB` variant on postgres, no postgres-only types in tests), mypy `--strict`,
  ruff (lint + format, line length 100), pre-commit config, GitHub Actions CI (ruff, mypy, pytest).
- Docker Compose: `bot`, `postgres:17`, optional `caddy` profile for HTTPS on a domain.

## 2. Repository layout

```
bomiso/                          (repo root; product Strikt)
  README.md  PROMPTS.md  RESEARCH.md  UX.md  DEMO.md  BRAND.md  CLAUDE.md  AGENTS.md  LICENSE
  pyproject.toml  uv.lock  alembic.ini  Dockerfile  docker-compose.yml  .env.example
  .github/workflows/ci.yml  .pre-commit-config.yaml  Makefile  .gitignore  .dockerignore
  Caddyfile
  brand/                         logo, avatar, wordmark, palette, brand sheet, card mockups (svg/png/html)
  migrations/env.py  migrations/versions/0001_initial.py
  scripts/build_prompts_md.py    concatenates src/strikt/agent/prompts/*.md into PROMPTS.md
  scripts/render_brand.mjs       renders brand PNGs with playwright
  src/strikt/
    __init__.py  __main__.py  app.py  config.py  logging.py  events.py  privacy.py
    core/types.py                Macros, FoodItem, DayTotals, DayState, Incoming/Outgoing message types
    db/engine.py  db/models.py  db/repo.py  db/crypto.py
    nutrition/math.py  sanity.py  units.py  resolve.py  off.py  usda.py
    memory/daystate.py  summaries.py  notes.py  retrieval.py
    agent/client.py  loop.py  context.py  verify.py  usage.py  proactive_decide.py
    agent/prompts/coach.md  onboarding.md  proactive.md  verify.md  summarize.md  import.md
    agent/tools/registry.py  schemas.py  food.py  training.py  body.py  state.py  profile.py  research.py  memory.py
    telegram/bot.py  handlers.py  media.py  voice.py  render.py  keyboards.py  daycard.py  queue.py  messenger.py
    proactive/scheduler.py  triggers.py  engine.py  ladder.py
    integrations/base.py  registry.py  whoop.py  withings.py  apple_health.py
    web/server.py
    onboarding/checklist.py  importer.py
  tests/  (conftest.py with sqlite engine + FakeLLM + FakeMessenger; one test module per package)
```

## 3. Data model (`db/models.py`, SQLAlchemy 2 typed, `Mapped[...]`)

All tables carry `user_id` (except `users`, `invites`, `foods`) and every query filters by it.
Timestamps are UTC `DateTime(timezone=True)`; "local date" columns are `Date` computed in the user's
timezone. IDs are integer autoincrement (`BigInteger` with sqlite variant `Integer`).

- `users`: id, telegram_id (unique), chat_id, status enum(invited|onboarding|active|paused|deleted),
  language (bcp47, default from Telegram), timezone (IANA), created_at, last_seen_at, invite_code.
- `profiles` (1:1 user): name, city, country, height_cm, birth_year, sex, goal_text, primary_kpi
  enum(waist|weight|bodyfat|performance), kpi_target_low, kpi_target_high, kpi_unit, wake_time (time),
  bed_time (time), work_pattern text, training_plan JSON ({days:[..], sessions_per_week:int, kinds:[..]}),
  meal_sources JSON, wearable text, likes JSON list, dislikes JSON list, allergies JSON, dietary_rules JSON,
  alcohol text, sweet_tooth text, comfort_food text, health_context text, medications text,
  coaching_intensity enum(gentle|direct|pushy|drill_sergeant) default pushy, explanation_level
  enum(short|full) default short, proactive_enabled bool default true, quiet_start time 00:00,
  quiet_end time 07:30, checkin_times JSON, temp_intensity, temp_intensity_until, onboarding_step int,
  onboarding_done_at, waist_cadence_days 14, weight_cadence_days 7, updated_at.
- `protocols`: id, user_id, version, kcal, protein_g, fat_g, carbs_g, fiber_g, rationale, active bool,
  created_at. Exactly one active per user.
- `days`: id, user_id, date (local), opened_at, closed_at, plan JSON (the morning commitment), verdict
  text, card_message_id, flags JSON (salty|alcohol|travel|sick|planned_indulgence|off), notes text.
  Unique (user_id, date).
- `meals`: id, user_id, day_date, slot enum(breakfast|lunch|dinner|snack|unknown), logged_at,
  eaten_at, source enum(photo|text|voice|imported|planned|forwarded), raw_ref JSON, note, deleted_at.
- `meal_items`: id, meal_id, user_id, name, brand, restaurant, quantity, unit, grams, kcal, protein_g,
  carbs_g, fat_g, fiber_g, sodium_mg, alcohol_g, confidence float, source enum(label|off|usda|web|model|user),
  source_url, countable bool, model_estimate JSON, user_correction JSON, flags JSON (sanity flags),
  position int.
- `foods` (shared cache): id, key (normalized name+brand+restaurant, unique), name, brand, restaurant,
  barcode, per_100g JSON, serving_g, serving_desc, source, source_url, confidence, fetched_at.
- `workouts`: id, user_id, source enum(whoop|screenshot|manual|apple_health|other), external_id,
  sport, started_at, ended_at, duration_min, strain, kcal, avg_hr, max_hr, zones_min JSON (z0..z5),
  distance_m, raw JSON, note, created_at. Unique (user_id, source, external_id).
- `sleep`: id, user_id, source, external_id, started_at, ended_at, in_bed_min, asleep_min,
  performance_pct, stages_min JSON, respiratory_rate, disturbances, raw, created_at.
- `recoveries`: id, user_id, source, external_id, date, score, rhr, hrv_ms, spo2, skin_temp_c, raw.
- `measurements`: id, user_id, type enum(weight|waist|bodyfat|bp_sys|bp_dia|steps|rhr|hrv|other),
  value float, unit, measured_at, source, raw JSON, note.
- `labs`: id, user_id, taken_at (date), marker, value, unit, ref_low, ref_high, flag, source, raw_ref.
- `notes` (coach observations): id, user_id, kind enum(preference|pattern|health|rule|event|answer|
  commitment), text, confidence float, source_turn_id, created_at, expires_at, last_confirmed_at,
  superseded_by (fk notes.id), active bool.
- `reminders`: id, user_id, due_at, text, kind, status enum(pending|sent|cancelled), created_at.
- `conversation_turns`: id, user_id, role enum(user|assistant), content JSON (Anthropic content
  blocks, images replaced by a `{"type":"text","text":"[image: <hash>]"}` stub after the turn is
  processed), text (plain, for search), telegram_message_id, created_at, input_tokens, output_tokens,
  cache_read_tokens, cache_write_tokens.
- `summaries`: id, user_id, kind enum(day|week), period_start, period_end, text, data JSON, created_at,
  updated_at. Unique (user_id, kind, period_start).
- `integrations`: id, user_id, provider enum(whoop|withings|apple_health), external_user_id,
  access_token_enc, refresh_token_enc, expires_at, scopes, status, last_sync_at, webhook_token,
  created_at. Unique (user_id, provider).
- `proactive_sends`: id, user_id, trigger, window_key, step int, sent_at, telegram_message_id, text,
  responded_at, response_turn_id.
- `token_usage`: id, user_id, date, model, purpose enum(turn|verify|proactive|summary|research|
  transcribe), calls, input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, cost_usd.
- `invites`: code (pk), created_by, created_at, used_by, used_at.
- `oauth_states`: state (pk), user_id, provider, created_at (10-minute validity).

## 4. Core types (`core/types.py`, pydantic v2)

```python
class Macros(BaseModel): kcal: float; protein_g: float; carbs_g: float; fat_g: float; fiber_g: float = 0; sodium_mg: float | None = None; alcohol_g: float = 0
class FoodItemIn(BaseModel): name; quantity: float | None; unit: str | None; grams: float | None; macros: Macros; confidence: float; source: Literal[...]; source_url: str | None; countable: bool; brand/restaurant optional
class DayTotals(BaseModel): macros: Macros; items: int; meals: int
class Remaining(BaseModel): kcal, protein_g, carbs_g, fat_g, fiber_g (target minus totals, may be negative)
class DayState(BaseModel): date; totals; targets(Macros); remaining; meals: list[MealView]; workouts: list[WorkoutView]; sleep: SleepView | None; recovery: RecoveryView | None; measurements_due: list[str]; closed: bool; flags: list[str]; plan: dict | None
class Attachment(BaseModel): kind: Literal[image|document|voice|link]; file_id; mime; bytes_b64 | None; text | None (transcript / fetched text)
class Incoming(BaseModel): user_id; chat_id; message_id; text: str | None; attachments: list[Attachment]; forwarded_from: str | None; received_at
class Outgoing(BaseModel): text (HTML); keyboard: list[list[Button]] | None; reply_to: int | None
```

## 5. Nutrition engine (`nutrition/`)

- `math.py`: `kcal_from_macros(p, c, f, alcohol=0)` (4/4/9/7), `scale(per_100g, grams)`, `sum_macros`,
  `per_serving(label, serving_g)`, `mismatch_ratio(stated_kcal, computed)`.
- `sanity.py`: pure functions returning `list[Flag]` (`Flag(code, severity, message, corrected: Macros | None)`):
  - `kcal_mismatch` when |stated − 4/4/9| > 10% → corrected kcal from macros.
  - `implausible_fiber` (eggs/meat/dairy claiming fiber; any single dish > 20 g unless legumes/bran).
  - `implausible_fat` (avocado/nut/oil dishes claiming < what the ingredient alone carries; a table of
    minimum fat per named ingredient).
  - `loose_under_report`: pasta/rice/noodles/sauce/soup/curry/salad-with-dressing → `countable=False`,
    apply +25% kcal/carb buffer (configurable 20–40%) and say why.
  - `vegetable_fat`: a vegetable side with ≥ 6 g fat → "roasted in oil" note.
  - `sodium_high`: ≥ 600 mg per serving or ≥ 1.5 g/100 g flagged; processed meat + CV-risk note if the
    profile's health_context mentions lipids/cardio (the flag carries a `needs_health_context` marker;
    the agent decides whether to mention it).
  - `portion_implausible`: pasta portion claiming < 40 g carbs, etc.
  - Test fixtures must include the brief's cases: chicken-avocado plate at 7 g fat; egg-and-toast at
    15 g fiber; large pasta at 26 g carbs; brussels sprouts at 9 g fat; lentil soup mix at 3.4 g
    sodium/100 g; smoked turkey 560 mg/100 g.
- `units.py`: gram conversions (oz, lb, cup approximations table, "1 egg" = 50 g, etc.).
- `resolve.py`: `resolve_food(query, brand, restaurant, barcode) -> FoodHit | None` order: foods
  cache → Open Food Facts (barcode) → USDA (generic) → None (agent then uses web_research or its own
  estimate and stores the result to `foods` with source=web|model). Clients in `off.py`, `usda.py`
  (httpx, timeouts 6 s, graceful failure returns None; never raises into the agent).

## 6. Agent (`agent/`)

### 6.1 LLM wrapper (`client.py`)
`class LLM` wrapping `AsyncAnthropic`: `async def message(*, purpose, system, messages, tools, max_tokens, effort, output_schema=None, user_id) -> LLMResult` records usage into `token_usage` (cost from a price table in config), handles `refusal` stop reason, retries via SDK defaults. `FakeLLM` in tests with scripted responses. Model id from `settings.model` (default `claude-sonnet-5`), `thinking={"type":"adaptive"}`, `output_config={"effort": ...}`, default effort `medium` for turns, `low` for verify/proactive/summaries.

### 6.1a Bring-your-own-key (added 2026-09-03)
Every model call for a user is billed to that user's own Anthropic key. `users` gains
`llm_key_enc` (Fernet ciphertext), `llm_key_last4`, `llm_key_set_at` (migration 0002); repo:
`set_llm_key` / `clear_llm_key` / `get_llm_key`. `LLMFactory(settings, recorder, cipher)` holds one
`LLM` per key (LRU 64 by sha256) and `for_user(session, user)` applies `LLM_KEY_MODE`: `user`
(default) → the user's key, else the server key only for `ADMIN_TELEGRAM_IDS`, else `None`;
`server` → the server key for everyone. `TurnDeps.llm` / `services["llm"]`, `LLMDecider`,
`ProactiveEngine` (skips keyless users, reason `llm_key_missing`) and the nightly summary resolve
per user. The chat handles a pasted `sk-ant-…` before anything else: `AnthropicKeyValidator` (one
`models.retrieve`, 10 s), store encrypted, delete the message, code-rendered copy (`key.*` in
`telegram/copy.py`); never a conversation turn, never logged (`logging.py` masks key-like strings).
Why: the operator must not pay for other users' model calls; the brief's "one window" stands — the
key is a message, not a settings screen.

### 6.2 Context assembly (`context.py`) — the infinite-memory contract
Order (for caching: stable → volatile):
1. `system[0]` = coach prompt (static text from `prompts/coach.md`), `cache_control {ephemeral, ttl 1h}`.
2. `system[1]` = profile block: profile + active protocol + active notes (kind/text), rendered
   deterministically (sorted keys, no timestamps), `cache_control {ephemeral}`. When onboarding is not
   done, `prompts/onboarding.md` is appended here with the checklist state.
3. `messages` = last N turns (N=30 or ≤ 40k tokens, whichever first) from `conversation_turns`,
   verbatim content blocks; then the current user message whose content starts with a text block
   `<context>` containing: local now, today's DayState (rendered compact), yesterday's close line,
   the rolling week summary, retrieved history rows (from `retrieval.search(text)` when the message
   looks like a question about the past), pending reminders, and the escalation state; followed by the
   user's text and image blocks. Top-level automatic `cache_control` for the conversation tail.
4. Tools: `registry.definitions()` sorted by name, `strict: True`, byte-stable.
Token budget per turn ≤ ~60k input. Never dump whole tables.

### 6.3 Turn loop (`loop.py`)
`async def run_turn(ctx: TurnContext, incoming: Incoming) -> list[Outgoing]`:
- persist the user turn; build context; loop `messages.create` until `end_turn`; execute tool calls
  through the registry (parallel calls executed concurrently, results returned in one user message);
  `is_error` results on exceptions; max 12 tool rounds.
- Reflexion verify (`verify.py`): if any tool of {log_meal, update_meal, delete_meal, get_day_state}
  ran, or the user asked to recalculate, re-derive `DayState` from the DB and compare with the numbers
  in the draft reply (regex over "kcal", "P", "protein", totals). On mismatch: one extra call with
  `prompts/verify.md` ("the log says X, your text says Y, rewrite the numbers") using effort low.
  Also cross-check each new meal_item with `sanity.check()`; flags are shown to the model as tool
  output so the reply names them.
- After the reply: update the Today card (`daycard.refresh`), persist the assistant turn (content with
  images stubbed), write notes the model asked for via `write_note`.
- Errors: LLM failure → reply "Claude недоступен, повторю через минуту" style in user language; tool
  failures → honest line ("couldn't verify, estimating from ingredients").

### 6.4 Tools (`tools/`), each a pydantic input model in `schemas.py` and a handler
`parse_food_image` is NOT a tool: images go into the model's context directly (vision). Tools:
- `search_food(name, brand?, restaurant?, barcode?) -> hits with per-100g macros + source url`
- `log_meal(items[], slot?, eaten_at?, note?) -> meal id, per-item macros after sanity, day totals, remaining`
- `update_meal(meal_id?, item_id?, changes) / delete_meal(meal_id) / undo_last()`
- `log_workout(sport, started_at, ended_at?, duration_min?, strain?, kcal?, avg_hr?, max_hr?, zones_min?, source, note?)`
- `log_sleep(started_at, ended_at, performance_pct?, ...)`, `log_measurement(type, value, unit, measured_at?)`
- `ingest_lab_report(markers[])` (the model reads the image; the tool stores structured rows)
- `get_day_state(date)`, `get_history(query: structured: kinds[], date_from, date_to, text?, limit)` +
  `search_history(text)` (FTS over turns, notes, summaries, meal item names)
- `update_profile(fields)`, `update_protocol(kcal, protein_g, fat_g, carbs_g, fiber_g, rationale)`
- `set_reminder(when, text)`, `cancel_reminder(id)`
- `write_note(kind, text, confidence, expires_at?, supersedes_id?)`, `retire_note(id)`
- `set_day_flag(date, flag, on)` (salty/alcohol/travel/sick/planned_indulgence), `set_day_plan(date, plan)`
- `close_day(date, verdict)` → writes day summary + verdict + bed line, triggers weekly summary update
- `web_research(query, urls?)` → separate LLM call with `web_search_20260209` (+ `web_fetch_20260209`)
  server tools, returns {answer, sources}; degraded → {error}. Keeps the main tool set stable.
- `render_day_card()` → returns the card text (the same renderer the pinned card uses)
- `connect_integration(provider)` → returns the OAuth link / Apple Health webhook instructions
- `set_coaching_intensity(level, until?)`, `finish_onboarding()`, `import_history(text)`
- `delete_everything()` is NOT a tool; it is the `/forget_me` command with a confirm button.

### 6.5 Proactive decision (`proactive_decide.py`)
`async def decide(user, trigger: TriggerFire) -> ProactiveDecision(send: bool, text: str, step: int)`.
Short prompt (`prompts/proactive.md`) + profile block + DayState + last 3 days' summaries + relevant
notes + the ladder state + response-rate stats; structured output {send, text}. Effort low.
The text is always model-written; never a template.

### 6.6 Summaries (`memory/summaries.py`)
`write_day_summary(user, date)` (on close_day and nightly 03:00 local for unclosed days),
`update_week_summary(user, week_start)`; prompt `prompts/summarize.md`; output is text + data JSON
(totals, adherence, patterns[], flagged[], user_said[]).

## 7. Telegram (`telegram/`)

- aiogram 3 `Dispatcher` with a single router; handlers for `/start`, `/today`, `/forget_me`,
  text, photo, document (image/*, application/pdf), voice/audio, media groups (debounce 1.2 s by
  `media_group_id`), callback queries; forwarded messages keep origin; links in text are fetched by
  the agent via `web_research`.
- `media.py`: download via bot API (≤ 20 MB), HEIC/HEIF → JPEG (pillow-heif), resize to ≤ 1568 px long
  edge, JPEG q85, sha256 hash, base64; PDFs passed as document blocks (≤ 32 MB, ≤ 100 pages).
- `voice.py`: `Transcriber` protocol; `OpenAITranscriber` (model from research/09; OGG/Opus accepted
  directly) and `NullTranscriber` (asks for text). Language hint from the profile.
- `queue.py`: per-chat `asyncio.Lock` + FIFO; a message that arrives during a run waits (never
  dropped); typing action every 4 s while running.
- `render.py`: HTML parse mode, escaping helper, number formatting (thin spaces), message splitting at
  4096, the card renderer.
- `daycard.py`: one pinned message per day; `refresh(user)` edits in place (ignores "message is not
  modified"); re-posts and re-pins when the message is gone or older than today; `/today` re-posts.
- `keyboards.py`: inline keyboards only where they remove typing: slot picker (breakfast/lunch/dinner/
  snack) when slot unknown; `undo`; `recalculate`; `close day`; `yes/no` for confirmations; callback
  data `s:<meal_id>:<slot>`, `undo:<meal_id>`, `recalc`, `close`, `forget:yes`.
- `messenger.py`: `Messenger` protocol (send, edit, pin, delete, chat_action) with an aiogram
  implementation and a `FakeMessenger` for tests.

## 8. Proactive engine (`proactive/`)

- `scheduler.py`: `AsyncIOScheduler`; `reschedule_user(user)` computes jobs from the profile in the
  user's timezone: `morning_line` (wake+0:15), `no_first_meal` (wake+3h), `no_lunch` (15:00),
  `fiber_check` (13:30), `protein_check` (18:00), `no_dinner` (21:00), `day_not_closed` (23:00),
  `bedtime_minus_30` (bed−0:30), `weekend_risk` (Fri 17:00), `weekly_review` (Sun 20:00),
  `measurement_overdue` (daily 08:05), `silence_check` (daily 12:00), `nightly_summary` (03:00),
  `integration_sync` (every 30 min). Jobs are recomputed on profile change.
- `triggers.py`: each trigger = precondition over the DB (pure, testable) → `TriggerFire | None`.
  Class A time-based, B data-based (from the event bus), C pattern-based (from summaries/notes).
- `ladder.py`: escalation state per `(user, window_key)` from `proactive_sends`; step = previous
  unanswered step + 1 (max 4); reset when the user replies (the turn loop marks `responded_at` on the
  latest send); follow-up delay 45 min; daily cap 5 (drill_sergeant: 8); quiet hours enforced except
  `bedtime_minus_30`; three clean days → cooldown flag; response-rate stats per trigger.
- `engine.py`: `fire(user, trigger)` → ladder → `decide` → send via Messenger → log `proactive_sends`
  → schedule follow-up. Idempotent per window_key. Event handlers: `whoop.workout` → analysis message
  (compare with last same-sport session + 30-day average); `whoop.recovery` low/high; `scale.weight` →
  log + 7-day trend comment (no single-reading comments; "that's water" after salty/alcohol flag);
  `whoop.sleep` → sleep onset vs bedtime; `sleep_debt` (3 nights under target) → intervention.

## 9. Integrations (`integrations/`)

- `base.py`: `class Integration(Protocol)`: `provider`, `auth_url(user) -> str`,
  `handle_callback(code, state) -> None`, `sync(user, since) -> list[Event]`, `handle_webhook(request) -> list[Event]`.
  Events: `WorkoutEvent`, `SleepEvent`, `RecoveryEvent`, `MeasurementEvent` (common schema from
  research/05). Tokens encrypted with Fernet (`db/crypto.py`).
- `whoop.py`: OAuth2 (URLs/scopes from research/04), v2 endpoints, pagination, webhook signature
  verification (HMAC-SHA256 base64 over timestamp+body), token refresh, kJ→kcal, zone_durations →
  minutes, dedupe by external id.
- `withings.py`: OAuth2, `getmeas` decoding `value × 10^unit`, notify subscribe, webhook.
- `apple_health.py`: `POST /webhooks/apple-health/<webhook_token>` accepting the JSON shapes from
  research/05 (Shortcuts and Health Auto Export); maps to measurements/sleep/workouts.
- `web/server.py`: aiohttp app: `GET /health`, `GET /oauth/<provider>/start?u=<signed>`, `GET
  /oauth/<provider>/callback`, `POST /webhooks/<provider>[/<token>]`, optional `POST /telegram`
  webhook. Runs in the same process as the bot (one container).

## 10. Onboarding and import

- Onboarding is a conversation driven by `prompts/onboarding.md` with the checklist in
  `onboarding/checklist.py` (the 10 steps from brief §4, each with the profile fields it fills).
  The system prompt shows which steps are done; `update_profile` marks steps; `finish_onboarding`
  requires the minimum set (name, timezone, height, weight, goal, kpi, wake/bed, protocol chosen).
  Resumable: any message continues from the first incomplete step. WHOOP connect offered at step 5.
- `import_history(text)`: the model calls it with structured rows it extracted from pasted summaries
  (`prompts/import.md` explains the shapes); `importer.py` writes meals/workouts/measurements/notes
  with `source=imported` and returns counts.

## 11. Security and privacy

- Invite-only: `ALLOWED_TELEGRAM_IDS` (comma list) and `invites` table; `/start <code>` consumes a
  code; anyone else gets one line and is ignored. Admin ids (`ADMIN_TELEGRAM_IDS`) can `/invite`
  to mint a code (the only admin command).
- Fernet key `TOKEN_ENCRYPTION_KEY` for integration tokens; webhook tokens random 32 bytes;
  OAuth `state` signed and single-use.
- `/forget_me` → confirm button → hard-delete every row for the user in one transaction, log a
  count; Telegram pinned card unpinned.
- No secrets in logs; structlog JSON in prod, pretty in dev.

## 12. Quality bar

- `ruff check`, `ruff format --check`, `mypy --strict src`, `pytest` all green; CI runs them.
- Tests: nutrition math + every sanity fixture; day totals/remaining; card rendering; context
  assembly determinism (same inputs → identical bytes); ladder logic (steps, cap, quiet hours, reset);
  trigger preconditions; WHOOP signature verification + payload mapping; Withings decoding; Apple
  Health payload mapping; tool schemas serialize with `additionalProperties: false`; agent loop with
  FakeLLM (tool round trip, verify mismatch path); importer; privacy delete.
- No real network in tests.

## 13. Docs (deliverables)

- `README.md`: what it is, architecture diagram (mermaid), why these choices (with the Instinct/
  Reflexion adopt/reject list summarized), deploy in 15 minutes (docker compose), env vars table,
  connecting WHOOP / Withings / Apple Health, importing history, cost notes, security, dev loop.
- `PROMPTS.md`: generated from `agent/prompts/*.md` by `scripts/build_prompts_md.py` (test asserts sync).
- `RESEARCH.md`: consolidated from `research/*.md` — findings and decisions.
- `UX.md`: Today card spec, message templates (food reply, menu ranking, workout analysis, day close,
  proactive ladder examples), keyboard map, error copy.
- `DEMO.md`: transcript onboarding → first food photo → WHOOP sync → day close → next-day query.
- `BRAND.md`: the brand system. `CLAUDE.md` + `AGENTS.md`: how to work in the repo.

## 14. Conventions

- Async everywhere; no blocking IO in handlers (PIL work via `asyncio.to_thread`).
- Type hints complete; `from __future__ import annotations`; pydantic models for all tool IO.
- Logging via `structlog.get_logger()`; never `print`.
- Time: store UTC, compute local with `zoneinfo`; helpers in `core/clock.py` (`Clock` protocol with a
  `FakeClock` for tests).
- Copy: the bot's replies are model-written; code-rendered strings (card, errors, buttons) live in
  `telegram/copy.py` with `ru` and `en` variants keyed by the user's language.
