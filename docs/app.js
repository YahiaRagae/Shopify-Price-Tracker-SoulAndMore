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

/* ------------------------------------------------------------------ price chart */
/**
 * Step-function price chart as inline SVG — no chart library.
 * Holds each observed price flat until the next change-point, then flat to last_day.
 */
function chart(variant) {
  const points = (variant.series || []).slice();

  if (!points.length) {
    return el('p', { class: 'chart-empty', text: 'No price observations recorded for this variant yet.' });
  }

  // A day-one series is a single dot: give it a shorter box so it reads as "one observation"
  // rather than as a tall, mostly-empty (i.e. broken-looking) chart.
  const W = 340, H = points.length === 1 ? 116 : 170;
  const pad = { l: 46, r: 14, t: 16, b: 26 };

  // ---- x domain: first observation .. last time we saw the variant at all
  const x0 = dayNum(points[0][0]);
  let x1 = dayNum(variant.last_day);
  if (!isFinite(x1) || x1 < dayNum(points[points.length - 1][0])) {
    x1 = dayNum(points[points.length - 1][0]);
  }
  const singleDay = !(x1 > x0); // tracked for less than a day: one dot, centred
  const sx = (day) => {
    if (singleDay) return pad.l + (W - pad.l - pad.r) / 2;
    return pad.l + ((dayNum(day) - x0) / (x1 - x0)) * (W - pad.l - pad.r);
  };

  // ---- y domain: observed low..high with breathing room (never compare_at)
  const lo = variant.low != null ? variant.low : points[0][1];
  const hi = variant.high != null ? variant.high : points[0][1];
  const span = hi - lo;
  const padY = span > 0 ? span * 0.18 : Math.max(hi * 0.08, 100);
  const yMin = lo - padY, yMax = hi + padY;
  const sy = (price) => {
    const t = yMax === yMin ? 0.5 : (price - yMin) / (yMax - yMin);
    return H - pad.b - t * (H - pad.t - pad.b);
  };

  const endX = singleDay ? sx(points[0][0]) : pad.l + (W - pad.l - pad.r);
  const lastY = sy(points[points.length - 1][1]);

  // ---- provenance split. A point's 3rd element is its source: 'wayback' (an archival estimate
  // recovered from the Internet Archive) or 'live'/absent (observed by this tracker). Wayback
  // points precede live ones by construction (they are all older). We draw the wayback stretch
  // dashed/muted and the live stretch solid, so an inferred price never looks like an observed one.
  const src = (p) => (p[2] === 'wayback' ? 'wayback' : 'live');
  const firstLive = points.findIndex((p) => src(p) === 'live');
  const t = firstLive === -1 ? points.length : firstLive;
  const wb = points.slice(0, t);
  const live = points.slice(t);

  const step = (pts, extendToX) => {
    if (!pts.length) return '';
    const seg = [`M ${sx(pts[0][0]).toFixed(1)} ${sy(pts[0][1]).toFixed(1)}`];
    for (let i = 1; i < pts.length; i++) {
      seg.push(`H ${sx(pts[i][0]).toFixed(1)}`);
      seg.push(`V ${sy(pts[i][1]).toFixed(1)}`);
    }
    if (extendToX != null) seg.push(`H ${extendToX.toFixed(1)}`);
    return seg.join(' ');
  };

  const paths = [];
  if (!wb.length) {
    // All live: one solid step line with an area fill (the day-one and post-launch case).
    const line = step(live, endX);
    paths.push(svg('path', { class: 'area', d: `${line} V ${(H - pad.b).toFixed(1)} H ${sx(points[0][0]).toFixed(1)} Z` }));
    paths.push(svg('path', { class: 'line', d: line }));
  } else if (!live.length) {
    // Only archival points (no live observation): entirely dashed.
    paths.push(svg('path', { class: 'line line-wb', d: step(wb, endX) }));
  } else {
    // Archival stretch (dashed) → a dashed connector holding the last archival price across the
    // untracked gap → the live stretch (solid, with area). The dashed gap is the honest signal
    // that we do not actually know the price between the last archive snapshot and launch.
    const lw = wb[wb.length - 1], fl = live[0];
    paths.push(svg('path', { class: 'line line-wb', d: step(wb) }));
    paths.push(svg('path', {
      class: 'line line-gap',
      d: `M ${sx(lw[0]).toFixed(1)} ${sy(lw[1]).toFixed(1)} H ${sx(fl[0]).toFixed(1)} V ${sy(fl[1]).toFixed(1)}`,
    }));
    const livePath = step(live, endX);
    paths.push(svg('path', { class: 'area', d: `${livePath} V ${(H - pad.b).toFixed(1)} H ${sx(fl[0]).toFixed(1)} Z` }));
    paths.push(svg('path', { class: 'line', d: livePath }));
  }

  const gridline = (price, cls) => [
    svg('line', { class: `grid ${cls}`, x1: pad.l, x2: W - pad.r, y1: sy(price), y2: sy(price) }),
    svg('text', { class: `grid-label ${cls}`, x: pad.l - 6, y: sy(price) + 3, 'text-anchor': 'end' },
      money(price).replace(CURRENCY + ' ', '')),
  ];

  const dots = points.map((p) =>
    svg('circle', {
      class: 'pt' + (src(p) === 'wayback' ? ' pt-wb' : '') +
        (p[1] === lo ? ' pt-low' : p[1] === hi ? ' pt-high' : ''),
      cx: sx(p[0]), cy: sy(p[1]), r: 3.4,
    }));

  const archival = wb.length;
  const summary = points.length === 1
    ? `One observed price, ${money(points[0][1])}, since ${niceDay(points[0][0])}.`
    : `${points.length} price points between ${niceDay(points[0][0])} and ${niceDay(variant.last_day)}` +
      (archival ? `, of which ${archival} ${archival === 1 ? 'is an archival estimate' : 'are archival estimates'} from the Internet Archive` : '') +
      `. Lowest ${money(lo)}, highest ${money(hi)}, latest ${money(points[points.length - 1][1])}.`;

  const node = svg('svg', {
    class: 'chart',
    viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: 'xMidYMid meet',
    role: 'img',
    'aria-label': `Price history chart. ${summary}`,
  },
    svg('title', {}, 'Observed price history'),
    svg('desc', {}, summary),
    hi !== lo ? gridline(hi, 'is-high') : null,
    gridline(lo, 'is-low'),
    paths,
    dots,
    // The "now" marker only makes sense when the latest point is a live observation.
    src(points[points.length - 1]) === 'live'
      ? svg('circle', { class: 'pt pt-now', cx: endX, cy: lastY, r: 4.6 })
      : null,
    singleDay
      ? svg('text', { class: 'axis', x: sx(points[0][0]), y: H - 8, 'text-anchor': 'middle' },
          niceDay(points[0][0]))
      : [
          svg('text', { class: 'axis', x: pad.l, y: H - 8, 'text-anchor': 'start' },
            niceDay(points[0][0])),
          svg('text', { class: 'axis', x: W - pad.r, y: H - 8, 'text-anchor': 'end' },
            niceDay(variant.last_day)),
        ]);

  return node;
}

/** True when a variant's series contains any Internet-Archive-derived point. */
function hasArchival(variant) {
  return (variant.series || []).some((p) => p[2] === 'wayback');
}

/* ------------------------------------------------------------------ detail view */
function statBlock(label, value, note) {
  return el('div', { class: 'stat' },
    el('dt', { text: label }),
    el('dd', {}, el('span', { class: 'stat-value', text: value }),
      note ? el('span', { class: 'stat-note', text: note }) : null));
}

function variantPanel(product, variant) {
  const points = variant.series || [];
  const last = points.length ? points[points.length - 1] : null;
  const held = last ? daysBetween(last[0], variant.last_day) : null;
  const drop = dropFraction(variant);

  const heldText = variant.delisted
    ? 'no longer sold'
    : held == null ? '—'
      : held === 0 ? 'today'
        : `${held} day${held === 1 ? '' : 's'}`;

  // Delisted variants carry a null current price by contract — show the last price we actually
  // observed, clearly labelled as such, never a blank or a literal "null".
  const lastKnown = isLastKnown(variant);

  return el('div', { class: 'variant-panel' },
    el('div', { class: 'price-headline' },
      el('span', { class: 'price-now', text: money(effectivePrice(variant)) }),
      drop > 0
        ? el('span', { class: 'drop drop-lg', text: `−${Math.round(drop * 100)}% vs its own high` })
        : null,
      variant.delisted
        ? el('span', { class: 'badge badge-gone', text: 'No longer sold' })
        : variant.available === true
          ? el('span', { class: 'badge badge-ok', text: 'In stock' })
          : el('span', { class: 'badge badge-oos', text: 'Out of stock' })),

    lastKnown
      ? el('p', { class: 'compare-at' },
          'last observed price · last seen ', niceDay(variant.last_day))
      : claimedWas(variant)
        ? el('p', { class: 'compare-at', text: `store lists as ${money(claimedWas(variant))}` })
        : null,

    chart(variant),

    hasArchival(variant)
      ? el('p', { class: 'chart-legend' },
          el('span', { class: 'lg lg-wb' }, 'Archival estimate (Internet Archive)'),
          el('span', { class: 'lg lg-live' }, 'Observed by this tracker'))
      : null,

    points.length === 1
      ? el('p', { class: 'chart-note' },
          variant.delisted
            ? `Tracking started ${niceDay(variant.first_day)} — only one price was observed before this stopped being listed. `
            : `Tracking started ${niceDay(variant.first_day)} — only one price observed so far. `,
          el('span', {
            class: 'muted',
            text: variant.delisted
              ? 'The line holds that price up to the day we last saw it.'
              : 'Check back in a few days for a price line.',
          }))
      : null,

    el('dl', { class: 'stats' },
      lastKnown
        ? statBlock('Last observed', money(effectivePrice(variant)), niceDay(variant.last_day))
        : statBlock('Now', money(variant.price)),
      statBlock('Observed low', money(variant.low),
        variant.price != null && variant.low === variant.price && points.length > 1
          ? 'at its lowest' : null),
      statBlock('Observed high', money(variant.high)),
      statBlock('Days at this price', heldText, last ? niceDay(last[0]) : null)),

    el('p', { class: 'fineprint' },
      `Observed ${niceDay(variant.first_day)} – ${niceDay(variant.last_day)}`,
      variant.sku ? ` · SKU ${variant.sku}` : '',
      ` · ${points.length} price point${points.length === 1 ? '' : 's'}`,
      (() => {
        const a = points.filter((p) => p[2] === 'wayback').length;
        return a ? ` (${a} from the Internet Archive)` : '';
      })()));
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
