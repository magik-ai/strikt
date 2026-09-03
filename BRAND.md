# Strikt — brand

The system in words, for a human and for the code. The assets live in `brand/`; the numbers behind
them are in `brand/BRAND-NOTES.md`.

## 1. Name

"Strict" with one letter changed. English ears hear *strict*; it is the plain word for strict in
German, Dutch and the Scandinavian languages; Russian readers see «стрикт». The changed letter is the
k, and the word contains *strike*: the product counts what you eat and closes the day with a stroke
through the count. Written **Strikt** in prose and in the bot's name; the wordmark is lowercase
*strikt*.

## 2. The mark — the tally

Four vertical round-capped strokes in ink and one diagonal strike in red: the oldest way people log
a count. Each stroke is a mark you made; the fifth closes the group. The red is the pen a strict
coach corrects with. The bot's card has five rows (kcal, P, C, F, fiber); the five strokes are the
table.

Rules:

- The strike always rises from bottom-left to top-right. The other direction is a prohibition sign;
  never draw it.
- Four verticals, always. Three or fewer without a strike is the *open day* (see the state rule);
  a strike over fewer than four bars does not exist.
- Two cuts. **Full** (`brand/logo/mark.svg`) at 64 px and above: stroke 9, gap 10, strike 9 at 28°,
  overshoot 4.5. **Small** (`mark-small.svg`) at 48 px and below, the avatar and favicons: stroke 8.5,
  gap 11, overshoot 6. Same angle.
- Colourways: ink + red (`mark.svg`), all ink (`mark-ink.svg`, for the favicon and single-colour
  print), night (`mark-night.svg`: text-dark strokes, strike-dark strike, strike width equal to the
  verticals because red drops visually on dark).
- Point-symmetric about the centre: geometric centre = optical centre, so it sits in a circle crop
  without nudging.
- Never mirrored, never rotated, never all red, never outlined, never with a second colour beside it,
  never inside a badge or a ring.
- Clear space: one stroke pitch (19 units) on every side.

### The state rule (the one permitted motion)

The mark is the day. One vertical per meal logged today — breakfast, lunch, dinner, snack, the four
slots the bot knows — drawn left to right, four at most. The red fifth stroke is drawn only when the
day is closed: it is the verdict. So an image of an open day shows one to four bars and no strike
(`brand/images/hero-1920x1080.png`, three meals in); a closed day shows the full mark
(`card-closed-1920x1080.png`). If the mark ever animates, the only motion is the strike being drawn
along its length (200 ms, `cubic-bezier(0.2, 0, 0, 1)`) after the fourth bar; nothing pulses, nothing
loops. In code: `StriktMark.svg({bars: n, strike: closed})` from `brand/src/mark.js`.

## 3. Colour

One accent per composition apart from the mark. In the shipped images red appears only in the mark.

Light ("paper" world, the default):

| Token | Hex | Use | Contrast |
|---|---|---|---|
| paper | `#F6F2E9` | image and page ground | — |
| card | `#FFFCF5` | bubbles, cards, panels | — |
| rule | `#E3DDD1` | hairlines, user bubbles, bar track | — |
| mute | `#8A857A` | captions and timestamps, 14 px and up; never body text | 3.3 on paper |
| ink | `#1A1814` | text, the four strokes | 15.9 on paper, 17.3 on card |
| strike | `#D3392B` | the fifth stroke; as text only at 18 px+ or bold | 4.3 on paper |
| strike-deep | `#B32E22` | red as small text, pressed states | 5.6 on paper |
| strike-soft | `#F5D6D1` | tinted chip, the track under a red fill | — |

Night (dark clients, dark image variants):

| Token | Hex | Use | Contrast |
|---|---|---|---|
| night | `#161513` | ground | — |
| night-card | `#201E1A` | bubbles | — |
| rule-dark | `#35322C` | hairlines, user bubbles | — |
| text-dark | `#EFEAE0` | text, the strokes | 15.2 on night |
| strike-dark | `#F0604E` | the fifth stroke, red text | 5.6 on night, 5.1 on night-card |
| mute-dark | `#9B968A` | captions on night (added for the images) | 6.2 on night |

Ratios are WCAG 2.x, computed in `brand/BRAND-NOTES.md`. Mute fails AA for body on purpose: it is a
caption colour.

## 4. Type

Three roles, all OFL and bundled in `brand/fonts/`:

- **Display — Newsreader** 400/500, optical size 72, letter-spacing −0.01em, leading 1.05–1.1.
  Headlines, pull quotes, the wordmark. 36 px and up. Never for body copy inside Telegram images.
- **UI and body — DM Sans** 400/500/600. Body 16/1.55, UI 14/1.4. The alternate wordmark
  (DM Sans 500, −0.02em) is shipped on the brand sheet; Newsreader is primary.
- **Numbers — JetBrains Mono** 400 (500 for a total line). Tabular figures, thin-space thousands
  (`1 940`), labels uppercase at 0.08em. Every number the bot writes is mono; the number is the
  product.
- Cyrillic: DM Sans and Newsreader have none. Russian UI text is set in **Golos Text** 400/500
  (Paratype, OFL), including the Latin food names inside a Russian sentence. JetBrains Mono covers
  Cyrillic itself.

## 5. Spacing, radius, elevation, motion

- Base 4 px. Scale 4 · 8 · 12 · 16 · 24 · 32 · 48 · 64 · 96. Section gaps in images 48–64 px at 1×.
- Radius: 8 controls, 12 inputs, 16 bubbles and cards, 24 large cards and panels, pill for tags.
- No drop shadows. Elevation is a surface step (paper → card) plus a 1 px rule. If a shadow is
  unavoidable: `0 1px 3px rgba(20,20,19,0.08)`.
- Motion: 150 ms fast, 200 ms base, `cubic-bezier(0.2, 0, 0, 1)`, opacity and transform only. The
  only animated element is the strike (section 2). No parallax, no gradients, no sweeps.

## 6. Illustration and product shots

- Illustration is a single-weight line drawing, ink on paper: 2 px at 24 px, 1.5 px at 16 px, round
  caps and joins, at most one flat red fill. Loose curves are fine in illustrations; the mark stays
  geometric. Example: the plate in `food-reply-1920x1080.png`.
- Icons on a 24 px grid, 2 px stroke, ink; no filled icons.
- No 3D, no gloss, no stock photography of people. Photography, if ever, is still life of real food
  in natural light.
- Product is shown as flat cards on paper: Telegram bubbles with their avatar, never a phone bezel.
- Copy inside images follows the bot's voice: fact first, no greeting, no emoji, no exclamation marks,
  no praise, numbers before words. Lowercase captions are fine; the product name in prose is Strikt.

## 7. Do / don't

Do: ink on paper, one red, the strike rising left to right; the mark in the ascender box of the
wordmark; mono numbers; captions in mute; cards flat on paper.

Don't: strike the other way; add a second accent; use emoji or praise in copy; put the chat in a
device frame; set body copy in the serif; use mute for body text; put red anywhere but the mark
inside one composition; stretch, outline or recolour the mark; use the full cut under 48 px.

## 8. Lock-up

The mark sits in the ascender box of the wordmark: top level with the top of the k, bottom on the
baseline. Inside the lock-up the strokes are thinned to 8 (89 % of the full cut) so they sit near the
wordmark's stems; the gap from the strike tip to the s is half a cap height. Files:
`brand/logo/lockup-light.svg`, `lockup-night.svg` (self-contained: the Newsreader file is embedded,
the mark is paths). In HTML use `StriktMark.lockupHTML({size, night, sans})`.

## 9. Telegram setup

Bot name: **Strikt**. Username as registered (not part of the brand).

Avatar: upload `brand/avatar/avatar-512.jpg` (512 × 512, JPG, paper baked in; the mark occupies 67 % of
the side and stays inside Telegram's circle crop with the farthest ink at 66 % of the diameter).

1. BotFather → `/setuserpic` → choose the bot → send `avatar-512.jpg` **as a photo**, not as a file.
2. Or from code (Bot API 9.4+): `setMyProfilePhoto` with `InputProfilePhotoStatic` and the JPG as a new
   upload (profile photos cannot be reused by `file_id`). `avatar-1024.png` is the master; the night
   variant is for the brand sheet only, the one JPG serves light and dark clients.

Texts (BotFather `/setabouttext`, `/setdescription`; or `setMyShortDescription` /
`setMyDescription` with `language_code="ru"` for the Russian pair):

About, en (76 chars, limit 120):

> A coach in one chat. Send food, get the number. The day ends with a verdict.

About, ru (80 chars):

> Тренер в одном чате. Присылай еду — получай цифру. День заканчивается вердиктом.

Description, en (462 chars, limit 512):

> Strikt logs food, training, sleep and measurements from one Telegram chat. Send a photo, a screenshot, a voice note or text; the reply is kcal, protein, carbs, fat and fiber per item, the day so far and what is left. A day card stays pinned and is edited in place. When you go quiet it writes first: the fact, then the pattern from your own data, then an instruction with a deadline. The day closes with a verdict. No greetings, no emoji, no praise. Invite-only.

Description, ru (448 chars):

> Strikt записывает еду, тренировки, сон и замеры из одного чата в Telegram. Пришли фото, скриншот, голосовое или текст — в ответ ккал, белки, углеводы, жиры и клетчатка по каждому пункту, итог дня и остаток. Карточка дня закреплена и правится на месте. Если ты замолчал, пишет первым: факт, затем закономерность из твоих же данных, затем указание со сроком. День закрывается вердиктом. Без приветствий, без эмодзи, без похвал. Доступ по приглашению.

Commands (`/setcommands` or `setMyCommands`, en and ru):

```
start - Begin, or resume where you left off
today - Re-post the day card
forget_me - Delete everything about you
```
```
start - Начать или продолжить с того же места
today - Заново отправить карточку дня
forget_me - Удалить всё о тебе
```

The chat itself stays in Telegram's own theme; the brand lives in the avatar, the card format and
the voice.

## 10. Landing page

The page sits on a black-and-white site, so the brand appears only inside images. Ship
`brand/images/og-1200x630.png` as the Open Graph card, the 1920 × 1080 images as content, and
`brand/logo/favicon.svg` / `favicon-32.png` / `favicon-180.png` (ink; the red version only inside
raster images). No page CSS carries the palette.

## 11. Regenerating the assets

```
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers /opt/node22/bin/node brand/render.mjs          # everything
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers /opt/node22/bin/node brand/render.mjs hero og  # a subset
/opt/node22/bin/node brand/render.mjs --list
```

`render.mjs` writes the logo SVGs from `brand/src/mark.js`, opens each `brand/src/*.html` (and
`brand/sheet.html`) in headless Chromium with the bundled fonts, screenshots it at the listed size, and
fails if any text node fell back to a system font. No network is needed. Set `PLAYWRIGHT_MODULE` if
playwright is not at `/opt/node22/lib/node_modules/playwright`. To change copy or numbers in several
images at once, edit `brand/src/gen-sources.py`, run it, then render.

Layout: `brand/logo/` marks, favicons and lock-ups · `brand/avatar/` · `brand/images/` ·
`brand/src/` one HTML source per image plus `mark.js` and `strikt.css` · `brand/fonts/` ·
`brand/sheet.html` + `sheet.png` the brand sheet · `brand/BRAND-NOTES.md` the numbers.

## 12. Licences

Newsreader (Production Type), DM Sans (Colophon Foundry), JetBrains Mono (JetBrains) and Golos Text
(Paratype) are under the SIL Open Font License 1.1; the files, copyright lines and sources are listed
in `brand/fonts/LICENSE.md`. The OFL allows bundling and embedding (the lock-up SVGs embed Newsreader)
and forbids selling the fonts on their own. The mark, the wordmark composition and the images are part
of this repository and follow its licence.
