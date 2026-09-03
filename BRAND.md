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
- Four cuts, same angle. **Full** (`brand/logo/mark.svg`) at 64 px and above: stroke 9, gap 10,
  strike 9 at 28°, overshoot 4.5. **Small** (`mark-small.svg`, `favicon.svg`) at 48 px and below, the
  avatar: stroke 8.5, gap 11, overshoot 6. **Tiny**, for a 32 px raster (`favicon-32.png`): drawn in
  device pixels — 3 px strokes on whole-pixel edges, 2 px gaps, a **2 px** strike, box 81 % of the
  side. **Micro**, for the 16 px raster a browser tab actually paints (`favicon-16.png`): 2 px
  strokes on whole-pixel edges, 2 px gaps, 2 px strike, no overshoot. The two pixel cuts are the one
  place the gap drops under 1.1 × stroke: at these sizes what matters is two clean background pixels
  between strokes, and a wider gap would force strokes that vanish. Their strike is thinner than the
  verticals for the same reason — a 3 px pen at 32 px bridged two bars over four rows and made the
  centre a block; at 2 px it joins one pair of bars over two rows and the other two gaps stay open.
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

The avatar is the exception: it is always the full mark. Telegram holds one uploaded picture per bot
and cannot repaint it through the day, so the state lives in the card, not in the profile photo —
which is why `hero-1920x1080.png` shows a three-bar mark beside a struck 40 px avatar and says so.

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
- **Numbers — JetBrains Mono** 400 (500 for a total line). Tabular figures, labels uppercase at
  0.08em. **Tables and totals are mono**: the Today card's five macro rows, a reply's per-item block,
  its meal / today / left rows. **Numbers inside a sentence follow the sentence** — the Left line,
  the verdict, a ladder push and a chat-list preview are DM Sans (Golos Text in Russian), because
  they are prose. This is what `telegram/render.py` writes and what the images show.
- Thousands are separated, and by two different characters for one reason: inside a `<code>` block a
  thin space is 0.31 of a cell in JetBrains Mono, so a value past 999 would drag its bar a third of a
  character left of the others. Mono columns therefore use a **figure space** U+2007, which is
  digit-width by definition (`render._cells`); prose keeps the **thin space** U+2009
  (`render.fmt_num`). Both read as `1 880`.
- Russian macro columns are **Б · Ж · У** (protein · fat · carbs), the order a Russian reader says
  and reads; Б · У · Ж would be a transliteration of P · C · F. English keeps P · C · F. Both live in
  `telegram/copy.py` (`card.remaining`, `card.over`) and in `russian-1920x1080.png`.
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
device frame; set body copy in the serif; use mute for body text or below 14 px; put red anywhere but
the mark inside one composition; stretch, outline or recolour the mark; use the full cut under 48 px.

The lock-up is the one exception to that last rule: its mark is the full cut thinned to stroke 8
(section 8), and that thinned geometry is used at every lock-up size, including the 34 px footer
lock-up on the images. The mark alone still follows the three cuts of section 2.

## 8. Lock-up

The mark sits in the ascender box of the wordmark: top level with the top of the k, bottom on the
baseline. Inside the lock-up the strokes are thinned to 8 (89 % of the full cut) so they sit near the
wordmark's stems; the gap from the strike tip to the s is half a cap height. That thinned full-cut
geometry is the lock-up's own, used at any lock-up size — the 48 px floor in section 7 governs the
mark on its own, not the lock-up. Files:
`brand/logo/lockup-light.svg`, `lockup-night.svg` (self-contained: the Newsreader file is embedded,
the mark is paths). In HTML use `StriktMark.lockupHTML({size, night, sans})`.

## 9. Telegram setup

Bot name: **Strikt**. Username as registered (not part of the brand).

Avatar: upload `brand/avatar/avatar-512.jpg` (512 × 512, JPG, paper baked in; the mark occupies 67 % of
the side and stays inside Telegram's circle crop — the farthest ink is 168 px from the centre, 66 % of
the radius, i.e. 33 % of the diameter, inside the 74 % safe circle with 21 px to spare).

1. BotFather → `/setuserpic` → choose the bot → send `avatar-512.jpg` **as a photo**, not as a file.
2. Or from code (Bot API 9.4+): `setMyProfilePhoto` with `InputProfilePhotoStatic` and the JPG as a new
   upload (profile photos cannot be reused by `file_id`). `avatar-1024.png` is the master; the night
   variant is for the brand sheet only, the one JPG serves light and dark clients.

Texts (BotFather `/setabouttext`, `/setdescription`; or `setMyShortDescription` /
`setMyDescription` with `language_code="ru"` for the Russian pair). These four strings and the
command list below are the same bytes the bot registers at startup: they live in
`src/strikt/telegram/copy.py` as `bot.short`, `bot.description` and `cmd.*`, and
`tests/test_brand_copy.py` fails if this section and that table drift apart.

About, en (76 chars, limit 120):

> A coach in one chat. Send food, get the number. The day ends with a verdict.

About, ru (80 chars):

> Тренер в одном чате. Присылай еду — получай цифру. День заканчивается вердиктом.

Description, en (466 chars, limit 512):

> Strikt logs food, training, sleep and measurements from one Telegram chat. Send a photo, a screenshot, a voice note or text; the reply is kcal, protein, carbs, fat and fiber per item, the day so far and what is left. The Today card stays pinned and is edited in place. When you go quiet it writes first: the fact, then the pattern from your own data, then an instruction with a deadline. The day closes with a verdict. No greetings, no emoji, no praise. Invite-only.

Description, ru (448 chars):

> Strikt записывает еду, тренировки, сон и замеры из одного чата в Telegram. Пришли фото, скриншот, голосовое или текст — в ответ ккал, белки, углеводы, жиры и клетчатка по каждому пункту, итог дня и остаток. Карточка дня закреплена и правится на месте. Если ты замолчал, пишет первым: факт, затем закономерность из твоих же данных, затем указание со сроком. День закрывается вердиктом. Без приветствий, без эмодзи, без похвал. Доступ по приглашению.

Commands (`/setcommands` or `setMyCommands`, en and ru):

```
start - Begin, or resume where you left off
today - Re-post the Today card
forget_me - Delete everything about you
```
```
start - Начать или продолжить с того же места
today - Заново отправить карточку дня
forget_me - Удалить всё о тебе
```

The chat itself stays in Telegram's own theme; the brand lives in the avatar, the card format and
the voice. `telegram-profile-1920x1080.png` is painted in that theme and not in the palette above —
white rows on `#F1F1F1`, `#212121` rows on `#181818`, secondary text `#707579` / `#AAAAAA` — and the
contact avatars carry Telegram's own initial colours: client chrome, not a second brand accent. The
point of the image is that the 40 px paper mark holds up inside someone else's window, so the cool
tints sit next to it and the two warm ones sit at the bottom of the list. The same image shows the one paper JPG on both a light
and a dark client, because that is what Telegram does with it; the night mark
(`avatar-night-512.png`, `mark-night.svg`) is for the brand sheet only.

## 10. Landing page

The page sits on a black-and-white site, so the brand appears only inside images. Ship
`brand/images/og-1200x630.png` as the Open Graph card (lock-up plus one ink line — the middle
sentence of the BotFather About text, "Send food, get the number.", set in ink because a feed renders
the card at roughly half size and mute grey drops out there), and the 1920 × 1080 images as content.
Icons: `favicon.svg`, `favicon-32.png` and `favicon-16.png` are all ink — the SVG is the small cut,
the two PNGs the tiny and micro cuts of section 2; link the rasters with explicit sizes
(`<link rel="icon" sizes="16x16" href="favicon-16.png">`) so a tab paints the hand-fitted 16 px file
instead of downsampling the 32. `favicon-180.png` (apple touch) is the small cut with the red strike
on a paper ground. No page CSS carries the palette.

## 11. Regenerating the assets

```
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers /opt/node22/bin/node brand/render.mjs          # everything
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers /opt/node22/bin/node brand/render.mjs hero og  # a subset
/opt/node22/bin/node brand/render.mjs --list
```

`render.mjs` writes the logo SVGs from `brand/src/mark.js`, opens each `brand/src/*.html` (and
`brand/sheet.html`) in headless Chromium with the bundled fonts, screenshots it at the listed size, and
fails if any text fell back to a system font. The font check is glyph-level: over a CDP session it
asks Chromium which platform fonts it actually rasterised each text element with
(`CSS.getPlatformFontsForNode`), so one character outside a subset's `unicode-range` — an arrow, a ≥,
a ✓ — fails the build instead of shipping as DejaVu. No network is needed. Set `PLAYWRIGHT_MODULE` if
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
