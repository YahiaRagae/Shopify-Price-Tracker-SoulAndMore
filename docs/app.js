/* Soul & More price tracker — the whole site.
 *
 * No framework, no build step, no external request: the only thing fetched is ./data.json
 * (docs/SCHEMA.md §3), with ./sample-data.json as a fallback so the committed site always
 * renders. Prices are integer minor units (piastres) end to end and are divided by 100 exactly
 * once, at render time — see the money rule in SCHEMA.md.
 *
 * Mobile first: the primary reader is a phone on flaky Egyptian mobile data.
 */
'use strict';

/* ------------------------------------------------------------------ tiny DOM helpers */
const SVGNS = 'http://www.w3.org/2000/svg';

/** Build an element. All data-derived text goes through textContent — never innerHTML. */
function el(tag, attrs, ...kids) {
  return fill(document.createElement(tag), attrs, kids);
}

/** Same, in the SVG namespace. */
function svg(tag, attrs, ...kids) {
  return fill(document.createElementNS(SVGNS, tag), attrs, kids);
}

function fill(node, attrs, kids) {
  for (const key in attrs || {}) {
    const val = attrs[key];
    if (val == null || val === false) continue;
    if (key === 'class') node.setAttribute('class', val);
    else if (key === 'text') node.textContent = String(val);
    else if (key.slice(0, 2) === 'on') node.addEventListener(key.slice(2), val);
    else node.setAttribute(key, val === true ? '' : String(val));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    node.appendChild(typeof kid === 'object' ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ formatting */
// Fixed 'en-US' grouping so the digits stay Western Arabic regardless of the phone's locale
// (an ar-EG locale would otherwise render ١٩٩٫٠٠ and read as a different number).
const MONEY = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** minor units (piastres) -> "EGP 1,299.00". The one and only division by 100. */
function money(minor) {
  if (minor == null || !isFinite(minor)) return '—';
  return CURRENCY + ' ' + MONEY.format(minor / 100);
}

let CURRENCY = 'EGP';

/** "2026-07-29" -> whole days since epoch (UTC), for chart geometry and day counts. */
function dayNum(day) {
  const parts = String(day || '').split('-');
  if (parts.length !== 3) return NaN;
  return Date.UTC(+parts[0], +parts[1] - 1, +parts[2]) / 86400000;
}

function daysBetween(fromDay, toDay) {
  const a = dayNum(fromDay), b = dayNum(toDay);
  return isFinite(a) && isFinite(b) ? Math.max(0, Math.round(b - a)) : null;
}

/** "2026-07-29" -> "29 Jul 2026" (compact, unambiguous, no locale surprises). */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function niceDay(day) {
  const parts = String(day || '').split('-');
  if (parts.length !== 3) return String(day || '—');
  return `${+parts[2]} ${MONTHS[+parts[1] - 1] || '?'} ${parts[0]}`;
}

/** ISO-8601 ...Z -> "3 hours ago". Never lies about the future (clock skew -> "just now"). */
function relativeTime(iso) {
  const then = Date.parse(iso);
  if (!isFinite(then)) return 'at an unknown time';
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 90) return 'just now';
  const units = [
    [60, 'minute'], [24, 'hour'], [7, 'day'], [4.35, 'week'], [12, 'month'],
  ];
  let value = secs / 60, name = 'minute';
  for (let i = 0; i < units.length - 1; i++) {
    if (Math.abs(value) < units[i][0]) break;
    value /= units[i][0];
    name = units[i + 1][1];
  }
  value = Math.round(value);
  if (value < 0) return 'just now';
  return `${value} ${name}${value === 1 ? '' : 's'} ago`;
}

/** Ask Shopify's CDN for a thumbnail instead of the full-size original. */
function thumbUrl(src) {
  return src + (src.includes('?') ? '&' : '?') + 'width=200';
}

/* ------------------------------------------------------------------ derived values */

/**
 * The price to show for a variant: its current price, or — for a delisted variant, whose
 * current price is null by contract — the last price we actually observed. Never null when the
 * variant has any history, so the UI never renders a blank or "null" price.
 */
function effectivePrice(variant) {
  if (!variant) return null;
  if (variant.price != null) return variant.price;
  const pts = variant.series || [];
  return pts.length ? pts[pts.length - 1][1] : null;
}

/** True when this variant's shown price is a last-known price, not a current one. */
function isLastKnown(variant) {
  return !!variant && variant.price == null && effectivePrice(variant) != null;
}

/** The variant whose price the card shows: the cheapest in-stock one, else the cheapest. */
function shownVariant(product) {
  const variants = product.variants || [];
  const priced = variants.map((v) => ({ v, e: effectivePrice(v) })).filter((x) => x.e != null);
  const live = priced.filter((x) => x.v.available === true && !x.v.delisted);
  const pool = live.length ? live : priced;
  if (!pool.length) return variants[0] || null;
  return pool.reduce((best, x) => (best.e <= x.e ? best : x)).v;
}

/**
 * The seller's "compare at" claim, but only when it is actually above the current price.
 * A compare_at equal to (or below) the price tells the reader nothing, and repeating it would
 * dress a non-discount up as one.
 */
function claimedWas(variant) {
  if (!variant || variant.compare_at == null || variant.price == null) return null;
  return variant.compare_at > variant.price ? variant.compare_at : null;
}

/** Drop against the variant's OWN observed high — never against seller-set compare_at. */
function dropFraction(variant) {
  if (!variant || variant.price == null || !variant.high) return 0;
  if (variant.price >= variant.high) return 0;
  return (variant.high - variant.price) / variant.high;
}

/** "Biggest real drop" sort key: best drop among variants with >= 2 observed price points. */
function realDrop(product) {
  let best = -1;
  for (const v of product.variants || []) {
    if (!v.series || v.series.length < 2) continue;
    const d = dropFraction(v);
    if (d > best) best = d;
  }
  return best;
}

/** "Recently changed" sort key: latest observed price-change day (ISO strings sort correctly). */
function lastChangeDay(product) {
  let latest = '';
  for (const v of product.variants || []) {
    const pts = v.series || [];
    if (pts.length && pts[pts.length - 1][0] > latest) latest = pts[pts.length - 1][0];
  }
  return latest;
}

function allDelisted(product) {
  const vs = product.variants || [];
  return vs.length > 0 && vs.every((v) => v.delisted);
}

/* ------------------------------------------------------------------ state */
const state = {
  data: null,
  products: [],
  query: '',
  sort: 'name',
  inStockOnly: false,
  usedSample: false,
  navigatedInPage: false, // so "back" from a deep link doesn't leave the site
};

/* ------------------------------------------------------------------ data loading */
async function getJSON(url) {
  const res = await fetch(url, { credentials: 'omit' });
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
  return res.json();
}

async function loadData() {
  try {
    const data = await getJSON('./data.json');
    console.log('[site] loaded ./data.json', {
      generated_at: data.generated_at,
      products: data.product_count,
      variants: data.variant_count,
    });
    return data;
  } catch (err) {
    console.warn('[site] ./data.json unavailable, falling back to ./sample-data.json:', err.message);
    const data = await getJSON('./sample-data.json');
    state.usedSample = true;
    console.log('[site] loaded ./sample-data.json (SAMPLE DATA — not real observations)');
    return data;
  }
}

function fatal(err) {
  console.error('[site] could not load any price data:', err);
  $('freshness').textContent = 'Price data unavailable';
  const hint = location.protocol === 'file:'
    ? 'This page was opened straight from disk, so the browser blocks its data file. Serve the folder over HTTP instead (for example: python3 -m http.server) and reload.'
    : 'The price file could not be downloaded. This is usually a connection problem — please try again in a moment.';
  const box = $('empty');
  box.hidden = false;
  box.textContent = hint;
  $('count').textContent = '';
}

/* ------------------------------------------------------------------ list view */
function matches(product, needle) {
  if (!needle) return true;
  return (product._haystack || '').includes(needle);
}

function visibleProducts() {
  const needle = state.query.trim().toLowerCase();
  let out = state.products.filter((p) => matches(p, needle));
  if (state.inStockOnly) out = out.filter((p) => p.available === true);

  const byName = (a, b) =>
    String(a.p.title).localeCompare(String(b.p.title), 'en', { sensitivity: 'base' });

  // Decorate–sort–undecorate: each sort key is computed once per product, not once per
  // comparison (183 products x 318 variants is small, but this keeps it honest).
  let rows;
  if (state.sort === 'drop') {
    rows = out.map((p) => ({ p, k: realDrop(p) }));
    rows.sort((a, b) => b.k - a.k || byName(a, b));
  } else if (state.sort === 'price') {
    rows = out.map((p) => ({ p, k: p.min_price == null ? Infinity : p.min_price }));
    rows.sort((a, b) => a.k - b.k || byName(a, b));
  } else if (state.sort === 'recent') {
    rows = out.map((p) => ({ p, k: lastChangeDay(p) }));           // ISO days sort as strings
    rows.sort((a, b) => (a.k < b.k ? 1 : a.k > b.k ? -1 : byName(a, b)));
  } else {
    rows = out.map((p) => ({ p, k: 0 }));
    rows.sort(byName);
  }
  return rows.map((r) => r.p);
}

/** Product thumbnail, or a neutral placeholder when the store has no image (or it 404s). */
function thumbnail(product, extraClass, size) {
  const cls = 'thumb' + (extraClass ? ' ' + extraClass : '');
  const px = size || 64;
  const placeholder = () =>
    el('span', { class: cls + ' thumb-placeholder', 'aria-hidden': 'true' },
      svg('svg', { viewBox: '0 0 24 24', focusable: 'false' },
        svg('path', { d: 'M4 5h16v14H4z' }),
        svg('path', { d: 'm4 15 5-5 4 4 3-2 4 4' }),
        svg('circle', { cx: '9', cy: '9', r: '1.6' })));

  if (!product.image) return placeholder();

  const img = el('img', {
    class: cls,
    src: thumbUrl(product.image),
    alt: '',            // decorative: the title next to it already names the product
    loading: 'lazy',
    decoding: 'async',
    width: px,
    height: px,
  });
  // A dead CDN URL must not leave a broken-image glyph on a phone.
  img.addEventListener('error', () => {
    console.warn('[site] image failed to load:', product.image);
    img.replaceWith(placeholder());
  }, { once: true });
  return img;
}

function badges(product) {
  const out = [];
  if (allDelisted(product)) {
    out.push(el('span', { class: 'badge badge-gone', text: 'No longer sold' }));
  } else if (product.available !== true) {
    out.push(el('span', { class: 'badge badge-oos', text: 'Out of stock' }));
  }
  return out;
}

function card(product) {
  const v = shownVariant(product);
  const drop = dropFraction(v);
  const meta = [product.vendor, product.product_type].filter(Boolean).join(' · ');
  const flags = badges(product);

  return el('li', { class: 'card' },
    el('a', { class: 'card-link', href: `#p=${encodeURIComponent(product.product_id)}` },
      thumbnail(product),
      el('div', { class: 'card-body' },
        el('h3', { class: 'card-title', text: product.title }),
        meta ? el('p', { class: 'card-meta', text: meta }) : null,
        el('p', { class: 'card-price' },
          el('span', { class: 'price', text: money(product.min_price) }),
          drop > 0
            ? el('span', {
                class: 'drop',
                text: `−${Math.round(drop * 100)}% vs its own high`,
              })
            : null),
        // A delisted variant has no current price, so say plainly that this is the last one
        // we saw rather than letting it read as a price you could still pay today.
        isLastKnown(v)
          ? el('p', { class: 'compare-at', text: `last seen ${niceDay(v.last_day)}` })
          : claimedWas(v)
            ? el('p', { class: 'compare-at', text: `store lists as ${money(claimedWas(v))}` })
            : null,
        flags.length ? el('p', { class: 'badges' }, flags) : null)));
}

function renderList() {
  const results = $('results');
  const list = visibleProducts();

  const frag = document.createDocumentFragment();
  for (const product of list) frag.appendChild(card(product));
  results.replaceChildren(frag);

  const total = state.products.length;
  $('count').textContent = total === 0
    ? ''
    : list.length === total
      ? `${total} product${total === 1 ? '' : 's'} tracked`
      : `${list.length} of ${total} products`;

  // Day one, every series is a single point, so "biggest real drop" has nothing to rank.
  // Say so rather than silently showing an alphabetical list under a "drop" heading.
  const hint = $('hint');
  const noDropsYet = state.sort === 'drop' && !list.some((p) => realDrop(p) > 0);
  hint.hidden = !(noDropsYet && list.length > 0);
  hint.textContent = hint.hidden
    ? ''
    : 'No price drops observed yet — a drop can only be shown once this tracker has seen a product change price at least once. Sorted by name for now.';

  const empty = $('empty');
  if (total === 0) {
    empty.hidden = false;
    empty.textContent =
      'No products tracked yet. The collector has not recorded anything for this store — check back after its next run.';
  } else if (list.length === 0) {
    empty.hidden = false;
    empty.textContent = state.query
      ? `Nothing matches “${state.query}”.`
      : 'Nothing matches the current filters.';
  } else {
    empty.hidden = true;
    empty.textContent = '';
  }
}

/* ------------------------------------------------------------------ price verdict + position */

/** minor units, no currency or decimals: 23000 -> "230". For inline prose / chart labels. */
function compactPrice(minor) {
  return (minor / 100).toLocaleString('en-US', { maximumFractionDigits: 0 });
}
/** Prose money, no decimals: "EGP 230". */
function moneyC(minor) {
  return CURRENCY + ' ' + compactPrice(minor);
}
/** "2025-09-08" -> "8 Sep". */
function shortDay(day) {
  const p = String(day || '').split('-');
  return p.length === 3 ? `${+p[2]} ${MONTHS[+p[1] - 1] || '?'}` : String(day || '');
}
/** "2025-09-08" -> "Sep 2025". */
function monthYear(day) {
  const p = String(day || '').split('-');
  return p.length === 3 ? `${MONTHS[+p[1] - 1] || '?'} ${p[0]}` : '';
}
function monthsBetween(fromDay, toDay) {
  const d = daysBetween(fromDay, toDay);
  return d == null ? null : Math.max(1, Math.round(d / 30.4));
}

/**
 * Where the current price sits in its own observed range -> a plain-language buying verdict.
 * This is the answer a shopper actually wants ("is this good right now?"), so it leads the panel.
 */
function verdictOf(now, lo, hi) {
  if (now == null || lo == null || hi == null || hi === lo) return null;
  const p = (now - lo) / (hi - lo);
  if (now <= lo) return { cls: 'good', mark: '✓', head: 'Lowest price we’ve seen', sub: 'Great time to buy' };
  if (p <= 0.18) return { cls: 'good', mark: '✓', head: 'Near its lowest — good time to buy', sub: 'Close to the cheapest we’ve tracked' };
  if (p < 0.66) return { cls: 'mid', mark: '≈', head: 'Around its usual price', sub: 'No unusual discount right now' };
  return { cls: 'high', mark: '↑', head: 'Higher than usual — maybe wait', sub: 'Near the top of its tracked range' };
}

/** Horizontal LOWEST — NOW — HIGHEST track: the price's position in its range, at a glance. */
function positionBar(now, lo, hi) {
  const pct = hi === lo ? 0 : ((now - lo) / (hi - lo)) * 100;
  const flag = Math.min(88, Math.max(12, pct));
  const mark = Math.min(98, Math.max(2, pct));
  return el('div', { class: 'pos' },
    el('div', { class: 'pos-scale' },
      el('div', { class: 'pos-track' }),
      el('div', { class: 'pos-now', style: `left:${mark}%` }),
      el('div', { class: 'pos-flag', style: `left:${flag}%` },
        el('span', { class: 'cap', text: 'NOW' }), ' ' + money(now))),
    el('div', { class: 'pos-ends' },
      el('div', { class: 'pos-end' },
        el('div', { class: 'k lo', text: 'LOWEST' }), el('div', { class: 'v', text: money(lo) })),
      el('div', { class: 'pos-end hi' },
        el('div', { class: 'k', text: 'HIGHEST' }), el('div', { class: 'v', text: money(hi) }))));
}

/* ------------------------------------------------------------------ price history chart */
/**
 * History as evenly-spaced observation dots (NOT to time scale — the archive samples this store
 * only a handful of irregular days a year, so a real time axis crams everything into a corner).
 * Archival estimates are hollow rings on the left; our own observations are solid on the right,
 * with a labelled "no data" break across the gap. Every point carries its own value/date label.
 */
function historyChart(variant) {
  const series = (variant.series || []).slice();
  const wb = series.filter((p) => p[2] === 'wayback');
  const live = series.filter((p) => p[2] !== 'wayback');
  const delisted = !!variant.delisted;
  const lo = variant.low, hi = variant.high;

  const W = 340, H = 152, top = 30, bot = 112, plotH = bot - top, padL = 14, padR = 14, innerW = W - padL - padR;
  const spanY = (hi - lo) || hi * 0.12;
  const yMin = lo - spanY * 0.45, yMax = hi + spanY * 0.55;
  const Y = (v) => top + ((yMax - v) / (yMax - yMin)) * plotH;
  const X = (f) => padL + f * innerW;
  const frac = (i, n, a, b) => (n <= 1 ? (a + b) / 2 : a + (i * (b - a)) / (n - 1));

  const hasGap = wb.length > 0 && live.length > 0;
  const aXf = wb.map((_, i) => frac(i, wb.length, 0.06, hasGap ? 0.44 : 0.9));
  const lXf = live.map((_, i) =>
    live.length === 1 ? (hasGap ? 0.9 : 0.5) : frac(i, live.length, hasGap ? 0.62 : 0.1, 0.9));

  const nodes = [];

  // "usual {high}" reference hairline
  nodes.push(svg('line', { class: 'h-usual', x1: padL, x2: X(0.9) + 8, y1: Y(hi), y2: Y(hi) }));
  nodes.push(svg('text', { class: 'h-usual-label', x: padL, y: Y(hi) - 6 }, `usual ${compactPrice(hi)}`));

  // break band across the untracked gap
  if (hasGap) {
    const bx = X((aXf[aXf.length - 1] + lXf[0]) / 2);
    const mo = monthsBetween(wb[wb.length - 1][0], live[0][0]);
    nodes.push(svg('line', { class: 'h-break', x1: bx, x2: bx, y1: top - 6, y2: bot + 6 }));
    nodes.push(svg('text', { class: 'h-gap', x: bx, y: top - 12, 'text-anchor': 'middle' }, mo ? `~${mo} mo` : 'gap'));
    nodes.push(svg('text', { class: 'h-gap', x: bx, y: bot + 18, 'text-anchor': 'middle' }, 'no data'));
  }

  // dashed connector through archival estimates; solid through live observations
  if (wb.length > 1) {
    nodes.push(svg('polyline', { class: 'h-arch-line', points: wb.map((a, i) => `${X(aXf[i]).toFixed(1)},${Y(a[1]).toFixed(1)}`).join(' ') }));
  }
  if (live.length > 1) {
    nodes.push(svg('polyline', { class: 'h-live-line', points: live.map((a, i) => `${X(lXf[i]).toFixed(1)},${Y(a[1]).toFixed(1)}`).join(' ') }));
  }

  const aMin = wb.length ? Math.min(...wb.map((p) => p[1])) : null;
  const hits = []; // {x, y, date, price, source} for the tap/focus targets built below

  // archival: hollow rings + date labels, lowest one flagged
  wb.forEach((a, i) => {
    const x = X(aXf[i]), y = Y(a[1]);
    nodes.push(svg('circle', { class: 'h-arch-dot', cx: x, cy: y, r: 4.3 }));
    nodes.push(svg('text', { class: 'h-date', x: x, y: bot + 18, 'text-anchor': 'middle' }, shortDay(a[0])));
    if (a[1] === aMin && aMin < hi) {
      nodes.push(svg('text', { class: 'h-dip', x: x, y: y + 18, 'text-anchor': 'middle' }, `▾ ${compactPrice(a[1])}`));
    }
    hits.push({ x, y, date: a[0], price: a[1], source: 'wayback' });
  });

  // live: solid dots; the last one is emphasised as "now/today" unless the variant is delisted
  live.forEach((a, i) => {
    const x = X(lXf[i]), y = Y(a[1]);
    const isNow = i === live.length - 1 && !delisted;
    nodes.push(svg('circle', { class: isNow ? 'h-now-dot' : 'h-live-dot', cx: x, cy: y, r: isNow ? 6.4 : 4.3 }));
    if (isNow) {
      nodes.push(svg('text', { class: 'h-now-val', x: x, y: y - 11, 'text-anchor': 'middle' }, compactPrice(a[1])));
      nodes.push(svg('text', { class: 'h-today', x: x, y: bot + 18, 'text-anchor': 'middle' }, 'Today'));
    } else {
      nodes.push(svg('text', { class: 'h-date', x: x, y: bot + 18, 'text-anchor': 'middle' }, shortDay(a[0])));
    }
    hits.push({ x, y, date: a[0], price: a[1], source: 'live' });
  });

  const archival = wb.length;
  const nowP = live.length ? live[live.length - 1][1] : series[series.length - 1][1];
  const summary = `${series.length} price points; lowest ${money(lo)}, highest ${money(hi)}, now ${money(nowP)}.` +
    (archival ? ` ${archival} ${archival === 1 ? 'is an archival estimate' : 'are archival estimates'} from the Internet Archive.` : '');

  // ---- tap-to-inspect: a tooltip pinned to whichever point you tap (or focus with the keyboard).
  const tip = el('div', { class: 'chart-tip', hidden: true, role: 'status', 'aria-live': 'polite' });
  const keyOf = (info) => `${info.date}:${info.price}`;
  const showTip = (info) => {
    const arch = info.source === 'wayback';
    tip.replaceChildren(
      el('span', { class: 'tip-price', text: money(info.price) }),
      el('span', { class: 'tip-date', text: niceDay(info.date) }),
      el('span', { class: `tip-src ${arch ? 'is-arch' : 'is-obs'}`,
        text: arch ? 'Internet Archive estimate' : 'Observed by this tracker' }));
    tip.style.left = `${Math.min(84, Math.max(16, (info.x / W) * 100))}%`;
    tip.style.top = `${(info.y / H) * 100}%`;
    tip.classList.toggle('below', info.y < H * 0.42); // flip under the point when it sits up top
    tip.hidden = false;
    tip.dataset.k = keyOf(info);
  };
  const toggleTip = (info) => {
    if (!tip.hidden && tip.dataset.k === keyOf(info)) tip.hidden = true;
    else showTip(info);
  };
  const hitNodes = hits.map((info) =>
    svg('circle', {
      class: 'h-hit', cx: info.x, cy: info.y, r: 16, tabindex: '0', role: 'button',
      'aria-label': `${niceDay(info.date)}, ${money(info.price)}, ${info.source === 'wayback' ? 'Internet Archive estimate' : 'observed by this tracker'}. Activate to pin.`,
      onclick: (e) => { e.stopPropagation(); toggleTip(info); },
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleTip(info); } },
      onfocus: () => showTip(info),
    }));

  const chartEl = svg('svg',
    { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `Price history. ${summary}` },
    svg('desc', {}, summary), nodes, hitNodes);
  const wrap = el('div', { class: 'chartwrap' }, chartEl, tip);
  wrap.addEventListener('click', () => { tip.hidden = true; }); // tap the chart background to dismiss
  return wrap;
}

/** Plain-language one-liner under the chart. Factual: never claims "on sale" (we can't know that). */
function storyText(variant) {
  const series = variant.series || [];
  const now = variant.price != null ? variant.price : (series.length ? series[series.length - 1][1] : null);
  const lo = variant.low, hi = variant.high;
  const v = verdictOf(now, lo, hi);
  const nodes = [];
  if (hi != null && lo != null && hi > lo) {
    const lowPt = series.reduce((m, p) => (p[1] < m[1] ? p : m), series[0]);
    nodes.push('Usually about ', el('b', { text: moneyC(hi) }), '. ');
    nodes.push('Seen as low as ', el('b', { text: moneyC(lo) }), ` (${monthYear(lowPt[0])}). `);
  }
  nodes.push('Now ', el('b', { text: moneyC(now) }));
  if (v && v.cls === 'good') nodes.push(' — ', el('span', { class: 'g', text: now <= lo ? 'the lowest we’ve tracked.' : 'near its lowest.' }));
  else if (v && v.cls === 'high') nodes.push(' — higher than usual.');
  else if (v) nodes.push(' — around its usual price.');
  else nodes.push('.');
  return nodes;
}

/** True when a variant's series contains any Internet-Archive-derived point. */
function hasArchival(variant) {
  return (variant.series || []).some((p) => p[2] === 'wayback');
}

/* ------------------------------------------------------------------ detail view */
function variantPanel(product, variant) {
  const series = variant.series || [];
  const hasHist = series.length >= 2;
  const delisted = !!variant.delisted;
  const lastKnown = isLastKnown(variant);
  const now = effectivePrice(variant);
  const was = claimedWas(variant);
  const dropPct = variant.price != null && was ? Math.round(((was - variant.price) / was) * 100) : 0;

  const kids = [];

  // price row: current price, the store's struck-through "was", a discount chip, stock badge
  kids.push(el('div', { class: 'price-headline' },
    el('span', { class: 'price-now', text: money(now) }),
    was ? el('span', { class: 'was', text: money(was) }) : null,
    dropPct > 0 ? el('span', { class: 'drop', text: `−${dropPct}%` }) : null,
    delisted
      ? el('span', { class: 'badge badge-gone', text: 'No longer sold' })
      : variant.available === true
        ? el('span', { class: 'badge badge-ok', text: 'In stock' })
        : el('span', { class: 'badge badge-oos', text: 'Out of stock' })));

  if (lastKnown) {
    kids.push(el('p', { class: 'compare-at' }, 'last observed price · last seen ', niceDay(variant.last_day)));
  }

  // Day one: one live price, nothing to compare against yet — say so plainly, don't fake a chart.
  if (!hasHist && !delisted) {
    kids.push(el('p', { class: 'newpill' }, el('span', { class: 'np-dot' }), 'New — first price recorded today'));
    kids.push(el('p', { class: 'emptynote' },
      'We started tracking this on ', el('b', { text: niceDay(variant.first_day) }),
      '. A price verdict and history will appear here once we’ve seen a few more prices.'));
    return el('div', { class: 'variant-panel' }, ...kids.filter(Boolean));
  }

  // The buying answer, up front: verdict + where "now" sits in the observed range.
  if (variant.price != null) {
    const v = verdictOf(variant.price, variant.low, variant.high);
    if (v) {
      kids.push(el('div', { class: `verdict ${v.cls}` },
        el('span', { class: 'v-badge', text: v.mark }),
        el('span', {}, v.head, el('span', { class: 'sub', text: v.sub }))));
    }
    if (variant.high > variant.low) kids.push(positionBar(variant.price, variant.low, variant.high));
  }

  // Historical detail, secondary.
  if (hasHist) {
    kids.push(el('div', { class: 'rule' }));
    kids.push(el('div', { class: 'hist' },
      el('div', { class: 'hist-head' },
        el('div', { class: 'hist-title', text: 'Price history' }),
        el('div', { class: 'legend' },
          el('span', { class: 'li' }, el('span', { class: 'obs' }), 'Observed'),
          el('span', { class: 'li' }, el('span', { class: 'arc' }), 'Archived est.'))),
      historyChart(variant),
      el('p', { class: 'story' }, ...storyText(variant)),
      hasArchival(variant)
        ? el('p', { class: 'foot', text: 'Hollow points are Internet Archive estimates, not prices we observed — spaced evenly by observation, not to time scale.' })
        : null));
  }

  kids.push(el('p', { class: 'fineprint' },
    `Observed ${niceDay(variant.first_day)} – ${niceDay(variant.last_day)}`,
    variant.sku ? ` · SKU ${variant.sku}` : '',
    ` · ${series.length} price point${series.length === 1 ? '' : 's'}`,
    (() => {
      const a = series.filter((p) => p[2] === 'wayback').length;
      return a ? ` (${a} from the Internet Archive)` : '';
    })()));

  return el('div', { class: 'variant-panel' }, ...kids.filter(Boolean));
}

function renderDetail(product) {
  const body = $('detail-body');
  const variants = product.variants || [];
  let index = 0;

  const panelHost = el('div', { class: 'panel-host' });
  const paint = () => panelHost.replaceChildren(variantPanel(product, variants[index]));

  let selector = null;
  if (variants.length > 1 && variants.length <= 5) {
    // Few variants: chips are the biggest tap target.
    const chips = variants.map((v, i) =>
      el('button', {
        type: 'button',
        class: 'chip',
        'aria-pressed': i === index ? 'true' : 'false',
        text: v.variant_title || `Variant ${i + 1}`,
        onclick: () => {
          index = i;
          for (const [j, c] of chips.entries()) c.setAttribute('aria-pressed', j === i ? 'true' : 'false');
          paint();
        },
      }));
    selector = el('div', { class: 'chips', role: 'group', 'aria-label': 'Choose a variant' }, chips);
  } else if (variants.length > 5) {
    // Many variants: a native select stays usable (and searchable) on a phone.
    const sel = el('select', {
      id: 'variant-select',
      onchange: (e) => { index = +e.target.value; paint(); },
    }, variants.map((v, i) => el('option', { value: i, text: v.variant_title || `Variant ${i + 1}` })));
    selector = el('div', { class: 'field' }, el('label', { for: 'variant-select', text: 'Variant' }), sel);
  }

  const meta = [product.vendor, product.product_type].filter(Boolean).join(' · ');

  // replaceChildren() stringifies null into a literal "null" text node — filter first.
  body.replaceChildren(...[
    el('header', { class: 'detail-head' },
      thumbnail(product, 'detail-thumb', 96),
      el('div', {},
        el('h2', { id: 'detail-heading', class: 'detail-title', text: product.title }),
        meta ? el('p', { class: 'card-meta', text: meta }) : null,
        product.url
          ? el('a', {
              class: 'store-link', href: product.url, target: '_blank',
              rel: 'noopener noreferrer nofollow',
            }, 'View on soulandmore.co ↗')
          : null)),
    selector,
    panelHost,
  ].filter(Boolean));
  paint();

  // Keep the accessible name of the section in sync with what's on screen.
  document.title = `${product.title} — Soul & More price tracker`;
}

/* ------------------------------------------------------------------ routing */
function productIdFromHash() {
  const m = /(?:^|[#&])p=([^&]+)/.exec(location.hash || '');
  return m ? decodeURIComponent(m[1]) : null;
}

function route() {
  const wanted = productIdFromHash();
  const listView = $('view-list');
  const detailView = $('view-detail');

  if (wanted == null) {
    detailView.hidden = true;
    listView.hidden = false;
    document.title = 'Soul & More price tracker (unofficial)';
    return;
  }

  const product = state.products.find((p) => String(p.product_id) === wanted);
  if (!product) {
    console.warn('[site] no product for hash', location.hash);
    detailView.hidden = true;
    listView.hidden = false;
    const empty = $('empty');
    empty.hidden = false;
    empty.textContent = 'That product is not in the current data file. Showing everything instead.';
    return;
  }

  listView.hidden = true;
  detailView.hidden = false;
  renderDetail(product);
  window.scrollTo(0, 0);
  $('back').focus({ preventScroll: true });
}

/* ------------------------------------------------------------------ boot */
function wireControls() {
  const q = $('q');
  const clear = $('q-clear');

  q.addEventListener('input', () => {
    state.query = q.value;
    clear.hidden = !q.value;
    renderList();
  });
  q.addEventListener('search', () => { // native "×" inside type=search
    state.query = q.value;
    clear.hidden = !q.value;
    renderList();
  });
  clear.addEventListener('click', () => {
    q.value = '';
    state.query = '';
    clear.hidden = true;
    renderList();
    q.focus();
  });

  $('sort').addEventListener('change', (e) => { state.sort = e.target.value; renderList(); });
  $('instock').addEventListener('change', (e) => { state.inStockOnly = e.target.checked; renderList(); });

  $('back').addEventListener('click', () => {
    // Deep-linked arrivals have nowhere to go back to — drop the hash instead of leaving.
    if (state.navigatedInPage && history.length > 1) history.back();
    else location.hash = '';
  });

  document.addEventListener('click', (e) => {
    const link = e.target.closest && e.target.closest('a.card-link');
    if (link) state.navigatedInPage = true;
  }, true);

  window.addEventListener('hashchange', route);
}

function showFreshness(data) {
  const when = data.generated_at;
  $('freshness').textContent = state.usedSample
    ? 'Sample data — not a live reading'
    : `Updated ${relativeTime(when)}`;
  if (when) $('freshness').title = when;
  $('sample-notice').hidden = !state.usedSample;
}

async function boot() {
  wireControls();
  let data;
  try {
    data = await loadData();
  } catch (err) {
    fatal(err);
    return;
  }

  state.data = data;
  CURRENCY = data.currency || 'EGP';
  state.products = (data.products || []).map((p) => {
    // Precomputed search haystack: title / handle / vendor, matched as a plain substring.
    p._haystack = [p.title, p.handle, p.vendor, p.product_type].filter(Boolean).join(' ').toLowerCase();
    return p;
  });

  showFreshness(data);
  renderList();
  route();
  console.log(`[site] rendered ${state.products.length} product(s)`);
}

boot();
