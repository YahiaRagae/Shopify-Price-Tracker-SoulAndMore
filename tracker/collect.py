"""Collector: fetch the Shopify products feed, diff it against state, append events.

Layout follows the split mandated by the plan:

* `process()` is PURE — no network, no disk. It takes the previous state plus the raw
  products of a *complete* fetch and returns `(events, new_state)`. Every diff rule in
  docs/SCHEMA.md §1/§2 lives here, so the whole correctness surface is unit-testable.
* Everything else is a thin I/O wrapper: `fetch_all_products` (pagination),
  `load_state` / `append_events` / `write_state` (persistence), `collect_store` (one store,
  fetch-then-write so a failed fetch never leaves a partial snapshot) and `main()`.

Money is only ever handled through `tracker.common.to_minor` — never `float()`.
Stdlib only. Target Python 3.12.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

from tracker.common import (
    FetchError,
    assert_sane_minor,
    load_stores,
    now_iso,
    http_get_json,
    to_minor,
)

# Consecutive absent runs before a variant is declared delisted (docs/SCHEMA.md §1).
DELIST_AFTER_MISSES = 4

# A fetch that returns fewer than 90% of the currently-listed variants is treated as a
# truncated / partial feed rather than a genuine mass-delist. Compared with integers only.
SANITY_MIN_NUMERATOR = 9
SANITY_MIN_DENOMINATOR = 10

# Shopify caps /products.json at 250 per page. The page cap is a runaway guard only: a store
# that keeps returning products forever should fail loudly, not spin.
PAGE_SIZE = 250
MAX_PAGES = 100

# Politeness delay between successive pages of the same store.
PAGE_SLEEP_SECONDS = 1.0

STORES_PATH = "stores.yml"
DATA_DIR = "data"

# Exactly the keys docs/SCHEMA.md §1 defines for a history line, in the order json.dumps
# (sort_keys=True) writes them — "prev_*" sorts before "price".
EVENT_KEYS = (
    "available",
    "compare_at",
    "event",
    "handle",
    "image",
    "prev_available",
    "prev_compare_at",
    "prev_price",
    "price",
    "product_id",
    "product_title",
    "product_type",
    "sku",
    "store",
    "ts",
    "variant_id",
    "variant_key",
    "variant_title",
    "vendor",
)


class SanityError(RuntimeError):
    """The fetch looks truncated/partial — abort before writing anything."""


def _log(message: str) -> None:
    """Verbose progress logging. stderr so stdout stays free for machine-readable output."""
    print(f"[collect] {message}", file=sys.stderr)


# --------------------------------------------------------------------------- normalisation
def normalize_variant(product: dict, variant: dict, *, base_url: str) -> dict:
    """One raw (product, variant) pair -> the flat record the state/event shapes are built from.

    Raises ValueError / AssertionError on anything that is not clean money, so a broken feed
    aborts the run instead of poisoning the history log.
    """
    product_id = int(product["id"])
    handle = str(product.get("handle") or "")
    variant_title = str(variant.get("title") or "").strip()

    price = to_minor(variant.get("price"))
    compare_at = to_minor(variant.get("compare_at_price"))
    if price is None:
        raise ValueError(f"variant {product_id}:{variant_title} has no price")
    if compare_at == 0:
        # Observed on 18 rows of the captured live feed: Shopify emits "0.00" for "no
        # compare-at price" as well as null. docs/SCHEMA.md §1 wants null in that case, and
        # assert_sane_minor rightly refuses 0 as a price, so normalise it away here.
        compare_at = None
    assert_sane_minor(price)
    assert_sane_minor(compare_at)

    images = product.get("images") or []
    image = str(images[0].get("src") or "") if images else ""

    return {
        "product_id": product_id,
        "variant_id": int(variant["id"]),
        "variant_key": f"{product_id}:{variant_title}",
        "sku": str(variant.get("sku") or ""),
        "handle": handle,
        "product_title": str(product.get("title") or ""),
        "variant_title": variant_title,
        "vendor": str(product.get("vendor") or ""),
        "product_type": str(product.get("product_type") or ""),
        "image": image,
        "url": f"{base_url}/products/{handle}",
        "price": price,
        "compare_at": compare_at,
        "available": bool(variant.get("available")),
    }


def normalize_products(raw_products: list[dict], *, base_url: str) -> dict[str, dict]:
    """Flatten the feed to {variant_key: record}, first occurrence wins on a duplicate key."""
    records: dict[str, dict] = {}
    for product in raw_products:
        for variant in product.get("variants") or []:
            record = normalize_variant(product, variant, base_url=base_url)
            key = record["variant_key"]
            if key in records:
                # Two variants of one product sharing a title: the feed is ambiguous. Keep the
                # first (stable across runs) and say so loudly rather than flip-flopping.
                _log(f"WARNING duplicate variant_key in feed, ignoring later copy: {key}")
                continue
            records[key] = record
    return records


# --------------------------------------------------------------------------- event helpers
def _event(
    source: dict,
    event: str,
    *,
    store: str,
    ts: str,
    price: int | None,
    compare_at: int | None,
    available: bool | None,
    prev_price: int | None,
    prev_compare_at: int | None,
    prev_available: bool | None,
) -> dict:
    """Build a history line. `source` may be a fetched record or a stored state entry —
    both carry the display metadata the event needs."""
    return {
        "available": available,
        "compare_at": compare_at,
        "event": event,
        "handle": source["handle"],
        "image": source["image"],
        "price": price,
        "prev_available": prev_available,
        "prev_compare_at": prev_compare_at,
        "prev_price": prev_price,
        "product_id": source["product_id"],
        "product_title": source["product_title"],
        "product_type": source["product_type"],
        "sku": source["sku"],
        "store": store,
        "ts": ts,
        "variant_id": source["variant_id"],
        "variant_key": source["variant_key"],
        "variant_title": source["variant_title"],
        "vendor": source["vendor"],
    }


def _state_entry(record: dict, *, ts: str, first_seen: str) -> dict:
    """A fresh (or re-listed) state entry for a variant seen in this run."""
    return {
        "product_id": record["product_id"],
        "variant_id": record["variant_id"],
        "variant_key": record["variant_key"],
        "sku": record["sku"],
        "handle": record["handle"],
        "product_title": record["product_title"],
        "variant_title": record["variant_title"],
        "vendor": record["vendor"],
        "product_type": record["product_type"],
        "image": record["image"],
        "url": record["url"],
        "price": record["price"],
        "compare_at": record["compare_at"],
        "available": record["available"],
        "misses": 0,
        "delisted": False,
        "first_seen": first_seen,
        "last_seen": ts,
    }


# --------------------------------------------------------------------------- pure core
def process(
    prev_state: dict,
    raw_products: list[dict],
    ts: str,
    run_count: int,
    *,
    store: str | None = None,
    base_url: str | None = None,
) -> tuple[list[dict], dict]:
    """Diff a complete fetch against the previous state. PURE: no disk, no network.

    `store` / `base_url` default to what the previous state knows (a first run has neither,
    so `collect_store` always passes them explicitly from stores.yml).

    Returns (events, new_state). Raises SanityError if the fetch looks truncated.
    """
    prev_variants: dict = dict(prev_state.get("variants") or {})
    store = store if store is not None else str(prev_state.get("store") or "")
    if base_url is None:
        # stores.yml keeps slug == domain; only ever used if a caller omits base_url.
        base_url = f"https://{store}" if store else ""
    base_url = base_url.rstrip("/")

    records = normalize_products(raw_products, base_url=base_url)

    # --- sanity guard: run BEFORE emitting anything, so a truncated feed writes nothing.
    if prev_variants:
        listed_before = sum(1 for e in prev_variants.values() if not e.get("delisted"))
        if listed_before and (
            len(records) * SANITY_MIN_DENOMINATOR < listed_before * SANITY_MIN_NUMERATOR
        ):
            raise SanityError(
                f"refusing to diff: fetch returned {len(records)} variants but state has "
                f"{listed_before} listed "
                f"(<{SANITY_MIN_NUMERATOR}/{SANITY_MIN_DENOMINATOR} of it) — "
                "looks like a truncated or partial fetch, not a mass delist"
            )

    events: list[dict] = []
    variants: dict[str, dict] = {}

    # --- variants present in this fetch (feed order → deterministic event order).
    for key, record in records.items():
        prev = prev_variants.get(key)
        if prev is None:
            variants[key] = _state_entry(record, ts=ts, first_seen=ts)
            events.append(
                _event(
                    record,
                    "listed",
                    store=store,
                    ts=ts,
                    price=record["price"],
                    compare_at=record["compare_at"],
                    available=record["available"],
                    prev_price=None,
                    prev_compare_at=None,
                    prev_available=None,
                )
            )
            continue

        if prev.get("delisted"):
            # Back from the dead: a fresh `listed`, but keep the original first_seen so the
            # state still records when we first ever saw this variant.
            first_seen = str(prev.get("first_seen") or ts)
            variants[key] = _state_entry(record, ts=ts, first_seen=first_seen)
            events.append(
                _event(
                    record,
                    "listed",
                    store=store,
                    ts=ts,
                    price=record["price"],
                    compare_at=record["compare_at"],
                    available=record["available"],
                    prev_price=None,
                    prev_compare_at=None,
                    prev_available=None,
                )
            )
            continue

        prev_price = prev.get("price")
        prev_compare_at = prev.get("compare_at")
        prev_available = prev.get("available")
        changed = (
            record["price"] != prev_price
            or record["compare_at"] != prev_compare_at
            or record["available"] != prev_available
        )

        entry = dict(prev)
        # Display metadata is refreshed every run; it never emits an event on its own.
        entry.update(
            {
                "product_id": record["product_id"],
                "variant_id": record["variant_id"],
                "variant_key": key,
                "sku": record["sku"],
                "handle": record["handle"],
                "product_title": record["product_title"],
                "variant_title": record["variant_title"],
                "vendor": record["vendor"],
                "product_type": record["product_type"],
                "image": record["image"],
                "url": record["url"],
                "price": record["price"],
                "compare_at": record["compare_at"],
                "available": record["available"],
                "misses": 0,
                "delisted": False,
                "first_seen": str(prev.get("first_seen") or ts),
                "last_seen": ts,
            }
        )
        variants[key] = entry

        if changed:
            events.append(
                _event(
                    record,
                    "change",
                    store=store,
                    ts=ts,
                    price=record["price"],
                    compare_at=record["compare_at"],
                    available=record["available"],
                    prev_price=prev_price,
                    prev_compare_at=prev_compare_at,
                    prev_available=prev_available,
                )
            )

    # --- variants known to state but absent from this fetch (debounced delist).
    for key, prev in prev_variants.items():
        if key in variants:
            continue
        entry = dict(prev)
        misses = int(entry.get("misses") or 0) + 1
        entry["misses"] = misses
        # last_seen deliberately untouched: it means "last run this variant was in the feed".
        if misses >= DELIST_AFTER_MISSES and not entry.get("delisted"):
            events.append(
                _event(
                    entry,
                    "delisted",
                    store=store,
                    ts=ts,
                    price=None,
                    compare_at=None,
                    available=None,
                    prev_price=entry.get("price"),
                    prev_compare_at=entry.get("compare_at"),
                    prev_available=entry.get("available"),
                )
            )
            entry["delisted"] = True
            # Mirrors the event (and docs/SCHEMA.md §3 "null if delisted"); the last known
            # values survive in the log as the delisted event's prev_*.
            entry["price"] = None
            entry["compare_at"] = None
            entry["available"] = None
        variants[key] = entry

    new_state = {
        "store": store,
        "generated_at": ts,
        "run_count": int(run_count),
        "variants": variants,
    }
    return events, new_state


# --------------------------------------------------------------------------- fetch
def fetch_all_products(base_url: str, *, opener=None, sleep=time.sleep) -> list[dict]:
    """Page through /products.json until a page comes back empty. All-or-nothing.

    Any page failing (after tracker.common's retries) raises FetchError, which the caller
    must let abort the run — a half-fetched catalogue would look like a mass delist.
    """
    base = base_url.rstrip("/")
    products: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{base}/products.json?limit={PAGE_SIZE}&page={page}"
        _log(f"GET {url}")
        payload = http_get_json(url, opener=opener, sleep=sleep)
        if not isinstance(payload, dict) or "products" not in payload:
            raise FetchError(f"GET {url} returned no 'products' key")
        batch = payload["products"] or []
        _log(f"page {page}: {len(batch)} products (running total {len(products) + len(batch)})")
        if not batch:
            return products
        products.extend(batch)
        sleep(PAGE_SLEEP_SECONDS)
    raise FetchError(f"{base}: pagination did not terminate within {MAX_PAGES} pages")


# --------------------------------------------------------------------------- persistence
def store_paths(slug: str, *, data_dir: str = DATA_DIR) -> tuple[str, str]:
    """(history.jsonl, state.json) for a store slug."""
    directory = os.path.join(data_dir, slug)
    return os.path.join(directory, "history.jsonl"), os.path.join(directory, "state.json")


def load_state(path: str) -> dict:
    """Read state.json. A missing file is a first run -> {}. A corrupt one fails loudly."""
    if not os.path.exists(path):
        _log(f"no state at {path} — treating this as a first run")
        return {}
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)
    if not isinstance(state, dict):
        raise ValueError(f"{path}: expected a JSON object, got {type(state).__name__}")
    return state


def append_events(path: str, events: list[dict]) -> None:
    """Append events as JSONL (sorted keys, compact separators, UTF-8), then fsync.

    No events -> the file is not touched at all, so the log only ever changes when something
    real happened (docs/SCHEMA.md §1).
    """
    if not events:
        _log(f"no events — leaving {path} untouched")
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for event in events:
            fh.write(
                json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
        fh.flush()
        os.fsync(fh.fileno())
    _log(f"appended {len(events)} event(s) to {path}")


def write_state(path: str, state: dict) -> None:
    """Atomically rewrite state.json (temp file in the same dir + os.replace)."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, f".{os.path.basename(path)}.tmp")
    body = json.dumps(state, sort_keys=True, ensure_ascii=False, indent=2)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    _log(f"wrote {path} ({len(state.get('variants') or {})} variants)")


# --------------------------------------------------------------------------- per store
def collect_store(
    store: dict,
    ts: str,
    *,
    data_dir: str = DATA_DIR,
    opener=None,
    sleep=time.sleep,
) -> dict:
    """One store, end to end: fetch fully FIRST, diff, and only then write.

    Anything that goes wrong before the writes (fetch failure, sanity guard, bad money) leaves
    both data files exactly as they were.
    """
    slug = str(store["slug"])
    base_url = str(store["base_url"]).rstrip("/")
    history_path, state_path = store_paths(slug, data_dir=data_dir)

    products = fetch_all_products(base_url, opener=opener, sleep=sleep)
    _log(f"{slug}: fetched {len(products)} products")

    prev_state = load_state(state_path)
    run_count = int(prev_state.get("run_count") or 0) + 1
    events, new_state = process(
        prev_state, products, ts, run_count, store=slug, base_url=base_url
    )

    counts = {"listed": 0, "change": 0, "delisted": 0}
    for event in events:
        counts[event["event"]] = counts.get(event["event"], 0) + 1
    _log(
        f"{slug}: run {run_count} — {len(new_state['variants'])} variants, "
        f"{counts['listed']} listed / {counts['change']} change / {counts['delisted']} delisted"
    )

    # History first: it is the durable source of truth, state.json is a rebuildable projection.
    append_events(history_path, events)
    write_state(state_path, new_state)

    return {
        "store": slug,
        "run_count": run_count,
        "products": len(products),
        "variants": len(new_state["variants"]),
        "events": len(events),
        "counts": counts,
    }


def main() -> int:
    """Entry point for `python -m tracker.collect`. Exit 0 only if every store succeeded."""
    ts = now_iso()
    stores = load_stores(STORES_PATH)
    if not stores:
        print(f"[collect] ERROR no stores configured in {STORES_PATH}", file=sys.stderr)
        sys.exit(1)

    failures = 0
    for store in stores:
        slug = store.get("slug", "?")
        try:
            summary = collect_store(store, ts)
        except Exception as exc:  # noqa: BLE001 — process boundary: report and keep going
            failures += 1
            print(f"[collect] ERROR {slug}: {exc}", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            print(f"[collect] {slug}: nothing was written for this store", file=sys.stderr)
            continue
        print(
            f"{summary['store']}: run {summary['run_count']}, "
            f"{summary['variants']} variants, {summary['events']} events "
            f"({summary['counts']['listed']} listed / {summary['counts']['change']} change / "
            f"{summary['counts']['delisted']} delisted)"
        )

    if failures:
        print(f"[collect] {failures} store(s) failed", file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
