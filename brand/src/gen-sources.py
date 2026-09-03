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
def bar(filled):
    return '<span class="bar">' + ''.join('<i class="%s"></i>' % ('f' if i<filled else 'e') for i in range(8)) + '</span>'
def macro(label, val, tgt, unit, filled):
    # mirrors render._macro_line: f"{label:<5}{value:>6} /{target:>6}{unit}" + 2 spaces + 8-cell bar
    return '%-5s%6s /%6s%s  %s' % (label, val, tgt, unit, bar(filled))
def bubble(inner, time, user=False, extra=''):
    return '<div class="bubble%s"%s>%s<div class="time">%s</div></div>' % (' user' if user else '', extra, inner, time)
def row(inner, user=False, night=False):
    if user: return '<div class="row user">%s</div>' % inner
    return '<div class="row">%s%s</div>' % (av(40, night), inner)
def kb(*labels):
    return '<div class="kb">%s</div>' % ''.join('<span>%s</span>' % l for l in labels)

BAR_CSS = """
.bar{display:inline-flex;vertical-align:-0.08em;height:0.68em;margin-left:0}
.bar i{display:block;width:1ch;height:100%;background:var(--rule)}
.bar i.f{background:var(--ink)}
.night .bar i{background:var(--rule-dark)} .night .bar i.f{background:var(--text-dark)}
"""
TS=' '

# ---------------- og ----------------
W('og.html', HEAD('Strikt og', """
.stage{width:1200px;height:630px;padding:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.line{font-size:26px;color:var(--mute);margin-top:44px}
""") + '<div class="stage"><div data-lockup="168"></div><div class="line">Send the photo. Get the number.</div></div>' + TAIL())

# ---------------- hero + closed ----------------
CARD_CSS = BAR_CSS + """
.stage{display:flex;align-items:center;gap:120px}
.chat{width:900px}
.bubble{max-width:900px;padding:22px 28px 14px}
.bubble .code{font-size:22px}
.state{display:flex;flex-direction:column;align-items:flex-start;gap:24px;width:440px}
.state .cap{line-height:1.75}
.top{position:absolute;left:112px;top:80px}
"""
def card(title, lines, left, meals, tail, time):
    b = '<p><b>%s</b></p>' % title
    b += '<span class="code">%s</span>' % '\n'.join(lines)
    b += '<p style="margin-top:6px">%s</p>' % left
    b += '<p><b>Meals</b><br>%s</p>' % '<br>'.join(meals)
    b += ''.join('<p style="margin-top:6px">%s</p>' % t for t in tail)
    b += '<div class="time">%s</div>' % time
    return b
open_lines=[macro('kcal','1 340','2 100','',5),macro('P','105','180','g',5),macro('C','120','200','g',5),macro('F','47','70','g',5),macro('fiber','13','30','g',3)]
closed_lines=[macro('kcal','1 940','2 100','',7),macro('P','157','180','g',7),macro('C','168','200','g',7),macro('F','69','70','g',8),macro('fiber','22','30','g',6)]
meals=['• 08:10 breakfast — Skyr, oats, blueberries · 480','• 13:20 lunch — Chicken thigh, rice, cucumber salad · 590','• 16:40 snack — Greek yogurt, walnuts · 270','• 19:50 dinner — Salmon, potatoes, broccoli · 600']
hero = HEAD('Strikt hero', CARD_CSS) + '<div class="stage"><div class="top cap">the today card · pinned in the chat · day open</div>'
hero += '<div class="chat"><div class="row">%s<div class="bubble">%s</div></div></div>' % (av(), card('Today · Wed 3 Sep', open_lines, 'Left: 760 kcal · 75 P · 80 C · 23 F', meals[:3], ['<b>Sleep</b>: 6h48 · 84% · recovery 61%'], '17:02'))
hero += '<div class="state"><div data-mark="full" data-size="260" data-bars="3"></div><div class="cap">day open · three meals logged<br>one stroke per meal, four at most<br>no strike until the day is closed</div></div>'
hero += '<div class="foot"><div data-lockup="34"></div><div class="cap">the day card · pinned, edited in place, closed at night</div></div></div>' + TAIL()
W('hero.html', hero)
closed = HEAD('Strikt card closed', CARD_CSS) + '<div class="stage"><div class="top cap">the today card · pinned in the chat · day closed</div>'
closed += '<div class="chat"><div class="row">%s<div class="bubble">%s</div></div></div>' % (av(), card('Today · Wed 3 Sep · closed', closed_lines, 'Left: 160 kcal · 23 P · 32 C · 1 F', meals,
   ['<b>Training</b>: strength · 62 min · strain 12.4 · 410 kcal','<b>Sleep</b>: 6h48 · 84% · recovery 61%','<b>Verdict</b>: Closed at 1'+TS+'940 / 157 P / 22 fiber. Protein short 23 g, third day running. Bed by 00:30.'], '22:41'))
closed += '<div class="state"><div data-mark="full" data-size="260"></div><div class="cap">day closed · four meals<br>the fifth stroke is the verdict<br>drawn once, in red, by the bot</div></div>'
closed += '<div class="foot"><div data-lockup="34"></div><div class="cap">the day card · a verdict, not encouragement</div></div></div>' + TAIL()
W('card-closed.html', closed)

# ---------------- food reply ----------------
PLATE = """<svg viewBox="0 0 400 300" width="400" height="300" fill="none" stroke="#1A1814" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
<ellipse cx="200" cy="152" rx="168" ry="118"/>
<ellipse cx="200" cy="152" rx="138" ry="92"/>
<path d="M92 156c-4-28 18-52 52-54c30-2 56 14 60 40c3 24-14 42-44 46c-34 4-64-6-68-32z"/>
<path d="M120 132l8-3M136 122l9-2M156 118l9 2M176 128l6 6M112 156l10-1M132 166l10 2M158 168l10-2M180 156l8 4M146 144l10 0M164 144l8 4M124 148l8 2"/>
<path d="M214 100c14-16 40-20 62-12c24 9 36 30 30 52c-5 18-22 30-44 30c-8 0-15-2-21-5l-16 22c-4 6-13 7-18 2c-5-5-4-13 2-18l18-14c-20-14-27-38-13-57z"/>
<path d="M240 118c10-6 24-6 34 2M232 138c12 10 28 12 42 4M256 160c8 2 16 0 22-4"/>
<circle cx="238" cy="206" r="19"/><circle cx="238" cy="206" r="11"/><path d="M238 187v8M238 217v8M219 206h8M249 206h8"/>
<circle cx="278" cy="196" r="19"/><circle cx="278" cy="196" r="11"/><path d="M278 177v8M278 207v8M259 196h8M289 196h8"/>
<circle cx="204" cy="224" r="19"/><circle cx="204" cy="224" r="11"/><path d="M204 205v8M204 235v8M185 224h8M215 224h8"/>
</svg>"""
FOOD_CSS = BAR_CSS + """
.stage{display:flex;align-items:center;justify-content:center}
.chat{width:960px}
.bubble{max-width:960px}
.bubble.photo{padding:10px;background:var(--rule)}
.bubble.photo .img{background:var(--card);border:0;width:400px;height:300px}
.bubble .code{font-size:21px}
.top{position:absolute;left:112px;top:80px}
"""
food_reply = ('<p>Chicken thigh, rice, cucumber salad. About 450 g.</p>'
 '<span class="code">' +
 'chicken thigh 180 g      325 kcal · 41 P ·  0 C · 17 F\n'
 'rice 150 g               195 kcal ·  4 P · 42 C ·  1 F\n'
 'cucumber salad 120 g      70 kcal ·  1 P ·  4 C ·  5 F\n'
 '<b>meal                     590 kcal · 46 P · 46 C · 23 F · 3 fiber</b>\n'
 'today                  1 070 / 2 100 kcal · 84 P\n'
 'left                   1 030 kcal · 96 P · 92 C · 38 F · 19 fiber' +
 '</span><p style="margin-top:10px">Fiber 11 of 30. Dinner gets a vegetable.</p>')
food = HEAD('Strikt food reply', FOOD_CSS) + '<div class="stage"><div class="top cap">a food photo · answered with the number</div><div class="chat">'
food += row(bubble('<div class="img">%s</div>' % PLATE, '13:20', user=True, extra=' class="bubble user photo"').replace('class="bubble user" class="bubble user photo"','class="bubble user photo"'), user=True)
food += row(bubble(food_reply, '13:21')) + kb('Undo','Recalculate')
food += '</div><div class="foot"><div data-lockup="34"></div><div class="cap">per item, the meal, the day, what is left · one line of advice at most</div></div></div>' + TAIL()
W('food-reply.html', food)

# ---------------- ladder ----------------
LADDER_CSS = """
.stage{padding-top:80px}
.top{position:absolute;left:112px;top:80px}
.lad{position:absolute;left:112px;right:112px;top:400px}
.line{position:absolute;left:0;right:0;top:0;height:1px;background:var(--rule)}
.steps{display:grid;grid-template-columns:repeat(4,1fr);column-gap:40px}
.step{position:relative;padding-top:0}
.tick{position:absolute;left:0;top:-4px;width:1px;height:9px;background:var(--mute)}
.step .cap{padding-top:22px;line-height:1.7}
.step .cap b{color:var(--ink);font-weight:500}
.step .row{margin-top:26px;align-items:flex-start}
.step .bubble{font-size:23px;padding:18px 22px 12px}
.note{position:absolute;left:112px;right:112px;bottom:80px;display:flex;justify-content:space-between;align-items:flex-end}
"""
steps=[('14:10','1 · prompt','one line, factual','Nothing logged yet. Breakfast?'),
       ('14:55','2 · push','the pattern, from data','Two hours past your usual first meal. Skipped breakfasts in your history end at 2'+TS+'600 kcal evenings.'),
       ('15:40','3 · demand','an instruction with a deadline','Eat something with 40 g protein in the next hour and send me a photo.'),
       ('16:25','4 · consequence','the goal in concrete terms','Waist target is 94. You\'re at 103. Days like this cost a week each.')]
lad = HEAD('Strikt ladder', LADDER_CSS) + '<div class="stage"><div class="top cap">the escalation ladder · one silent day · four sends, 45 minutes apart</div>'
lad += '<div class="lad"><div class="line"></div><div class="steps">'
for t,s,d,txt in steps:
    lad += '<div class="step"><div class="tick"></div><div class="cap"><b>%s</b> · %s<br>%s</div>%s</div>' % (t,s,d,row(bubble(txt,t)))
lad += '</div></div>'
lad += '<div class="note"><div class="foot" style="position:static"><div data-lockup="34"></div><div class="cap">never beyond step four · resets on any reply · quiet hours 00:00–07:30 · at most five sends a day</div></div></div></div>' + TAIL()
W('ladder.html', lad)

# ---------------- menu ----------------
MENU_CSS = BAR_CSS + """
.stage{display:flex;align-items:center;justify-content:center;gap:56px}
.chat{width:1620px;flex-direction:row;align-items:flex-end;gap:48px}
.bubble.photo{padding:10px;background:var(--rule)}
.app{width:420px;background:#fff;border-radius:8px;padding:22px 24px 8px;color:#1A1814;font-size:18px}
.app .hd{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid #E3DDD1;padding-bottom:14px;margin-bottom:6px}
.app .hd b{font-size:22px;font-weight:600}
.app .hd span{color:#8A857A;font-size:15px}
.app .it{display:flex;justify-content:space-between;align-items:center;padding:14px 0;border-bottom:1px solid #EEE9DF}
.app .it:last-child{border-bottom:0}
.app .it .n{font-weight:500}
.app .it .w{color:#8A857A;font-size:15px;margin-top:2px}
.app .it .p{font-family:var(--mono);font-size:17px;color:#1A1814}
.app .it .ph{width:56px;height:56px;border-radius:8px;background:#EEE9DF;margin-right:14px;flex:none}
.app .it .l{display:flex;align-items:center;flex:1}
.bubble.bot{max-width:none}
.bubble .code{font-size:21px}
.bubble .code b{font-weight:500}
.top{position:absolute;left:112px;top:80px}
"""
items=[('Grilled chicken plate','380 g','9.50'),('Beef burger with fries','450 g','8.90'),('Falafel bowl','420 g','7.90'),('Margherita pizza','33 cm','9.90'),('Caesar with chicken','300 g','8.50')]
app = '<div class="app"><div class="hd"><b>Grill house</b><span>25–35 min · 4.7</span></div>'
for n,w,p in items:
    app += '<div class="it"><div class="l"><div class="ph"></div><div><div class="n">%s</div><div class="w">%s</div></div></div><div class="p">%s</div></div>' % (n,w,p)
app += '</div>'
menu_reply = ('<p>Left: 1'+TS+'030 kcal · 96 P.</p><span class="code">' +
 '<b>pick</b>  grilled chicken plate     540 kcal · 52 P    9.6 P / 100 kcal\n'
 'okay  beef burger, no fries     620 kcal · 38 P    6.1\n'
 'okay  falafel bowl              580 kcal · 24 P    4.1\n'
 'skip  caesar with chicken       690 kcal · 31 P    4.5\n'
 'skip  margherita pizza        1 180 kcal · 44 P    3.7' +
 '</span><p style="margin-top:10px">Chicken plate. Extra rice if you train tonight.</p>')
menu = HEAD('Strikt menu', MENU_CSS) + '<div class="stage"><div class="top cap">a delivery menu screenshot · pick, okay, skip · protein per calorie</div><div class="chat">'
menu += '<div class="row user" style="align-items:flex-end">%s</div>' % bubble(app, '18:47', user=True).replace('class="bubble user"','class="bubble user photo"')
menu += '<div class="row" style="align-items:flex-end">%s<div class="bubble bot">%s<div class="time">18:47</div></div></div>' % (av(), menu_reply)
menu += '</div><div class="foot"><div data-lockup="34"></div><div class="cap">the decision first, then the reason · the same numbers every time</div></div></div>' + TAIL()
W('menu.html', menu)

# ---------------- russian ----------------
RU_CSS = BAR_CSS + """
.stage{display:flex;align-items:center;justify-content:center}
.chat{width:1000px}
.bubble{max-width:1000px;font-family:var(--cyr)}
.bubble .code{font-size:21px}
.kb span{font-family:var(--cyr)}
.top{position:absolute;left:112px;top:80px}
"""
ru_reply = ('<p>Chicken thigh, рис, салат из огурцов. Около 450 г.</p><span class="code">' +
 'chicken thigh 180 г        325 ккал · 41 Б ·  0 У · 17 Ж\n'
 'рис 150 г                  195 ккал ·  4 Б · 42 У ·  1 Ж\n'
 'салат из огурцов 120 г      70 ккал ·  1 Б ·  4 У ·  5 Ж\n'
 '<b>приём                      590 ккал · 46 Б · 46 У · 23 Ж · 3 клетчатка</b>\n'
 'сегодня                  1 070 / 2 100 ккал · 84 Б\n'
 'осталось                 1 030 ккал · 96 Б · 92 У · 38 Ж · 19 клетчатка' +
 '</span><p style="margin-top:10px">Клетчатка 11 из 30. На ужин — овощи.</p>')
ru = HEAD('Strikt russian', RU_CSS, lang='ru') + '<div class="stage"><div class="top cap">language mirroring · russian in, russian out · food names stay as written</div><div class="chat">'
ru += row(bubble('обед: chicken thigh с рисом и салат из огурцов, грамм 450', '13:20', user=True), user=True)
ru += row(bubble(ru_reply, '13:21')) + kb('Отменить','Пересчитать')
ru += '</div><div class="foot"><div data-lockup="34"></div><div class="cap">the bot never switches language on its own · units metric</div></div></div>' + TAIL()
W('russian.html', ru)

# ---------------- telegram profile ----------------
PROF_CSS = """
.stage{display:flex;gap:64px;align-items:flex-end;justify-content:center;padding:0 112px 80px}
.panel{width:816px;height:860px;border-radius:24px;padding:36px 0 0;overflow:hidden;background:var(--card);border:1px solid var(--rule);color:var(--ink)}
.panel.night{background:var(--night-card);border-color:var(--rule-dark);color:var(--text-dark)}
.prof{display:flex;align-items:center;gap:26px;padding:0 36px 30px;border-bottom:1px solid var(--rule)}
.night .prof{border-color:var(--rule-dark)}
.prof .nm{font-size:30px;font-weight:600;letter-spacing:-.01em}
.prof .st{font-size:17px;color:var(--mute);margin-top:2px}
.night .prof .st{color:var(--mute-dark)}
.about{padding:22px 36px 22px;font-size:19px;line-height:1.45;border-bottom:8px solid var(--paper)}
.night .about{border-color:var(--night)}
.about .lab{font-size:14px;color:var(--mute);letter-spacing:.06em;text-transform:uppercase;font-family:var(--mono);margin-bottom:6px}
.night .about .lab{color:var(--mute-dark)}
.chats .ttl{font-family:var(--mono);font-size:14px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);padding:20px 36px 8px}
.night .chats .ttl{color:var(--mute-dark)}
.ci{display:flex;align-items:center;gap:18px;padding:14px 36px;border-bottom:1px solid var(--rule)}
.night .ci{border-color:var(--rule-dark)}
.ci .ini{width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:var(--paper);font-weight:600;font-size:17px;flex:none}
.ci .tx{flex:1;min-width:0}
.ci .n{font-size:18px;font-weight:600;display:flex;justify-content:space-between;align-items:baseline}
.ci .n span{font-family:var(--mono);font-size:13px;font-weight:400;color:var(--mute);letter-spacing:.02em}
.night .ci .n span{color:var(--mute-dark)}
.ci .m{font-size:16px;color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px;display:flex;justify-content:space-between;gap:12px}
.night .ci .m{color:var(--mute-dark)}
.ci .m em{font-style:normal;color:var(--ink)}
.night .ci .m em{color:var(--text-dark)}
.ci .badge{font-family:var(--mono);font-size:12px;background:var(--mute);color:var(--paper);border-radius:999px;padding:2px 8px;flex:none}
.night .ci .badge{background:var(--mute-dark);color:var(--night)}
.top{position:absolute;left:112px;top:80px}
"""
contacts=[('Strikt',None,'Closed at 1'+TS+'940 / 157 P / 22 fiber. Protein short 23 g, third day running. Bed by 00:30.','22:41','1'),
          ('Anna','A','see you at 8','22:13',None),('Running club','R','<em>Marina:</em> 10k tomorrow, 7:30 at the bridge','21:50','3'),
          ('Dad','D','Call me when you land','19:02',None),('Kirill','K','Photo','18:27',None),('Deliveries','De','Your order is on its way','13:05',None)]
tints=['#8A857A','#A39D90','#6E6960','#B7B1A4','#8A857A']
def panel(night):
    p = '<div class="panel%s">' % (' night' if night else '')
    p += '<div class="prof">%s<div><div class="nm">Strikt</div><div class="st">bot</div></div></div>' % av(96, night)
    p += '<div class="about"><div class="lab">about</div>A coach in one chat. Send food, get the number. The day ends with a verdict.</div>'
    p += '<div class="chats"><div class="ttl">chats</div>'
    ti=0
    for n,ini,m,t,b in contacts:
        if ini is None: a = av(40, night)
        else:
            a = '<div class="ini" style="background:%s">%s</div>' % (tints[ti%len(tints)], ini[0]); ti+=1
        p += '<div class="ci">%s<div class="tx"><div class="n">%s<span>%s</span></div><div class="m"><span style="overflow:hidden;text-overflow:ellipsis">%s</span>%s</div></div></div>' % (a,n,t,m,('<span class="badge">%s</span>'%b) if b else '')
    p += '</div></div>'
    return p
prof = HEAD('Strikt telegram profile', PROF_CSS) + '<div class="stage"><div class="top cap">telegram · bot profile and chat list · 40 px avatar on paper and on night</div>' + panel(False) + panel(True) + '</div>' + TAIL()
W('telegram-profile.html', prof)

# ---------------- avatars and favicons ----------------
W('avatar.html', HEAD('Strikt avatar', 'html,body{width:100vw;height:100vh;background:#F6F2E9;overflow:hidden}.b{width:67vw;height:67vw;margin:16.5vw auto 0}.b svg{width:100%;height:100%}') + '<div class="b" data-mark="small" data-size="100"></div>' + TAIL())
W('avatar-night.html', HEAD('Strikt avatar night', 'html,body{width:100vw;height:100vh;background:#161513;overflow:hidden}.b{width:67vw;height:67vw;margin:16.5vw auto 0}.b svg{width:100%;height:100%}') + '<div class="b" data-mark="small" data-size="100" data-night></div>' + TAIL())
W('favicon.html', HEAD('Strikt favicon', 'html,body{width:100vw;height:100vh;background:transparent;overflow:hidden}.b{width:100vw;height:100vw}.b svg{width:100%;height:100%}') + '<div class="b" data-mark="small" data-size="100" data-red="#1A1814"></div>' + TAIL())
W('favicon-180.html', HEAD('Strikt favicon 180', 'html,body{width:100vw;height:100vh;background:#F6F2E9;overflow:hidden}.b{width:72vw;height:72vw;margin:14vw auto 0}.b svg{width:100%;height:100%}') + '<div class="b" data-mark="small" data-size="100"></div>' + TAIL())
print('written')

# ---------------- system sheet (1920x1080) ----------------
SYS_CSS = BAR_CSS + """
.stage{padding:72px 112px}
.head{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid var(--rule);padding-bottom:16px}
.head .h{font-size:28px}
.cols{display:grid;grid-template-columns:500px 520px 1fr;column-gap:56px;margin-top:34px}
.sec{margin-top:26px}.sec:first-child{margin-top:0}
.sw{display:grid;grid-template-columns:40px 130px 90px 1fr;align-items:center;column-gap:14px;padding:7px 0;border-bottom:1px solid var(--rule);font-size:15px}
.sw:last-child{border-bottom:0}
.sw i{display:block;width:40px;height:28px;border-radius:6px;box-shadow:inset 0 0 0 1px rgba(26,24,20,.08)}
.sw .nm{font-weight:500}.sw .hx{font-family:var(--mono);font-size:14px;color:var(--mute)}.sw .us{color:var(--mute);font-size:14px}
.nt{background:var(--night);color:var(--text-dark);border-radius:16px;padding:14px 18px 6px;margin-top:14px}
.nt .sw{border-color:var(--rule-dark)}.nt .sw .hx,.nt .sw .us{color:var(--mute-dark)}
.tp{border-bottom:1px solid var(--rule);padding:14px 0 16px}.tp:last-child{border-bottom:0}
.tp .cap{margin-bottom:8px}
.tp .s1{font-family:var(--serif);font-variation-settings:'opsz' 72;font-weight:500;font-size:40px;letter-spacing:-.01em;line-height:1.08}
.tp .s2{font-size:21px;line-height:1.5}
.tp .s3{font-family:var(--mono);font-size:20px;line-height:1.55;white-space:pre;font-variant-numeric:tabular-nums}
.tp .s3 b{font-weight:500}
.cons{display:flex;gap:28px;align-items:flex-start}
.cons .notes{font-family:var(--mono);font-size:13.5px;line-height:1.8;color:var(--mute);white-space:nowrap}
.cons .notes b{color:var(--ink);font-weight:500}
.small{display:flex;gap:22px;align-items:center;margin-top:8px}
.c{border-radius:50%;background:var(--paper);box-shadow:inset 0 0 0 1px var(--rule);display:flex;align-items:center;justify-content:center;overflow:hidden}
.c.n{background:var(--night);box-shadow:none}
.dd{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-top:10px}
.dd .t{background:var(--card);border:1px solid var(--rule);border-radius:12px;height:112px;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden}
.dd .l{font-family:var(--mono);font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin-top:8px;line-height:1.5}
.dd .l b{color:var(--strike-deep);font-weight:500}.dd .l b.ok{color:var(--ink)}
.mini{font-size:11px;line-height:1.35;background:var(--paper);border:1px solid var(--rule);border-radius:8px;border-bottom-left-radius:2px;padding:7px 9px;max-width:120px}
.phone{width:54px;height:96px;border:2px solid var(--ink);border-radius:10px;position:relative;display:flex;align-items:center;justify-content:center}
.phone i{position:absolute;top:5px;left:50%;width:14px;height:3px;margin-left:-7px;border-radius:2px;background:var(--ink)}
.phone .bars{display:flex;flex-direction:column;gap:4px;width:32px}
.phone .bars i{position:static;width:100%;height:4px;margin:0;border-radius:1px;background:var(--rule)}
.phone .bars i:first-child{background:var(--ink);width:70%}
"""
def swatch(name, hexv, use):
    return '<div class="sw"><i style="background:%s"></i><span class="nm">%s</span><span class="hx">%s</span><span class="us">%s</span></div>' % (hexv,name,hexv,use)
light=[('paper','#F6F2E9','image and page ground'),('card','#FFFCF5','bubbles, cards'),('rule','#E3DDD1','hairlines, user bubble, bar track'),('mute','#8A857A','captions, timestamps, never body'),('ink','#1A1814','text, the four strokes'),('strike','#D3392B','the one accent: the fifth stroke'),('strike-deep','#B32E22','accent as small text (5.6:1)'),('strike-soft','#F5D6D1','tinted chip, track under red')]
dark=[('night','#161513','ground'),('night-card','#201E1A','bubbles'),('rule-dark','#35322C','hairlines'),('text-dark','#EFEAE0','text, strokes'),('strike-dark','#F0604E','the fifth stroke on night')]
sysh = HEAD('Strikt system', SYS_CSS) + '<div class="stage">'
sysh += '<div class="head"><div class="h">Strikt <span class="mute" style="font-weight:400">— the system on one page</span></div><div class="cap">paper · ink · one red · newsreader / dm sans / jetbrains mono · the tally mark</div></div>'
sysh += '<div class="cols">'
# col 1 palette
sysh += '<div><div class="cap">palette · light · one accent per composition apart from the mark</div><div style="margin-top:8px">' + ''.join(swatch(*x) for x in light) + '</div>'
sysh += '<div class="nt"><div class="cap">palette · night</div>' + ''.join(swatch(*x) for x in dark) + '</div>'
sysh += '<div class="cap" style="margin-top:16px">contrast on paper · ink 15.9 · mute 3.3 (captions only) · strike 4.3 (≥ 18 px) · strike-deep 5.6<br>on night · text-dark 15.2 · mute-dark 6.2 · strike-dark 5.6</div></div>'
# col 2 type
sysh += '<div><div class="cap">type · three roles</div>'
sysh += '<div class="tp"><div class="cap">newsreader 500 · opsz 72 · display and wordmark · 36 px and up · −0.01em</div><div class="s1">Closed at 1'+TS+'940.<br>Bed by 00:30.</div></div>'
sysh += '<div class="tp"><div class="cap">dm sans 400 / 500 / 600 · ui and body · 16 / 1.55 · cyrillic in golos text</div><div class="s2">Take the pizza. 95 g protein at 620 kcal, twice the burger\'s ratio. Fiber is fine. Sleep is the one parameter you have not hit once this month.</div></div>'
sysh += '<div class="tp"><div class="cap">jetbrains mono 400 / 500 · every number · tabular · thin-space thousands</div><div class="s3">' + macro('kcal','1 940','2 100','',7) + '\n' + macro('P','157','180','g',7) + '\n<b>left   160 kcal · 23 P · 32 C · 1 F</b></div></div>'
sysh += '<div class="tp"><div class="cap">spacing · radius · motion</div><div class="s2" style="font-size:17px;line-height:1.6">Base 4 px: 4 · 8 · 12 · 16 · 24 · 32 · 48 · 64 · 96. Radius 8 controls, 12 inputs, 16 bubbles and cards, 24 large cards, pill. No shadows: elevation is paper → card plus a 1 px rule. Motion 150 / 200 ms, opacity and transform only; the one permitted animation is the strike being drawn.</div></div></div>'
# col 3 mark
sysh += '<div><div class="cap">mark · construction · viewbox 0 0 100 100</div><div class="cons" style="margin-top:6px">'
sysh += '<svg width="250" height="250" viewBox="0 0 100 100" style="flex:none"><rect width="100" height="100" fill="#FFFCF5" stroke="#E3DDD1" stroke-width=".6"/>'
for x in (21.5,40.5,59.5,78.5): sysh += '<line x1="%s" y1="6" x2="%s" y2="94" stroke="#8A857A" stroke-width=".4" stroke-dasharray="2 2"/>' % (x,x)
sysh += '<line x1="4" y1="74.46" x2="96" y2="25.54" stroke="#8A857A" stroke-width=".4" stroke-dasharray="2 2"/><line x1="4" y1="50" x2="96" y2="50" stroke="#8A857A" stroke-width=".4" stroke-dasharray="2 2"/>'
sysh += '<g data-markinline></g></svg>'
sysh += '<div class="notes"><b>strokes</b> x 21.5 · 40.5 · 59.5 · 78.5<br><b>width</b> 9 · <b>gap</b> 10 = 1.11 w<br><b>height</b> y 19.5 → 80.5 (caps 15 → 85)<br><b>strike</b> w 9 · 28° · bottom-left up<br><b>overshoot</b> 4.5 past the outer edge<br><b>symmetry</b> point, about (50, 50)<br><b>box</b> 84 × 70 · round caps<br><b>night</b> strike width = stroke width<br><b>never</b> mirrored, never all red</div></div>'
sysh += '<div class="cap" style="margin-top:18px">small cut · ≤ 48 px · stroke 8.5 · gap 11 · overshoot 6 · avatar box 67 %</div><div class="small">'
for d in (64,48,40,32):
    sysh += '<div class="c" style="width:%dpx;height:%dpx"><div data-mark="small" data-size="%d"></div></div>' % (d,d,round(d*0.675))
for d in (48,40):
    sysh += '<div class="c n" style="width:%dpx;height:%dpx"><div data-mark="small" data-size="%d" data-night></div></div>' % (d,d,round(d*0.675))
sysh += '<div data-mark="small" data-size="32" data-red="#1A1814"></div><span class="cap">favicon · ink</span></div>'
# do / don't
sysh += '<div class="cap" style="margin-top:20px">do · don\'t</div><div class="dd">'
sysh += '<div><div class="t" style="background:var(--paper)"><div data-mark="full" data-size="72"></div></div><div class="l"><b class="ok">do</b> · ink on paper, one red, bottom-left up</div></div>'
sysh += '<div><div class="t"><svg width="72" height="72" viewBox="0 0 100 100"><g data-markinline="nostrike"></g><path fill="#D3392B" d="M8.5 25.5 L91.5 74.5" stroke="#D3392B" stroke-width="9" stroke-linecap="round"/></svg></div><div class="l"><b>don\'t</b> · strike the other way: that is a prohibition sign</div></div>'
sysh += '<div><div class="t"><div data-mark="full" data-size="72"></div><span style="position:absolute;right:16px;top:16px;width:14px;height:14px;border-radius:50%;background:#6C98C4"></span></div><div class="l"><b>don\'t</b> · a second accent colour</div></div>'
sysh += '<div><div class="t"><div class="mini">Nice work! Keep it up</div></div><div class="l"><b>don\'t</b> · praise, emoji, exclamation marks</div></div>'
sysh += '<div><div class="t"><div class="phone"><i></i><div class="bars"><i></i><i></i><i></i><i></i></div></div></div><div class="l"><b>don\'t</b> · device frames: cards sit flat on paper</div></div>'
sysh += '</div></div></div>'
sysh += '<div class="foot" style="bottom:56px"><div data-lockup="34"></div><div class="cap">state rule: one stroke per logged meal, four at most · the red strike is drawn only when the day is closed</div></div>'
sysh += '</div><script src="mark.js"></script><script>StriktMark.mount();document.querySelectorAll("[data-markinline]").forEach(function(g){g.innerHTML=StriktMark.paths({cut:"full",strike:g.dataset.markinline!=="nostrike"})});</script></body></html>'
W('system.html', sysh)

# ---------------- brand sheet (1600x1000) ----------------
SHEET_CSS = BAR_CSS + """
html,body{width:1600px;height:1000px}
.sheet{position:relative;width:1600px;height:1000px;padding:52px 64px;overflow:hidden}
.head{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid var(--rule);padding-bottom:14px}
.head .h{font-size:26px}
.grid{display:grid;grid-template-columns:440px 400px 1fr;column-gap:56px;margin-top:28px;height:820px}
.col{display:flex;flex-direction:column}
.big{width:440px;height:400px;display:flex;align-items:center;justify-content:center}
.cap{font-size:11.5px;letter-spacing:.06em}
.c{border-radius:50%;background:var(--paper);box-shadow:inset 0 0 0 1px var(--rule);display:flex;align-items:center;justify-content:center;overflow:hidden;flex:none}
.c.n{background:var(--night);box-shadow:none}
.sizes{display:flex;align-items:flex-end;gap:32px;margin-top:8px}
.cl{width:400px;background:var(--card);border:1px solid var(--rule);border-radius:16px;padding:10px 14px;display:flex;align-items:center;gap:12px;font-size:13.5px}
.cl .n{font-weight:600;font-size:15px}.cl .m{color:var(--mute);margin-top:2px}.cl .tm{font-family:var(--mono);font-size:11.5px;color:var(--mute);margin-left:auto;align-self:flex-start}
.cl.n{background:var(--night-card);border-color:var(--rule-dark);color:var(--text-dark)}.cl.n .m,.cl.n .tm{color:var(--mute-dark)}
.states{display:flex;gap:14px;align-items:center;margin-top:10px}
.tiles{display:flex;gap:20px;margin-top:10px}
.tile{width:190px;height:150px;border-radius:24px;display:flex;align-items:center;justify-content:center}
.night{background:var(--night);border-radius:16px;padding:22px 34px 22px 28px;margin-top:10px;display:inline-block;align-self:flex-start}
.cons{display:flex;align-items:center;gap:24px;margin-top:12px}
.cons .notes{font-family:var(--mono);font-size:11.5px;line-height:1.75;color:var(--mute);white-space:nowrap}
.cons .notes b{color:var(--ink);font-weight:500}
.bubbleWrap{margin-top:auto;display:flex;gap:12px;align-items:flex-end}
.bubble{background:var(--card);border:1px solid var(--rule);border-radius:16px;border-bottom-left-radius:4px;padding:14px 18px 10px;width:520px;font-size:15px;line-height:1.45}
.bubble .code{font-size:13.5px;line-height:1.55}
.bubble .time{font-size:11px;margin-top:6px}
.lbl{margin-top:10px}
"""
sheet = HEAD('Strikt brand sheet', SHEET_CSS, rel='./') + '<div class="sheet">'
sheet += '<div class="head"><div class="h">Strikt <span class="mute" style="font-weight:400">— brand sheet · the tally mark</span></div><div class="cap">four strokes in ink · the fifth is the strike · viewbox 0 0 100 100 · fonts bundled</div></div>'
sheet += '<div class="grid"><div class="col">'
sheet += '<div class="big"><div data-mark="full" data-size="380"></div></div><div class="cap">mark.svg · 400 px · ink #1A1814 / strike #D3392B · full cut: stroke 9 · gap 10 · strike 9 at 28° · overshoot 4.5</div>'
sheet += '<div class="cap" style="margin-top:56px">avatar-512 · small cut · box 67 % · 74 % safe circle</div><div class="cons">'
sheet += '<svg width="176" height="176" viewBox="0 0 512 512"><rect width="512" height="512" fill="#F6F2E9" stroke="#E3DDD1" stroke-width="2"/><circle cx="256" cy="256" r="255" fill="none" stroke="#E3DDD1" stroke-width="2"/><circle cx="256" cy="256" r="189.4" fill="none" stroke="#8A857A" stroke-dasharray="8 8" stroke-width="2"/><g transform="translate(84.48 84.48) scale(3.4304)" data-markinline="small"></g></svg>'
sheet += '<div class="notes"><b>small cut</b> stroke 8.5 · gap 11 · overshoot 6<br><b>at 40 px</b> stroke 2.3 px · gap 3 px<br><b>farthest ink</b> 168 px from centre · 66 % Ø<br><b>night</b> strike width = stroke width<br><b>one jpg</b> serves light and dark clients</div></div></div>'
# col 2
sheet += '<div class="col"><div class="cap">telegram · circle crop · 96 px and 40 px · paper and night</div><div class="sizes">'
for d,n in ((96,False),(40,False),(96,True),(40,True)):
    sheet += '<div class="c%s" style="width:%dpx;height:%dpx"><div data-mark="small" data-size="%d"%s></div></div>' % (' n' if n else '', d,d, round(d*0.675), ' data-night' if n else '')
sheet += '</div><div class="cap lbl" style="margin-top:24px">chat list · 40 px</div>'
for n in (False,True):
    sheet += '<div class="cl%s" style="margin-top:%dpx"><div class="c%s" style="width:40px;height:40px"><div data-mark="small" data-size="27"%s></div></div><div><div class="n">Strikt</div><div class="m">Left: 760 kcal · 75 P · 80 C · 23 F</div></div><div class="tm">17:02</div></div>' % (' n' if n else '', 10 if not n else 8, ' n' if n else '', ' data-night' if n else '')
sheet += '<div class="cap lbl" style="margin-top:24px">state · one stroke per logged meal · the strike when the day is closed</div><div class="states">'
for b in (1,2,3,4): sheet += '<div data-mark="full" data-size="64" data-bars="%d" data-strike="0"></div>' % b
sheet += '<div data-mark="full" data-size="64"></div><div data-mark="full" data-size="64" data-red="#1A1814"></div></div><div class="cap" style="margin-top:6px">1 · 2 · 3 · 4 · closed · all-ink variant (mark-ink.svg)</div>'
sheet += '<div class="cap" style="margin-top:40px">light tile · night tile (mark-night.svg)</div><div class="tiles"><div class="tile" style="background:var(--card);border:1px solid var(--rule)"><div data-mark="full" data-size="88"></div></div><div class="tile" style="background:var(--night)"><div data-mark="full" data-size="88" data-night></div></div></div></div>'
# col 3
sheet += '<div class="col"><div class="cap">lock-up · newsreader 500, opsz 72 · primary</div><div style="margin-top:14px"><div data-lockup="112"></div></div>'
sheet += '<div class="cap" style="margin-top:26px">lock-up · dm sans 500 · alternate</div><div style="margin-top:14px"><div data-lockup="100" data-sans></div></div>'
sheet += '<div class="cap" style="margin-top:26px">lock-up on night · for images on the black-and-white site</div><div class="night"><div data-lockup="60" data-night></div></div>'
sheet += '<div class="cap" style="margin-top:auto">telegram · food reply</div><div class="bubbleWrap" style="margin-top:10px"><div class="c" style="width:40px;height:40px"><div data-mark="small" data-size="27"></div></div><div class="bubble"><p>Chicken thigh, rice, cucumber salad. About 450 g.</p><span class="code">chicken thigh 180 g    325 kcal · 41 P ·  0 C · 17 F\nrice 150 g             195 kcal ·  4 P · 42 C ·  1 F\ncucumber salad 120 g    70 kcal ·  1 P ·  4 C ·  5 F\n<b>meal                   590 kcal · 46 P · 46 C · 23 F</b>\nleft                 1 030 kcal · 96 P · 92 C · 38 F</span><p style="margin-top:6px">Fiber 11 of 30. Dinner gets a vegetable.</p><div class="time mono mute" style="text-align:right">13:21</div></div></div></div>'
sheet += '</div></div><script src="src/mark.js"></script><script>StriktMark.mount();document.querySelectorAll("[data-markinline]").forEach(function(g){g.innerHTML=StriktMark.paths({cut:g.dataset.markinline||"full"})});</script></body></html>'
W('sheet.html', sheet, root=True)
print('written system + sheet')
