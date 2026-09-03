# Generates the HTML sources in this folder and ../sheet.html. Optional: the HTML files are the shipped sources;
# run this only to regenerate them after changing copy or numbers, then run ../render.mjs.
# Generates brand/src/*.html and brand/sheet.html. Working tool; the HTML files are the shipped sources.
import re, os
OUT=__import__("os").path.dirname(__import__("os").path.abspath(__file__))
def HEAD(title, css='', lang='en', rel='../'):
    return ('<!doctype html><html lang="%s"><head><meta charset="utf-8"><title>%s</title>'
            '<link rel="stylesheet" href="%sfonts/local.css"><link rel="stylesheet" href="%ssrc/strikt.css">'
            '<style>%s</style></head><body>' % (lang, title, rel, rel if rel!='../' else '', css)).replace('src/strikt.css','strikt.css') if rel=='../' else \
           ('<!doctype html><html lang="%s"><head><meta charset="utf-8"><title>%s</title>'
            '<link rel="stylesheet" href="%sfonts/local.css"><link rel="stylesheet" href="%ssrc/strikt.css">'
            '<style>%s</style></head><body>' % (lang, title, rel, rel, css))
def TAIL(rel='../'):
    js = 'mark.js' if rel=='../' else rel+'src/mark.js'
    return '<script src="%s"></script><script>StriktMark.mount()</script></body></html>' % js
def W(name, s, root=False):
    p = os.path.join(os.path.dirname(OUT), name) if root else os.path.join(OUT, name)
    open(p,'w').write(s)

def av(size=40, night=False):
    inner = round(size*0.675)
    return '<div class="av" style="width:%dpx;height:%dpx"><div data-mark="small" data-size="%d"%s></div></div>' % (size,size,inner,' data-night' if night else '')
# render.bar writes the day card's progress bar with the shade glyphs U+2593 / U+2591; the images
# set the same string in JetBrains Mono (the full build has both), so the mock is the message.
def bar(filled):
    return '\u2593'*filled + '\u2591'*(8-filled)
def macro(label, val, tgt, unit, filled):
    # mirrors render._macro_line: f"{label:<5}{value:>6} /{target:>6}{unit or ' '}" + 2 spaces + 8-cell bar.
    # The unit column is one character wide on every row (blank for kcal) so all five bars start
    # on the same column; without the pad the kcal bar sits one mono character to the left.
    return '%-5s%6s /%6s%s  %s' % (label, val, tgt, unit or ' ', bar(filled))
# Telegram's delivery ticks, drawn (the check glyph is not in the DM Sans / Newsreader subsets).
def ticks(colour='#8A857A'):
    return ('<svg class="tick" width="17" height="11" viewBox="0 0 17 11" fill="none" stroke="%s" '
            'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M1 6.2 4.3 9.5 10.4 1.6"/><path d="M8.2 8.1 9.6 9.5 15.7 1.6"/></svg>') % colour
TICKS = ticks()
def bubble(inner, time, user=False, extra=''):
    cls = ' user' if user else ''
    return '<div class="bubble%s"%s>%s<div class="time">%s%s</div></div>' % (
        cls, extra, inner, time, TICKS if user else '')
# A bot row is avatar + bubble. Telegram hangs the inline keyboard *under* the bubble and leaves the
# avatar on the bubble's own bottom edge, so the row is a two-row grid: the avatar and the bubble
# share row 1 and end on the same baseline, the keyboard sits alone in row 2.
def row(inner, user=False, night=False, kbd=''):
    if user: return '<div class="row user">%s</div>' % inner
    if kbd: return '<div class="row kbrow">%s%s%s</div>' % (av(40, night), inner, kbd)
    return '<div class="row">%s%s</div>' % (av(40, night), inner)
def kb(*labels):
    return '<div class="kb">%s</div>' % ''.join('<span>%s</span>' % l for l in labels)

# Two thousands separators, exactly as render.py writes them:
#   TS  thin space U+2009 — prose (a verdict, a ladder line, a chat preview). Measured in the
#       render at 100 px: 20 px in DM Sans, Newsreader, Golos Text and JetBrains Mono alike, i.e.
#       the 1/5 em a thin space is defined to be. (U+202F NARROW NO-BREAK SPACE is *narrower* in
#       these faces — 14 px in DM Sans, 12 in Newsreader — so it is not the fix it looks like.)
#   FS  figure space U+2007 — inside a mono block, where it is digit-width, i.e. one full cell
#       (60 px of a 60 px cell), so the padded columns and the bars stay on one vertical. The thin
#       space is 1/3 of a cell there, which is why the two are not interchangeable.
TS='\u00a0'  # no-break space, the separator render.fmt_num writes
FS=' '

# ---------------- og ----------------
W('og.html', HEAD('Strikt og', """
.stage{width:1200px;height:630px;padding:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.line{font-size:28px;color:var(--ink);margin-top:40px}
""") + '<div class="stage"><div data-lockup="168"></div><div class="line">Send food, get the number.</div></div>' + TAIL())

# ---------------- hero + closed ----------------
# The two frames register: the card is the same box in both (left edge 166 = 112 grid + 40 avatar +
# 14 gap, fixed width 844, top pinned at 240), so only its contents change when the day closes. The
# state column is the same box too — the mark svg carries the same −20.8 px left bearing in both
# files, so bars 1–3 of the open day sit exactly where bars 1–3 of the closed mark sit.
CARD_CSS = """
.chat{position:absolute;left:112px;top:240px;width:898px}
.bubble{padding:22px 28px 14px}
.row .bubble{width:844px;max-width:844px}
.bubble .code{font-size:22px}
/* centred between the card's right edge (1010) and the 1808 grid margin */
.state{position:absolute;left:1010px;right:112px;top:300px;display:flex;justify-content:center}
.state .in{width:348px}
.state .cap{line-height:1.75;margin-top:24px}
.top{position:absolute;left:112px;top:80px}
"""
def card(title, lines, left, meals, tail, time):
    b = '<p><b>%s</b></p>' % title
    b += '<span class="code">%s</span>' % '\n'.join(lines)
    if left: b += '<p style="margin-top:6px">%s</p>' % left
    b += '<p style="margin-top:12px"><b>Meals</b><br>%s</p>' % '<br>'.join(meals)
    b += ''.join('<p style="margin-top:6px">%s</p>' % t for t in tail)
    b += '<div class="time">%s</div>' % time
    return b
open_lines=[macro('kcal','1'+FS+'340','2'+FS+'100','',5),macro('P','105','180','g',5),macro('C','120','200','g',5),macro('F','47','70','g',5),macro('fiber','13','30','g',3)]
closed_lines=[macro('kcal','1'+FS+'880','2'+FS+'100','',7),macro('P','157','180','g',7),macro('C','162','200','g',6),macro('F','65','70','g',7),macro('fiber','19','30','g',5)]
# The day chains across the images: the menu at 18:47 recommends the grilled chicken plate
# (540 kcal · 52 P from the menu rows), so the closed card's dinner is that plate.
meals=['• 08:55 breakfast — Skyr, oats, blueberries · 480','• 13:20 lunch — Chicken thigh, rice, cucumber salad · 590','• 16:40 snack — Greek yogurt, walnuts · 270','• 19:50 dinner — Grilled chicken plate · 540']
DATE='Thu 3 Sep'
VERDICT='Closed at 1'+TS+'880 / 157 P / 19 fiber. Protein short 23 g, third day running. Bed by 00:30.'
# The mark svg is offset by its own left bearing so the ink of the *closed* mark — the strike cap at
# unit 8 — sits on the caption's left edge. The open frame carries the same offset, not its own, so
# bars 1–3 land on exactly the x they occupy in the closed frame. 260 px = 2.6 px per unit.
def state(inner, caps):
    return '<div class="state"><div class="in"><div style="margin-left:-20.8px">%s</div>%s</div></div>' % (inner, caps)
hero = HEAD('Strikt hero', CARD_CSS) + '<div class="stage"><div class="top cap">the today card · pinned in the chat · day open</div>'
hero += '<div class="chat"><div class="row">%s<div class="bubble">%s</div></div></div>' % (av(), card('Today · '+DATE, open_lines, 'Left: 760 kcal · 75 P · 80 C · 23 F', meals[:3], ['<b>Sleep</b>: 6h48 · 84% · recovery 61%'], '17:02'))
hero += state('<div data-mark="full" data-size="260" data-bars="3"></div>',
   '<div class="cap">day open · three meals logged<br>one stroke per meal, four at most<br>no strike until the day is closed</div>'
   '<div class="cap" style="margin-top:18px">the avatar is always the full mark<br>the state lives in the card</div>')
hero += '<div class="foot"><div data-lockup="34"></div><div class="cap">the today card · pinned, edited in place, closed at night</div></div></div>' + TAIL()
W('hero.html', hero)
closed = HEAD('Strikt card closed', CARD_CSS) + '<div class="stage"><div class="top cap">the today card · pinned in the chat · day closed</div>'
# A closed day has no "left": the day is over, and the verdict already carries the shortfall.
closed += '<div class="chat"><div class="row">%s<div class="bubble">%s</div></div></div>' % (av(), card('Today · '+DATE+' · closed', closed_lines, '', meals,
   ['<b>Training</b>: strength · 62 min · strain 12.4 · 410 kcal','<b>Sleep</b>: 6h48 · 84% · recovery 61%','<b>Verdict</b>: '+VERDICT], '22:41'))
closed += state('<div data-mark="full" data-size="260"></div>',
   '<div class="cap">day closed · four meals<br>the fifth stroke is the verdict<br>drawn once, in red, by the bot</div>')
closed += '<div class="foot"><div data-lockup="34"></div><div class="cap">the today card · a verdict, not encouragement</div></div></div>' + TAIL()
W('card-closed.html', closed)

# ---------------- food reply ----------------
# Chicken thigh (hatched, bone end), a mound of rice with grain marks, five overlapping cucumber
# slices with seeds. Single-weight ink line, 2.6 px, round caps — the illustration rule. Nothing
# abstract on the plate: no cutlery stub, no double rings.
# The thigh is boneless: a flat irregular oval with one skin line across it and a light hatch — a
# drumstick with a bone knuckle is a different cut, and the message names a thigh.
PLATE = """<svg viewBox="0 0 400 300" width="400" height="300" fill="none" stroke="#1A1814" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
<ellipse cx="200" cy="152" rx="168" ry="118"/>
<ellipse cx="200" cy="152" rx="138" ry="92"/>
<path d="M86 164c-4-34 22-62 54-62c32 0 58 28 54 62c-18 8-36-4-54 0c-18 4-36-4-54 0z"/>
<path d="M102 152l11-4M124 142l11-3M146 140l11 3M168 148l9 4M98 158l11-2M120 156l11 2M142 154l11-3M164 160l10 3M130 124l11 2M152 126l10 5M112 132l10 3"/>
<path d="M214 148c-6-22 10-42 36-46c30-5 58 6 64 24c6 18-8 38-32 46c-26 9-62 0-68-24z"/>
<path d="M234 142c13-12 34-17 54-13c11 2 20 8 24 15"/>
<path d="M240 158l14-12M264 163l14-13M288 160l12-12"/>
<circle cx="136" cy="204" r="19"/><circle cx="168" cy="214" r="19"/><circle cx="200" cy="205" r="19"/><circle cx="232" cy="215" r="19"/><circle cx="264" cy="203" r="19"/>
<circle cx="131" cy="203" r="1.7" fill="#1A1814" stroke="none"/><circle cx="137" cy="199" r="1.7" fill="#1A1814" stroke="none"/><circle cx="140" cy="206" r="1.7" fill="#1A1814" stroke="none"/><circle cx="163" cy="213" r="1.7" fill="#1A1814" stroke="none"/><circle cx="169" cy="209" r="1.7" fill="#1A1814" stroke="none"/><circle cx="172" cy="216" r="1.7" fill="#1A1814" stroke="none"/><circle cx="195" cy="204" r="1.7" fill="#1A1814" stroke="none"/><circle cx="201" cy="200" r="1.7" fill="#1A1814" stroke="none"/><circle cx="204" cy="207" r="1.7" fill="#1A1814" stroke="none"/><circle cx="227" cy="214" r="1.7" fill="#1A1814" stroke="none"/><circle cx="233" cy="210" r="1.7" fill="#1A1814" stroke="none"/><circle cx="236" cy="217" r="1.7" fill="#1A1814" stroke="none"/><circle cx="259" cy="202" r="1.7" fill="#1A1814" stroke="none"/><circle cx="265" cy="198" r="1.7" fill="#1A1814" stroke="none"/><circle cx="268" cy="205" r="1.7" fill="#1A1814" stroke="none"/>
</svg>"""
FOOD_CSS = """
.stage{display:flex;align-items:center;justify-content:center}
.chat{width:693px}
/* the bot bubble takes the whole chat column minus the avatar and its gap, so both bubbles
   end on the same right edge (693 − 40 − 14 = 639); 639 = 41 mono cells at 24 px (590.4) plus the
   bubble's 2 × 24 px padding */
.bubble{max-width:639px}
.row .bubble:not(.user){width:639px}
.bubble .code{font-size:24px}
.top{position:absolute;left:112px;top:80px}
"""
# Six rows: per item, the meal in bold, the day so far, what is left. Every row carries the same
# four columns, so the block has one right edge; fiber lives in the prose line under it, where it
# is the one thing worth saying. 53 mono columns — a phone renders a <code> block at roughly that
# width, and the bubble is sized to it rather than to the canvas.
# Four columns under one header instead of a unit word on every cell: 41 mono cells, inside the
# 35-45 a phone gives a <code> block, where the old 53-cell rows wrapped.
def frow(name, kcal, p, c, f):
    return '%-20s%6s%5s%5s%5s' % (name, kcal, p, c, f)
food_rows = [frow('', 'kcal', 'P', 'C', 'F'),
             frow('chicken thigh 180 g', '325', '41', '0', '17'),
             frow('rice 150 g', '195', '4', '42', '1'),
             frow('cucumber salad 120 g', '70', '1', '4', '5'),
             '<b>' + frow('meal', '590', '46', '46', '23') + '</b>',
             frow('today', '1'+FS+'070', '84', '108', '32'),
             frow('left', '1'+FS+'030', '96', '92', '38')]
food_reply = ('<p>Chicken thigh, rice, cucumber salad. About 450 g.</p>'
 '<span class="code">' + '\n'.join(food_rows) +
 '</span><p style="margin-top:10px">Fiber 11 of 30. Dinner gets a vegetable.</p>')
# Telegram draws a photo edge to edge inside the bubble and overlays the time on the picture in a
# dark translucent pill; it does not frame it and does not give it a padded caption row.
def photo(plate, time):
    return ('<div class="row user"><div class="bubble user photo">%s'
            '<div class="stamp">%s%s</div></div></div>' % (plate, time, ticks('#FFFCF5')))
food = HEAD('Strikt food reply', FOOD_CSS) + '<div class="stage"><div class="top cap">a food photo · answered with the number</div><div class="chat">'
food += photo(PLATE, '13:20')
food += row(bubble(food_reply, '13:21'), kbd=kb('Undo','Recalculate'))
food += '</div><div class="foot"><div data-lockup="34"></div><div class="cap">per item, the meal, the day, what is left · one line of advice at most</div></div></div>' + TAIL()
W('food-reply.html', food)

# ---------------- ladder ----------------
LADDER_CSS = """
.stage{padding-top:80px}
.top{position:absolute;left:112px;top:80px}
.lad{position:absolute;left:112px;right:112px;top:250px}
.line{position:absolute;left:0;right:0;top:0;height:1px;background:var(--rule)}
.steps{display:grid;grid-template-columns:repeat(4,1fr);column-gap:40px}
.step{position:relative;padding-top:0}
.tk{position:absolute;left:0;top:-4px;width:1px;height:9px;background:var(--mute)}
.step .cap{padding-top:22px;line-height:1.7}
.step .cap b{color:var(--ink);font-weight:500}
/* the avatar sits on the bubble's bottom edge, the way Telegram draws it and the way every other
   image in the set draws it */
.step .row{margin-top:26px;align-items:flex-end}
.step .bubble{font-size:23px;padding:18px 22px 12px}
.note{position:absolute;left:112px;right:112px;bottom:80px;display:flex;justify-content:space-between;align-items:flex-end}
/* the staircase carries the timeline's grid: 1696 px wide, four columns of 434 px, so a riser
   stands on the left edge of the column whose moment it opens */
.stair{position:absolute;left:112px;right:112px;top:636px}
.stair svg{display:block;width:100%;height:160px}
.stair .why{display:grid;grid-template-columns:repeat(4,1fr);column-gap:40px;margin-top:14px}
.stair .why div{line-height:1.7}
.stair .why b{color:var(--ink);font-weight:500}
"""
# The clock: the user's usual first meal is 08:55 (the breakfast on the Today card), the silence
# trigger fires at wake + 3 h, and the follow-ups are 45 minutes apart — so 10:10 · 10:55 · 11:40 ·
# 12:25 and step 2's "two hours past your usual first meal" is exact (08:55 + 2 h = 10:55). The
# brief's sentence is kept verbatim; the card moved instead.
# No personal body numbers in any image: the waist figures are generic (CLAUDE.md law 3).
steps=[('10:10','1 · prompt','one line, factual','Nothing logged yet. Breakfast?'),
       ('10:55','2 · push','the pattern, from data','Two hours past your usual first meal. Skipped breakfasts in your history end at 2'+TS+'600 kcal evenings.'),
       ('11:40','3 · demand','an instruction with a deadline','Eat something with 40 g protein in the next hour and send me a photo.'),
       ('12:25','4 · consequence','the goal in concrete terms','Waist target is 90. You\'re at 97. Days like this cost a week each.')]
# One label per step: the timestamped header sits above the bubble, so the row under the staircase
# carries the timing facts only — it does not repeat "prompt · push · demand · consequence".
rungs=[('wake + 3 h','nothing logged yet today'),('+ 45 min','still nothing'),('+ 45 min','a deadline attached'),('+ 45 min','the last send of the ladder')]
lad = HEAD('Strikt ladder', LADDER_CSS) + '<div class="stage"><div class="top cap">the escalation ladder · one silent morning · four sends, 45 minutes apart</div>'
lad += '<div class="lad"><div class="line"></div><div class="steps">'
for t,s,d,txt in steps:
    lad += '<div class="step"><div class="tk"></div><div class="cap"><b>%s</b> · %s<br>%s</div>%s</div>' % (t,s,d,row(bubble(txt,t)))
lad += '</div></div>'
# the rungs as one ink stair: each tread is one column of the timeline above it
# Risers at 434 · 868 · 1302 in the svg's own 1696-unit width — the same grid the header hairline
# and the four columns use (column pitch 434, first column left edge 0), so every step rises on the
# exact x where its send begins instead of 14, 24 and 34 px early.
lad += '<div class="stair"><svg viewBox="0 0 1696 132" preserveAspectRatio="none" fill="none" stroke="#1A1814" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M0 122 H434 V88 H868 V54 H1302 V20 H1696" vector-effect="non-scaling-stroke"/></svg><div class="why">'
for (t,s,d,txt),(when,what) in zip(steps,rungs):
    lad += '<div class="cap"><b>%s</b> · %s</div>' % (when, what)
lad += '</div></div>'
lad += '<div class="note"><div class="foot" style="position:static"><div data-lockup="34"></div><div class="cap">never beyond step four · resets on any reply · quiet hours 00:00–07:30 · at most five sends a day</div></div></div></div>' + TAIL()
W('ladder.html', lad)

# ---------------- menu ----------------
MENU_CSS = """
.stage{display:flex;align-items:center;justify-content:center}
/* the screenshot and the reply are one centred composition, like the food reply */
.chat{width:auto;flex-direction:row;align-items:center;gap:80px}
/* The screenshot is somebody else's app, so it is painted in plain client chrome — white card,
   system-sans names and prices with proportional figures, grey photo placeholders, neutral greys —
   never in the palette. No mono anywhere in it: mono is the bot's own table. Only the bubble
   around it is ours, because a user sent it. */
.app{width:420px;background:#FFFFFF;border-radius:12px;padding:20px 22px 16px;color:#111111;font-size:18px;font-variant-numeric:proportional-nums}
.app .hd{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid #ECECEC;padding-bottom:14px;margin-bottom:2px}
.app .hd b{font-size:22px;font-weight:600;letter-spacing:-.01em}
.app .hd span{color:#8E8E93;font-size:16px;font-weight:500}
.app .it{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid #F2F2F2}
.app .it:last-of-type{border-bottom:0}
.app .it .n{font-weight:500}
.app .it .w{color:#8E8E93;font-size:15px;margin-top:2px}
.app .it .p{font-size:17px;font-weight:500;color:#111111;white-space:nowrap}
.app .it .ph{width:56px;height:56px;border-radius:8px;margin-right:14px;flex:none;overflow:hidden}
.app .it .ph svg{display:block}
.app .it .l{display:flex;align-items:center;flex:1;min-width:0}
.app .chip{display:inline-block;margin-top:14px;background:#F2F2F2;border-radius:8px;padding:7px 13px;font-size:15px;color:#8E8E93}
/* phone width: the mono block is 42 columns at 24 px (604.8 px) inside 2 × 24 px padding */
.bubble.bot{max-width:none;width:653px}
.bubble .code{font-size:24px}
.bubble .code b{font-weight:500}
.top{position:absolute;left:112px;top:80px}
"""
# Grey photo placeholders — a horizon, a sun and a hill in five neutral greys. Deliberately not the
# brand's line illustrations: this tile belongs to the other app, and the reader has to see that.
# a plate seen from above, out of focus: three neutral greys, no line work, nothing that could be
# mistaken for the brand's own illustration
THUMB = [(28,28,21,12),(27,29,20,13),(29,27,21,11),(28,29,20,12),(27,28,21,13)]
def thumb(i):
    cx,cy,r1,r2 = THUMB[i % len(THUMB)]
    return ('<svg width="56" height="56" viewBox="0 0 56 56">'
            '<rect width="56" height="56" fill="#EFEFEF"/>'
            '<circle cx="%d" cy="%d" r="%d" fill="#E1E1E1"/>'
            '<circle cx="%d" cy="%d" r="%d" fill="#D2D2D2"/></svg>') % (cx,cy,r1,cx,cy,r2)
items=[('Grilled chicken plate','380 g','AED 34'),('Beef burger with fries','450 g','AED 32'),
       ('Falafel bowl','420 g','AED 28'),('Margherita pizza','33 cm','AED 36'),
       ('Caesar with chicken','300 g','AED 31')]
app = '<div class="app"><div class="hd"><b>Grill house</b><span>4.7</span></div>'
for i,(n,w,p) in enumerate(items):
    app += '<div class="it"><div class="l"><div class="ph">%s</div><div><div class="n">%s</div><div class="w">%s</div></div></div><div class="p">%s</div></div>' % (thumb(i),n,w,p)
app += '<div class="chip">delivery 25–35 min</div></div>'
# Ranked by protein per 100 kcal, so the column is monotonic (9.6 · 6.1 · 4.5 · 4.1 · 3.7) and the
# verdicts fall out of it; the last column is the brief's one line of why per row.
#            verdict  item                    kcal   P    F   P/100 kcal  the one line of why
# The why never repeats a number already in its own row: the falafel's case is the fat, not the
# 24 P printed two columns left of it, and the pick's is the protein still owed, not "52 of 75".
menu_rows=[('pick','grilled chicken plate','540','52','18','9.6','leaves 23 P'),
           ('okay','beef burger, no fries','620','38','30','6.1','fries add 360'),
           ('okay','caesar with chicken','690','31','48','4.5','dressing aside'),
           ('skip','falafel bowl','580','24','29','4.1','deep fried'),
           ('skip','margherita pizza','1'+FS+'180','44','38','3.7','over by 420')]
# Two lines per item, 42 mono columns at the widest — a phone renders a <code> block at 35-45, and
# one 78-column row would wrap into three broken lines there. Line 1 is the decision, the dish and
# its protein per 100 kcal; line 2 the numbers and the one line of why.
def mrow(i, r):
    v,n,k,p,f,ratio,why = r
    head = '%-6s%-21s%5s' % (v,n,ratio)
    if i==0: head = head.replace(v, '<b>%s</b>' % v, 1)
    return head + '\n' + '  %5s kcal · %2s P · %2s F  %s' % (k,p,f,why)
menu_reply = ('<p>Left: 760 kcal · 75 P. Ranked by protein per 100 kcal.</p><span class="code">' +
 '\n'.join(mrow(i,r) for i,r in enumerate(menu_rows)) +
 '</span><p style="margin-top:10px">Chicken plate. Nothing else on the list reaches 50 P.</p>')
menu = HEAD('Strikt menu', MENU_CSS) + '<div class="stage"><div class="top cap">a delivery menu screenshot · pick, okay, skip · ranked by protein per calorie</div><div class="chat">'
menu += photo(app, '18:47')
menu += '<div class="row" style="align-items:flex-end">%s<div class="bubble bot">%s<div class="time">18:47</div></div></div>' % (av(), menu_reply)
menu += '</div><div class="foot"><div data-lockup="34"></div><div class="cap">the decision first, then the reason · the same numbers every time</div></div></div>' + TAIL()
W('menu.html', menu)

# ---------------- russian ----------------
RU_CSS = """
.stage{display:flex;align-items:center;justify-content:center}
/* wider than the english column: the Cyrillic labels («осталось», «ккал») are longer, so the same
   four columns take 42 mono cells instead of 41 */
.chat{width:707px}
/* 707 − 40 avatar − 14 gap: the bot bubble ends on the user bubble's right edge; 653 = 42 mono
   cells at 24 px (604.8) plus the bubble's 2 × 24 px padding */
.bubble{max-width:653px;font-family:var(--cyr)}
.row .bubble:not(.user){width:653px}
.bubble .code{font-size:24px}
.kb span{font-family:var(--cyr)}
.top{position:absolute;left:112px;top:80px}
"""
# «итого» is the Russian word for a table total; «приём» stays the name of the unknown meal slot.
# Column order is БЖУ — protein · fat · carbs — because that is how a Russian reader says and reads
# it; Б·У·Ж would be a transliteration of P·C·F. `copy.py` card.remaining (ru) has the same order.
# 42 mono cells with the unit words in one header row, the way the English reply now sets it; the
# name column is one cell wider because «огуречный салат 120 г» is.
ru_rows=[('chicken thigh 180 г','325','41','17','0'),('рис 150 г','195','4','1','42'),
         ('огуречный салат 120 г','70','1','5','4'),('итого','590','46','23','46'),
         ('сегодня','1'+FS+'070','84','32','108'),('осталось','1'+FS+'030','96','38','92')]
def rurow(i, r):
    line = '%-21s%6s%5s%5s%5s' % r
    return '<b>%s</b>' % line if i==3 else line
ru_reply = ('<p>Chicken thigh, рис, огуречный салат. Около 450 г.</p><span class="code">' +
 '\n'.join(['%-21s%6s%5s%5s%5s' % ('','ккал','Б','Ж','У')] + [rurow(i,r) for i,r in enumerate(ru_rows)]) +
 '</span><p style="margin-top:10px">Клетчатка 11 из 30. На ужин — овощи.</p>')
ru = HEAD('Strikt russian', RU_CSS, lang='ru') + '<div class="stage"><div class="top cap">language mirroring · russian in, russian out · food names stay as written</div><div class="chat">'
ru += row(bubble('обед: chicken thigh с рисом и салат из огурцов, грамм 450', '13:20', user=True), user=True)
ru += row(bubble(ru_reply, '13:21'), kbd=kb('Убрать','Пересчитать'))
ru += '</div><div class="foot"><div data-lockup="34"></div><div class="cap">the bot never switches language on its own · units metric</div></div></div>' + TAIL()
W('russian.html', ru)

# ---------------- telegram profile ----------------
# The chat stays in Telegram's own theme (BRAND.md §9), so both panels are painted in Telegram's
# neutral chrome — white rows on #F1F1F1, and #212121 rows on #181818 — not in the brand palette.
# The only brand object in the image is the one paper avatar, which is the point of it.
PROF_CSS = """
/* the panels end at y 900 like every other composition's content, and the frame carries the same
   34 px lock-up and mono caption at the 80 px bottom margin */
.stage{display:flex;gap:64px;align-items:flex-end;justify-content:center;padding:0 112px 180px}
.panel{width:816px;border-radius:24px;padding:36px 0 16px;overflow:hidden;background:#FFFFFF;border:1px solid #E4E4E4;color:#000000}
.panel.night{background:#212121;border-color:#2E2E2E;color:#FFFFFF}
.prof{display:flex;align-items:center;gap:26px;padding:0 36px 30px;border-bottom:1px solid #E9E9E9}
.night .prof{border-color:#2E2E2E}
.prof .nm{font-size:30px;font-weight:600;letter-spacing:-.01em}
.prof .st{font-size:17px;color:#707579;margin-top:2px}
.night .prof .st{color:#AAAAAA}
.about{padding:22px 36px 22px;font-size:19px;line-height:1.45;border-bottom:8px solid #F1F1F1}
.night .about{border-color:#181818}
/* Telegram sets its section labels and its times in the same system sans as the rows, sentence
   case, in the secondary grey — no mono, no tracking, no uppercase. DM Sans stands in for SF /
   Roboto here; the brand's own typography stops at the edge of somebody else's window. */
.about .lab{font-size:15px;font-weight:500;color:#707579;margin-bottom:6px}
.night .about .lab{color:#AAAAAA}
.chats .ttl{font-size:15px;font-weight:500;color:#707579;padding:20px 36px 8px}
.night .chats .ttl{color:#AAAAAA}
.ci{display:flex;align-items:center;gap:18px;padding:14px 36px;border-bottom:1px solid #E9E9E9}
.ci:last-child{border-bottom:0}
.night .ci{border-color:#2E2E2E}
.ci .ini{width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#FFFFFF;font-weight:600;font-size:17px;flex:none}
.ci .tx{flex:1;min-width:0}
.ci .n{font-size:18px;font-weight:600;display:flex;justify-content:space-between;align-items:baseline}
.ci .n span{font-size:14px;font-weight:400;color:#707579}
.night .ci .n span{color:#AAAAAA}
.ci .m{font-size:16px;color:#707579;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;display:flex;justify-content:space-between;gap:12px}
.night .ci .m{color:#AAAAAA}
.ci .m em{font-style:normal;color:#000000}
.night .ci .m em{color:#FFFFFF}
.ci .badge{font-size:13px;font-weight:500;background:#C4C9CC;color:#FFFFFF;border-radius:999px;padding:2px 9px;flex:none}
.night .ci .badge{background:#4E4E4E;color:#FFFFFF}
.top{position:absolute;left:112px;top:80px}
"""
# Telegram's own initial-avatar colours (client chrome, not a second brand accent): the point of
# this image is that the 40 px mark holds its own next to them. The cool tints sit next to the
# Strikt row so no red lands beside the mark; the two warm ones are at the bottom of the list.
# Times stay in descending order, the way a client sorts a chat list.
contacts=[('Strikt',None,None,'Closed at 1'+TS+'880 / 157 P / 19 fiber. Protein short 23 g, third day running. Bed by 00:30.','22:41','1'),
          ('Running club','R','#65AADD','<em>Marina:</em> 10k tomorrow, 7:30 at the bridge','22:13','3'),
          ('Dad','D','#A695E7','Call me when you land','21:50',None),
          ('Kirill','K','#7BC862','Photo','19:02',None),
          ('Anna','A','#E17076','see you at 8','18:27',None)]
def panel(night):
    p = '<div class="panel%s">' % (' night' if night else '')
    # one uploaded JPG serves both clients, so the avatar is the paper disc on night too
    p += '<div class="prof">%s<div><div class="nm">Strikt</div><div class="st">bot</div></div></div>' % av(96, False)
    p += '<div class="about"><div class="lab">Info</div>A coach in one chat. Send food, get the number. The day ends with a verdict.</div>'
    p += '<div class="chats"><div class="ttl">Chats</div>'
    for n,ini,tint,m,t,b in contacts:
        if ini is None: a = av(40, False)
        else:
            a = '<div class="ini" style="background:%s">%s</div>' % (tint, ini[0])
        p += '<div class="ci">%s<div class="tx"><div class="n">%s<span>%s</span></div><div class="m"><span style="overflow:hidden;text-overflow:ellipsis">%s</span>%s</div></div></div>' % (a,n,t,m,('<span class="badge">%s</span>'%b) if b else '')
    p += '</div></div>'
    return p
prof = HEAD('Strikt telegram profile', PROF_CSS) + '<div class="stage"><div class="top cap">telegram · bot profile and chat list · one paper jpg in telegram\'s own light and dark chrome · the tints are the client\'s</div>' + panel(False) + panel(True)
prof += '<div class="foot"><div data-lockup="34"></div><div class="cap">one uploaded jpg · the paper disc reads as a disc in both client themes</div></div></div>' + TAIL()
W('telegram-profile.html', prof)

# ---------------- avatars and favicons ----------------
W('avatar.html', HEAD('Strikt avatar', 'html,body{width:100vw;height:100vh;background:#F6F2E9;overflow:hidden}.b{width:67vw;height:67vw;margin:16.5vw auto 0}.b svg{width:100%;height:100%}') + '<div class="b" data-mark="small" data-size="100"></div>' + TAIL())
W('avatar-night.html', HEAD('Strikt avatar night', 'html,body{width:100vw;height:100vh;background:#161513;overflow:hidden}.b{width:67vw;height:67vw;margin:16.5vw auto 0}.b svg{width:100%;height:100%}') + '<div class="b" data-mark="small" data-size="100" data-night></div>' + TAIL())
# favicon-32.png uses the tiny cut: 3 px strokes on whole-pixel edges, 2 px holes, a 2 px strike,
# box 81 % of the side. favicon-16.png uses the micro cut, hand-fitted to 16 device pixels, for the
# size a browser actually paints in a tab.
W('favicon.html', HEAD('Strikt favicon', 'html,body{width:100vw;height:100vh;background:transparent;overflow:hidden}.b{width:100vw;height:100vw}.b svg{width:100%;height:100%}') + '<div class="b" data-mark="tiny" data-size="100" data-red="#1A1814"></div>' + TAIL())
W('favicon-16.html', HEAD('Strikt favicon 16', 'html,body{width:100vw;height:100vh;background:transparent;overflow:hidden}.b{width:100vw;height:100vw}.b svg{width:100%;height:100%}') + '<div class="b" data-mark="micro" data-size="100" data-red="#1A1814"></div>' + TAIL())
W('favicon-180.html', HEAD('Strikt favicon 180', 'html,body{width:100vw;height:100vh;background:#F6F2E9;overflow:hidden}.b{width:72vw;height:72vw;margin:14vw auto 0}.b svg{width:100%;height:100%}') + '<div class="b" data-mark="small" data-size="100"></div>' + TAIL())
print('written')

# ---------------- system sheet (1920x1080) ----------------
SYS_CSS = """
.stage{padding:72px 112px}
.head{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid var(--rule);padding-bottom:16px}
.head .h{font-size:28px}
.cols{display:grid;grid-template-columns:500px 520px 1fr;column-gap:56px;margin-top:34px}
.sec{margin-top:26px}.sec:first-child{margin-top:0}
.sw{display:grid;grid-template-columns:40px 130px 90px 1fr;align-items:center;column-gap:14px;padding:5px 0;border-bottom:1px solid var(--rule);font-size:15px}
.sw:last-child{border-bottom:0}
/* every swatch carries a hairline so the ground colours — paper, card, night — read as chips */
.sw i{display:block;width:40px;height:28px;border-radius:6px;box-shadow:inset 0 0 0 1px var(--rule)}
.sw .nm{font-weight:500}.sw .hx{font-family:var(--mono);font-size:14px;color:var(--mute)}.sw .us{color:var(--mute);font-size:14px}
.nt{background:var(--night);color:var(--text-dark);border-radius:16px;padding:14px 18px 6px;margin-top:14px}
.nt .sw{border-color:var(--rule-dark)}.nt .sw .hx,.nt .sw .us{color:var(--mute-dark)}
.nt .sw i{box-shadow:inset 0 0 0 1px var(--rule-dark)}
.tp{border-bottom:1px solid var(--rule);padding:14px 0 16px}.tp:last-child{border-bottom:0}
.tp .cap{margin-bottom:8px}
.tp .s1{font-family:var(--serif);font-variation-settings:'opsz' 72;font-weight:500;font-size:40px;letter-spacing:-.01em;line-height:1.08}
.tp .s2{font-size:21px;line-height:1.5}
.tp .s3{font-family:var(--mono);font-size:20px;line-height:1.55;white-space:pre;font-variant-numeric:tabular-nums}
.tp .s3 b{font-weight:500}
.cons{display:flex;gap:24px;align-items:flex-start}
/* 14 px is the floor for mute text (BRAND.md §3): the construction column sits on it */
.cons .notes{font-family:var(--mono);font-size:14px;line-height:1.75;color:var(--mute);white-space:nowrap}
.cons .notes b{color:var(--ink);font-weight:500}
/* every mark in the row carries its own size under it, so 48 can be told from 40 from 32 from 16 */
.small{display:flex;gap:18px;align-items:flex-end;margin-top:8px}
.small .u{display:flex;flex-direction:column;align-items:center;gap:5px}
.small .u .sz{font-family:var(--mono);font-size:14px;letter-spacing:.04em;color:var(--mute);line-height:1}
.c{border-radius:50%;background:var(--paper);box-shadow:inset 0 0 0 1px var(--rule);display:flex;align-items:center;justify-content:center;overflow:hidden}
.c.n{background:var(--night);box-shadow:none}
/* do / don't as five rows: the tile on the left, one legible 15 px line beside it */
.dd{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.dd .r{display:flex;align-items:center;gap:16px}
.dd .t{background:var(--card);border:1px solid var(--rule);border-radius:12px;width:116px;height:52px;flex:none;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden}
.dd .l{font-family:var(--mono);font-size:15px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);line-height:1.5}
.dd .l b{color:var(--strike-deep);font-weight:500}.dd .l b.ok{color:var(--ink)}
.mini{font-size:14px;line-height:1.3;background:var(--paper);border:1px solid var(--rule);border-radius:8px;border-bottom-left-radius:2px;padding:6px 9px}
.phone{width:28px;height:46px;border:2px solid var(--ink);border-radius:6px;position:relative;display:flex;align-items:center;justify-content:center}
.phone i{position:absolute;top:4px;left:50%;width:10px;height:2px;margin-left:-5px;border-radius:2px;background:var(--ink)}
.phone .bars{display:flex;flex-direction:column;gap:2px;width:16px}
.phone .bars i{position:static;width:100%;height:3px;margin:0;border-radius:1px;background:var(--rule)}
.phone .bars i:first-child{background:var(--ink);width:70%}
"""
def swatch(name, hexv, use):
    return '<div class="sw"><i style="background:%s"></i><span class="nm">%s</span><span class="hx">%s</span><span class="us">%s</span></div>' % (hexv,name,hexv,use)
light=[('paper','#F6F2E9','image and page ground'),('card','#FFFCF5','bubbles, cards'),('rule','#E3DDD1','hairlines, user bubble, button edges'),('mute','#8A857A','captions, timestamps, never body'),('ink','#1A1814','text, the four strokes'),('strike','#D3392B','the one accent: the fifth stroke'),('strike-deep','#B32E22','accent as small text (5.6:1)'),('strike-soft','#F5D6D1','tinted chip, track under red')]
dark=[('night','#161513','ground'),('night-card','#201E1A','bubbles'),('rule-dark','#35322C','hairlines'),('text-dark','#EFEAE0','text, strokes'),('mute-dark','#9B968A','captions on night'),('strike-dark','#F0604E','the fifth stroke on night')]
sysh = HEAD('Strikt system', SYS_CSS) + '<div class="stage">'
sysh += '<div class="head"><div class="h">Strikt <span class="mute" style="font-weight:400">— the system on one page</span></div><div class="cap">paper · ink · one red · newsreader / dm sans / jetbrains mono · the tally mark</div></div>'
sysh += '<div class="cols">'
# col 1 palette
sysh += '<div><div class="cap">palette · light · one accent per composition</div><div style="margin-top:8px">' + ''.join(swatch(*x) for x in light) + '</div>'
sysh += '<div class="nt"><div class="cap">palette · night</div>' + ''.join(swatch(*x) for x in dark) + '</div>'
sysh += '<div class="cap" style="margin-top:16px">contrast on paper · ink 15.9 · mute 3.3 (captions only) · strike 4.3 (≥ 18 px) · strike-deep 5.6<br>on night · text-dark 15.2 · mute-dark 6.2 · strike-dark 5.6</div></div>'
# col 2 type
sysh += '<div><div class="cap">type · three roles</div>'
sysh += '<div class="tp"><div class="cap">newsreader 500 · opsz 72 · display and wordmark · 36 px and up · −0.01em</div><div class="s1">Closed at 1'+TS+'880.<br>Bed by 00:30.</div></div>'
sysh += '<div class="tp"><div class="cap">dm sans 400 / 500 / 600 · ui and body · 16 / 1.55 · cyrillic in golos text</div><div class="s2">Chicken plate. 52 P at 540 kcal, the best ratio on the list. Fiber 11 of 30. Sleep is the one target you have not hit once this month.</div></div>'
# The specimen quotes an OPEN day: a closed day has no "left" line (render.render_day_card skips
# it, the verdict carries the shortfall), so the closed totals with a Left under them were a card
# state that cannot exist. The Left line is DM Sans here because the card sets it in DM Sans.
sysh += '<div class="tp"><div class="cap">jetbrains mono 400 / 500 · every number · tabular<br>figure-space thousands · the card&#39;s own bar glyphs</div><div class="s3">' + macro('kcal','1'+FS+'340','2'+FS+'100','',5) + '\n' + macro('P','105','180','g',5) + '</div><div class="s2" style="font-size:19px;margin-top:8px">Left: 760 kcal · 75 P · 80 C · 23 F</div><div class="cap" style="margin-top:6px">the left line is dm sans, as the card sets it</div></div>'
# "paper to card", not "paper → card": U+2192 is not in the DM Sans latin subset and would be
# rasterised from a system font.
sysh += '<div class="tp"><div class="cap">spacing · radius · motion</div><div class="s2" style="font-size:17px;line-height:1.6">Base 4 px: 4 · 8 · 12 · 16 · 24 · 32 · 48 · 64 · 96. Radius 8 controls, 12 inputs, 16 bubbles and cards, 24 large cards, pill. No shadows: elevation is a surface step, paper to card, plus a 1 px rule. Motion 150 / 200 ms, opacity and transform only; the one permitted animation is the strike being drawn.</div></div></div>'
# col 3 mark
sysh += '<div><div class="cap">mark · construction · viewbox 0 0 100 100</div><div class="cons" style="margin-top:6px">'
sysh += '<svg width="230" height="230" viewBox="0 0 100 100" style="flex:none"><rect width="100" height="100" fill="#FFFCF5" stroke="#E3DDD1" stroke-width=".6"/>'
for x in (21.5,40.5,59.5,78.5): sysh += '<line x1="%s" y1="6" x2="%s" y2="94" stroke="#8A857A" stroke-width=".4" stroke-dasharray="2 2"/>' % (x,x)
sysh += '<line x1="4" y1="74.46" x2="96" y2="25.54" stroke="#8A857A" stroke-width=".4" stroke-dasharray="2 2"/><line x1="4" y1="50" x2="96" y2="50" stroke="#8A857A" stroke-width=".4" stroke-dasharray="2 2"/>'
sysh += '<g data-markinline></g></svg>'
sysh += '<div class="notes"><b>strokes</b> x 21.5 · 40.5 · 59.5 · 78.5<br><b>width</b> 9 · <b>gap</b> 10 = 1.11 w<br><b>height</b> y 19.5 → 80.5 (caps 15 → 85)<br><b>strike</b> w 9 · 28° · bottom-left up<br><b>overshoot</b> 4.5 past the outer edge<br><b>symmetry</b> point, about (50, 50)<br><b>box</b> 84 × 70 · round caps<br><b>night</b> strike width = stroke width<br><b>never</b> mirrored, never all red</div></div>'
sysh += '<div class="cap" style="margin-top:16px">small cut · ≤ 48 px · stroke 8.5 · gap 11 · overshoot 6 · box 67 % · pixel cuts at 32 and 16</div><div class="small">'
def unit(inner, label):
    return '<div class="u">%s<span class="sz">%s</span></div>' % (inner, label)
for d in (64,48,40,32):
    sysh += unit('<div class="c" style="width:%dpx;height:%dpx"><div data-mark="small" data-size="%d"></div></div>' % (d,d,round(d*0.675)), d)
for d in (48,40):
    sysh += unit('<div class="c n" style="width:%dpx;height:%dpx"><div data-mark="small" data-size="%d" data-night></div></div>' % (d,d,round(d*0.675)), d)
sysh += unit('<div data-mark="tiny" data-size="32" data-red="#1A1814"></div>', 32)
sysh += unit('<div data-mark="micro" data-size="16" data-red="#1A1814"></div>', 16)
sysh += '</div><div class="cap" style="margin-top:8px">small cut · paper and night · favicon 32 · 16 · ink</div>'
# do / don't — one row per rule so the label is a legible 15 px line, not five wrapped 11 px lines
sysh += '<div class="cap" style="margin-top:12px">do · don\'t</div><div class="dd">'
sysh += '<div class="r"><div class="t" style="background:var(--paper)"><div data-mark="full" data-size="56"></div></div><div class="l"><b class="ok">do</b> · ink on paper, one red, the strike rising</div></div>'
sysh += '<div class="r"><div class="t"><svg width="56" height="56" viewBox="0 0 100 100"><g data-markinline="nostrike"></g><path fill="#D3392B" d="M8.5 25.5 L91.5 74.5" stroke="#D3392B" stroke-width="9" stroke-linecap="round"/></svg></div><div class="l"><b>don\'t</b> · mirror the strike: that is a prohibition sign</div></div>'
sysh += '<div class="r"><div class="t"><div data-mark="full" data-size="56"></div><span style="position:absolute;right:14px;top:14px;width:14px;height:14px;border-radius:50%;background:#6C98C4"></span></div><div class="l"><b>don\'t</b> · a second accent colour</div></div>'
sysh += '<div class="r"><div class="t"><div class="mini">Nice work!</div></div><div class="l"><b>don\'t</b> · praise, emoji, exclamation marks</div></div>'
sysh += '<div class="r"><div class="t"><div class="phone"><i></i><div class="bars"><i></i><i></i><i></i><i></i></div></div></div><div class="l"><b>don\'t</b> · device frames: cards sit flat on paper</div></div>'
sysh += '</div></div></div>'
sysh += '<div class="foot"><div data-lockup="34"></div><div class="cap">state rule: one stroke per logged meal, four at most · the red strike is drawn only when the day is closed</div></div>'
sysh += '</div><script src="mark.js"></script><script>StriktMark.mount();document.querySelectorAll("[data-markinline]").forEach(function(g){g.innerHTML=StriktMark.paths({cut:"full",strike:g.dataset.markinline!=="nostrike"})});</script></body></html>'
W('system.html', sysh)

# ---------------- brand sheet (1600x1000) ----------------
# One grid, two rows: the specimen band on top, the applications band under it. Every cell in the
# bottom row opens with its caption, so the three captions sit on one baseline and no column ends
# in a void. Gaps are the 56 / 96 steps of BRAND.md section 5.
SHEET_CSS = """
html,body{width:1600px;height:1000px}
.sheet{position:relative;width:1600px;height:1000px;padding:52px 64px;overflow:hidden}
.head{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid var(--rule);padding-bottom:14px}
.head .h{font-size:26px}
.grid{display:grid;grid-template-columns:440px 400px 1fr;column-gap:56px;row-gap:64px;margin-top:28px;align-content:start}
.col{display:flex;flex-direction:column}
.big{width:440px;height:450px;display:flex;align-items:center;justify-content:center}
.cap{font-size:11.5px;letter-spacing:.06em}
.nb{white-space:nowrap}
.c{border-radius:50%;background:var(--paper);box-shadow:inset 0 0 0 1px var(--rule);display:flex;align-items:center;justify-content:center;overflow:hidden;flex:none}
.c.n{background:var(--night);box-shadow:none}
.sizes{display:flex;align-items:flex-end;gap:32px;margin-top:8px}
.cl{width:400px;background:var(--card);border:1px solid var(--rule);border-radius:16px;padding:10px 14px;display:flex;align-items:center;gap:12px;font-size:13.5px}
.cl .tx{min-width:0;flex:1}
.cl .n{font-weight:600;font-size:15px}
.cl .m{color:var(--mute);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cl .tm{font-family:var(--mono);font-size:11.5px;color:var(--mute);align-self:flex-start;flex:none}
.cl.n{background:var(--night-card);border-color:var(--rule-dark);color:var(--text-dark)}.cl.n .m,.cl.n .tm{color:var(--mute-dark)}
.states{display:flex;gap:12px;align-items:center;margin-top:10px}
.tiles{display:flex;gap:20px;margin-top:10px}
.tile{width:190px;height:150px;border-radius:24px;display:flex;align-items:center;justify-content:center}
.night{background:var(--night);border-radius:16px;padding:22px 34px 22px 28px;margin-top:10px;display:inline-block;align-self:flex-start}
.cons{display:flex;align-items:center;gap:24px;margin-top:12px}
.cons .notes{font-family:var(--mono);font-size:11.5px;line-height:1.75;color:var(--mute);white-space:nowrap}
.cons .notes b{color:var(--ink);font-weight:500}
.bubbleWrap{display:flex;gap:12px;align-items:flex-end;margin-top:10px}
/* the reply is 41 mono columns wide, the width a phone gives a code block: 397 px holds it at
   15 px with the bubble padding (41 × 9 + 2 × 14) */
.bubble{background:var(--card);border:1px solid var(--rule);border-radius:16px;border-bottom-left-radius:4px;padding:14px 14px 10px;width:397px;font-size:15px;line-height:1.45}
.bubble .code{font-size:15px;line-height:1.6}
.bubble .time{font-size:11px;margin-top:6px}
.lbl{margin-top:10px}
"""
sheet = HEAD('Strikt brand sheet', SHEET_CSS, rel='./') + '<div class="sheet">'
sheet += '<div class="head"><div class="h">Strikt <span class="mute" style="font-weight:400">— brand sheet · the tally mark</span></div><div class="cap">four strokes in ink · the fifth is the strike · viewbox 0 0 100 100 · fonts bundled</div></div>'
sheet += '<div class="grid">'
# --- row 1: the mark, the client sizes, the lock-ups ---
sheet += '<div class="col"><div class="big"><div data-mark="full" data-size="420"></div></div>'
# Captions are broken by hand at a middot rather than left to wrap: "full cut" split across two
# lines, "night" orphaned on a line of its own and "mark-ink.svg" broken at its hyphen.
sheet += '<div class="cap">mark.svg · 420 px · ink #1A1814 / strike #D3392B<br>full cut: stroke 9 · gap 10 · strike 9 at 28° · over 4.5</div></div>'
sheet += '<div class="col"><div class="cap">telegram · circle crop · 96 and 40 px<br>paper · night</div><div class="sizes">'
for d,n in ((96,False),(40,False),(96,True),(40,True)):
    sheet += '<div class="c%s" style="width:%dpx;height:%dpx"><div data-mark="small" data-size="%d"%s></div></div>' % (' n' if n else '', d,d, round(d*0.675), ' data-night' if n else '')
sheet += '</div><div class="cap lbl" style="margin-top:24px">chat list · 40 px</div>'
# A Telegram preview always starts at the first line of the last message: the closed card's verdict.
for n in (False,True):
    sheet += '<div class="cl%s" style="margin-top:%dpx"><div class="c%s" style="width:40px;height:40px"><div data-mark="small" data-size="27"%s></div></div><div class="tx"><div class="n">Strikt</div><div class="m">%s</div></div><div class="tm">22:41</div></div>' % (' n' if n else '', 10 if not n else 8, ' n' if n else '', ' data-night' if n else '', VERDICT)
sheet += '<div class="cap lbl" style="margin-top:24px">state · one stroke per logged meal<br>the strike when the day is closed</div><div class="states">'
for b in (1,2,3,4): sheet += '<div data-mark="full" data-size="56" data-bars="%d" data-strike="0"></div>' % b
sheet += '<div data-mark="full" data-size="56"></div><div data-mark="full" data-size="56" data-red="#1A1814"></div></div><div class="cap" style="margin-top:6px">1 · 2 · 3 · 4 · closed · all-ink <span class="nb">(mark-ink.svg)</span></div></div>'
sheet += '<div class="col"><div class="cap">lock-up · newsreader 500, opsz 72 · primary</div><div style="margin-top:14px"><div data-lockup="112"></div></div>'
sheet += '<div class="cap" style="margin-top:44px">lock-up · dm sans 500 · alternate</div><div style="margin-top:14px"><div data-lockup="100" data-sans></div></div>'
sheet += '<div class="cap" style="margin-top:44px">lock-up on night · for images on the black-and-white site</div><div class="night"><div data-lockup="60" data-night></div></div></div>'
# --- row 2: the avatar geometry, the tiles, one real message ---
sheet += '<div class="col"><div class="cap">avatar-512 · small cut · box 67 % · 74 % safe circle</div><div class="cons">'
sheet += '<svg width="176" height="176" viewBox="0 0 512 512"><rect width="512" height="512" fill="#F6F2E9" stroke="#E3DDD1" stroke-width="2"/><circle cx="256" cy="256" r="255" fill="none" stroke="#E3DDD1" stroke-width="2"/><circle cx="256" cy="256" r="189.4" fill="none" stroke="#8A857A" stroke-dasharray="8 8" stroke-width="2"/><g transform="translate(84.48 84.48) scale(3.4304)" data-markinline="small"></g></svg>'
sheet += '<div class="notes"><b>small cut</b> stroke 8.5 · gap 11 · overshoot 6<br><b>at 40 px</b> stroke 2.3 px · gap 3 px<br><b>farthest ink</b> 168 px from centre · 66 % of r<br><b>night</b> strike width = stroke width<br><b>one jpg</b> serves light and dark clients</div></div></div>'
sheet += '<div class="col"><div class="cap">light tile · night tile (mark-night.svg)</div><div class="tiles"><div class="tile" style="background:var(--card);border:1px solid var(--rule)"><div data-mark="full" data-size="88"></div></div><div class="tile" style="background:var(--night)"><div data-mark="full" data-size="88" data-night></div></div></div></div>'
sheet += '<div class="col"><div class="cap">telegram · food reply</div><div class="bubbleWrap"><div class="c" style="width:40px;height:40px"><div data-mark="small" data-size="27"></div></div><div class="bubble"><p>Chicken thigh, rice, cucumber salad. About 450 g.</p><span class="code">' + food_reply.split('<span class="code">')[1].split('</span>')[0] + '</span><p style="margin-top:6px">Fiber 11 of 30. Dinner gets a vegetable.</p><div class="time mono mute" style="text-align:right">13:21</div></div></div></div>'
sheet += '</div><script src="src/mark.js"></script><script>StriktMark.mount();document.querySelectorAll("[data-markinline]").forEach(function(g){g.innerHTML=StriktMark.paths({cut:g.dataset.markinline||"full"})});</script></body></html>'
W('sheet.html', sheet, root=True)
print('written system + sheet')
