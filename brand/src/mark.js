/* Strikt mark — single source of geometry. Plain script: works in the browser (window.StriktMark)
   and in node (module.exports). Every stroke is emitted as a filled capsule path, so the SVG files
   contain pure paths and no stroke attributes. viewBox is always 0 0 100 100. */
(function (root) {
  'use strict';

  var C = {
    paper: '#F6F2E9', card: '#FFFCF5', rule: '#E3DDD1', mute: '#8A857A', ink: '#1A1814',
    strike: '#D3392B', strikeDeep: '#B32E22', strikeSoft: '#F5D6D1',
    night: '#161513', nightCard: '#201E1A', ruleDark: '#35322C', textDark: '#EFEAE0', strikeDark: '#F0604E'
  };

  // Angle of the strike from horizontal, bottom-left to top-right. 28° sits inside the 27–29° window.
  var ANGLE = 28;
  var TAN = Math.tan(ANGLE * Math.PI / 180);

  // Three cuts. "full" for >= 64 px, "small" for <= 48 px (favicons, the 512 px avatar which is seen
  // at 40 px), "tiny" for a 32 px raster and below (favicon-32.png).
  //   w      vertical stroke width
  //   gap    clear space between two verticals (>= 1.1 w in the full cut)
  //   over   overshoot of the strike cap centre beyond the outer edge of the outer verticals
  //   sw     strike width (night variants set sw = w)
  //   y0,y1  cap centres of the verticals
  //
  // The tiny cut is drawn in device pixels first and converted (1 px = 3.125 units on a 32 px
  // canvas): stroke 3 px on whole-pixel edges (centres 8.5 · 13.5 · 18.5 · 23.5 px), gap 2 px,
  // ink from y 4 to y 28, strike overshoot 2.5 px. It is the one place the gap drops below 1.1 w:
  // at 32 px a 2 px hole between 3 px strokes is two clean background pixels, which is what keeps
  // the four verticals separate; a wider gap would force strokes that vanish at 16 px. Its strike
  // is 2 px, not 3: a 3 px strike bridged the 2 px holes and fused the bars into one ink block
  // across the crossing (alpha rows 12–19), which is the one thing the small gap has to survive.
  //
  // Both pixel cuts are drawn with BUTT caps, not round ones: a round cap at 32 px spends half a
  // pixel row of grey above and below every stroke, so 3 px bars read as fuzzy-tipped 2 px bars and
  // the "whole-pixel edges" claim is only true horizontally. With butt caps the ink of the tiny cut
  // covers rows 4–27 solid and nothing else.
  //
  // The micro cut is the same drawing on a 16 px canvas (1 px = 6.25 units): 2 px strokes on
  // whole-pixel edges (centres 2 · 6 · 10 · 14 px), 2 px gaps, ink y 2 → 14. Its strike is not a
  // rotated stroke at all — at 16 px a 2 px diagonal is 8 rows of grey smudge between the bars — but
  // a hand-placed staircase: four 4 × 2 px blocks at rows 10 · 8 · 6 · 4, one step per gap, every
  // edge on a whole pixel and no antialiasing anywhere. Its slope (6 rows over 12 px between block
  // centres) is 26.6°, inside the 27 ± 2° window to within half a degree.
  var CUTS = {
    full:  { w: 9,      gap: 10,   over: 4.5,    sw: 9,      y0: 19.5,    y1: 80.5 },
    small: { w: 8.5,    gap: 11,   over: 6,      sw: 8.5,    y0: 19.5,    y1: 80.5 },
    tiny:  { w: 9.375,  gap: 6.25, over: 7.8125, sw: 6.25,   y0: 17.1875, y1: 82.8125 },
    micro: { w: 12.5,   gap: 12.5, over: 0,      sw: 12.5,   y0: 18.75,   y1: 81.25 }
  };

  function geometry(cutName, opts) {
    opts = opts || {};
    var c = CUTS[cutName || 'full'];
    var w = opts.w || c.w, gap = opts.gap || c.gap, over = opts.over != null ? opts.over : c.over;
    var sw = opts.sw || c.sw;
    var pitch = w + gap;
    var xs = [-1.5, -0.5, 0.5, 1.5].map(function (k) { return 50 + k * pitch; });
    var edgeL = xs[0] - w / 2, edgeR = xs[3] + w / 2;
    var x0 = edgeL - over, x1 = edgeR + over;
    var dy = (x1 - x0) * TAN / 2;
    return {
      w: w, gap: gap, over: over, sw: sw, xs: xs, y0: c.y0, y1: c.y1,
      strike: { x0: x0, y0: 50 + dy, x1: x1, y1: 50 - dy },
      angle: ANGLE, edgeL: edgeL, edgeR: edgeR,
      // visible extremes including round caps
      box: { left: x0 - sw / 2, right: x1 + sw / 2, top: c.y0 - w / 2, bottom: c.y1 + w / 2 }
    };
  }

  function r3(v) { return Math.round(v * 1000) / 1000; }

  // Filled capsule from (x1,y1) to (x2,y2) with width w: two straight sides and two semicircular caps.
  function capsule(x1, y1, x2, y2, w) {
    var r = w / 2, dx = x2 - x1, dy = y2 - y1, L = Math.hypot(dx, dy);
    var nx = -dy / L * r, ny = dx / L * r;
    var A = [x1 + nx, y1 + ny], B = [x2 + nx, y2 + ny], Cc = [x2 - nx, y2 - ny], D = [x1 - nx, y1 - ny];
    return 'M' + r3(A[0]) + ' ' + r3(A[1]) +
      'L' + r3(B[0]) + ' ' + r3(B[1]) +
      'A' + r3(r) + ' ' + r3(r) + ' 0 0 0 ' + r3(Cc[0]) + ' ' + r3(Cc[1]) +
      'L' + r3(D[0]) + ' ' + r3(D[1]) +
      'A' + r3(r) + ' ' + r3(r) + ' 0 0 0 ' + r3(A[0]) + ' ' + r3(A[1]) + 'Z';
  }

  // The same shape with butt caps: a rectangle whose ends are square, extended by w/2 along the
  // axis so the ink covers exactly the same extent as the capsule. Used by the pixel cuts.
  function bar(x1, y1, x2, y2, w) {
    var h = w / 2, dx = x2 - x1, dy = y2 - y1, L = Math.hypot(dx, dy);
    var ux = dx / L, uy = dy / L, nx = -uy * h, ny = ux * h;
    var ax = x1 - ux * h, ay = y1 - uy * h, bx = x2 + ux * h, by = y2 + uy * h;
    return 'M' + r3(ax + nx) + ' ' + r3(ay + ny) +
      'L' + r3(bx + nx) + ' ' + r3(by + ny) +
      'L' + r3(bx - nx) + ' ' + r3(by - ny) +
      'L' + r3(ax - nx) + ' ' + r3(ay - ny) + 'Z';
  }

  // The 16 px cut, hand-placed on device pixels (1 px = 6.25 units): four 2 px bars in columns
  // 1 · 5 · 9 · 13 running rows 2–13, and a four-block staircase for the strike.
  var MICRO_PX = {
    bars: [1, 5, 9, 13],          // left edge of each 2 px bar; rows 2 → 14
    step: [[0, 10], [4, 8], [8, 6], [12, 4]]  // left edge, top row of each 4 × 2 px block
  };
  function pxRect(x, y, w, h) {
    var U = 6.25;
    return 'M' + r3(x * U) + ' ' + r3(y * U) + 'H' + r3((x + w) * U) + 'V' + r3((y + h) * U) + 'H' + r3(x * U) + 'Z';
  }

  // Inner SVG for a mark.
  //   cut:    'full' | 'small'
  //   bars:   1..4 verticals (state of the day); 4 = the complete count
  //   strike: draw the diagonal (the day is closed)
  //   ink / red: colours; pass red = ink for the all-ink variant
  //   night:  strike width equals the verticals
  function paths(o) {
    o = o || {};
    var cut = o.cut || 'full';
    var bars = o.bars == null ? 4 : o.bars;
    var ink = o.ink || C.ink, red = o.red || C.strike;
    var out = [];
    if (cut === 'micro') {
      for (var m = 0; m < bars; m++) out.push('<path fill="' + ink + '" d="' + pxRect(MICRO_PX.bars[m], 2, 2, 12) + '"/>');
      if (o.strike !== false && bars === 4) {
        for (var k = 0; k < MICRO_PX.step.length; k++) {
          out.push('<path fill="' + red + '" d="' + pxRect(MICRO_PX.step[k][0], MICRO_PX.step[k][1], 4, 2) + '"/>');
        }
      }
      return out.join('\n  ');
    }
    var g = geometry(cut, o.night ? { sw: CUTS[cut].w } : null);
    // the pixel cut squares its stroke ends so they land on whole rows
    var pen = cut === 'tiny' ? bar : capsule;
    for (var i = 0; i < bars; i++) {
      out.push('<path fill="' + ink + '" d="' + pen(g.xs[i], g.y0, g.xs[i], g.y1, g.w) + '"/>');
    }
    if (o.strike !== false && bars === 4) {
      out.push('<path fill="' + red + '" d="' + pen(g.strike.x0, g.strike.y0, g.strike.x1, g.strike.y1, g.sw) + '"/>');
    }
    return out.join('\n  ');
  }

  function svg(o) {
    o = o || {};
    var size = o.size || 100;
    var attrs = 'xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="' + size + '" height="' + size + '"';
    if (o.cls) attrs += ' class="' + o.cls + '"';
    if (o.style) attrs += ' style="' + o.style + '"';
    var bg = o.bg ? '<rect width="100" height="100" fill="' + o.bg + '"/>\n  ' : '';
    return '<svg ' + attrs + '>\n  ' + bg + paths(o) + '\n</svg>';
  }

  var api = { C: C, CUTS: CUTS, ANGLE: ANGLE, MICRO_PX: MICRO_PX, geometry: geometry, capsule: capsule, bar: bar, paths: paths, svg: svg };

  // Browser helper: fill every [data-mark] element. Attributes: data-mark="full|small",
  // data-size, data-bars, data-strike="0", data-night, data-ink, data-red, data-bg.
  api.mount = function (doc) {
    doc = doc || root.document;
    if (!doc) return;
    var els = doc.querySelectorAll('[data-mark]');
    for (var i = 0; i < els.length; i++) {
      var e = els[i], d = e.dataset;
      var night = d.night != null;
      e.innerHTML = svg({
        cut: d.mark || 'full', size: d.size ? parseFloat(d.size) : 100,
        bars: d.bars ? parseInt(d.bars, 10) : 4, strike: d.strike !== '0',
        night: night, ink: d.ink || (night ? C.textDark : C.ink), red: d.red || (night ? C.strikeDark : C.strike),
        bg: d.bg || null, style: 'display:block'
      });
    }
  };

  // Lock-up. Newsreader 500 (opsz 72) metrics measured in Chromium at 100 px: k ascender 75, cap height 72,
  // left side bearing of "s" 3, descent 27, font ascent 74. The mark's visible height (70 units) equals the
  // ascender of the k, the mark's bottom sits on the baseline, the strokes are thinned to 8 (89 % of 9) with
  // the pitch kept at 19, and the gap from the strike tip to the "s" is 0.5 x cap height.
  var LOCK = { asc: 0.75, cap: 0.72, sLeft: 0.03, kTop: -0.015, geo: { w: 8, gap: 11, sw: 8 },
    css: "font-family:Newsreader,Georgia,serif;font-variation-settings:'opsz' 72;font-weight:500;letter-spacing:-0.01em" };
  // DM Sans 500 alternate (measured the same way: k ascender 72, cap 70, s bearing 4, k top 0.12 em below the 1-em box top)
  var LOCK_SANS = { asc: 0.72, cap: 0.70, sLeft: 0.04, kTop: 0.12, geo: { w: 8, gap: 11, sw: 8 },
    css: "font-family:'DM Sans',system-ui,sans-serif;font-weight:500;letter-spacing:-0.02em" };
  api.LOCK = LOCK; api.LOCK_SANS = LOCK_SANS;
  api.lockupLayout = function (F, LK) {
    var K = LK || api.LOCK;
    var s = K.asc * F / 70;             // px per mark unit
    var svgSize = 100 * s;
    var g = geometry('full', K.geo);
    return {
      s: s, svgSize: svgSize, g: g,
      markTop: K.kTop * F - g.box.top * s,          // svg top relative to the text box top
      textLeft: g.box.right * s + 0.5 * K.cap * F - K.sLeft * F,   // text origin from the svg left edge
      gap: 0.5 * K.cap * F
    };
  };
  api.lockupHTML = function (o) {
    o = o || {};
    var LK = o.sans ? LOCK_SANS : api.LOCK;
    var F = o.size || 100, L = api.lockupLayout(F, LK), night = !!o.night;
    var ink = night ? C.textDark : C.ink, red = night ? C.strikeDark : C.strike;
    var g = geometry('full', night ? { w: 8, gap: 11, sw: 8 } : LOCK.geo);
    var inner = [];
    for (var i = 0; i < 4; i++) inner.push('<path fill="' + ink + '" d="' + capsule(g.xs[i], g.y0, g.xs[i], g.y1, g.w) + '"/>');
    inner.push('<path fill="' + red + '" d="' + capsule(g.strike.x0, g.strike.y0, g.strike.x1, g.strike.y1, g.sw) + '"/>');
    return '<span class="lockup' + (o.cls ? ' ' + o.cls : '') + '" style="display:inline-flex;align-items:flex-start;line-height:1;height:' + F + 'px;white-space:nowrap">' +
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="' + r3(L.svgSize) + '" height="' + r3(L.svgSize) + '" style="display:block;flex:none;margin-top:' + r3(L.markTop) + 'px">' + inner.join('') + '</svg>' +
      '<span style="' + LK.css + ';font-size:' + F + 'px;line-height:1;color:' + ink + ';margin-left:' + r3(L.textLeft - L.svgSize) + 'px">strikt</span></span>';
  };
  api.mountLockups = function (doc) {
    doc = doc || root.document;
    var els = doc.querySelectorAll('[data-lockup]');
    for (var i = 0; i < els.length; i++) {
      var e = els[i];
      e.innerHTML = api.lockupHTML({ size: parseFloat(e.dataset.lockup) || 100, night: e.dataset.night != null, sans: e.dataset.sans != null });
    }
  };
  var _mount = api.mount;
  api.mount = function (doc) { _mount(doc); api.mountLockups(doc); };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.StriktMark = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
