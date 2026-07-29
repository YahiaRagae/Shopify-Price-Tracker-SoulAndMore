# Shopify Price Tracker — soulandmore.co

A self-hosted "camelcamelcamel"-style price history tracker for the Egyptian Shopify store
[soulandmore.co](https://soulandmore.co) — 183 products / 318 variants, priced in EGP.

It answers two questions a store's own product page never will:

- **Is this price actually good right now?** — compared against the real historical low we've
  observed, not the seller's advertised "compare at" discount.
- **How often does this brand actually run offers?** — a per-variant timeline of every price
  change ever seen.

## Architecture

```
GitHub Actions (cron, every 6h — 4×/day)
    │  fetch public /products.json
    ▼
tracker.collect  →  diff against stored state  →  append change events
    │
    ├── data/soulandmore.co/history.jsonl   (append-only event log; git log is a price history)
    └── data/soulandmore.co/state.json      (current state, rebuilt every run)
    │
    ▼
tracker.build  →  reads history + state  →  writes docs/data.json
    │
    ▼
git commit + push (back to main)
    │
    ▼
GitHub Pages serves docs/ from main  →  static site, no server
```

The whole pipeline is stdlib-only Python, driven by a scheduled GitHub Actions workflow. There
is no backend server: the browser downloads `docs/data.json` and renders everything client-side.

The maintainer's personal PHP host (yahiaragae.com) is **not** part of this — it is kept only as
an optional offsite backup of the data directory, never in the serving path.

## Data model

The full contract — exact field lists, event semantics, and the built `docs/data.json` shape —
lives in [`docs/SCHEMA.md`](docs/SCHEMA.md). The short version:

- **Money** is always an integer in minor units (piastres): `199.00 EGP` is stored as `19900`.
  Never a float.
- **Identity** is `variant_key = f"{product_id}:{variant_title}"` — Shopify variant IDs churn
  when a product is rebuilt, so they're kept for reference only, never as the diffing key.
- **`price` is ground truth.** `compare_at` is seller-controlled marketing copy and is never used
  to compute the "real" historical low or a "you're saving X%" claim.

## Running locally

Requires Python 3.12+. No third-party dependencies — everything is standard library.

```bash
python -m tracker.collect   # fetch + diff + append (writes data/soulandmore.co/*)
python -m tracker.build     # rebuild docs/data.json from the data above
```

Then open `docs/index.html` in a browser.

Run the test suite:

```bash
python -m unittest
```

## Known limitations

This tracker only ever sees **catalogue prices** from the public `/products.json` feed. It is
blind to:

- Discount codes (e.g. `SOUL20`, newsletter or influencer codes)
- Automatic cart-level or BOGO discounts applied at checkout
- Free-shipping thresholds
- Unpublished or hidden products
- In-store-only promotions at physical branches

It also can't verify a merchant's claimed discount: `compare_at_price` is set by the seller and
can be inflated to manufacture the appearance of a sale. That's exactly why this project treats
`price` as ground truth and reports the real historical low we've observed, rather than trusting
the seller's advertised saving.

This project is unofficial and unaffiliated with soulandmore.co.

A Wayback Machine historical backfill (to seed price history from before this tracker existed)
is a planned future phase, not part of v1 — the Internet Archive only has ~9 archived days of
this site in the last year, so it would add sparse coverage at best.

## Deploy runbook

For the maintainer, standing this up on a fresh clone of the (public) repo:

1. **Enable GitHub Pages**: repo Settings → Pages → Source = "Deploy from a branch" → branch
   `main`, folder `/docs`.
2. **Verify the automated path**: Actions tab → `track` workflow → "Run workflow"
   (`workflow_dispatch`) to trigger one run by hand and confirm it collects, builds, and commits
   successfully.
3. After that, the `schedule` trigger takes over — no further manual steps.
