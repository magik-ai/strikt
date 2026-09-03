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
    3. checks that no text fell back to a system font (fails loudly if it did). The check asks
       Chromium which platform fonts it actually rasterised each text element with
       (CSS.getPlatformFontsForNode over a CDP session), so a single glyph outside a subset's
       unicode-range — an arrow, a ≥, a ✓ — is caught, not just a wrong font-family;
    4. checks that no glyph was rasterised with LCD subpixel antialiasing. Chromium is launched
       with --disable-lcd-text, so every glyph edge is a grey blend between the text colour and
       what is behind it. The check decodes the PNG it just wrote and walks every text element's
       box: a pixel whose channel spread (max − min) is over 40 and which does not sit on the
       line between two of that element's own colours is a colour fringe, and fails the run.

  Requirements: node >= 18, playwright (module path below) and its Chromium build. Nothing else.
*/
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PW = process.env.PLAYWRIGHT_MODULE || '/opt/node22/lib/node_modules/playwright';
const { chromium } = require(PW);
const M = require(path.join(HERE, 'src', 'mark.js'));
const C = M.C;
// the only families any glyph in any image may be rasterised from
const BUNDLED = ['DM Sans', 'Newsreader', 'JetBrains Mono', 'Golos Text'];

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
  favicon16: ['favicon-16.html', 'logo/favicon-16.png', 16, 16, 1, { transparent: true }],
  favicon32: ['favicon.html', 'logo/favicon-32.png', 32, 32, 1, { transparent: true }],
  favicon180:['favicon-180.html', 'logo/favicon-180.png', 180, 180, 1],
};

// ---------- fringe check ----------
// A minimal PNG reader (8-bit, non-interlaced, RGB or RGBA — what Chromium writes) so the build can
// look at the pixels it just produced without a native dependency.
function decodePNG(buf) {
  let pos = 8, w = 0, h = 0, depth = 0, colour = 0, interlace = 0;
  const idat = [];
  while (pos + 8 <= buf.length) {
    const len = buf.readUInt32BE(pos), type = buf.toString('ascii', pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === 'IHDR') { w = data.readUInt32BE(0); h = data.readUInt32BE(4); depth = data[8]; colour = data[9]; interlace = data[12]; }
    else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    pos += 12 + len;
  }
  const ch = { 0: 1, 2: 3, 4: 2, 6: 4 }[colour];
  if (depth !== 8 || interlace !== 0 || !ch) throw new Error(`unsupported png (depth ${depth}, colour ${colour})`);
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = w * ch, out = Buffer.alloc(h * stride);
  let p = 0;
  for (let y = 0; y < h; y++) {
    const f = raw[p++], line = raw.subarray(p, p + stride); p += stride;
    const cur = out.subarray(y * stride, (y + 1) * stride);
    const prev = y ? out.subarray((y - 1) * stride, y * stride) : null;
    for (let x = 0; x < stride; x++) {
      const a = x >= ch ? cur[x - ch] : 0, b = prev ? prev[x] : 0, c = prev && x >= ch ? prev[x - ch] : 0;
      let v = line[x];
      if (f === 1) v += a;
      else if (f === 2) v += b;
      else if (f === 3) v += (a + b) >> 1;
      else if (f === 4) {
        const pa = Math.abs(b - c), pb = Math.abs(a - c), pc = Math.abs(a + b - 2 * c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      cur[x] = v & 255;
    }
  }
  return { w, h, ch, data: out };
}

// Distance from a colour to the segment between two palette colours: greyscale antialiasing puts
// every edge pixel on such a segment, LCD subpixel antialiasing pushes it off one.
function distToSegment(px, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], dz = b[2] - a[2];
  const len2 = dx * dx + dy * dy + dz * dz;
  let t = 0;
  if (len2 > 0) t = Math.max(0, Math.min(1, ((px[0] - a[0]) * dx + (px[1] - a[1]) * dy + (px[2] - a[2]) * dz) / len2));
  const ex = px[0] - (a[0] + t * dx), ey = px[1] - (a[1] + t * dy), ez = px[2] - (a[2] + t * dz);
  return Math.sqrt(ex * ex + ey * ey + ez * ez);
}

const FRINGE_SPREAD = 40;   // max(channel) − min(channel) above which a pixel is coloured
const FRINGE_TOL = 26;      // how far off its own palette a coloured pixel may sit

function scanFringe(img, boxes, texts, scale) {
  const hits = [];
  let total = 0;
  for (let i = 0; i < boxes.length; i++) {
    const box = boxes[i];
    const x0 = Math.max(0, Math.floor(box.x * scale)), y0 = Math.max(0, Math.floor(box.y * scale));
    const x1 = Math.min(img.w, Math.ceil((box.x + box.w) * scale)), y1 = Math.min(img.h, Math.ceil((box.y + box.h) * scale));
    const pal = box.pal;
    if (!pal.length || x1 <= x0 || y1 <= y0) continue;
    let n = 0, sample = '';
    for (let y = y0; y < y1; y++) {
      for (let x = x0; x < x1; x++) {
        const o = (y * img.w + x) * img.ch;
        if (img.ch === 4 && img.data[o + 3] < 8) continue;
        const r = img.data[o], g = img.data[o + 1], b = img.data[o + 2];
        if (Math.max(r, g, b) - Math.min(r, g, b) <= FRINGE_SPREAD) continue;
        let best = Infinity;
        for (let a = 0; a < pal.length && best > FRINGE_TOL; a++)
          for (let c = a; c < pal.length && best > FRINGE_TOL; c++)
            best = Math.min(best, distToSegment([r, g, b], pal[a], pal[c]));
        if (best > FRINGE_TOL) { n++; if (!sample) sample = `(${r},${g},${b}) at ${x},${y}`; }
      }
    }
    if (n) { total += n; hits.push({ n, sample, text: texts[i] }); }
  }
  hits.sort((a, b) => b.n - a.n);
  return { total, hits };
}

async function render(names) {
  // --disable-lcd-text: without it Chromium rasterises glyphs with RGB subpixel antialiasing and
  // every text edge picks up orange/blue fringes, which is stray colour in compositions whose rule
  // is one accent. --font-render-hinting=none keeps the outlines identical at every scale.
  const browser = await chromium.launch({ args: ['--disable-lcd-text', '--font-render-hinting=none'] });
  let failed = 0;
  for (const name of names) {
    const [src, out, w, h, scale, opt = {}] = JOBS[name];
    const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: scale });
    page.on('console', m => { if (m.type() === 'error') console.log(`  [${name}] console: ${m.text()}`); });
    page.on('requestfailed', q => console.log(`  [${name}] request failed: ${q.url()}`));
    await page.goto('file://' + path.join(HERE, 'src', src), { waitUntil: 'load' });
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(250);
    // font check, two layers:
    //   a. the declared family of every text node must be one of ours (catches a wrong font-family);
    //   b. the *platform* fonts Chromium actually rasterised each text element with must all be
    //      ours (catches a single glyph — an arrow, a ≥, a ✓ — falling out of a subset's
    //      unicode-range into DejaVu, which layer a cannot see).
    const report = await page.evaluate(() => {
      const faces = [...document.fonts].map(f => `${f.family} ${f.weight} ${f.status}`);
      const bad = new Set();
      const ok = new Set(['DM Sans', 'Newsreader', 'JetBrains Mono', 'Golos Text']);
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let n, i = 0;
      const tagged = [], boxes = [];
      // every colour this element is allowed to paint: its own text colour and its descendants'
      // (a <b> in the accent inside a mute caption), plus every non-transparent background behind
      // it. A greyscale-antialiased glyph is always a blend of two of them.
      const rgb = (v) => {
        const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(v || '');
        if (!m) return null;
        if (m[4] !== undefined && parseFloat(m[4]) < 0.05) return null;
        return [Math.round(+m[1]), Math.round(+m[2]), Math.round(+m[3])];
      };
      const palette = (el) => {
        const out = [];
        const push = (c) => { if (c && !out.some(o => o[0] === c[0] && o[1] === c[1] && o[2] === c[2])) out.push(c); };
        for (const e of [el, ...el.querySelectorAll('*')]) {
          const cs = getComputedStyle(e);
          push(rgb(cs.color));
          push(rgb(cs.backgroundColor));
          push(rgb(cs.borderTopColor)); push(rgb(cs.borderBottomColor));
          push(rgb(cs.borderLeftColor)); push(rgb(cs.borderRightColor));
          if (e.tagName === 'svg' || e.namespaceURI === 'http://www.w3.org/2000/svg') {
            push(rgb(cs.fill)); push(rgb(cs.stroke));
          }
        }
        for (let a = el; a; a = a.parentElement) push(rgb(getComputedStyle(a).backgroundColor));
        return out;
      };
      while ((n = walker.nextNode())) {
        if (!n.textContent.trim()) continue;
        const el = n.parentElement;
        const fam = getComputedStyle(el).fontFamily.split(',')[0].replace(/["']/g, '').trim();
        if (!ok.has(fam)) bad.add(fam + ' <- ' + n.textContent.trim().slice(0, 30));
        if (!el.hasAttribute('data-fontcheck')) {
          el.setAttribute('data-fontcheck', String(i++));
          tagged.push(n.textContent.trim().slice(0, 48));
          const r = el.getBoundingClientRect();
          boxes.push({ x: r.left, y: r.top, w: r.width, h: r.height, pal: palette(el) });
        }
      }
      return { faces, bad: [...bad], failed: faces.filter(f => /error/.test(f)), tagged, boxes };
    });
    const client = await page.context().newCDPSession(page);
    await client.send('DOM.enable');
    await client.send('CSS.enable');
    const { root } = await client.send('DOM.getDocument', { depth: -1, pierce: true });
    const glyphBad = [];
    for (let i = 0; i < report.tagged.length; i++) {
      const { nodeId } = await client.send('DOM.querySelector', { nodeId: root.nodeId, selector: `[data-fontcheck="${i}"]` });
      if (!nodeId) continue;
      const { fonts } = await client.send('CSS.getPlatformFontsForNode', { nodeId });
      for (const f of fonts) {
        if (!BUNDLED.some(b => f.familyName.startsWith(b))) {
          glyphBad.push(`${f.familyName} (${f.glyphCount} glyph${f.glyphCount === 1 ? '' : 's'}) <- ${JSON.stringify(report.tagged[i])}`);
        }
      }
    }
    await client.detach();
    if (report.bad.length || report.failed.length || glyphBad.length) {
      failed++;
      console.log(`  [${name}] FONT PROBLEM`, [...report.bad, ...glyphBad, ...report.failed].join('\n    '));
    }
    const outPath = path.join(HERE, out);
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    const shot = await page.screenshot({
      path: outPath,
      type: opt.jpeg ? 'jpeg' : 'png',
      quality: opt.jpeg || undefined,
      omitBackground: !!opt.transparent,
      clip: { x: 0, y: 0, width: w, height: h },
    });
    if (!opt.jpeg && report.boxes.length) {
      const fringe = scanFringe(decodePNG(shot), report.boxes, report.tagged, scale);
      if (fringe.total) {
        failed++;
        console.log(`  [${name}] COLOUR FRINGE on text: ${fringe.total} px in ${fringe.hits.length} element(s)\n    ` +
          fringe.hits.slice(0, 6).map(x => `${x.n} px ${x.sample} <- ${JSON.stringify(x.text)}`).join('\n    '));
      }
    }
    console.log(`${name.padEnd(12)} -> ${out} (${w * scale}x${h * scale})`);
    await page.close();
  }
  await browser.close();
  if (failed) { console.error(`${failed} job(s) failed the font / fringe check`); process.exit(1); }
}

const args = process.argv.slice(2);
if (args.includes('--list')) { console.log(Object.keys(JOBS).join('\n')); process.exit(0); }
const wanted = args.length ? args : Object.keys(JOBS);
for (const n of wanted) if (!JOBS[n]) { console.error('unknown job ' + n); process.exit(2); }
writeLogos();
await render(wanted);
