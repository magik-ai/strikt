# Strikt brand — the numbers behind every decision

Companion to `/BRAND.md` (the system in words). Everything here is what the files actually contain;
`src/mark.js` is the single source for the geometry and `render.mjs` regenerates every raster from it.

## 1. The mark

Canvas `viewBox 0 0 100 100`. Every stroke is emitted as a filled capsule path (two straight edges,
two semicircular caps), so the SVGs carry no `stroke` attributes and no font dependency.

### Full cut (`mark.svg`, `mark-ink.svg`, `mark-night.svg`; use at 64 px and above)

| Item | Value | Why |
|---|---|---|
| Vertical stroke width `w` | 9 | judges' fix: 10 read as a barcode at 96 px |
| Gap between strokes | 10 = 1.11 w | brief: ≥ 1.1 w so the four stay separate under ink spread |
| Pitch | 19 | w + gap |
| Stroke centres x | 21.5 · 40.5 · 59.5 · 78.5 | 50 ± 9.5, 50 ± 28.5 |
| Stroke ends y (cap centres) | 19.5 → 80.5 | ink from 15 to 85 |
| Group outer edges | 17 → 83 | stroke centre ± 4.5 |
| Strike width | 9 (night: 9, i.e. equal to the verticals) | equal-width on paper; on night the red drops visually, so the night file keeps it at the vertical width too — the full cut shares the value because 9 was already the vertical width; the rule matters for the small cut and any thinned variant |
| Strike angle | 28.0° from horizontal, bottom-left up | inside the 27–29° window; tan 28° = 0.5317 |
| Strike overshoot | 4.5 beyond each outer edge (cap centre) | reads as a pen leaving the count, not a border |
| Strike cap centres | (12.5, 69.94) → (87.5, 30.06) | dx 75, dy 39.88 |
| Ink extremes | x 8 → 92, y 15 → 85 | box 84 × 70, point-symmetric about (50, 50) |

Reading checks done with the Read tool: at 400 px and 96 px the mark reads as a count crossed by a
pen; the four verticals do not fuse; the red does not read as a prohibition sign because the stroke
rises left to right and overshoots by less than one stroke width beyond the group.

### Small cut (`mark-small.svg`, `favicon.svg`, the avatars; use at 48 px and below)

| Item | Value |
|---|---|
| Stroke width | 8.5 |
| Gap | 11 (1.29 w) · pitch 19.5 |
| Stroke centres x | 20.75 · 40.25 · 59.75 · 79.25 |
| Strike | width 8.5, 28°, overshoot 6, cap centres (10.5, 71.0) → (89.5, 29.0) |
| Ink extremes | x 6.25 → 93.75, y 15.25 → 84.75 |

Rendered at 40 px on a 1× screen (`avatar-512.jpg` scaled by the browser): stroke 2.3 px, gap 3.0 px,
the four verticals stay separate on paper and on night (checked at 1× and 3×).

### Tiny cut (`favicon-32.png`; the 32 px raster)

Drawn in device pixels on a 32 px canvas and converted at 3.125 units per pixel, so every edge lands
on a whole pixel and nothing is antialiased into grey:

| Item | Device px (at 32) | Units |
|---|---|---|
| Stroke width | 3 | 9.375 |
| Gap | 2 | 6.25 · pitch 15.625 |
| Stroke centres x | 8.5 · 13.5 · 18.5 · 23.5 (strokes 7–10, 12–15, 17–20, 22–25) | 26.5625 · 42.1875 · 57.8125 · 73.4375 |
| Stroke ends y (butt-cap ends) | ink 4 → 28 (rows 4–27) | 17.1875 → 82.8125 |
| Strike | width **2**, 28°, overshoot 2.5 | 6.25 · over 7.8125 |
| Ink box | 26 × 24 = 81 % × 75 % of the side | — |
| Caps | **butt** | rectangles, not capsules |

The gap is 0.67 × the stroke, the only cut that breaks the ≥ 1.1 rule: at 32 px what matters is two
whole background pixels between strokes.

The caps are **butt**, not round. `mark.js` draws both pixel cuts with `bar()` (a rectangle whose
ends are square, extended by w/2 along the axis so the ink covers the same extent a capsule would)
instead of `capsule()`. With round caps the top and bottom rows of every stroke were half-alpha —
the cap is a semicircle, so it covers a fraction of its last row — which made the 3 px bars read as
fuzzy-tipped 2 px bars and made "whole-pixel edges" true horizontally only. Read off the current
PNG: rows 4–27 are solid `###` in every stroke column, rows 3 and 28 are empty.

The strike is the one place the tiny cut departs from the geometry as well: 2 px, not the 3 px of the
verticals. At 3 px the pen covered 3.4 rows at every x, so where it crossed a 2 px hole it bridged
two bars for four rows and the alpha map read `###..###..########` at row 12 and `#########` at rows
17–19 — an ink block in the middle of the icon. At 2 px **the pen bridges each gap for one or two
rows and no gap for more than two**: read off the current PNG, gap 3–4 is closed at row 13 (cols
17–24 solid) with row 12 partial, gap 2–3 at rows 15–16, gap 1–2 at row 18 (cols 7–14 solid) with
row 19 partial; rows 4–11 and 23–27 carry four separate runs. A pen crossing a count has to touch it;
what it must not do is fill the count in. (An earlier note claimed it "joins one pair of bars and the
other two gaps stay open" — the raster bridges all three, briefly, and that is what it should say.)

### Micro cut (`favicon-16.png`; the 16 px raster a tab actually paints)

The browser downsamples a 32 px icon to 16 px for tabs and bookmarks, which turns 3 px strokes and
2 px gaps into grey. So the 16 px raster is drawn at 16 px, at 6.25 units per pixel:

| Item | Device px (at 16) | Units |
|---|---|---|
| Stroke width | 2 (cols 1–2, 5–6, 9–10, 13–14) | 12.5 |
| Gap | 2 (cols 3–4, 7–8, 11–12) | 12.5 · pitch 25 |
| Stroke centres x | 2 · 6 · 10 · 14 | 12.5 · 37.5 · 62.5 · 87.5 |
| Stroke ends y | ink 2 → 14 (rows 2–13, butt caps) | 12.5 → 87.5 |
| Strike | **a staircase, not a rotated stroke** | four 4 × 2 px blocks |
| Strike blocks (x, top row) | (0, 10) · (4, 8) · (8, 6) · (12, 4) | 4 px wide, 2 px tall |
| Ink box | 14 × 12 = 88 % × 75 % of the side | — |

`mark.js` special-cases this cut (`MICRO_PX`): it emits eight axis-aligned rectangles on whole device
pixels and nothing else, so the 16 px PNG has **no antialiased pixel at all**. Read off the current
file, rows 2–13 are fully opaque in every bar column and rows 0–1 and 14–15 are empty.

A rotated 2 px stroke does not survive here: at 16 px it covers roughly eight rows of partial alpha
across the three gaps and reads as a smudge between the bars rather than a pen. The staircase steps
once per gap — the step from row 10 to 8 falls in gap 1–2, 8 to 6 in gap 2–3, 6 to 4 in gap 3–4 — so
it crosses all four bars and lands on the two 1 px overshoots at columns 0 and 15. Slope: 6 rows over
12 px between block centres = **26.6°**, half a degree outside the 27–29° window every other cut
sits in, and the one place the angle is allowed to move because the alternative is grey.

Link it explicitly (`<link rel="icon" sizes="16x16">`); browsers only pick it over the 32 if told.

### Night colourway

Strokes `text-dark #EFEAE0`, strike `strike-dark #F0604E`, strike width = stroke width. In the
`data-night` path of `mark.js` the strike width is forced to the vertical width for every cut.

### Day-state rule (the one motion)

`bars` 1–4 = meals logged today (breakfast, lunch, dinner, snack are the four slots in `copy.py`),
drawn left to right at the full-cut positions; the strike exists only when the day is closed.
`StriktMark.svg({bars: n, strike: false})` draws an open day; `{bars: 4}` draws the closed mark.
Animation, if ever used: strike drawn along its length, 200 ms, `cubic-bezier(0.2, 0, 0, 1)`; bars fade
in over 150 ms; nothing else moves.

## 2. Avatar and favicons

- `avatar-1024.png` / `avatar-512.jpg` (JPEG q 92, no alpha, paper baked in): small cut, mark box
  67 % of the side (343 px of 512), centred. Scale 3.43 px per unit.
- Farthest ink from the centre: the lower-left strike cap, 49.0 units = 168 px at 512. Measured back
  off `avatar-512.jpg` (darkest-pixel scan): 167.6 px at (108, 335) → **66 % of the radius, 33 % of
  the diameter**, inside the 74 % safe circle (r 189.4 px) with 21 px to spare. Top of the outer
  strokes: 46.5 units = 160 px. (An earlier note read "66 % of the diameter"; the ratio is of the
  radius.)
- `avatar-night-512.png`: same box, night colourway; for the brand sheet only — the one JPG serves light
  and dark clients because paper reads as a neutral disc on dark UIs, which is what
  `telegram-profile-1920x1080.png` now shows on both panels.
- `favicon.svg`: small cut, all ink, transparent. `favicon-32.png`: **tiny cut**, all ink, box 81 %
  (stroke 3 px, gap 2 px, strike 2 px, butt caps, pixel-snapped). `favicon-16.png`: **micro cut**,
  all ink, box 88 % (stroke 2 px, gap 2 px, a four-block staircase for the strike, no antialiasing). `favicon-180.png` (apple touch): small cut with the
  red strike, paper background, box 72 %.

## 3. Lock-up

Metrics of Newsreader 500 (opsz 72) measured in Chromium at 100 px: k ascender 75, cap height 72,
x-height 52, left side bearing of "s" 3, font ascent 74, descent 27.

- Mark visible height (70 units) = ascender of the k → 1.0714 px per unit at 100 px; the mark's bottom
  (unit 85) sits on the baseline; its top (unit 15) is level with the top of the k.
- Strokes thinned to 8 units (89 % of the full cut), pitch kept at 19 (gap 11), strike 8.
- Gap from the strike tip (unit 91) to the ink of the "s": 0.5 × cap height = 36 px at 100 px; the
  wordmark origin is therefore at x = 91 × 1.0714 + 36 − 3 = 130.5 px from the mark's left edge.
- Strike tip top at unit 26 → 26 px below the ascender line, so nothing hangs into the s.
- Wordmark: "strikt", Newsreader 500, opsz 72, letter-spacing −0.01em; DM Sans 500 (−0.02em) alternate
  uses its own metrics (k asc 72, cap 70, s bearing 4).
- `lockup-light.svg` / `lockup-night.svg`: viewBox `0 0 375 100`, baseline y 87.5, the Newsreader
  woff2 (132 kB, latin subset) embedded as a base64 `@font-face`; total 177 kB each.

## 4. Colour and contrast (WCAG 2.x ratios)

| Pair | Ratio | Use |
|---|---|---|
| ink #1A1814 on paper #F6F2E9 | 15.9 | text |
| ink on card #FFFCF5 | 17.3 | text in bubbles |
| mute #8A857A on paper | 3.3 | captions ≥ 14 px, timestamps; never body |
| mute on card | 3.6 | same |
| strike #D3392B on paper | 4.3 | ≥ 18 px or bold only (AA large) |
| strike-deep #B32E22 on paper | 5.6 | red as small text |
| paper on strike | 4.3 | filled red button, ≥ 18 px text |
| text-dark #EFEAE0 on night #161513 | 15.2 | text |
| text-dark on night-card #201E1A | 13.9 | text in bubbles |
| mute-dark #9B968A on night | 6.2 | captions |
| strike-dark #F0604E on night | 5.6 | red text and the strike on night |
| strike-dark on night-card | 5.1 | same |

`mute-dark #9B968A` is not in the brief's palette; it is the night caption colour used in the images
(the brief lists no night secondary). One accent per composition apart from the mark: the images use
red nowhere but in the mark.

## 5. Type sizes used in the images (1920 × 1080 canvases at 1×)

| Role | Face | Size / leading | Tracking |
|---|---|---|---|
| Captions | JetBrains Mono 400 | 15 / 1.6, uppercase | 0.08em |
| Bubble text | DM Sans 400 (bold 600) | 24 / 1.4 | 0 |
| Numbers in bubbles | JetBrains Mono 400 (500 for the meal total) | 22 / 1.55 on the day card; 24 in the food, Russian and menu replies, where mono and prose are the same size, as Telegram renders a `<code>` block | 0 |
| Size label under a mark | JetBrains Mono 400 | 14 | 0.04em |
| Timestamps | JetBrains Mono 400 | 14 | 0.02em |
| Inline buttons | DM Sans 500 | 19 | 0 |
| Wordmark in the foot | Newsreader 500 | 34 | −0.01em |
| System sheet display sample | Newsreader 500 | 40 / 1.08 | −0.01em |
| og line | DM Sans 400, ink | 28 | 0 |

Nothing set in `mute` is under 14 px. On `system-1920x1080.png` the do/don't labels and the
mark-construction column were 11.5 and 13.5 px; both are 14–15 px now, which is why the do/don't
became five rows (tile plus one line) instead of five narrow columns with five-line labels. The
og line is ink, not mute: a feed renders the 1200 × 630 card at roughly half size, and 3.3 : 1 grey at
an effective 14 px drops out of the card.

Russian text: DM Sans and Newsreader ship no Cyrillic, so Russian UI copy is set in Golos Text
400/500 (OFL, Paratype), including the Latin food names inside Russian sentences so a line has one
texture. JetBrains Mono covers Cyrillic itself. The full JetBrains Mono build (not the Google subset)
is bundled, and the images now set the card's bar as the **glyph string `render.bar` actually
writes** — `▓▓▓▓▓░░░`, U+2593 and U+2591, one cell each in JetBrains Mono (measured: 60 px of a 60 px
cell at 100 px, same as a digit). It was drawn as flat CSS cells before, on the grounds that the
shade glyphs dither; they do, and that is the point — the dither is what a Telegram client shows, and
an image that draws a cleaner bar promises a card the bot cannot send. Checked at 22 px and 24 px:
`▓` and `░` are two clearly different greys and the eight cells sit on the mono grid. The `BAR_CSS`
block and its `<i>` cells are gone from `gen-sources.py`, and `rule` no longer paints a bar track —
the empty cells are `░` in ink, like the filled ones.

## 6. Card numbers (internally consistent)

Protocol: 2 100 kcal · 180 P · 200 C · 70 F · 30 fiber.

| Meal | kcal | P | C | F | fiber | Atwater check (4P + 4C + 9F) |
|---|---|---|---|---|---|---|
| 08:55 breakfast — skyr, oats, blueberries | 480 | 38 | 62 | 9 | 8 | 481 |
| 13:20 lunch — chicken thigh 180 g (325 · 41 · 0 · 17), rice 150 g (195 · 4 · 42 · 1), cucumber salad 120 g (70 · 1 · 4 · 5) | 590 | 46 | 46 | 23 | 3 | 575 |
| 16:40 snack — greek yogurt, walnuts | 270 | 21 | 12 | 15 | 2 | 267 |
| 19:50 dinner — grilled chicken plate | 540 | 52 | 42 | 18 | 6 | 538 |
| after lunch (food reply, russian) | 1 070 | 84 | 108 | 32 | 11 | left 1 030 · 96 · 92 · 38 · 19 |
| after snack (hero, 17:02; menu, 18:47) | 1 340 | 105 | 120 | 47 | 13 | left 760 · 75 · 80 · 23 |
| closed (card-closed, profile, sheet) | 1 880 | 157 | 162 | 65 | 19 | — |

Bars follow `render.bar`: `round(value / target × 8)` filled cells — after snack 5/5/5/5/3, closed
7/7/6/7/5.

The day runs 10:10 → 13:21 → 17:02 → 18:47 → 22:41 and every image is the same Thursday (3 September
2026 is a Thursday; the cards used to read "Wed 3 Sep", which is 2025), so the figures have to agree
across images:

- **The day chains.** The menu reply at 18:47 opens on **Left: 760 kcal · 75 P** — the state the hero
  card shows at 17:02 — and ends "Chicken plate. Nothing else on the list reaches 50 P." So the
  closed card's 19:50 dinner *is* that plate: 540 kcal · 52 P · 18 F from the menu row, plus 42 C and
  6 fiber (Atwater 538). The totals move with it — 1 880 / 157 P / 162 C / 65 F / 19 fiber — and the
  verdict reads "Closed at 1 880 / 157 P / 19 fiber. Protein short 23 g" (180 − 157 = 23). The
  dinner used to be salmon, potatoes, broccoli at 600, so a reader following the day watched the
  bot's own recommendation get ignored without comment.
- The food reply, the Russian reply and the sheet carry **four columns on every row** (kcal · P · C ·
  F). Fiber is in the prose line under the block — "Fiber 11 of 30", and 11 + 19 = 30 — not a fifth
  column that appears on the last three rows only and steps the table's right edge out.
- The closed card carries **no Left line**: once the verdict is written the day is over, and the
  verdict already says "Protein short 23 g". `render.render_day_card` skips the line when
  `state.closed`.
- The chat-list previews (sheet, profile) quote the **first line of the last message**, which is all
  a client can show: the verdict. A preview cannot start mid-message, so the sheet no longer previews
  the card's "Left:" row.

Menu image: protein per 100 kcal = P / kcal × 100 (52/540 → 9.6, 38/620 → 6.1, 31/690 → 4.5,
24/580 → 4.1, 44/1180 → 3.7). The rows are sorted by that ratio, so the column is monotonic and the
pick / okay / skip boundary follows it — caesar (4.5) is okay and falafel (4.1) is skip, the way
round the numbers actually argue. Every row carries a fat column and the brief's one line of why
(5.7). **The why never repeats a number already printed in its own row and never states one
ambiguously.** "52 of 75 P left" read as *52 of the remaining 75 are still left* rather than
*covered*, so the pick row says "leaves 23 P" (75 − 52). "deep fried, 24 P" repeated the 24 P two
columns to its left, so the falafel row says "deep fried" and lets its 29 F carry the case. The
burger's is "fries add 360" (620 → 980).

The delivery-app screenshot is client chrome, not brand: `#FFFFFF` card on radius 12, `#111111`
names and prices in DM Sans 500 with proportional figures, `#8E8E93` secondary, `#ECECEC` / `#F2F2F2`
separators, a `#F2F2F2` delivery chip, and 56 px photo placeholders drawn as three neutral greys
(`#EFEFEF` ground, `#E1E1E1` and `#D2D2D2` discs — a plate out of focus). No mono, no palette token
and no line illustration anywhere inside it: mono is the bot's table and the single-weight ink line
is the bot's drawing, and this tile has to read as another company's app. Only the bubble around it
is ours, because a user sent it — and, being a photo message, it is edge to edge in that bubble with
the 18:47 pill on top of it.

The staircase under the four bubbles carries the **same grid as the timeline above it**: the `.lad`
and `.stair` boxes are both x 112 → 1808, the four columns have a 434 px pitch (394 wide, 40 gap), so
a riser stands on the left edge of the column whose send it opens — 546 · 980 · 1414, measured off
the PNG at 545.5 · 979.5 · 1413.5 against column text starting at 547 · 981 · 1415. The svg is
`viewBox 0 0 1696 132`, `preserveAspectRatio="none"`, risers at 434 · 868 · 1302. Before, the path
used four equal 424-unit steps from 0, which put every riser 14, 24 and 34 px left of its own moment
and landed the third one in the gutter before its label. The staircase also moved up 14 px (`top`
650 → 636, height 150 → 160): bubbles end at 537, stair ink runs 660 → 784, the why row 810 → 835,
the footer 965 → 1000.

One more collision fixed there: the column tick was `.tick`, which is also `strikt.css`'s class for
the drawn delivery ticks (`margin-left: 6px`). Every tick was inheriting that margin and standing
6 px right of its own column. It is `.tk` now, and the ticks measure 112 · 546 · 980 · 1414.

Ladder timestamps 10:10 · 10:55 · 11:40 · 12:25 — 45 minutes apart (the follow-up delay in
`ladder.py`), starting at wake + 3 h. Step 2 quotes the brief (7.2) verbatim — "Two hours past your
usual first meal" — so the card moved rather than the sentence: breakfast is logged at **08:55**, and
08:55 + 2 h = 10:55 exactly. At the old 08:10 the line was 2 h 45 out, which is not "two hours".

**Two deliberate departures from the brief's wording in the ladder, both on purpose.**

1. Steps 1, 2 and 3 are the brief's sentences (7.2) verbatim. Step 4 is the brief's sentence with two
   numbers changed: it reads "Waist target is 90. You're at 97." where the brief writes 94 and 103.
   Those two figures are the first user's own waist measurements. CLAUDE.md law 3 keeps that person's
   numbers out of the repository, and the spec line "exact wording per brief section 7.2 examples"
   loses to it. The sentence is otherwise untouched, and no image in the set carries a personal body
   number — the protocol on the cards (2 100 kcal · 180 P · 200 C · 70 F · 30 fiber) is invented for
   the images too, not the first user's scheme. A reviewer diffing the image against the brief will
   see this and should stop here.
2. Step 2 prints "2 600 kcal" where the brief writes "2,600 kcal". That is house style, not a
   change of meaning: every number in the system uses a space separator, never a comma (section 8),
   and `render.fmt_num` writes it that way in the product too.

## 7. Spacing and radius in the images

Base 4 px. Stage padding 88 / 112 px; chat column 794–898 px; bubble padding 18 × 24 px, radius 16
with a 4 px corner on the tail side; avatar 40 px with the small cut at 67.5 % (27 px); cards and
panels radius 24; no shadows anywhere; user bubbles are `rule` on paper, bot bubbles `card` with a
1 px `rule` hairline.

Three edges that have to line up and did not:

- **One card, two states.** `hero` and `card-closed` are the only pair that shows the state rule, so
  they have to be the same frame twice. The bubble has a fixed width (844 px, the closed card's
  natural width) and a pinned top (240 px), so the card box is x 166 → 1010 in both; before, the
  bubble shrink-wrapped its longest line and the card changed width and position when the day closed.
  The state mark is one box too: both files draw a 260 px full-cut svg with the same −20.8 px left
  bearing, so bars 1–3 of the open day land on exactly the x of bars 1–3 of the closed mark —
  measured off the PNGs, ink from x 1258 in both, the closed frame adding bar 4 out to 1429 — and the
  block (mark plus caption, 348 px wide) is centred between the card's right edge and the 1808
  margin. The hero's caption carries the one thing the frame cannot show: the avatar is always the
  full mark, because Telegram holds one picture per bot; the state lives in the card.

- **One right edge per chat column.** A bot bubble sits behind a 40 px avatar and a 14 px gap, so its
  width is the column minus 54, not the column: 794 → 740 (food reply), 834 → 780 (russian). Both
  bubbles in a column end on the same vertical.
- **One left edge per image.** Captions, footers and the left edge of a left-aligned composition sit
  on the 112 px grid. The state mark is offset by the *closed* mark's left bearing (−20.8 px at
  260 px, 2.6 px per unit) so its strike cap sits on the caption's left edge — and the open frame
  keeps that same offset instead of its own, which is what puts its three bars in the closed frame's
  slots. The system sheet's footer sits at the shared baseline (y 982) with its do/don't column
  ending 77 px above it; it used to sit 21 px lower and 18 px under the last tile.

**The inline keyboard is detached, and the avatar ignores it.** Telegram draws an inline keyboard as
separate rounded buttons in a card tint below the bubble, and leaves the sender avatar on the
*bubble's* bottom edge; the keyboard hangs under it without moving it. So a row carrying a keyboard
is a two-row CSS grid (`.row.kbrow`, `grid-template-columns: 40px auto`, `align-items: end`): the
avatar and the bubble share row 1 and therefore end on the same y, and the keyboard has row 2 to
itself, 6 px lower, at the bubble's width. Buttons are radius 8, `card` on a 1 px `rule`, 14 px
padding. Measured in `food-reply`: bubble 469 → 866, avatar 826 → 866, keyboard 886 → 935. Before,
the keyboard was a bar fused to the bubble with a hairline and the avatar sat level with it, 45 px
below the bubble it belongs to.

**A photo fills its bubble.** `.bubble.photo` has zero padding, radius 16 with the 4 px tail corner,
and the picture is the bubble; the time and the ticks sit on the image bottom-right in a
`rgba(26,24,20,.55)` pill with paper-coloured text. It used to be a 12 px `rule` frame around the
picture with a padded caption row under it, which is a layout Telegram does not have. The menu
screenshot uses the same treatment, because a screenshot is a photo message: 420 px app card, edge to
edge, radius 0 inside the bubble's own 16.

Outgoing messages carry drawn delivery ticks — the ✓ glyph is not in the
DM Sans or Newsreader subsets, so they are a 17 × 11 SVG in `mute` at 1.6 px (paper on the photo
pill), matching the illustration rule.

**Bubble widths follow the mono block, not the canvas, and the block fits a phone.** A phone renders
a `<code>` block at roughly 35–45 columns, so a 53-column row is a message that wraps on the only
device the product runs on. Every table was re-cut to fit, by lifting the unit words out of every
cell into one header row:

| Block | Was | Now | Bubble at 24 px mono |
|---|---|---|---|
| food reply | 53 cells (`chicken thigh 180 g    325 kcal · 41 P ·   0 C · 17 F`) | **41** (`%-20s%6s%5s%5s%5s`, header `kcal P C F`) | 639 px = 41 × 14.4 + 2 × 24 |
| Russian reply | 55 | **42** (`%-21s%6s%5s%5s%5s`, header `ккал Б Ж У`) | 653 px |
| menu reply | 53 | **42** (line 1 `%-6s%-21s%5s` = 32, line 2 `  %5s kcal · %2s P · %2s F  %s`) | 653 px |
| sheet food reply | 53 at 12.5 px | **41** at 15 px | 397 px = 41 × 9 + 2 × 14 |

JetBrains Mono's advance is 0.6 em exactly (measured: 60 px at 100 px, digits, spaces, figure space
and the two shade glyphs alike), so a cell is 14.4 px at 24 px and 9 px at 15 px. The mono size in
the three reply images went 20–21 px → 24 px, the size of the prose beside it, which is how Telegram
renders a `<code>` block. Chat columns follow: 693 px (food), 707 px (Russian). The menu screenshot
and its reply are one centred composition, like the food reply, instead of a row stretched to the
1 696 px grid.

The Russian reply names the salad «огуречный салат» in both its prose line and its table, one cell
shorter than «салат из огурцов» and consistent with itself; the user's own message keeps the phrase
they typed, and the English food name stays English, which is the point of the image.

**The system sheet's third column and its small-cut row.** Every mark in the row now carries its own
size in 14 px mono under it — 64 · 48 · 40 · 32 on paper, 48 · 40 on night, then the 32 and 16 px
favicons — so a reader can tell which is which; the row's caption is one line of its own under it
("small cut · paper and night · favicon 32 · 16 · ink", 50 characters, inside the 55 the 564 px
column holds at 15 px with 0.08em tracking) instead of a `favicon 32 · 16 · ink` fragment beside the
marks that broke with `· ink` orphaned on a second line. Paying for the label row cost 21 px, so the
do/don't tiles went 56 → 52 px high and three gaps tightened by 2–6 px: the column now ends at 952.7
with the footer at 964.7, where it used to run under it.

The mono specimen on the same sheet quoted the **closed** totals with a `Left:` line under them —
a card state that cannot exist, because `render.render_day_card` skips that line when the day is
closed and the verdict carries the shortfall. It shows the open day instead (kcal 1 340 / 2 100,
P 105 / 180 g, bars 5/5), and its `Left: 760 kcal · 75 P · 80 C · 23 F` is set in DM Sans, which is
where the card sets it: prose, not a column.

**`telegram-profile` matches the rest of the set.** It was the one 1920 × 1080 frame without the
34 px lock-up and mono caption, and its two panels ran to y 1000 against a 212 px top margin. The
chat list lost one row (Deliveries), the stage's bottom padding went 80 → 180, and the panels now sit
184.7 → 900 with the standard footer at 964.7 → 1000, like every other image. Its section labels and
timestamps moved from tracked uppercase JetBrains Mono to DM Sans 500 / 400 at 15 and 14 px in
`#707579` / `#AAAAAA`, sentence case ("Info", "Chats"): Telegram sets both in its system sans, and
an image whose whole claim is "this is someone else's window" cannot set them in the brand's face.

`sheet.html` is one grid of two rows — the specimen band, then the applications band — 56 px columns,
64 px row gap. Every cell in the bottom row opens with its caption, so the three captions sit on one
baseline (y 695) instead of three (714 · 744 · 772) with 180 px of void above them.

Its captions are broken by hand rather than left to wrap, because an 11.5 px mono line at 0.06em
holds about 54 characters in the 400–440 px columns and three of them were orphaning a word:
"mark.svg · 420 px · ink … / strike …" now breaks at the middot before "full cut" and ends
"over 4.5"; "telegram · circle crop · 96 and 40 px" breaks before "paper · night"; "state · one
stroke per logged meal" breaks before "the strike when the day is closed"; and the filename in
"1 · 2 · 3 · 4 · closed · all-ink (mark-ink.svg)" is wrapped in `.nb { white-space: nowrap }` so it
can never break at its hyphen. Measured after: every caption on the sheet is one or two lines, and
every break is one that was chosen.

`telegram-profile-1920x1080.png` is painted in Telegram's own chrome, not in the palette: light
`#FFFFFF` rows with `#E9E9E9` separators and an `#F1F1F1` section gap; dark `#212121` rows on
`#181818`; secondary text `#707579` / `#AAAAAA`. That is the claim the image makes — the paper avatar
holding up inside someone else's window — so it has to be someone else's window. The panel height
follows its rows (it was a fixed 860 px, leaving an 85 px empty strip), and the contacts are ordered
so the cool client tints (blue, violet, green) sit next to the Strikt row and the two warm ones
(coral, pink) at the bottom, with the timestamps still descending the way a client sorts a list.

The plate in `food-reply-1920x1080.png` is a **boneless** chicken thigh — an irregular flat oval
with one skin line across it and three grain marks — a mound of rice with grain hatching, and five
overlapping single-ring cucumber slices with three seeds each. It carried a bone knuckle (two 7.5-unit
circles and two short lines off the top-right of the meat) until now, which drew a drumstick; the
message names a thigh, and the plate has to be the food the message names. It used to carry double-ring slices (life buoys) and a circle with a horizontal tail
between the mounds that read as a lollipop; a plate drawn under the illustration rule still has to
read as the food the message names.

## 8. Thousands separators, and why there are two

Measured in the render at 100 px, with `getBoundingClientRect` on a `white-space: pre` span:

| Character | DM Sans | Newsreader | Golos Text | JetBrains Mono |
|---|---|---|---|---|
| U+0020 space | 27 | 23 | 25 | 60 |
| U+00A0 no-break space | 27 | 23 | 25 | 60 |
| U+2009 THIN SPACE | 20 | 20 | 20 | 20 |
| U+202F NARROW NO-BREAK SPACE | **14** | **12** | 20 | 30 |
| U+2007 FIGURE SPACE | 68 | 60 | 50 | 60 |
| digit `8` | 61 | 60 | 50 | 60 |

So the thin space is the 1/5 em it is defined to be in every bundled face, and **one third of a cell**
in JetBrains Mono — not the 0.31 an earlier note claimed, but close enough that the conclusion is the
same. `render._macro_line` pads its columns by character count, so any value past 999 pulled its bar
two thirds of a character left of the others — visible on every card, because kcal is always four
digits. U+2007 FIGURE SPACE is digit-width by definition and measures a full cell in JetBrains Mono.

The consequence, stated plainly because a reviewer will notice it: **the closed card writes `1 880`
twice at two different gap widths** — 14.4 px in the 24 px mono row and 4.8 px in the 24 px DM Sans
verdict — and at chat-preview sizes (13.5 px on the sheet, 16 px in the profile) the prose gap is
under 3 px and close to invisible. That is the price of a mono column that has to hold a cell, and it
is the state of the shipped files. If it has to go, the fix is one character in `render.fmt_num`:
U+00A0, at 0.27 em in DM Sans, is the widest of the candidates and stays unbreakable. It is **not**
U+202F, which the table above shows is *narrower* than the thin space in DM Sans and Newsreader and
would tighten the gap rather than open it. Changing it is a `src/strikt/telegram/` edit, outside this
folder, so it is recorded here rather than done here.

So: `render.fmt_num` keeps the thin space for prose, `render._cells` swaps it for a figure space
inside `<code>`, and `gen-sources.py` carries the same pair as `TS` and `FS`.
`agent/numbers.py` accepts both (plus U+00A0, U+202F and a comma) when it parses a number back.

The second half of the same defect: `_macro_line` wrote `{unit}`, empty on the kcal row, so that row
was one character shorter than P / C / F / fiber. It is `{unit or ' '}` now. Measured bar starts,
before and after, in `hero.html`: 468 · 481 · 481 · 481 · 481 → 481 × 5.

## 9. The font check in `render.mjs`

The old check read the first family of each text node's computed `font-family`. That cannot see a
single glyph falling out of a subset's `unicode-range`: `system.html` shipped a U+2192 arrow
rasterised from DejaVu Sans inside a paragraph whose computed family was DM Sans.

The check now also opens a CDP session, tags every element holding text, and asks
`CSS.getPlatformFontsForNode` which platform fonts Chromium actually used; any `familyName` that is
not DM Sans / Newsreader / JetBrains Mono / Golos Text fails the run with a non-zero exit. Verified
by putting the arrow back: `[system] FONT PROBLEM DejaVu Sans (1 glyph) <- "Base 4 px: …"`.
Glyphs that are **not** in the DM Sans / Newsreader / Golos Text subsets and must not be set in them:
`→` U+2192, `≥` U+2265, `≤` U+2264, `✓` U+2713. JetBrains Mono (the full build, not the Google
subset) has all four, which is why the captions and the construction notes can use `≤ 48 px` and
`y 19.5 → 80.5`. The arrow in the spacing paragraph became the words "paper to card"; the delivery
ticks became an SVG.

## 10. Greyscale text, and the check that keeps it that way

`chromium.launch()` took no arguments, so Chromium rasterised every glyph with **LCD subpixel
antialiasing**: each edge pixel is tinted orange or blue because the three subpixels of a screen cell
are covered by different amounts. That is stray colour in a system whose rule is one accent, it puts
dozens of small red clusters on ordinary ink text (the red-cluster scan that checks "only the mark is
red" was finding them in every image), and it makes glyphs hazy on a retina screen or under any
scaling, because the fringes are only correct for one physical subpixel order. Measured on the old
files: the hero meals list carried 4 397 fringed pixels of 82 680, the OG tagline 999 of 14 400, the
Russian bubble 9 172 of 194 400, `sheet.png`'s food reply 4 668 of 74 800.

The browser is launched with `--disable-lcd-text --font-render-hinting=none` now, which puts every
glyph edge on a grey blend between the text colour and what is behind it, and the renderer proves it
on every run:

1. it keeps the screenshot buffer and decodes it (a ~60-line 8-bit non-interlaced PNG reader on
   `node:zlib` — no native dependency);
2. for every element that holds text it collects the colours that element is allowed to paint: its
   own text colour, the text and background colours of all its descendants, and every non-transparent
   background up its ancestor chain;
3. inside that element's box, a pixel fails if its channel spread (max − min) is over 40 **and** it
   is more than 26 away, in RGB, from the nearest point on any segment between two of those colours.
   Greyscale antialiasing always lands on such a segment; a subpixel fringe does not.

The palette-segment test is what lets the check run over the whole set without exceptions: the white
initial on a `#65AADD` Telegram avatar, the `#B32E22` "don't" inside a mute caption and the red
strike itself are all blends of two declared colours, and they pass. Verified both ways — with the
flag every job reports zero; with it removed, `card-closed` alone reports **26 304 px in 15
elements**, e.g. `(240,195,139)` at 196,487 on the meals list.

