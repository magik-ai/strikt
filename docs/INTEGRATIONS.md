# Integrations: status, how the data flows, and the owner's checklist

**Status:** current as of 2026-11-05 (issue #18). Read this before turning an integration on, or
when one goes quiet.

## Where each one stands

| Integration | Built | What reaches the coach | What is left for the owner |
|---|---|---|---|
| WHOOP | OAuth, v2 sync every 30 min, signed webhooks, token rotation | every workout (analysed within minutes), sleep, recovery, day strain and kcal burned; `<day>`, `<recent>`, the nudges | a developer app, two env vars, "connect WHOOP" in the chat |
| Withings | OAuth, `getmeas`, `getactivity`, notifications for weight, pressure, activity | weight and body composition, visceral fat, BMR, metabolic age, blood pressure with pulse, daily steps and active kcal; `<body>`, `<recent>`, the weigh-in nudge | a developer app, two env vars, "connect Withings" in the chat |
| MyFitnessPal | the website's own session (no official API exists), a day mirror | nothing goes *to* the coach: every line the coach logs goes *to* MFP | "connect MyFitnessPal" in the chat, then one paste on a computer |
| Apple Health | a personal webhook (Shortcuts / Health Auto Export) | weight, steps, resting HR, HRV, sleep, workouts | unchanged, see the README |

Every connected source is polled by the scheduler's `integration_sync` job every 30 minutes, so
data lands every day even when a webhook is lost. A connection that dies (a refresh rejected, the
MFP session logged out) is told to the user once in Telegram ("WHOOP got disconnected... write
connect WHOOP"), outside quiet hours and only to active users; reconnecting clears it. A network
error or a 5xx from a provider is never taken for a dead connection. "Disconnect MyFitnessPal"
(or any other) in the chat deletes the stored login (`disconnect_integration`); what was already
imported stays.

## WHOOP

How it works: `integrations/whoop.py`. A finished workout, sleep or recovery arrives by webhook
within minutes; the 30-minute poll asks for the last run's window plus a day and publishes every
record again (marked `changed` when it is new or WHOOP rescored it). The proactive engine sends at
most one message per window, so a workout is analysed once, and a recovery that arrived inside
quiet hours still gets its message from the first poll after them.

What the coach sees: today's workouts, last night's sleep and the recovery (score, RHR, HRV, SpO2,
day strain, kcal burned) in `<day>`; 14 days of training, sleep hours and recovery in `<recent>`.

Owner's steps:

1. developer.whoop.com, sign in with the WHOOP account, Create App.
   - Redirect URI: `https://<public domain>/oauth/whoop/callback`, exactly.
   - Scopes: `offline read:recovery read:cycles read:sleep read:workout read:profile
     read:body_measurement`.
   - Webhook URL: `https://<public domain>/webhooks/whoop`, model version **v2**.
2. Railway, the bot service, Variables: `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`. Check that
   `PUBLIC_BASE_URL` is the same public HTTPS domain. Railway redeploys on save.
3. In the chat: "подключи WHOOP". Open the link, allow access. The browser page and a Telegram
   message report what came from the last 7 days.

Limits: an unapproved WHOOP app takes 10 members; file for approval before inviting more people.

## Withings

How it works: `integrations/withings.py`. `getmeas` brings weight, fat ratio and mass, lean and
muscle mass, water (total, extra and intracellular), bone mass, visceral fat, BMR, metabolic age,
vascular age, pulse wave velocity, blood pressure, heart rate, SpO2 and body temperature,
whichever the devices measure. `getactivity` brings one row per day (steps, active kcal), filed
at local noon and updated in place as the day goes on. Notifications are subscribed for weight
(1), pressure and heart rate (4) and activity (16); they are only a hint, the data is re-fetched
with the user's token. Sleep stays with WHOOP.

What the coach sees: `<body>` in every turn (latest weight with date and source, 7-day average,
change over about 30 days, body fat, waist, visceral fat, blood pressure with pulse, yesterday's
steps and active kcal) and the day's last weigh-in on each `<recent>` line. A new weigh-in
triggers the trend comment (never a single-reading comment).

Owner's steps:

1. developer.withings.com, Developer Dashboard, create an application (Public API).
   - Callback URL: `https://<public domain>/oauth/withings/callback`. The same domain must answer
     `HEAD /webhooks/withings` with 200 (it does) for the notifications.
2. Railway Variables: `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`.
3. In the chat: "подключи Withings". Allow `user.info, user.metrics, user.activity` (all three:
   without `user.activity` there are no steps).

Limits: an app in restricted mode takes 10 linked users.

## MyFitnessPal

Why this route: MyFitnessPal has no usable official API. The partner program says "We are not
accepting requests for API access at this time" (myfitnesspalapi.com) and its v2 docs list only
exercise, steps and water writes, no food. The website itself uses the same v2 API with a bearer
token it gets from the login cookies; open-source clients (python-myfitnesspal,
myfitnesspal-mcp-python, which runs on Railway as of 2026-10) do the same, and so does Strikt.
It is unofficial: MyFitnessPal can change it without notice, and the log says so
(`mfp_mirror_failed`, `mfp_session_expired`) when it does.

How it works: `integrations/myfitnesspal.py`.

- The login: the user's cookies are Fernet-encrypted (`integrations.refresh_token_enc`), traded
  for a bearer token at `www.myfitnesspal.com/user/auth_token`, renewed every 6 hours (with the
  NextAuth session) so MFP keeps re-issuing them. They never reach the model and are never
  logged. When MFP stops accepting them, the user is told once and asked to connect again.
- The mirror: per coaching day (the date `set_day_food` files food under), one diary entry per
  line of the coach's list, in Breakfast / Lunch / Dinner / Snacks (an unknown slot goes by the
  time it was eaten). Each line is a private custom food (brand "Strikt") whose one serving holds
  the line's numbers exactly; identical lines reuse one food. `integration_links` records what
  Strikt created: only those entries are ever deleted, whatever the user logs in MFP is left
  alone. A changed day is mirrored 20 seconds after the turn; the 30-minute sync compares the
  last 14 days with the links and reads today and yesterday back from MFP (an entry deleted
  there by hand comes back: the coach's list is the source of truth).
- On connect, today and the 6 days before are copied in the background. A past day that already
  holds food in MFP is skipped, and the user is told which ones; after a reconnect that covers the
  days logged by hand while the connection was down. Connecting another MFP account starts from
  a clean slate.
- Every entry is known by its MFP id, never guessed from its food: a write is recorded as
  pending before the call and gets its id after it, so an answer that never came back (MFP stored
  the entry, the reply timed out) is matched on the next run and never added twice; an entry of
  Strikt's missing from one listing is remembered by id and removed if it reappears next to its
  replacement. An entry the user logs by hand is never touched, even when it uses a Strikt food
  from their "My Foods".
- "Disconnect MyFitnessPal" deletes the login and stops the mirror at once (a mirror that was
  writing at that moment cannot store the login again); the record of what Strikt created stays,
  so reconnecting the same account does not copy those days twice.
- `/forget_me` deletes Strikt's records of what it created, not the entries in the user's MFP
  account (that account is the user's).

Why MFP is worth it: MFP writes nutrition to Apple Health and Health Connect, where WHOOP
(Auto-Log Nutrition) and other apps read it, and several (Garmin among them) connect to MFP
directly. So a meal logged here reaches them too, with no second entry by hand.

Owner's steps (nothing to set on the server):

1. In the chat: "подключи MyFitnessPal". The coach sends a link (valid 24 hours, and dead once
   it has connected an account: a copy left in a browser history cannot rebind it).
2. Open it on a computer where you are logged in to myfitnesspal.com in Chrome, follow the four
   steps on the page: F12, Network, reload, right-click the first request, Copy, Copy as cURL,
   paste, Connect.
3. Telegram confirms; within a few minutes today and the last 6 days are in the MFP diary.
4. Check once in the MFP app that the entries and numbers look right, and that MFP is connected
   to Apple Health (its apps and devices settings) with nutrition allowed to write, so WHOOP and
   the rest see it.

If MFP blocks the server (the firewall in front of it, `MfpBlockedError` in the log), the paste is
stored as "unchecked" and retried every 30 minutes; if it keeps failing, write to the
maintainer: the fallback is a TLS-impersonating client (`curl_cffi`), which is not installed.

## Troubleshooting

- Nothing arrives from WHOOP: Railway logs `whoop_webhook` (deliveries), `whoop_synced` (the
  poll); `/health` lists the registered providers. A `whoop_sync_unauthorized` means the refresh
  token was rejected: reconnect.
- Withings readings but no steps: the account has no activity data (no watch, Health Mate
  without phone tracking), or `user.activity` was unticked at connect
  (`withings_activity_unavailable`).
- MFP entries missing: `mfp_day_mirrored` shows each day's adds and removes; `mfp_mirror_failed`
  carries MFP's HTTP status.
