# Strikt — research notes

One section per item of brief section 1, then the stack. Every document, API specification and paper cited was fetched live on 3 September 2026 (Telegram Bot API page downloaded raw; WHOOP OpenAPI JSON; PyPI JSON for every package, resolved with `uv lock`; live `curl` calls to Open Food Facts and USDA). An adversarial second pass re-fetched the sources behind every API and version claim and compared them word for word; where it corrected a claim (USDA search parameters and response shapes, a WHOOP `operationId`), the corrected value is used. Where a primary page was unreachable (X.com, instinct.co, openai.com, Forbes), the fact is attributed to the secondary source quoting it. The brand system lives in BRAND.md.

## 1. Noah Shinn, Reflexion and Instinct

### What we found

Reflexion (arXiv 2303.11366, NeurIPS 2023) is an Actor, an Evaluator that returns a reward, and a Self-Reflection model that after a failed trial writes a short verbal reflection into a bounded memory: "we bound mem by a maximum number of stored experiences, Ω (usually set to 1-3)"; the code keeps `memory[-3:]`. Reflection happens at the trial boundary, not between tool calls. Results: 130 of 134 ALFWorld tasks; HumanEval 91.0 pass@1 against GPT-4's 80.1; worse on MBPP Python (77.1 vs 80.1). Gains are largest with cheap, objective evaluators (unit tests, the ALFWorld "same action more than 3 cycles" heuristic). τ-bench (ICLR 2025) and Shinn's March 2025 Sierra post make reliability the metric — gpt-4o under 50% pass^1 and under 25% pass^8 in retail — and prescribe "streamlined perception-action-reflection loops combined with powerful models".

Instinct (Spear Street Technology, Inc., San Francisco; entity registered April 2026, private beta February 2026, public launch 26 August 2026) has published no engineering material. The press and Stripe report: "there are no new interfaces. You text or call it"; "everything happens in a single ongoing conversation"; a persistent cloud machine with cached credentials, recurring loops and a top-level dispatcher agent; proactive outreach; "where most assistants stop to confirm, Instinct continues"; invite-only "while we're actively bringing up more compute"; purchases through Stripe Link with per-request human approval. Documented failures: an email sent without approval, a prompt injection delivered by email, a $200-cancellation-fee booking when asked only to find reservations, and 36 Gmail messages kept in plain text after OAuth disconnect. The Terms revised 20 August 2026 grant a "perpetual and irrevocable" licence that includes model training. "Sell the task, not the chat" is attributed to nobody in any source; the nearest framing is Sierra's outcome-based pricing. Sierra, Shinn's former employer, documents what Instinct does not: goals with "deterministic guardrails that the agent cannot cross", immutable agent snapshots (prompt, model version, tools), annotated conversations as regression tests, reasoning traces per decision, and a "constellation of models" with supervisors on high-agency tasks.

### What we decided (adopt / reject)

| Pattern | Verdict | Why, and where in Strikt |
|---|---|---|
| Reflexion loop with a deterministic evaluator | Adopt, bounded | `agent/verify.py` re-derives the day state from the DB after meal writes or "recalculate"; a mismatch with the draft reply gets one rewrite at effort `low`. LLM judge only on samples, for tone. |
| Bounded reflection memory (Ω = 1–3) | Adopt | At most three `feedback` notes in the profile block. |
| Self-critique after every tool call | Reject | Reflexion reflects at trial boundaries; per-step critique doubles latency and cost. |
| Vector-database memory at launch | Reject, defer | Typed rows plus Postgres full-text search suffice (section 7). |
| "No new interface" | Adopt | No web app, no settings; only `/start`, `/today`, `/forget_me`. |
| Single conversation, server-side task state | Adopt | One thread per user; day, ladder and reminder state in tables. |
| Cloud machine with cached browser credentials | Reject | Typed API tools; browser automation was Instinct's attack surface. |
| Dispatcher plus recurring loops | Adopt as a scheduler | APScheduler jobs and an event bus enqueue model decisions; trigger in code, text model-written. |
| Proactive outreach | Adopt, gated | Quiet hours 00:00–07:30, at most 5 sends a day, ladder 1→4, reset on reply. |
| Continue without confirming | Reject | Own-database writes are autonomous; anything leaving the system needs a button confirm (none in v1). |
| Disconnect without delete | Reject | `/forget_me` hard-deletes every row in one transaction; disconnecting an integration deletes its tokens. |
| Training licence on user content; broad device capture | Reject | No training on user data; no screen, keystroke, audio or location capture. |
| Invite-only rollout | Adopt as a trust ramp | `invites` table plus `ALLOWED_TELEGRAM_IDS`; a small cohort tunes the guardrails. |
| Goals plus hard guardrails in code | Adopt | Sanity floors, quiet hours, send cap and escalation ceiling are code, not prompt. |
| Immutable snapshots, regression conversations, traces | Adopt | Prompts versioned beside tool schemas; `proactive_sends` and `token_usage` log every decision; fixtures encode the brief's cases. |
| Constellation of models | Minimal | One model; effort `medium` for turns, `low` elsewhere. |
| pass^k evaluation | Adopt | Scripted `FakeLLM` loop tests; live scenarios repeated 3–5 times. |
| Inbound content is data, never instructions | Adopt | Forwards, fetched pages and tool results are wrapped, never executed. |
| Scoped payment credentials (Stripe Link) | Not applicable | No purchases. |

## 2. Anthropic agent guidance (Claude Sonnet 5)

### What we found

- `claude-sonnet-5` (30 June 2026; the dateless id is the pinned snapshot): 1M-token context by default, 128K maximum output, $2 / $10 per MTok. Text and images in, text out: no audio block exists.
- Adaptive thinking is on by default; `budget_tokens`, non-default `temperature`/`top_p`/`top_k` and prefill return 400. `output_config.effort` takes `low|medium|high|xhigh|max` (`medium` is comparable to Sonnet 4.6 at high); changing effort or the output schema invalidates the cache. Unsupported: per-message effort, mid-conversation `role: "system"`, task budgets.
- Caching: minimum prefix 1,024 tokens, 4 breakpoints, `ttl` 5m or 1h; $2.50 / $4 / $0.20 per MTok for 5m write / 1h write / read; order tools → system → messages; a tool-definition change invalidates everything.
- Structured outputs via `output_config.format` (`json_schema`, `additionalProperties: false` on every object); strict tools via top-level `"strict": true`; no beta headers.
- Vision: JPEG, PNG, GIF and WebP only, no HEIC; 10 MB base64 per image; Sonnet 5 downsamples to a 2,576 px long edge and at most 4,784 visual tokens. PDFs: `document` blocks, 32 MB and 600 pages per request.
- Server tools: `web_search_20260318` is the latest type (`web_search_20260209` remains valid), likewise `web_fetch`; $10 per 1,000 searches; `encrypted_content` must be echoed back verbatim.
- Python SDK 1.3.0; 1.0.0 (20 August 2026) moved to `httpx2` and dropped `temperature` from `create`. Sonnet 5 "will reach for tools and run self-verification loops more readily" and "interprets prompts literally".

### What we decided

A ReAct loop on `messages.create` with strict tools, at most 12 tool rounds, parallel results returned in one user message. Context is ordered stable to volatile — coach prompt (1h cache), profile block (5m cache), verbatim turns, then a `<context>` block with the day state and retrieved history — with tool definitions sorted by name and byte-stable. Effort `medium` for turns, `low` for verify, proactive decisions and summaries; thinking `adaptive`. Images become JPEG q85 at a 1,568 px long edge. `web_research` is a separate model call carrying the server-side `web_search` and `web_fetch` tools (the plan pins the 20260209 versions), so the main tool set and its cache never change. Server-side compaction and the `memory_20250818` tool are not used for chat memory (section 7). Why: this is what the docs allow on Sonnet 5; the alternatives return 400 or are unsupported.

## 3. Telegram Bot API

### What we found

Bot API 10.3 (24 August 2026); aiogram 3.31.0 (26 August 2026) supports it, python-telegram-bot 22.8 stops at 10.0. `getFile` downloads up to 20 MB, the link holds for at least 1 hour, `file_id` is persistent. Photos arrive as `PhotoSize[]` without a MIME type. HEIC appears nowhere on the API page: an iPhone photo sent normally arrives as a JPEG `photo`, one sent "as file" as a `document` with a sender-set `image/heic` MIME type or `.heic` name. Albums are 2–10 items delivered as separate updates sharing a `media_group_id` with no end marker, so aggregation needs a debounce (0.7–1.0 s recommended). Voice notes are OGG/Opus. Limits: text 1–4096 characters after entity parsing, captions 0–1024, `callback_data` 1–64 bytes, `answerCallbackQuery` mandatory. `editMessageText` has no stated time limit for the bot's own messages; the FAQ allows about 1 message per second per chat, with `retry_after` on 429. Any non-service message can be pinned in a private chat. There is no scheduled-send method. Webhooks carry a `secret_token` in `X-Telegram-Bot-Api-Secret-Token`.

### What we decided

aiogram 3.31.0: PTB's API lag is decisive, the two-layer middleware fits album aggregation, and one `Dispatcher` serves polling in development and the aiohttp webhook handler in production. Albums aggregate per `(chat_id, media_group_id)` with a 1.2 s debounce (above the researched floor, for mobile jitter). Documents with an `image/*` MIME type or `.heic`/`.heif` name go through pillow-heif to JPEG. Voice is downloaded as OGG bytes and transcribed with OpenAI `gpt-transcribe` (`languages=["ru","en"]`; OGG accepted directly, no ffmpeg), `whisper-1` as fallback. Replies use `parse_mode=HTML`, escaped, split at 4,096. The Today card is one pinned message per day, edited at most once a second and re-posted on a new day. Inline keyboards only where they remove typing (slot picker, undo, recalculate, close day); no reply-keyboard menu. Proactive sends come from our own scheduler.

## 4. WHOOP Developer API

### What we found

v2 only: base `https://api.prod.whoop.com/developer`, paths under `/v2/`; v1 is "no longer supported" and its webhooks were removed (changelog 1 November 2025). OAuth2 authorization code at `https://api.prod.whoop.com/oauth/oauth2/auth` and `https://api.prod.whoop.com/oauth/oauth2/token` (form-encoded); `state` "must be eight characters long if you need to generate it yourself"; the redirect URI must match the dashboard exactly; scopes `read:recovery read:cycles read:sleep read:workout read:profile read:body_measurement` plus `offline`, mandatory for a refresh token. Tokens expire in 3600 s; a refresh rotates both tokens and concurrent refreshes race ("the first refresh request that reaches WHOOP will succeed"). Collections `/v2/cycle`, `/v2/recovery`, `/v2/activity/sleep`, `/v2/activity/workout`; per-cycle `/v2/cycle/{id}/recovery` and `/sleep`; profile and body endpoints; `DELETE /v2/user/access` (204, also stops webhooks). Pagination: `limit` default 10, maximum 25; `start` inclusive, `end` exclusive defaulting to now; `nextToken` in, `next_token` out; newest first. Rate limits: 100 requests per minute and 10,000 per day per client. Webhooks: an HTTPS URL registered in the dashboard with model version v2; verify `base64(HMAC_SHA256(client_secret, X-WHOOP-Signature-Timestamp + raw_body)) == X-WHOOP-Signature` — base64, not hex; events `workout|sleep|recovery` × `updated|deleted`; a v2 recovery event carries the sleep UUID, not the cycle id; five retries over about an hour; duplicates possible (`trace_id`); deliveries "may be missed"; no webhooks for cycles or body measurements. Data: energy in `kilojoule` (kcal = kJ / 4.184), durations `_milli`, strain 0–21, recovery 0–100, `score_state` in `SCORED|PENDING_SCORE|UNSCORABLE` with `score` absent unless scored, zones `zone_zero_milli` to `zone_five_milli`. An unapproved app is capped at 10 members and there is no sandbox ("we require all developers on the Developer Platform to have a WHOOP device").

### What we decided

`integrations/whoop.py`: server-side code flow with a signed, single-use 8-character `state`; Fernet-encrypted tokens; refresh serialized per user behind a lock and run by the 30-minute `integration_sync` job, not on 401; `limit=25` pagination; webhook verification by constant-time compare over the raw body, 2XX within a second, asynchronous processing, deduplication on `trace_id` and `unique (user_id, source, external_id)`; `recovery.updated` resolves sleep UUID → `cycle_id` → `/v2/cycle/{id}/recovery`; a reconciliation poll covers missed deliveries; kJ→kcal and ms→minutes at ingest; `score_state` checked first; disconnecting calls `DELETE /v2/user/access` and deletes the tokens. Why: webhooks are the only way to send the post-workout analysis "within minutes" the brief requires, but WHOOP says deliveries can be missed, so polling stays. The 10-member cap is accepted; approval is filed early.

## 5. Smart scales and Apple Health

### What we found

Withings is the only scale vendor with a public API: authorization at `https://account.withings.com/oauth2_user/authorize2` with comma-separated scopes `user.info,user.metrics,user.activity` and a code valid for 30 seconds; tokens from `POST https://wbsapi.withings.net/v2/oauth2` with `action=requesttoken` (client-secret variant, no nonce); access tokens last 3 hours, refresh tokens 1 year, and every call returns a new refresh token. Measures: `POST https://wbsapi.withings.net/measure` with `action=getmeas`, `meastypes=1,5,6,8,76,77,88,170,226` (weight through BMR), `category=1`, `lastupdate=<ts>`; real value = `value × 10^unit`. Notifications: `POST /notify` with `action=subscribe&callbackurl=…&appli=1` under a Bearer token; the callback must be HTTPS on a domain, port 80 or 443, and answer HEAD with 2xx; the POST is form-encoded (`userid, appli, startdate, enddate`) and unsigned; retried over about 5 hours and "missed notifications are not redelivered"; 120 requests per minute per application. Renpho, Eufy and Xiaomi have no public API; their path is app → Apple Health → bridge. Apple Shortcuts can bridge without an app (`Find Health Samples`, `Get Contents of URL` as a POST, a Time of Day automation), but there is no "health sample added" trigger and HealthKit is unreadable while the iPhone is locked. Health Auto Export posts `{"data":{"metrics":[…],"workouts":[…]}}` with dates `yyyy-MM-dd HH:mm:ss Z` and names such as `weight_&_body_mass` and `body_fat_percentage`. Oura v2 is OAuth-only with a 10-user cap until approval; Garmin is a business-only partner program; the legacy Fitbit Web API is turned off in September 2026 and its successor, the Google Health API (launched 24 March 2026), needs a restricted-scope privacy review.

### What we decided

Two ingestion paths, not five. `integrations/withings.py`: OAuth, `getmeas` decoding, the `notify` subscription, and a webhook that treats every POST as a hint and re-fetches via `lastupdate` with the user's token, so a spoofed POST can only cause a harmless re-sync; backfill on startup and on every notification because Withings never redelivers. `integrations/apple_health.py`: `POST /webhooks/apple-health/<webhook_token>` accepting the Health Auto Export dialect (detected by `data.metrics`) and a minimal native `samples[]` dialect a hand-built Shortcut can emit; a 32-byte random per-user token; an optional HMAC signature header with a 5-minute window, verified when present (Shortcuts and Health Auto Export cannot compute one); units normalized at ingest; idempotent on `(name, date, source)` or a client `sample_id`. Every source normalizes into `MeasurementEvent`, `SleepEvent` and `WorkoutEvent` behind one `Integration` protocol, so Oura, Garmin and Google Health are later modules. A scale reading is compared with the 7-day average, never commented on alone. Rejected: Renpho, Eufy and Xiaomi modules (no API), the legacy Fitbit API (dying this month), Garmin (no self-serve access), Google Health at launch (privacy review).

## 6. Food databases

### What we found

Open Food Facts serves a product at `/api/v3/product/{barcode}`; v2 is now "deprecated — still supported for backward compatibility", and both were verified live with identical `nutriments` keys (`energy-kcal_100g`, `proteins_100g`, `carbohydrates_100g`, `fat_100g`, `fiber_100g`, `sodium_100g` in grams). The intro page now states 15 requests per minute per IP for product reads (the brief's 100 is out of date) and 10 per minute for search, requires `User-Agent: AppName/Version (email)`, and returns 503 beyond. ODbL allows indefinite caching; 8,865 products are tagged UAE; fibre is often missing and must be treated as null. USDA FoodData Central: `GET /fdc/v1/foods/search` with `query`, `dataType` (exactly `Branded`, `Foundation`, `Survey (FNDDS)`, `SR Legacy` — the fact-check removed `Experimental` and the undocumented `startDate`, `endDate`, `tradeChannel`, `requireAllWords`), `pageSize` up to 200; `GET /fdc/v1/food/{fdcId}?format=abridged&nutrients=208,203,204,205,291,307`; 1,000 requests per hour per IP with a free key, then 429 and a one-hour block. Nutrient ids and numbers: energy 1008/208, protein 1003/203, fat 1004/204, carbohydrate 1005/205, fibre 1079/291, sodium 1093/307; the live search response uses `nutrientId`, `nutrientName`, `nutrientNumber`, `value` while the spec says `number`, `name`, `amount` (USDA-APIs issue #102); the data is CC0. Nutritionix has no free tier any more and covers US and UK chains only. FatSecret Basic is free at 5,000 calls per day and US-only; barcodes and UAE localization are Premier-only; nothing but ids may be cached beyond 24 hours. No database covers the Dubai outlets: Krave publishes per-dish macros, Kcal, Fitlab and Lifter Life plan-level ranges only, Kinoya nothing; some Talabat listings carry "NNN Kcal" in item names. Energy factors: Atwater 4/4/9 plus 7 kcal/g alcohol; EU Annex XIV adds 2 kcal/g fibre, and EU/GCC labels exclude fibre from carbohydrate where US labels include it. Self-reported intake under-reports energy by 10–30% against doubly labelled water.

### What we decided

Resolution order in `nutrition/resolve.py`: the `foods` cache → Open Food Facts by barcode (v3, v2 fallback) → USDA FDC for generic foods (`Foundation`, `SR Legacy`, `Survey (FNDDS)`; Branded by `gtinUpc` as a barcode fallback) → nothing, after which the agent calls `web_research` on the vendor's menu page or a Talabat listing, or estimates from the description with `source=model` and its confidence shown to the user. Natural-language parsing is done by the model. Clients use httpx with 6-second timeouts and return `None` on failure. Open Food Facts is budgeted at 15 per minute with the `Strikt/<version> (<contact>)` User-Agent and USDA at 1,000 per hour honouring `X-RateLimit-Remaining`; negative lookups are cached 7 days; OFF and USDA rows never expire and refresh lazily after 90 days. FatSecret (24-hour caching rule, Premier-only barcodes) and Nutritionix (no free tier, US chains) are not integrated. `nutrition/sanity.py` flags a 4/4/9 mismatch above 10% after trying both fibre conventions; macro bounds (no macro above 100 g per 100 g, no more than 900 kcal per 100 g); implausible fibre or fat against a per-ingredient table; a loose-food buffer of +25% (configurable 20–40%) on pasta, rice, noodles, sauces and soups, never on scanned or weighed items; a vegetable side at 6 g fat or more as "roasted in oil"; sodium at 600 mg per serving or 1.5 g per 100 g. The fixtures are the brief's cases: chicken-avocado at 7 g fat, egg and toast at 15 g fibre, a large pasta at 26 g carbs, brussels sprouts at 9 g fat, a lentil soup mix at 3.4 g sodium per 100 g, smoked turkey at 560 mg per 100 g.

## 7. Infinite-context patterns

### What we found

The canonical designs agree on tiers rather than one store. Generative Agents scores retrieval as `relevance + recency + importance` (recency decaying by 0.995 per hour) and runs periodic reflection that produces insights citing their sources. MemGPT/Letta keeps a fixed-size, model-editable "human" block in context, with recall and archival storage outside it, consolidated off the hot path. Mem0 decides `ADD | UPDATE | DELETE | NOOP` per fact against the top-10 similar memories. Recursive summarization fails measurably (about 10% erroneous memory items in one study; Anthropic's cookbook saw compaction keep 3 of 3 high-level facts and drop 3 of 3 obscure specifics), so numbers must live in typed rows. Persona drift is measurable within 8 rounds; the fix is restating constraints every 6 turns and opening every summary with the verbatim role line. Anthropic's compaction (`compact_20260112`, minimum trigger 50K input tokens) targets single long sessions, and the `memory_20250818` tool costs a `view` per turn; neither fits a chat turn of roughly 13K tokens. Postgres `websearch_to_tsquery` "will never raise syntax errors" on raw user input; `ts_rank_cd` over a stored generated `tsvector` with a GIN index is the documented setup. Claude Code's own memory (an index of at most 200 lines loaded every session, topic files on demand, contradictions that make Claude "pick one arbitrarily") is a production reference for the same shape.

### What we decided

Four tiers in `memory/`: the profile block (profile, active protocol, active notes), rendered deterministically and cached; coach `notes` with kind, confidence, source turn, `expires_at`, `superseded_by` and `active`, where a stateful fact supersedes its predecessor, never duplicates; rolling `summaries` (day, week) with structured `data`, written on `close_day`, at 03:00 local for open days, and weekly; and the raw log. A turn receives the last 30 turns or 40K tokens, today's day state, yesterday's close line, the week summary and rows from `get_history` and `search_history` (full-text search over turns, notes, summaries, item names); the plan caps a turn near 60K input tokens, above the research's working budget of about 12,500. Aggregate and date questions go to typed SQL. No embeddings at launch: pgvector with RRF only if a paraphrase evaluation shows full-text recall@12 below 0.8 or a user passes about 3,000 notes. Summaries cite typed rows rather than restating numbers. One model throughout with effort `low` as the cheap path: Haiku 4.5's 4,096-token cache minimum would silently skip caching a 2,500-token system block. Rejected: server-side compaction and the file-memory tool for chat memory; a `/memory` settings screen, because the brief forbids settings — notes are surfaced and corrected in conversation.

## 8. Stack

Every version was read from PyPI JSON on 3 September 2026 and the full set resolved with `uv lock` (73 packages, no conflicts); the fact-check confirmed all 14 version claims.

| Component | Pin | Reason |
|---|---|---|
| Python | 3.14 (`python:3.14-slim`); `requires-python >=3.13` | bugfix release (3.14.7) with cp314 wheels for every compiled dependency; 3.12 is security-only; aiogram forbids 3.15 |
| aiogram | 3.31.0 | Bot API 10.3; aiohttp-based, no clash with the httpx2 SDKs |
| anthropic | 1.3.0 | 1.x line on httpx2 |
| openai | 3.7.0 | `gpt-transcribe` at $0.0045 per minute, `whisper-1` fallback, 25 MB limit |
| SQLAlchemy | 2.0.52 with `[asyncio]` | 2.1 exists only as rc1 (2 September 2026) |
| asyncpg / Alembic | 0.31.0 / 1.19.1 | `postgresql+asyncpg://`; `alembic init -t async` |
| pydantic / pydantic-settings | 2.13.5 / 2.15.0 | aiogram requires pydantic below 2.14 |
| APScheduler | 3.11.3 `AsyncIOScheduler` | 4.x is alpha (`4.0.0a6`): "do NOT use this release in production" |
| aiohttp | 3.14.3 | OAuth callbacks and webhooks in-process; aiogram requires below 3.15; uv 0.12.9 builds the image |
| Pillow / pillow-heif | 12.3.0 / 1.6.0 | HEIC decoding; AVIF is native in Pillow, `register_avif_opener` is gone |
| structlog / cryptography | 26.1.0 / 50.0.1 | JSON logs; Fernet for tokens |
| httpx / uvloop | 0.28.1 / 0.22.1 | food-database clients (coexists with httpx2); `uvloop.run(main())` |
| Postgres | 18 (18.6) | volume at `/var/lib/postgresql`: PGDATA moved to `/var/lib/postgresql/18/docker` |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 | `asyncio_mode = "auto"`; unit tests on SQLite via aiosqlite |
| mypy / ruff | 2.3.1 `--strict` / 0.16.5 | mypy 2.0 changed defaults, dropped Python 3.9 |

## 9. Sources

All fetched on 3 September 2026 unless marked "(snippet)", meaning only a search snippet was available. Pages that returned 402, 403, 404 or 429 (instinct.co and its terms and privacy pages, Forbes, openai.com, help.openai.com, deliveroo.ae, WHOOP support, several blogs) contributed nothing and are omitted unless a snippet was quoted.

### Reflexion, τ-bench, Instinct, Sierra

- https://arxiv.org/abs/2303.11366
- https://arxiv.org/html/2303.11366 and https://arxiv.org/html/2303.11366v4
- https://github.com/noahshinn/reflexion
- https://raw.githubusercontent.com/noahshinn/reflexion/main/README.md
- https://github.com/noahshinn/reflexion/blob/main/alfworld_runs/alfworld_trial.py
- https://github.com/noahshinn/reflexion/blob/main/programming_runs/reflexion.py
- https://arxiv.org/abs/2406.12045
- https://nanothoughts.substack.com/p/reflecting-on-reflexion
- https://nanothoughts.substack.com/p/the-instinct-thesis-why-memory-is
- https://noahshinn.com/
- https://sierra.ai/author/noah-shinn
- https://sierra.ai/blog/tau-bench-shaping-development-evaluation-agents
- https://sierra.ai/product/agent-sdk
- https://sierra.ai/platform
- https://sierra.ai/blog/agent-development-life-cycle
- https://sierra.ai/blog/constellation-of-models
- https://sierra.ai/blog/agent-os-2-0
- https://sierra.ai/blog/agent-studio-2-0
- https://sierra.ai/blog/agents-as-a-service
- https://luma.com/435fmttp
- https://www.kucoin.com/news/flash/23-year-old-noah-shinn-instinct-founder-previously-worked-on-reflexion-and-bench
- https://techcrunch.com/2026/08/24/instincts-powerful-ai-assistant-is-raising-privacy-and-security-concerns/
- https://techcrunch.com/2026/08/26/viral-ai-startup-instinct-has-raised-350-million-at-a-2-5-billion-valuation/
- https://www.vellum.ai/blog/official-instinct-breakdown
- https://cellcog.ai/blog/what-is-instinct-ai/
- https://www.usecarly.com/blog/what-is-instinct-ai/
- https://sources.news/p/two-new-ai-assistants-have-silicon
- https://www.newcomer.co/p/amid-personal-assistant-investor
- https://qz.com/instinct-ai-assistant-series-b-funding-valuation-082726
- https://www.pymnts.com/news/artificial-intelligence/2026/instinct-nears-2-5-billion-valuation-as-ai-assistants-take-over-daily-chores/
- https://aipressroom.com/instinct-250m-personal-ai-agents/
- https://valueaddvc.com/blog/instinct-ai-valuation-2026-250m-series-b-2-5b-noah-shinns-viral-assistant
- https://ecorpit.com/instinct-ai-assistant-workspace-limited-use-training-split-2026/
- https://explainx.ai/blog/instinct-ai-agent-privacy-data-retention-claire-vo-august-2026
- https://servola.de/journal/instincts-2-5-billion-valuation-comes-with-a-documented-risk-list/
- https://captaincompliance.com/news/instincts-ai-assistant-can-book-the-table-and-keep-the-inbox-thats-the-privacy-problem/
- https://whatastartup.substack.com/p/time-to-trust-problem
- https://dev.to/judy_miranttie/personal-ai-assistant-instinct-hit-a-25b-valuation-in-weeks-a-top-agent-researcher-hid-complex-cbn
- https://trewknowledge.com/2026/08/28/ai-this-week-where-agent-work-actually-happens/
- https://stripe.com/blog/giving-agents-the-ability-to-pay
- https://x.com/noahrshinn/status/2092691344456351744 (snippet)
- https://x.com/noahrshinn/status/2093368510449877180 (snippet)
- https://x.com/stripe/status/2093386783899910486 (snippet)
- https://x.com/collision/status/2093391412792578284 (snippet)
- https://x.com/pitdesi/status/2090579987778937159 (snippet)

### Anthropic: Claude API, SDK, engineering guidance

- https://platform.claude.com/docs/en/models/sonnet-5/overview.md
- https://platform.claude.com/docs/en/models/sonnet-5/whats-new-sonnet-5.md
- https://platform.claude.com/docs/en/about-claude/models/overview.md
- https://platform.claude.com/docs/en/about-claude/pricing.md
- https://platform.claude.com/docs/en/api/messages/create.md
- https://platform.claude.com/docs/en/api/rate-limits.md
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview.md
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference.md
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use.md
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools.md
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool.md
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool.md
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool.md
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching.md
- https://platform.claude.com/docs/en/build-with-claude/structured-outputs.md
- https://platform.claude.com/docs/en/build-with-claude/prompt-caching.md
- https://platform.claude.com/docs/en/build-with-claude/vision.md
- https://platform.claude.com/docs/en/build-with-claude/pdf-support.md
- https://platform.claude.com/docs/en/build-with-claude/thinking.md
- https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost (target of the `adaptive-thinking.md` redirect)
- https://platform.claude.com/docs/en/build-with-claude/effort.md
- https://platform.claude.com/docs/en/build-with-claude/context-windows.md
- https://platform.claude.com/docs/en/build-with-claude/compaction.md
- https://platform.claude.com/docs/en/build-with-claude/context-editing.md
- https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages.md
- https://platform.claude.com/docs/en/build-with-claude/task-budgets.md
- https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons.md
- https://platform.claude.com/docs/en/build-with-claude/batch-processing.md
- https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5.md
- https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python (target of the `/docs/en/api/sdks/python` redirect)
- https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools
- https://code.claude.com/docs/en/memory
- https://www.anthropic.com/news/claude-sonnet-5
- https://www.anthropic.com/engineering/building-effective-agents
- https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- https://www.anthropic.com/engineering/writing-tools-for-agents
- https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- https://github.com/anthropics/anthropic-sdk-python/releases
- https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.0.0
- https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/main/MIGRATION.md
- https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/main/CHANGELOG.md
- https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/main/README.md
- https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/main/examples/structured_outputs.py

### Telegram

- https://core.telegram.org/bots/api
- https://core.telegram.org/bots/api-changelog
- https://core.telegram.org/bots/faq
- https://core.telegram.org/bots/features
- https://docs.aiogram.dev/en/latest/index.html
- https://docs.aiogram.dev/en/latest/changelog.html
- https://docs.aiogram.dev/en/latest/dispatcher/dispatcher.html
- https://docs.aiogram.dev/en/latest/dispatcher/long_polling.html
- https://docs.aiogram.dev/en/latest/dispatcher/webhook.html
- https://docs.aiogram.dev/en/latest/utils/media_group.html
- https://docs.aiogram.dev/en/latest/api/methods/send_voice.html
- https://docs.aiogram.dev/en/latest/api/methods/get_file.html
- https://docs.aiogram.dev/en/latest/api/download_file.html
- https://pypi.org/project/aiogram/
- https://pypi.org/project/python-telegram-bot/
- https://docs.python-telegram-bot.org/en/stable/changelog.html
- https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html
- https://docs.python-telegram-bot.org/en/stable/telegram.inputprofilephotostatic.html
- https://docs.python-telegram-bot.org/en/stable/telegram.inputprofilephotoanimated.html
- https://github.com/python-telegram-bot/python-telegram-bot/releases
- https://github.com/python-telegram-bot/python-telegram-bot/wiki/Frequently-requested-design-patterns
- https://github.com/python-telegram-bot/python-telegram-bot/wiki/Avoiding-flood-limits
- https://github.com/deptyped/aiogram-media-group and https://raw.githubusercontent.com/deptyped/aiogram-media-group/main/README.md
- https://gist.github.com/maqstein/c87a14619beffcf972775c32b12517e7
- https://grammy.dev/advanced/flood
- https://gramio.dev/rate-limits
- https://gramio.dev/telegram/methods/setmyprofilephoto
- https://github.com/telegramdesktop/tdesktop/issues/24444
- https://github.com/telegramdesktop/tdesktop/issues/23886
- https://community.n8n.io/t/telegram-bot-api-how-to-get-mime-type-of-received-photos/42463
- https://wooxy.com/knowledge-base/telegram/avatar-for-a-telegram-bot
- https://wooxy.com/knowledge-base/telegram/how-to-set-an-avatar-for-a-telegram-bot

### WHOOP

- https://developer.whoop.com/
- https://developer.whoop.com/docs/introduction/
- https://developer.whoop.com/docs/developing/oauth/
- https://developer.whoop.com/docs/developing/webhooks/
- https://developer.whoop.com/docs/developing/rate-limiting/
- https://developer.whoop.com/docs/developing/pagination/
- https://developer.whoop.com/docs/developing/v1-v2-migration/
- https://developer.whoop.com/docs/developing/getting-started/
- https://developer.whoop.com/docs/developing/app-approval/
- https://developer.whoop.com/docs/developing/support/
- https://developer.whoop.com/docs/developing/overview/
- https://developer.whoop.com/docs/developing/user-data/workout/
- https://developer.whoop.com/docs/developing/user-data/recovery/
- https://developer.whoop.com/docs/developing/user-data/sleep/
- https://developer.whoop.com/docs/developing/user-data/cycle/
- https://developer.whoop.com/docs/developing/user-data/user/
- https://developer.whoop.com/docs/tutorials/refresh-token-javascript/
- https://developer.whoop.com/docs/tutorials/access-token-passport/
- https://developer.whoop.com/docs/api-changelog/
- https://developer.whoop.com/api/
- https://api.prod.whoop.com/developer/doc/openapi.json
- https://developer-dashboard.whoop.com/
- https://www.community.whoop.com/t/app-approval-to-raise-the-10-user-limit-submitted-early-july-still-no-response/15892
- https://www.community.whoop.com/t/no-response-on-api-app-approval-to-raise-the-10-user-limit/15246?page=3
- https://docs.junction.com/wearables/guides/whoop
- https://github.com/nissand/whoop-mcp-server-claude
- https://github.com/hedgertronic/whoop
- https://tryterra.co/blog/whoop-integration-series-part-2-data-available-from-the-api-ec4337a9455b
- https://www.whoop.com/us/en/thelocker/more-personalized-heart-rate-zones-with-whoop/ (snippet)
- https://en.wikipedia.org/wiki/Calorie

### Withings, scales, Apple Health, other wearables

- https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/oauth-web-flow
- https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/oauth-authorization-url
- https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/access-and-refresh-tokens-no-recover
- https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/sign-your-requests
- https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/developer-account/create-your-accesses-no-medical-cloud
- https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/data-api/recommended-architecture
- https://developer.withings.com/developer-guide/v3/data-api/notifications/notification-overview
- https://developer.withings.com/developer-guide/v3/data-api/notifications/notification-subscribe
- https://developer.withings.com/developer-guide/v3/data-api/notifications/notification-content
- https://developer.withings.com/developer-guide/v3/withings-solutions/withings-api-plans
- https://developer.withings.com/api-reference/ (OpenAPI extracted from the `main.8ae1c0ad.js` bundle)
- https://renpho.ca/pages/faq-for-renpho-health-app
- https://pypi.org/project/renpho-api/
- https://service.eufy.com/article-description/Is-it-possible-to-sync-data-with-other-apps-like-Apple-Health-Google-Fit-Fitbit-etc-1618458383135
- https://github.com/AlexxIT/SmartScaleConnect
- https://apps.apple.com/sg/app/zepp-life-formerly-mifit/id938688461 (snippet)
- https://help.healthyapps.dev/en/health-auto-export/automations/rest-api/
- https://help.healthyapps.dev/en/health-auto-export/export-format/
- https://help.healthyapps.dev/en/health-auto-export/export-format/health-metrics
- https://help.healthyapps.dev/en/health-auto-export/export-format/workouts
- https://help.healthyapps.dev/en/health-auto-export/automations
- https://help.healthyapps.dev/en/health-auto-export/automations/schedule-automations-using-shortcuts
- https://github.com/Lybron/health-auto-export
- https://apps.apple.com/us/app/health-auto-export-json-csv/id1115567069
- https://support.apple.com/guide/shortcuts/intro-to-find-and-filter-actions-apd3c845e881/ios
- https://support.apple.com/guide/shortcuts/request-your-first-api-apd58d46713f/ios
- https://support.apple.com/guide/shortcuts/intro-to-personal-automation-apd690170742/ios
- https://support.apple.com/guide/shortcuts/event-triggers-apd932ff833f/ios
- https://support.apple.com/en-us/101583 (snippet)
- https://blog.maximeheckel.com/posts/build-personal-health-api-shortcuts-serverless/
- https://ladvien.com/syncing-apple-health-kit-data-postgres/
- https://hcwebhook.com/ios
- https://healthexportpro.com/ , https://apps.apple.com/us/app/my-health-export/id6748932482 , https://vitalina.app/export/format-reference (snippets)
- https://developer.garmin.com/gc-developer-program/health-api/
- https://developer.garmin.com/gc-developer-program/program-faq/
- https://developer.garmin.com/gc-developer-program/overview/
- https://github.com/stoufa06/php-garmin-connect-api/issues/23
- https://openwearables.io/blog/garmin-connect-api-developer-guide-activities-health-metrics
- https://aifitnessapi.com/fix/garmin-api-approval
- https://cloud.ouraring.com/v2/docs
- https://cloud.ouraring.com/v2/static/json/openapi-1.37.json
- https://cloud.ouraring.com/docs/authentication
- https://github.com/Pinta365/oura_api
- https://dev.fitbit.com/build/reference/web-api/
- https://dev.fitbit.com/build/reference/web-api/developer-guide/getting-started/
- https://dev.fitbit.com/build/reference/web-api/developer-guide/application-design/
- https://dev.fitbit.com/build/reference/web-api/developer-guide/using-subscriptions/
- https://developers.google.com/fit
- https://developer.android.com/health-and-fitness/health-connect/migration/fit/faq
- https://developers.google.com/health
- https://developers.google.com/health/about
- https://developers.google.com/health/endpoints
- https://developers.google.com/health/data-types
- https://developers.google.com/health/webhooks
- https://developers.google.com/health/release-notes
- https://sahha.ai/blog/fitbit-api-sunset-migration/

### Food databases, outlets, energy factors

- https://openfoodfacts.github.io/openfoodfacts-server/api/
- https://openfoodfacts.github.io/openfoodfacts-server/api/ref-cheatsheet/
- https://openfoodfacts.github.io/openfoodfacts-server/api/tutorial-off-api/
- https://openfoodfacts.github.io/search-a-licious/users/ref-openapi/
- https://static.openfoodfacts.org/data/data-fields.txt
- https://world.openfoodfacts.org/api/v2/product/3017620422003.json and https://world.openfoodfacts.org/api/v3/product/3017620422003 (live calls)
- https://world.openfoodfacts.org/api/v2/search?countries_tags=en:united-arab-emirates (live call)
- https://world.openfoodfacts.org/cgi/search.pl?search_terms=greek%20yogurt&json=1 (live call)
- https://search.openfoodfacts.org/search?q=nutella (live call)
- https://github.com/openfoodfacts/openfoodfacts-server/issues/8818
- https://calorieapi.com/blog/open-food-facts-api-rate-limit-production-app
- http://fdc.nal.usda.gov/api-guide/
- https://fdc.nal.usda.gov/api-spec/fdc_api.html
- https://github.com/USDA/USDA-APIs/issues/102
- http://fdc.nal.usda.gov/data-documentation/
- https://fdc.nal.usda.gov/api-key-signup
- https://gist.github.com/magdiel01/3c82068d71a745788c04f49aa23d5244
- https://api.nal.usda.gov/fdc/v1/foods/search?api_key=DEMO_KEY (live call)
- https://docx.syndigo.com/developers/docs/natural-language-for-nutrients
- https://docx.syndigo.com/developers/docs/instant-endpoint
- https://docx.syndigo.com/developers/docs/nutritionix-api-guide
- https://developer.nutritionix.com/
- https://www.nutritionix.com/database
- https://www.nutritionix.com/business/api
- https://calorieapi.com/blog/nutritionix-api-pricing
- https://www.spikeapi.com/blog/top-nutrition-apis-for-developers-2026
- https://trybytes.ai/blogs/best-apis-for-menu-nutrition-data
- https://platform.fatsecret.com/docs/guides/authentication/oauth2
- https://platform.fatsecret.com/docs/v3/foods.search
- https://platform.fatsecret.com/docs/v1/foods.search
- https://platform.fatsecret.com/docs/v4/food.get
- https://platform.fatsecret.com/docs/v2/food.find_id_for_barcode
- https://platform.fatsecret.com/docs/guides/localization
- https://platform.fatsecret.com/docs/guides/storable-data
- https://platform.fatsecret.com/api-editions
- https://platform.fatsecret.com/platform-api
- https://developer.edamam.com/food-database-api
- https://developer.edamam.com/food-database-api-docs
- https://spoonacular.com/food-api/pricing
- https://calorieninjas.com/pricing
- https://www.myfitnesspal.com/apps/api/version
- https://eatkrave.com/menu/
- https://kcallife.com/meal-plans/
- https://restaurants.kcallife.com/
- https://fitlab-me.com/our-plans
- https://lifter-life.ae/meal-plan
- https://www.talabat.com/uae/the-500-calorie-project
- https://www.joejuice.com/ingredients-allergens and https://joeandthejuice.zendesk.com/hc/en-us/articles/34307353612818-Allergens-and-ingredients (snippets)
- https://kinoya.com/assets/img/Kinoya%20Food%20Menu.pdf (snippet)
- https://gulfnews.com/uae/dubai-municipality-clarifies-heres-how-dubai-eateries-must-display-calories-on-menus-1.64070408 (snippet)
- https://www.hoteliermiddleeast.com/business/104960-dubai-municipality-delays-calorie-publishing-requirements (snippet)
- https://www.legislation.gov.uk/eur/2011/1169/annex/XIV
- https://eur-lex.europa.eu/eli/reg/2011/1169/oj/eng
- https://pmc.ncbi.nlm.nih.gov/articles/PMC3404807/
- https://link.springer.com/article/10.1186/s12874-025-02568-4 (snippet)

### Memory, retrieval, persona consistency

- https://ar5iv.labs.arxiv.org/html/2304.03442 (Generative Agents)
- https://ar5iv.labs.arxiv.org/html/2310.08560 (MemGPT)
- https://arxiv.org/html/2504.19413 (Mem0)
- https://ar5iv.labs.arxiv.org/html/2308.15022 (recursive summarization)
- https://arxiv.org/html/2601.04463 (ProMem)
- https://arxiv.org/abs/2410.10813 (LongMemEval)
- https://arxiv.org/html/2402.10962v3 (instruction instability, persona drift)
- https://arxiv.org/abs/2412.00804 (identity drift)
- https://www.letta.com/blog/agent-memory/
- https://www.letta.com/blog/sleep-time-compute/
- https://docs.letta.com/guides/agents/memory-blocks
- https://docs.letta.com/guides/agents/architectures/sleeptime/
- https://docs.letta.com/concepts/memory-management/
- https://docs.letta.com/guides/agents/memory
- https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
- https://llmrefs.com/blog/reverse-engineering-chatgpt-memory
- https://www.edtechinnovationhub.com/news/openai-rolls-out-new-chatgpt-memory-system-to-keep-personalization-current
- https://dev.to/akaranjkar08/openai-dreaming-v3-chatgpt-now-learns-while-you-sleep-4cd2
- https://help.openai.com/en/articles/8590148-memory-faq (snippet)
- https://openai.com/index/chatgpt-memory-dreaming/ (snippet)
- https://www.postgresql.org/docs/current/textsearch-controls.html
- https://www.postgresql.org/docs/current/textsearch-tables.html
- https://www.postgresql.org/docs/current/textsearch-indexes.html
- https://jkatz05.com/post/postgres/hybrid-search-postgres-pgvector/
- https://futureagi.com/blog/evaluating-llm-personas-style-2026/
- https://tianpan.co/blog/2026-05-02-persona-drift-long-horizon-agent-sessions
- https://pieces.app/blog/hierarchical-summarization
- https://gadgetsandwearables.com/2026/05/08/whoop-memory/
- https://strengthinsight.app/blog/using-whoop-ai-coach/
- https://ouraring.com/blog/oura-advisor/
- https://dev.to/saheelwagh/build-your-fitness-coach-with-hermes-37nm
- https://medium.com/@natetang/building-my-own-ai-fitness-coach-using-claude-code-cf52663370c2 (snippet)

### Python stack

- PyPI JSON for anthropic, aiogram, python-telegram-bot, sqlalchemy, alembic, asyncpg, psycopg, psycopg-binary, pydantic, pydantic-core, pydantic-settings, apscheduler, pillow, pillow-heif, structlog, cryptography, httpx, httpx2, aiohttp, openai, deepgram-sdk, faster-whisper, pytest, pytest-asyncio, mypy, ruff, uv, testcontainers, pytest-postgresql, uvloop, greenlet (`https://pypi.org/pypi/<name>/json`), plus https://pypi.org/project/httpx2/
- https://developers.openai.com/api/docs/guides/speech-to-text
- https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create
- https://developers.openai.com/api/docs/api-reference/audio
- https://developers.openai.com/api/docs/models/gpt-transcribe
- https://portkey.ai/error-library/invalid-file-format-error-10555
- https://developers.deepgram.com/docs/pre-recorded-audio
- https://developers.deepgram.com/docs/supported-audio-formats
- https://raw.githubusercontent.com/deepgram/deepgram-python-sdk/main/README.md
- https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/README.md
- https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/utils.py
- https://ffmpeg.org/general.html
- https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
- https://docs.sqlalchemy.org/en/21/changelog/migration_21.html
- https://alembic.sqlalchemy.org/en/latest/cookbook.html
- https://github.com/agronholm/apscheduler
- https://apscheduler.readthedocs.io/en/3.x/modules/schedulers/asyncio.html
- https://apscheduler.readthedocs.io/en/3.x/userguide.html
- https://github.com/bigcat88/pillow_heif
- https://raw.githubusercontent.com/bigcat88/pillow_heif/master/CHANGELOG.md
- https://pillow-heif.readthedocs.io/en/latest/pillow-plugin.html
- https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/
- https://www.structlog.org/en/stable/getting-started.html
- https://raw.githubusercontent.com/MagicStack/uvloop/master/README.rst
- https://pytest-asyncio.readthedocs.io/en/latest/reference/configuration.html
- https://pytest-asyncio.readthedocs.io/en/latest/reference/changelog.html
- https://testcontainers-python.readthedocs.io/en/latest/modules/postgres/README.html
- https://mypy.readthedocs.io/en/stable/changelog.html
- https://mypy.readthedocs.io/en/stable/config_file.html
- https://docs.astral.sh/ruff/configuration/
- https://docs.astral.sh/uv/guides/integration/docker/
- https://docs.astral.sh/uv/concepts/projects/sync/
- https://www.python.org/downloads/
- https://docs.python.org/3/whatsnew/3.13.html
- https://hub.docker.com/_/python
- https://hub.docker.com/_/postgres
- https://www.postgresql.org/support/versioning/

### Brand (see BRAND.md)

- https://geist.co/work/anthropic
- https://type.today/en/journal/anthropic
- https://raw.githubusercontent.com/anthropics/skills/main/skills/brand-guidelines/SKILL.md
- https://gooova.com/en/anthropic-designed-its-own-type-family/
- https://x.com/designbizco/status/1972759238049997006
- https://fontsinuse.com/type_designers/3628/chester-jenkins
- https://libraries.io/npm/anthropic-fonts
- https://www.shadcn.io/design/anthropic
- https://styles.refero.design/style/d469cba4-c448-4a43-a033-883f8bfcdc42
- https://designmd.cc/benchmarks/anthropic (snippet)
- https://mobbin.com/colors/brand/claude
- https://raw.githubusercontent.com/VoltAgent/awesome-design-md/main/design-md/claude/DESIGN.md
- https://open-design.ai/plugins/design-system-claude/
- https://www.designrush.com/best-designs/logo/claude-logo-design-the-ai-mark-built-to-look-none-of-its-rivals
- https://studiosiraj.com/blog/anthropic-brand-identity-case-study
- https://dwglogo.com/color-codes/claude/
- https://beginswithai.com/claude-ai-logo-color-codes-fonts-downloadable-assets/
- https://www.brandcolorcode.com/claude
- https://www.brandcolorcode.com/anthropic
- https://londonlogodesigns.co.uk/blog/claude-logo-history-the-evolution-of-anthropics-ai-identity/
- https://raw.githubusercontent.com/simple-icons/simple-icons/develop/icons/claude.svg
- https://claude.com/product/overview
- https://www.anthropic.com/
- https://www.brandinginasia.com/keep-thinking-anthropic-rolls-out-first-brand-campaign-via-mother/ (snippet)
- https://openai.com/brand/
- https://openai.com/chatgpt/overview/
- https://www.wallpaper.com/tech/openai-has-undergone-its-first-ever-rebrand-giving-fresh-life-to-chatgpt-interactions
- https://mobilesyrup.com/2025/02/05/openai-announces-new-rebrand-with-new-typeface-colour-palette-and-wallpaper/
- https://www.thecbo.world/post/openai-rebrand-balances-the-technical-and-the-human
- https://www.designyourway.net/blog/openai-logo/
- https://www.deck.gallery/blog/openai-brand-guidelines-breakdown/
- https://studiodumbar.com/work/openai
- https://webdesignerdepot.com/openai-gets-a-fresh-look-new-logo-custom-font-and-a-more-human-feel/
- https://designcompass.org/en/2025/02/06/openai-rebranding/
- https://www.stashmedia.tv/love-it-or-not-the-openai-refresh-looks-great-in-motion/
- https://www.houseofgai.com/blog/open-ais-rebrand-a-bold-leap-or-just-a-motion-design-flex
- https://news.designrush.com/openai-refreshes-its-visual-brand-identity-with-new-logo-typeface (snippet)
- https://raw.githubusercontent.com/google/fonts/main/ofl/newsreader/METADATA.pb
- https://raw.githubusercontent.com/google/fonts/main/ofl/fraunces/METADATA.pb
- https://raw.githubusercontent.com/google/fonts/main/ofl/sourceserif4/METADATA.pb
- https://raw.githubusercontent.com/google/fonts/main/ofl/instrumentserif/METADATA.pb
- https://raw.githubusercontent.com/google/fonts/main/ofl/dmsans/METADATA.pb
- https://raw.githubusercontent.com/google/fonts/main/ofl/geist/METADATA.pb
- https://raw.githubusercontent.com/google/fonts/main/ofl/inter/METADATA.pb
- https://raw.githubusercontent.com/google/fonts/main/ofl/manrope/METADATA.pb
