# Data contract (settled by the lead — do not change without lead sign-off)

This file is the single source of truth for every data shape in the project. Collector,
build step, and website all conform to exactly what is written here. If code and this file
disagree, this file wins.

**Global money rule:** every price is an **integer in minor units (piastres)** — `199.00 EGP`
is stored as `19900`. Convert once, at the ingest boundary, with
`int(Decimal(str(s).replace(",", "")) * 100)` and assert the result is integral. **Never use
`float()` on a price, anywhere.** Divide by 100 only when rendering in the browser.

**Identity rule:** a variant's stable key is `variant_key = f"{product_id}:{variant_title}"`
where `variant_title` is the Shopify variant `title` field (e.g. `"Default Title"` or
`"Destiny / Alp"`), stripped. Variant IDs churn when the merchant rebuilds a product, so they
are stored for reference but never used as the diffing key. Handles and titles are display
metadata only.

**Time rule:** all timestamps are UTC ISO-8601 with a trailing `Z` (e.g.
`"2026-07-29T06:00:11Z"`). Chart "days" are the UTC calendar date `YYYY-MM-DD` derived from a
timestamp. Never store a bare local date (Egypt observes DST and the feed emits mixed offsets).

---

## 1. `data/soulandmore.co/history.jsonl` — append-only event log (durable source of truth)

One JSON object per line, keys **sorted alphabetically**, compact separators (`,`/`:`), UTF-8,
`ensure_ascii=false`. Append only; never rewrite past lines. Changes only when a real event
occurs (so `git log -p` on this file is a human-readable price history).

Every line has exactly these keys:

| key | type | notes |
|---|---|---|
| `available` | bool \| null | current availability; `null` on `delisted` |
| `compare_at` | int \| null | minor units; `null` if no compare-at price |
| `event` | string | one of `listed`, `change`, `delisted` |
| `handle` | string | display only |
| `image` | string | product image src (may be `""`); no width param |
| `price` | int \| null | minor units; `null` on `delisted` |
| `prev_available` | bool \| null | value before this event; `null` on `listed` |
| `prev_compare_at` | int \| null | value before this event; `null` on `listed` |
| `prev_price` | int \| null | value before this event; `null` on `listed` |
| `product_id` | int | Shopify product id |
| `product_title` | string | display only |
| `product_type` | string | may be `""` |
| `sku` | string | join hint only, may be `""` |
| `store` | string | `"soulandmore.co"` |
| `ts` | string | UTC ISO-8601 `...Z` |
| `variant_id` | int | reference only, not the key |
| `variant_key` | string | `f"{product_id}:{variant_title}"` |
| `variant_title` | string | the variant option title |
| `vendor` | string | may be `""` |

Event rules:
- **`listed`** — first time a `variant_key` is seen. All three `prev_*` are `null`.
- **`change`** — emitted when **any** of `price`, `compare_at`, `available` differs from the
  stored value. `prev_*` carry the previous values.
- **`delisted`** — emitted once, only after a `variant_key` has been **absent from the feed for
  4 consecutive runs** (debounce). `price`, `compare_at`, `available` are `null`; `prev_*` carry
  the last known values. A delisted variant that reappears emits a fresh `listed`.

## 2. `data/soulandmore.co/state.json` — operational current state (rebuildable projection)

Single JSON object, keys sorted, pretty-printed with 2-space indent (readable diffs). Rewritten
every run. `generated_at` and `run_count` change every run → this is the commit that keeps the
scheduled workflow alive even when no price changed.

```jsonc
{
  "store": "soulandmore.co",
  "generated_at": "2026-07-29T06:00:11Z",
  "run_count": 1,
  "variants": {
    "8018543804582:Default Title": {
      "product_id": 8018543804582,
      "variant_id": 45056677806246,
      "variant_key": "8018543804582:Default Title",
      "sku": "6223009681108",
      "handle": "white-musk-splash",
      "product_title": "White Musk Body Splash",
      "variant_title": "Default Title",
      "vendor": "soulandmore",
      "product_type": "",
      "image": "https://cdn.shopify.com/s/files/1/0597/3586/7558/files/x.jpg?v=1700000000",
      "url": "https://soulandmore.co/products/white-musk-splash",
      "price": 19900,
      "compare_at": 35000,
      "available": true,
      "misses": 0,
      "delisted": false,
      "first_seen": "2026-07-29T06:00:11Z",
      "last_seen": "2026-07-29T06:00:11Z"
    }
  }
}
```

`misses` = consecutive runs this variant has been absent from the feed (0 while present). A
variant is never deleted from `variants`; once `delisted` it stays with `delisted: true`.

## 3. `docs/data.json` — the file the website downloads (built artifact)

Single JSON object, compact. Rewritten every run. This is the ONLY data file the browser
fetches. Prices are minor units (browser divides by 100).

**Multi-store (v1 rule):** `stores.yml` is designed for N stores, but `data.json` is single-store.
For v1 the build emits the **first configured store** to `docs/data.json` (the shape below,
unchanged) and warns on stderr about any additional stores it did not emit. A multi-store shape
(per-slug files + an index) is deferred until a second store is actually added.

```jsonc
{
  "store": "soulandmore.co",
  "generated_at": "2026-07-29T06:00:11Z",   // drives the "last updated" banner
  "currency": "EGP",
  "product_count": 183,
  "variant_count": 318,
  "products": [
    {
      "product_id": 8018543804582,
      "handle": "white-musk-splash",
      "title": "White Musk Body Splash",
      "vendor": "soulandmore",
      "product_type": "",
      "url": "https://soulandmore.co/products/white-musk-splash",
      "image": "https://cdn.shopify.com/s/files/.../x.jpg?v=1700000000",  // no width param; site appends
      "min_price": 19900,          // min current price across available variants (fallback: all)
      "on_sale": true,             // any variant priced below its own observed high
      "available": true,           // any variant available
      "variants": [
        {
          "variant_key": "8018543804582:Default Title",
          "variant_id": 45056677806246,
          "sku": "6223009681108",
          "variant_title": "Default Title",
          "price": 19900,          // current, minor units, null if delisted
          "compare_at": 35000,     // minor units or null
          "available": true,
          "delisted": false,
          "low": 19900,            // lowest price ever OBSERVED by us (minor units)
          "high": 27000,           // highest price ever OBSERVED by us (minor units)
          "first_day": "2026-07-29",
          "last_day": "2026-07-29",
          "series": [              // step-function change-points, chronological, minor units
            ["2026-06-14", 27000], // [UTC date of first observation at this price, price]
            ["2026-09-08", 19900]
          ]
        }
      ]
    }
  ]
}
```

`series` contains one point per **price change**: `[UTC date of the FIRST observation at this
price, price]`. Because a `change` event also fires on availability/compare_at edits, the build
step **must collapse consecutive equal prices**, keeping the first occurrence's day — a restock at
an unchanged price must NOT create a new series point. To draw the step chart: hold each price flat
until the next point, then extend the last price flat to `last_day`. A single-element series
renders as one dot / flat line (the common day-one state) — the site must handle it gracefully with
a "tracking started" empty state, never a broken axis.

Edge case — `state.json` present but `history.jsonl` missing/truncated so a variant has no price
events: reconstruct a single-point series `[day_of(first_seen), price]` from `state.json` (warn on
stderr) so a variant never has a current price but an empty chart. `low`/`high` are computed from observed prices only
(never from `compare_at`, which is seller-controlled and often inflated).

Sort keys the site should support (computed client-side from the above):
- **Biggest real drop** = `(high - price) / high`, restricted to variants with ≥2 series points.
- **Lowest price**, **Recently changed** (max series date), **Name A–Z** (default on day one).

## 4. Module interfaces (settled)

- `python -m tracker.collect` — fetch + diff + append; exit 0 on success, **non-zero on any
  failure** (never writes a partial snapshot). Reads/writes files in §1 and §2.
- `python -m tracker.build` — reads §1 + §2, writes §3 (`docs/data.json`). No network.
- `tracker.common` — shared helpers: `to_minor(s) -> int`, `now_iso() -> str`,
  `day_of(ts) -> str`, HTTP fetch with descriptive User-Agent + retry/backoff.
- Store config comes from `stores.yml` (parsed with stdlib only — see that file's format).

**Stdlib only.** No third-party runtime dependencies (no `requests`, no `pyyaml`). Tests may use
`pytest` if available but must also pass under `python -m unittest`.
