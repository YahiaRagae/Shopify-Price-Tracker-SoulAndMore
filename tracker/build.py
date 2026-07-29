"""Build docs/data.json — the only file the website downloads. No network.

Reads the collector's two artefacts per store (see docs/SCHEMA.md):
  * data/<slug>/history.jsonl  (§1) — append-only event log, the durable source of truth
  * data/<slug>/state.json     (§2) — current-state projection, rewritten every run
and writes docs/data.json (§3) — compact, minor units, products ordered by title.

Entry point: ``python -m tracker.build``.

Robustness rule: a missing/corrupt input is a warning on stderr, never a crash. The site must
always have a syntactically valid data.json to fetch, even on day zero.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from tracker.common import day_of, load_stores, now_iso

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORES_YML = os.path.join(REPO_ROOT, "stores.yml")
DEFAULT_DATA_DIR = os.path.join(REPO_ROOT, "data")
DEFAULT_OUT = os.path.join(REPO_ROOT, "docs", "data.json")

# Events that carry an observed price. `delisted` carries price=null by contract (§1) and is
# therefore never a series point — the chart simply stops at the last known price. `observed`
# is a Wayback backfill point (source="wayback"), merged in from data/<slug>/backfill.jsonl.
PRICE_EVENTS = frozenset({"listed", "change", "observed"})

# Top-level key order of §3. Kept explicit so the emitted file reads like the schema.
_TOP_KEYS = ("store", "generated_at", "currency", "product_count", "variant_count", "products")


# --------------------------------------------------------------------------- logging
def log(msg: str) -> None:
    """Verbose progress line. stderr so stdout stays clean for future piping."""
    print(f"[build] {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"[build] WARNING: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- money guard
def _as_minor(value, ctx: str):
    """Coerce a stored price to int minor units, loudly. Never returns a float.

    Prices arrive already in minor units from the collector (§1/§2). Anything that is not a
    plain int is an upstream bug: warn, salvage an integral float, drop anything else rather
    than draw a wrong chart.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass — reject before the int check
        warn(f"{ctx}: price is a bool ({value!r}) — dropped")
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        warn(f"{ctx}: price stored as float {value!r} — coerced to int minor units")
        return int(value)
    warn(f"{ctx}: price {value!r} is not int minor units — dropped")
    return None


# --------------------------------------------------------------------------- input
def read_history(path: str, *, optional: bool = False) -> list[dict]:
    """Read a JSONL event log (§1). Missing file or bad lines → warning + best effort.

    `optional=True` silences the missing-file warning — used for backfill.jsonl, which simply
    may not exist yet.
    """
    if not os.path.exists(path):
        if not optional:
            warn(f"no history log at {path} — series will be reconstructed from state.json")
        return []
    events: list[dict] = []
    bad = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    warn(f"{path}:{lineno}: not valid JSON — line skipped")
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
                else:
                    bad += 1
                    warn(f"{path}:{lineno}: not a JSON object — line skipped")
    except OSError as exc:
        warn(f"cannot read {path}: {exc}")
        return []
    log(f"read {len(events)} event(s) from {path}" + (f" ({bad} skipped)" if bad else ""))
    return events


def read_state(path: str) -> dict:
    """Read state.json (§2). Missing/corrupt → {} (the store then emits products: [])."""
    if not os.path.exists(path):
        warn(f"no state file at {path} — emitting an empty product list for this store")
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"cannot read {path}: {exc} — emitting an empty product list for this store")
        return {}
    if not isinstance(state, dict):
        warn(f"{path}: top level is not an object — emitting an empty product list")
        return {}
    log(f"read state from {path} ({len(state.get('variants') or {})} variant(s))")
    return state


# --------------------------------------------------------------------------- series
def collect_series(events: list[dict]) -> dict[str, list[list]]:
    """variant_key -> step-function change-points [[YYYY-MM-DD, minor_price], ...].

    One point per *price change*, chronological, per §3. Consecutive equal prices are
    collapsed keeping the FIRST day at that price: §1 emits a `change` event when price OR
    compare_at OR available differs, so a restock or a compare-at edit produces an event whose
    price is unchanged — that must not read as a price change.
    """
    by_key: dict[str, list[dict]] = {}
    for ev in events:
        if ev.get("event") not in PRICE_EVENTS:
            continue
        key = ev.get("variant_key")
        if not key or not isinstance(key, str):
            warn(f"history event without a usable variant_key: {ev.get('ts')!r} — skipped")
            continue
        by_key.setdefault(key, []).append(ev)

    series: dict[str, list[list]] = {}
    for key, evs in by_key.items():
        # Merge live + backfill: sort by ts (UTC ISO-8601 + Z sorts lexically). Wayback points
        # (2025..mid-2026) therefore precede the live points (tracking began 2026-07-29).
        evs.sort(key=lambda e: str(e.get("ts") or ""))
        points: list[list] = []
        for ev in evs:
            ts = ev.get("ts")
            if not ts:
                warn(f"{key}: event without a ts — skipped")
                continue
            price = _as_minor(ev.get("price"), f"{key} @ {ts}")
            if price is None:
                continue  # delisted, or an unusable value already warned about
            source = "wayback" if ev.get("source") == "wayback" else "live"
            day = day_of(ts)
            # Collapse only when BOTH price and provenance are unchanged, so the wayback->live
            # handoff always leaves a visible "tracking started" point even at an equal price.
            if points and points[-1][1] == price and points[-1][2] == source:
                continue
            points.append([day, price, source])
        series[key] = points
    return series


# --------------------------------------------------------------------------- assembly
def _variant_payload(key: str, entry: dict, points: list[list]) -> dict:
    """One §3 variant object from its state entry + observed series."""
    price = _as_minor(entry.get("price"), f"state {key}.price")
    first_seen = entry.get("first_seen") or entry.get("last_seen")
    last_seen = entry.get("last_seen") or entry.get("first_seen")

    if not points and price is not None and first_seen:
        # No history events for a variant that state says has a price: the log is missing or
        # truncated. Reconstruct the one point we can defend (state's own first_seen + current
        # price) so the site never gets a priced variant with an unplottable empty chart.
        warn(f"{key}: no history events — reconstructed a single point from state.json")
        points = [[day_of(first_seen), price, "live"]]

    observed = [p[1] for p in points]
    low = min(observed) if observed else price
    high = max(observed) if observed else price

    first_day = points[0][0] if points else (day_of(first_seen) if first_seen else None)
    last_day = day_of(last_seen) if last_seen else None
    if points:
        # last_day must never fall before the final change-point, or the chart draws backwards.
        last_day = max(last_day, points[-1][0]) if last_day else points[-1][0]

    return {
        "variant_key": key,
        "variant_id": entry.get("variant_id"),
        "sku": entry.get("sku") or "",
        "variant_title": entry.get("variant_title") or "",
        "price": price,
        "compare_at": _as_minor(entry.get("compare_at"), f"state {key}.compare_at"),
        "available": entry.get("available"),
        "delisted": bool(entry.get("delisted")),
        "low": low,
        "high": high,
        "first_day": first_day,
        "last_day": last_day,
        "series": points,
    }


def _effective_price(variant: dict):
    """Current price, or — when there is none — the last price we actually observed.

    A delisted variant carries price=null by contract (§1/§3), but its history is still real.
    Falling back to `series[-1]` keeps a fully-delisted product sortable and gives it a
    sensible last-known `min_price` instead of dropping it out of every price sort. Only
    `min_price` uses this; `on_sale` deliberately does not (see below).
    """
    if variant["price"] is not None:
        return variant["price"]
    series = variant["series"]
    return series[-1][1] if series else None


def _product_payload(entries: list[dict], variants: list[dict], base_url: str) -> dict:
    """One §3 product object. Display metadata comes from the freshest variant entry."""
    # "any variant's state entry" — pick deterministically: the most recently seen one, so a
    # renamed product shows its current title rather than a stale one.
    primary = max(entries, key=lambda e: (str(e.get("last_seen") or ""), str(e.get("variant_key") or "")))
    handle = primary.get("handle") or ""

    image = primary.get("image") or ""
    if not image:  # a variant-level image may be missing; borrow any sibling's
        for entry in entries:
            if entry.get("image"):
                image = entry["image"]
                break

    url = primary.get("url") or ""
    if not url and base_url and handle:
        url = f"{base_url}/products/{handle}"

    effective = [(v, _effective_price(v)) for v in variants]
    priced = [e for _, e in effective if e is not None]
    in_stock = [
        e for v, e in effective
        if e is not None and v["available"] is True and not v["delisted"]
    ]
    min_price = min(in_stock) if in_stock else (min(priced) if priced else None)

    return {
        "product_id": primary.get("product_id"),
        "handle": handle,
        "title": primary.get("product_title") or handle or "(untitled)",
        "vendor": primary.get("vendor") or "",
        "product_type": primary.get("product_type") or "",
        "url": url,
        "image": image,
        "min_price": min_price,
        # "below its own observed high" — never compared against seller-controlled compare_at,
        # and only for variants with a REAL current price: a delisted variant is not "on sale",
        # so its last observed price must not count here (unlike min_price above).
        "on_sale": any(
            v["price"] is not None and v["high"] is not None and v["price"] < v["high"]
            for v in variants
        ),
        "available": any(v["available"] is True and not v["delisted"] for v in variants),
        "variants": variants,
    }


def build_payload(store: dict, state: dict, series_by_key: dict[str, list[list]]) -> dict:
    """Assemble one store's §3 payload from its state + observed series."""
    slug = store.get("slug") or state.get("store") or ""
    currency = store.get("currency") or "EGP"
    base_url = (store.get("base_url") or "").rstrip("/")

    variants_state = state.get("variants") or {}
    if not isinstance(variants_state, dict):
        warn(f"{slug}: state.variants is not an object — emitting an empty product list")
        variants_state = {}

    grouped: dict[object, list[dict]] = {}
    for key, entry in variants_state.items():
        if not isinstance(entry, dict):
            warn(f"{slug}: state entry {key!r} is not an object — skipped")
            continue
        entry = dict(entry)
        entry.setdefault("variant_key", key)
        product_id = entry.get("product_id")
        if product_id is None:
            # Fall back to the identity rule: variant_key is f"{product_id}:{variant_title}".
            product_id = str(key).split(":", 1)[0]
            warn(f"{slug}: state entry {key!r} has no product_id — derived {product_id!r} from the key")
            entry["product_id"] = product_id
        grouped.setdefault(product_id, []).append(entry)

    products: list[dict] = []
    for product_id, entries in grouped.items():
        variants = [
            _variant_payload(str(e.get("variant_key")), e, series_by_key.get(str(e.get("variant_key")), []))
            for e in entries
        ]
        # Stable, human-sensible variant order for the detail-view selector.
        variants.sort(key=lambda v: (v["variant_title"].casefold(), v["variant_key"]))
        products.append(_product_payload(entries, variants, base_url))

    products.sort(key=lambda p: (str(p["title"]).casefold(), str(p["product_id"])))

    payload = {
        "store": slug,
        "generated_at": state.get("generated_at") or now_iso(),
        "currency": currency,
        "product_count": len(products),
        "variant_count": sum(len(p["variants"]) for p in products),
        "products": products,
    }
    return {k: payload[k] for k in _TOP_KEYS}


def build_store(store: dict, data_dir: str) -> dict:
    """Read one store's inputs and return its §3 payload (never raises on bad input)."""
    slug = store.get("slug") or ""
    store_dir = os.path.join(data_dir, slug)
    state = read_state(os.path.join(store_dir, "state.json"))
    events = read_history(os.path.join(store_dir, "history.jsonl"))
    events += read_history(os.path.join(store_dir, "backfill.jsonl"), optional=True)  # wayback
    payload = build_payload(store, state, collect_series(events))
    log(f"{slug}: {payload['product_count']} product(s), {payload['variant_count']} variant(s)")
    return payload


# --------------------------------------------------------------------------- output
def write_json(path: str, payload: dict) -> None:
    """Write compact JSON atomically (tmp + os.replace) so a crash never leaves a half file."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
        fh.write("\n")  # keeps git diffs free of "\ No newline at end of file"
    os.replace(tmp, path)
    log(f"wrote {path} ({os.path.getsize(path)} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tracker.build",
        description="Build docs/data.json (SCHEMA §3) from the collector's history + state. No network.",
    )
    parser.add_argument("--stores", default=DEFAULT_STORES_YML, help="path to stores.yml")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="directory holding <slug>/ data")
    parser.add_argument("--out", default=DEFAULT_OUT, help="output path for data.json")
    args = parser.parse_args(argv)

    try:
        stores = load_stores(args.stores)
    except OSError as exc:
        warn(f"cannot read {args.stores}: {exc} — emitting an empty data.json")
        stores = []

    if not stores:
        warn("no stores configured — emitting an empty data.json so the site still renders")
        write_json(args.out, build_payload({}, {}, {}))
        return 0

    if len(stores) > 1:
        # docs/SCHEMA.md §3 defines a single-store data.json (one `store`, one `currency`).
        # Until the lead defines a multi-store shape, only the first store is emitted.
        extra = ", ".join(str(s.get("slug")) for s in stores[1:])
        warn(
            f"{len(stores)} stores configured but SCHEMA §3 data.json holds one store; "
            f"emitting {stores[0].get('slug')!r} only — NOT emitted: {extra}"
        )

    write_json(args.out, build_store(stores[0], args.data_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
