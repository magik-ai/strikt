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

### Tiny cut (`favicon-32.png`; 32 px raster and below)

Drawn in device pixels on a 32 px canvas and converted at 3.125 units per pixel, so every edge lands
on a whole pixel and nothing is antialiased into grey:

| Item | Device px (at 32) | Units |
|---|---|---|
| Stroke width | 3 | 9.375 |
| Gap | 2 | 6.25 · pitch 15.625 |
| Stroke centres x | 8.5 · 13.5 · 18.5 · 23.5 (strokes 7–10, 12–15, 17–20, 22–25) | 26.5625 · 42.1875 · 57.8125 · 73.4375 |
| Stroke ends y (cap centres) | 5.5 → 26.5 (ink 4 → 28) | 17.1875 → 82.8125 |
| Strike | width 3, 28°, overshoot 2.5 | 9.375 · over 7.8125 |
| Ink box | 26 × 24 = 81 % × 75 % of the side | — |

The gap is 0.67 × the stroke, the only cut that breaks the ≥ 1.1 rule: at 32 px what matters is two
whole background pixels between strokes. Verified from the PNG's alpha channel — row 6 reads
`.......###..###..###..###.......` — and by eye at 32 px and at 16 px (the four verticals still
separate after a 2× downscale). The previous small cut at this size gave 2.7 px strokes on
half-pixel edges: a grey hatched blob.

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
  (stroke 3 px, gap 2 px, pixel-snapped). `favicon-180.png` (apple touch): small cut with the red
  strike, paper background, box 72 %.

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
| Numbers in bubbles | JetBrains Mono 400 (500 for the meal total) | 21–22 / 1.55, tabular; 20 in the menu reply, whose rows carry a why column | 0 |
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
is bundled so that the card's `▓░` bar glyphs would also render; in the images the eight-cell bar is
drawn as flat cells (ink / rule) in the same column, because the shade glyphs dither at 22 px.

## 6. Card numbers (internally consistent)

Protocol: 2 100 kcal · 180 P · 200 C · 70 F · 30 fiber.

| Meal | kcal | P | C | F | fiber | Atwater check (4P + 4C + 9F) |
|---|---|---|---|---|---|---|
| 08:10 breakfast — skyr, oats, blueberries | 480 | 38 | 62 | 9 | 8 | 481 |
| 13:20 lunch — chicken thigh 180 g (325 · 41 · 0 · 17), rice 150 g (195 · 4 · 42 · 1), cucumber salad 120 g (70 · 1 · 4 · 5) | 590 | 46 | 46 | 23 | 3 | 575 |
| 16:40 snack — greek yogurt, walnuts | 270 | 21 | 12 | 15 | 2 | 267 |
| 19:50 dinner — salmon, potatoes, broccoli | 600 | 52 | 48 | 22 | 9 | 598 |
| after lunch (food reply, russian) | 1 070 | 84 | 108 | 32 | 11 | left 1 030 · 96 · 92 · 38 · 19 |
| after snack (hero, 17:02; menu, 18:47) | 1 340 | 105 | 120 | 47 | 13 | left 760 · 75 · 80 · 23 |
| closed (card-closed, profile) | 1 940 | 157 | 168 | 69 | 22 | — |

Bars follow `render.bar`: `round(value / target × 8)` filled cells — after snack 5/5/5/5/3, closed
7/7/7/8/6.

The day runs 13:21 → 17:02 → 18:47 → 22:41 and every image is the same Wednesday, so the figures
have to agree across images:

- The food reply's `today` row now carries the same five columns as `left`, so "Fiber 11 of 30" can
  be checked on the card (11 + 19 = 30, 1 070 + 1 030 = 2 100, 84 + 96 = 180, and so on).
- The menu reply is at 18:47, after the 16:40 snack, so it opens on **Left: 760 kcal · 75 P** — the
  same state the hero card shows at 17:02, not the post-lunch 1 030 / 96 it used to quote.
- The closed card carries **no Left line**: once the verdict is written the day is over, and the
  verdict already says "Protein short 23 g". `render.render_day_card` skips the line when
  `state.closed`.

Menu image: protein per 100 kcal = P / kcal × 100 (52/540 → 9.6, 38/620 → 6.1, 31/690 → 4.5,
24/580 → 4.1, 44/1180 → 3.7). The rows are sorted by that ratio, so the column is monotonic and the
pick / okay / skip boundary follows it — caesar (4.5) is okay and falafel (4.1) is skip, the way
round the numbers actually argue. Every row carries a fat column and the brief's one line of why
(5.7): the caesar's 48 F is on the card next to "ask for dressing on the side".

Ladder timestamps 10:10 · 10:55 · 11:40 · 12:25 — 45 minutes apart (the follow-up delay in
`ladder.py`), starting at wake + 3 h. They used to run 14:10 → 16:25, which made step 2's "two hours
past your usual first meal" imply a first meal at 12:55 while every other image shows breakfast
logged at 08:10. Step 4 quotes generic waist figures (target 90, at 97): no image carries the
owner's body data.

## 7. Spacing and radius in the images

Base 4 px. Stage padding 88 / 112 px; chat column 860–1696 px; bubble padding 18 × 24 px, radius 16
with a 4 px corner on the tail side; avatar 40 px with the small cut at 67.5 % (27 px); cards and
panels radius 24; no shadows anywhere; user bubbles are `rule` on paper, bot bubbles `card` with a
1 px `rule` hairline.

Two edges that have to line up and did not:

- **One right edge per chat column.** A bot bubble sits behind a 40 px avatar and a 14 px gap, so its
  width is the column minus 54, not the column: 980 → 926 (food reply), 1 100 → 1 046 (russian),
  1 696 − 442 screenshot − 48 gap → 1 152 (menu). Both bubbles in a column now end on the same
  vertical, and the menu reply ends on the 112 px grid.
- **One left edge per image.** The state mark in `hero` / `card-closed` is offset by its own left
  bearing so the *ink* — the first bar when the day is open (unit 17), the strike cap when it is
  closed (unit 8) — sits on the caption's left edge rather than the viewBox edge: −44.2 px and
  −20.8 px at 260 px (2.6 px per unit). The menu screenshot starts at 112 px like every other
  image's caption and footer.

The inline keyboard is docked to the bubble's bottom edge the way Telegram draws it: a 1 px `rule`
across the full bubble width, buttons splitting it evenly with a 1 px divider, bubble
`padding-bottom: 0`. Outgoing messages carry drawn delivery ticks — the ✓ glyph is not in the
DM Sans or Newsreader subsets, so they are a 17 × 11 SVG in `mute` at 1.6 px, matching the
illustration rule.

Column widths on the Russian image are wider than the English (1 100 vs 980): «клетчатка» and
«осталось» are longer than "fiber" and "left", and the mono block must not be clipped at the same
21 px.

## 8. Thousands separators, and why there are two

`JetBrains Mono` gives U+2009 THIN SPACE an advance of 0.31 of a character cell (measured in the
render: 4 px of a 13 px cell at 21 px). `render._macro_line` pads its columns by character count, so
any value past 999 pulled its bar two thirds of a character left of the others — visible on every
card, because kcal is always four digits. U+2007 FIGURE SPACE is digit-width by definition and
measures a full cell in all four bundled faces.

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
