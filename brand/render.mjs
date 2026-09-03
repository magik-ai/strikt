#!/usr/bin/env node
/*
  Strikt brand renderer. One command regenerates every asset from the sources in this folder:

    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers /opt/node22/bin/node brand/render.mjs            # everything
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers /opt/node22/bin/node brand/render.mjs hero og    # a subset (job names below)
    /opt/node22/bin/node brand/render.mjs --list                                                # print job names

  What it does, in order:
    1. writes the logo SVGs (logo/mark*.svg, logo/favicon.svg, logo/lockup-*.svg) from src/mark.js —
       pure paths; the lock-ups embed the bundled Newsreader woff2 as a base64 @font-face;
    2. opens every src/*.html in headless Chromium with the fonts in fonts/ (no network) and
       screenshots it at the size and scale listed in JOBS below;
    3. checks that no text fell back to a system font (fails loudly if it did).

  Requirements: node >= 18, playwright (module path below) and its Chromium build. Nothing else.
*/
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PW = process.env.PLAYWRIGHT_MODULE || '/opt/node22/lib/node_modules/playwright';
const { chromium } = require(PW);
const M = require(path.join(HERE, 'src', 'mark.js'));
const C = M.C;

// ---------- 1. logo SVGs ----------
function writeLogos() {
  const out = path.join(HERE, 'logo');
  fs.mkdirSync(out, { recursive: true });
  const files = {
    'mark.svg': M.svg({ cut: 'full' }),
    'mark-ink.svg': M.svg({ cut: 'full', red: C.ink }),
    'mark-night.svg': M.svg({ cut: 'full', night: true, ink: C.textDark, red: C.strikeDark }),
    'mark-small.svg': M.svg({ cut: 'small' }),
    'favicon.svg': M.svg({ cut: 'small', red: C.ink }),
  };
  for (const [name, svg] of Object.entries(files)) fs.writeFileSync(path.join(out, name), svg + '\n');

  // lock-ups: mark as paths, wordmark as text with the font embedded
  const woff = fs.readFileSync(path.join(HERE, 'fonts', 'Newsreader-400_500-latin.woff2')).toString('base64');
  const F = 100, L = M.lockupLayout(F);
  const textW = 2.44 * F;                       // "strikt" at 100 px with -0.01em tracking (measured 244 px)
  const W = Math.ceil(L.textLeft + textW + 0.1 * F), H = 100, baseline = 87.5;
  const markTop = baseline - L.g.box.bottom * L.s; // mark bottom on the baseline
  const lock = (night) => {
    const ink = night ? C.textDark : C.ink, red = night ? C.strikeDark : C.strike;
    const g = M.geometry('full', M.LOCK.geo);
    const paths = g.xs.map(x => `<path fill="${ink}" d="${M.capsule(x, g.y0, x, g.y1, g.w)}"/>`)
      .concat(`<path fill="${red}" d="${M.capsule(g.strike.x0, g.strike.y0, g.strike.x1, g.strike.y1, g.sw)}"/>`);
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">
  <style>
    @font-face{font-family:'Newsreader';font-weight:400 500;font-style:normal;src:url(data:font/woff2;base64,${woff}) format('woff2')}
    text{font-family:'Newsreader',Georgia,serif;font-weight:500;font-size:${F}px;letter-spacing:-0.01em;font-variation-settings:'opsz' 72}
  </style>
  <g transform="translate(0 ${r(markTop)}) scale(${r(L.s)})">
    ${paths.join('\n    ')}
  </g>
  <text x="${r(L.textLeft)}" y="${baseline}" fill="${ink}">strikt</text>
</svg>
`;
  };
  fs.writeFileSync(path.join(out, 'lockup-light.svg'), lock(false));
  fs.writeFileSync(path.join(out, 'lockup-night.svg'), lock(true));
  console.log('logo/: ' + Object.keys(files).concat('lockup-light.svg', 'lockup-night.svg').join(' '));
}
const r = v => Math.round(v * 1000) / 1000;

// ---------- 2. raster jobs ----------
// src, out, width, height, scale, options
const JOBS = {
  og:        ['og.html', 'images/og-1200x630.png', 1200, 630, 1],
  hero:      ['hero.html', 'images/hero-1920x1080.png', 1920, 1080, 1],
  closed:    ['card-closed.html', 'images/card-closed-1920x1080.png', 1920, 1080, 1],
  food:      ['food-reply.html', 'images/food-reply-1920x1080.png', 1920, 1080, 1],
  ladder:    ['ladder.html', 'images/ladder-1920x1080.png', 1920, 1080, 1],
  menu:      ['menu.html', 'images/menu-1920x1080.png', 1920, 1080, 1],
  russian:   ['russian.html', 'images/russian-1920x1080.png', 1920, 1080, 1],
  system:    ['system.html', 'images/system-1920x1080.png', 1920, 1080, 1],
  profile:   ['telegram-profile.html', 'images/telegram-profile-1920x1080.png', 1920, 1080, 1],
  sheet:     ['../sheet.html', 'sheet.png', 1600, 1000, 1],
  avatar1024:['avatar.html', 'avatar/avatar-1024.png', 1024, 1024, 1],
  avatar512: ['avatar.html', 'avatar/avatar-512.jpg', 512, 512, 1, { jpeg: 92 }],
  avatarNight:['avatar-night.html', 'avatar/avatar-night-512.png', 512, 512, 1],
  favicon32: ['favicon.html', 'logo/favicon-32.png', 32, 32, 1, { transparent: true }],
  favicon180:['favicon-180.html', 'logo/favicon-180.png', 180, 180, 1],
};

async function render(names) {
  const browser = await chromium.launch();
  let failed = 0;
  for (const name of names) {
    const [src, out, w, h, scale, opt = {}] = JOBS[name];
    const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: scale });
    page.on('console', m => { if (m.type() === 'error') console.log(`  [${name}] console: ${m.text()}`); });
    page.on('requestfailed', q => console.log(`  [${name}] request failed: ${q.url()}`));
    await page.goto('file://' + path.join(HERE, 'src', src), { waitUntil: 'load' });
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(250);
    // font check: every loaded face must be one of ours, and every rendered text node must resolve to a bundled family
    const report = await page.evaluate(() => {
      const faces = [...document.fonts].map(f => `${f.family} ${f.weight} ${f.status}`);
      const bad = new Set();
      const ok = new Set(['DM Sans', 'Newsreader', 'JetBrains Mono', 'Golos Text']);
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = walker.nextNode())) {
        if (!n.textContent.trim()) continue;
        const fam = getComputedStyle(n.parentElement).fontFamily.split(',')[0].replace(/["']/g, '').trim();
        if (!ok.has(fam)) bad.add(fam + ' <- ' + n.textContent.trim().slice(0, 30));
      }
      return { faces, bad: [...bad], failed: faces.filter(f => /error/.test(f)) };
    });
    if (report.bad.length || report.failed.length) { failed++; console.log(`  [${name}] FONT PROBLEM`, report.bad, report.failed); }
    const outPath = path.join(HERE, out);
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    await page.screenshot({
      path: outPath,
      type: opt.jpeg ? 'jpeg' : 'png',
      quality: opt.jpeg || undefined,
      omitBackground: !!opt.transparent,
      clip: { x: 0, y: 0, width: w, height: h },
    });
    console.log(`${name.padEnd(12)} -> ${out} (${w * scale}x${h * scale})`);
    await page.close();
  }
  await browser.close();
  if (failed) { console.error(`${failed} job(s) rendered with a fallback font`); process.exit(1); }
}

const args = process.argv.slice(2);
if (args.includes('--list')) { console.log(Object.keys(JOBS).join('\n')); process.exit(0); }
const wanted = args.length ? args : Object.keys(JOBS);
for (const n of wanted) if (!JOBS[n]) { console.error('unknown job ' + n); process.exit(2); }
writeLogos();
await render(wanted);
