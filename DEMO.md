# Strikt — demo transcript

This is a constructed example, written against the code and prompts in this repository; it has not
been run live yet, and it is not a log. Times are Dubai local (Asia/Dubai). The user writes Russian
with English food names; the bot mirrors. Small italic notes name the tool calls the loop would
make. Cards are the exact output of `render_day_card` for these numbers. Protocol picked in
onboarding: 2 000 kcal · 210 P / 75 C / 105 F · fiber 30. Nothing below is hard-coded; every
number entered through the interview or a tool, and every model call is billed to the key Ilya
pastes in the first minute (`LLM_KEY_MODE=user`).

## 1. Onboarding — Thursday 3 September, 08:31

**08:31 · Ilya:** `/start`

**08:31 · Strikt** *(code, `start.welcome`)*: Strikt. Одно окно, без настроек. Присылай фото еды,
скриншоты, голос или текст — я записываю, считаю и подгоняю.

**08:31 · Strikt** *(code, `key.needed`; no model call yet)*:
Тренер работает на твоём ключе Anthropic. Сам тренер бесплатный; Anthropic списывает с ключа за
то, что он потратил.
1. Открой console.anthropic.com. Войди или создай аккаунт.
2. Billing → пополни баланс. Платишь только за то, что тренер израсходовал.
3. Settings → API keys → Create key. Назови strikt, скопируй ключ (начинается с sk-ant-).
4. Вставь ключ сюда сообщением.
Я проверю его, сохраню в зашифрованном виде, удалю твоё сообщение и больше не покажу.
Новый ключ заменяет старый. /forget_me удаляет его вместе со всем остальным.

**08:31 · Ilya:** `sk-ant-api03-…7Q2f` *(one call checks it — `GET /v1/models/claude-sonnet-5`
on that key — then it is Fernet-encrypted into `users`; the message is deleted from the chat and
never becomes a turn)*

**08:31 · Strikt** *(code, `key.saved`)*: Ключ сохранён, заканчивается на …7Q2f. Твоё сообщение с
ключом удалено.

**08:31 · Strikt** *(the interview starts on Ilya's key: the `/start` turn runs now)*: Десять
вопросов, минут десять, можно прерываться — еду присылай по ходу. Имя и город?

**08:32 · Ilya:** Илья, Дубай.

**08:32 · Strikt** *(update_profile → name, city, timezone Asia/Dubai, language ru, step 1)*:
Илья, Дубай, Asia/Dubai. Цель своими словами — что должно измениться?

**08:33 · Ilya:** Убрать живот. Талия 103 см, в апреле было 108. Вес 104, рост 190, 39 лет.

**08:33 · Strikt** *(update_profile → goal, KPI waist 94/90, cadence 14 d, height, birth year;
log_measurement waist 103 cm, weight 104 kg)*: KPI — талия у пупка. 103 сейчас, 94 — хорошо,
90 — отлично. Замер раз в 14 дней, утром натощак; вес — раз в неделю. 103 и 104 записал как
базу. Подъём и отбой в будни?

*Steps 4 and 5 omitted: wake 08:00, bed 00:30; three strength sessions and one run a week;
delivery for lunch, home for dinner.*

**08:38 · Ilya:** WHOOP есть.

**08:38 · Strikt** *(update_profile → wearable whoop; connect_integration whoop)*: Открой ссылку,
войди в WHOOP и разреши доступ. Подтяну последние 7 дней.
`https://api.prod.whoop.com/oauth/oauth2/auth?response_type=code&client_id=…&state=…`
Пока подключаешь: что любишь, чего не ешь, аллергии?

*Browser, after the OAuth callback (`whoop.connected`)*: WHOOP подключён. Забрал за 7 дней:
тренировок 2, снов 7, восстановлений 7. Возвращайся в Telegram.

*Steps 6–10 omitted: food preferences, health context, the protocol chosen from three options
(update_protocol rewrites the card that log_measurement pinned at 08:33), style pushy / short /
check-ins on / quiet 00:00–07:30, the summary, finish_onboarding at 08:49.*

**08:49 · Strikt:** Профиль собран. Дальше — только сообщения: «сбавь обороты на неделю», «белок
180», «напомни в 8 про талию». Пришли следующий приём еды фото — запишу и покажу бюджет.

## 2. First food — 09:10 and 13:40

**09:10 · Ilya:** 3 яйца и пол авокадо

**09:10 · Strikt** *(log_meal → meal 1, slot breakfast, 327 kcal)*:
яйца ×3 — 215 ккал / 19 Б / 1 У / 15 Ж
авокадо ½ (70 г) — 112 ккал / 1 Б / 6 У / 10 Ж / 5 клетчатка
Итого: 327 ккал / 20 Б / 7 У / 25 Ж / 5 клетчатка
Осталось: 1 673 ккал · 190 Б · 68 У · 80 Ж · 25 клетчатки

`[Убрать] [Пересчитать]`

**13:40 · Ilya:** *(screenshot: Krave app, "Chicken Shawarma Bowl — 540 kcal · 48 g protein ·
38 g carbs · 18 g fat")*

**13:40 · Strikt** *(log_meal → meal 2, slot lunch, 612 kcal; flags: fat_implausible,
loose_buffer)*:
Chicken Shawarma Bowl (Krave) — 612 ккал / 50 Б / 44 У / 26 Ж / 5 клетчатка
Жир 18 → 26: шаурма с чесночным соусом не бывает на 18 г. Рис — сыпучее, +20 %.
Итого: 939 ккал / 70 Б / 51 У / 51 Ж / 10 клетчатка
Осталось: 1 061 ккал · 140 Б · 24 У · 54 Ж · 20 клетчатки
Ужин должен дать 100 г белка. Кофта плюс творог закрывают.

`[Убрать] [Пересчитать]`

*The pinned card is edited in place (`DayStateChanged` → `DayCard.refresh`):*

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
<b>Сон</b>: 6h10 · 78% · восстановление 61%
```

## 3. WHOOP workout by webhook — 19:05

*`workout.updated` arrives at `/webhooks/whoop`; the signature checks out; the workout is fetched
and stored (running, 18:10–18:55, strain 12.1, 1 699 kJ → 406 kcal, avg HR 130, max 158);
`WorkoutEvent` → trigger `whoop_workout_synced`, step 1, `proactive_decide` writes the text.*

**19:05 · Strikt:** Бег 18:10–18:55: 45 мин, пульс 130, 406 ккал, strain 12.1.
Прошлый бег, сб 29 авг: 94 мин, пульс 104, 361 ккал. Сегодня 9,0 ккал/мин против 3,8 — вдвое
больше работы за половину времени.
Закончил за 5,5 часов до отбоя, сну не мешает. На ужин осталось 140 г белка — что и во сколько?

**19:07 · Ilya:** Кофта в восемь.

**19:07 · Strikt** *(set_day_plan → dinner: kofta, 20:00)*: Принял. 300 г кофты — около 55 г
белка; добей творогом до 90.

## 4. Dinner and the close — 20:20 and 22:40

**20:20 · Ilya:** Кофта 300 г, брюссельская капуста жареная 150 г, творог 0.5% 200 г

**20:20 · Strikt** *(log_meal → meal 3, slot dinner, 946 kcal; flag: veg_fat)*:
кофта, 300 г — 624 ккал / 54 Б / 8 У / 42 Ж / 1 клетчатка
брюссельская капуста, 150 г — 150 ккал / 5 Б / 12 У / 9 Ж / 6 клетчатка · 9 г жира — жарена в
масле, овощи не бесплатные
творог 0.5%, 200 г — 172 ккал / 33 Б / 6 У / 1 Ж
Итого: 1 885 ккал / 162 Б / 77 У / 103 Ж / 17 клетчатка
Осталось: 115 ккал · 48 Б · −2 У · 2 Ж · 13 клетчатки
До 210 не хватает 48 г белка при 115 ккал в запасе. Шейк на воде — 120 ккал / 25 Б — единственное,
что влезает. Или закрываем как есть.

`[Убрать] [Пересчитать]`

**22:40 · Ilya:** *(taps `Закрыть день` on the card → synthetic message «Закрой день.»)*

**22:40 · Strikt** *(close_day → verdict; day summary written; card closed)*:
День закрыт: 1 885 / 2 000 ккал · Б 162 / 210 · У 77 / 75 · Ж 103 / 105 · клетчатка 17 / 30.
Тренировка: бег 45 мин, 406 ккал, плотность вдвое выше субботней.
Сработало: три приёма, обед в 13:40 — вечер без срыва.
Исправить завтра: белок. −48 г при почти закрытых калориях — жир ушёл в 103 г. Кофту меняем на
сибас или стейк без соуса.
Отбой к 00:30. Ноутбук из комнаты в 23:30.

*The card now ends with the verdict and has no buttons:*

```
<b>Сегодня · чт 3 сен · закрыт</b>
<code>kcal  1 885 / 2 000   ▓▓▓▓▓▓▓▓</code>
<code>P       162 /   210g  ▓▓▓▓▓▓░░</code>
<code>C        77 /    75g  ▓▓▓▓▓▓▓▓</code>
<code>F       103 /   105g  ▓▓▓▓▓▓▓▓</code>
<code>fiber    17 /    30g  ▓▓▓▓▓░░░</code>

<b>Еда</b>
• 09:10 завтрак — яйца ×3, авокадо ½ · 327
• 13:40 обед — Chicken Shawarma Bo… · 612
• 20:20 ужин — кофта, брюссельская капуста, творог 0.5% · 946
<b>Тренировка</b>: running · 45 min · strain 12.1 · 406 kcal · avg HR 130
<b>Сон</b>: 6h10 · 78% · восстановление 61%
<b>Вердикт</b>: Закрыт на 1 885 / 162 Б / 17 клетчатки. Белок −48. Отбой к 00:30.
```

**00:00 · Strikt** *(`bedtime_minus_30`, exempt from quiet hours; the window is Thursday's night even
though the clock says Friday)*: 00:00. Ноутбук из комнаты.
Что открыто такого, что не подождёт до утра?

## 5. Next morning — Friday 4 September

**08:15 · Strikt** *(`morning_line`, wake + 15; recovery and sleep from the overnight WHOOP
sync)*: Восстановление 74 %. Сон 7ч05, встал 08:02. Вчера: 1 885 / 162 Б, закрыт в 22:40.
План на сегодня — завтрак, обед, ужин: что и примерно когда?

**08:20 · Ilya:** Напомни, что я вчера ел?

**08:20 · Strikt** *(get_history → kinds meals, 2026-09-03)*:
Четверг, 3 сентября — три приёма, 1 885 ккал:
09:10 завтрак — яйца ×3, авокадо ½ · 327
13:40 обед — Chicken Shawarma Bowl (Krave) · 612
20:20 ужин — кофта 300 г, брюссельская капуста 150 г, творог 200 г · 946
За день 162 Б / 77 У / 103 Ж / 17 клетчатки. Белок −48 к цели — это и есть задача на сегодня.
План?

*Nothing was re-read from the chat: the answer comes from the `meals` and `meal_items` rows the
day before wrote. On day 200 the same question about day 1 is the same tool call.*
