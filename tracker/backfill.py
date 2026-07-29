"""One-off historical backfill from the Internet Archive (Wayback Machine).

Seeds pre-launch price history for products we currently track, so the site shows *some*
history from day one instead of only single dots. This is a ONE-OFF you run locally and commit
the result of — it is deliberately NOT wired into CI (the archive gains ~nothing new for this
store, so re-running in CI would just re-hit archive.org for an immutable record).

What it does:
  1. CDX-enumerate every 200-status capture of soulandmore.co in the window.
  2. Fetch archived collection + product pages (raw, via the id_ modifier), caching each to disk
     so re-parsing after a code change costs zero archive.org requests.
  3. Extract per-variant prices from the `var meta = {...}` blob (integer minor units).
  4. Resolve each observation to a currently-tracked variant_key (f"{product_id}:{variant_title}",
     the same key the live collector uses) and drop anything that maps to no live variant.
  5. Emit one `observed`/source="wayback" event per (variant_key, capture-day) to
     data/<slug>/backfill.jsonl, sorted by time.

Honesty notes baked in: every event is tagged source="wayback"; `price` is the archived
catalogue price (ground truth), never compare_at; availability is unknown (null). The build step
and the site render these points as archival estimates, visually distinct from live observations.

Politeness: descriptive UA, ~2.5s between fetches, exponential backoff on 429/5xx and the
"closest-available" 302-to-root, hard cap on total requests. Run: `python -m tracker.backfill`.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from tracker.common import USER_AGENT, load_stores, to_minor, assert_sane_minor

WINDOW_FROM = "20250601"          # ~13 months back; captures all 9 archived days for this store
WINDOW_TO = "20260729"
CDX = "https://web.archive.org/cdx/search/cdx"
REQUEST_SPACING = 2.5             # seconds between archive.org fetches
BACKOFF = (2, 4, 8, 16, 32)
MAX_RETRIES = 5
MAX_FETCHES = 600                 # runaway guard for a one-off


def _log(msg: str) -> None:
    print(f"[backfill] {msg}", file=sys.stderr, flush=True)


def cdx_captures(store: str) -> list[dict]:
    """Return [{ts, original, kind}] for 200-status captures in the window."""
    q = {
        "url": store, "matchType": "domain",
        "from": WINDOW_FROM, "to": WINDOW_TO,
        "output": "json", "fl": "timestamp,original,statuscode",
        "filter": "statuscode:200",
    }
    url = CDX + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    rows = json.loads(urllib.request.urlopen(req, timeout=90).read())[1:]
    out = []
    for ts, original, _sc in rows:
        path = original.split("?", 1)[0]
        if "/collections/" in path:
            kind = "collection"
        elif "/products/" in path:
            kind = "product"
        else:
            continue
        out.append({"ts": ts, "original": original, "kind": kind})
    return out


def _cache_path(cache_dir: str, ts: str, original: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", original.split("//", 1)[-1])[:80]
    return os.path.join(cache_dir, f"{ts}_{slug}.html")


def fetch_archived(ts: str, original: str, cache_dir: str, *, opener=None, sleep=time.sleep) -> str | None:
    """Fetch an archived page raw (id_ modifier). Cached to disk. Returns HTML or None on failure."""
    cp = _cache_path(cache_dir, ts, original)
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as fh:
            return fh.read()
    url = f"https://web.archive.org/web/{ts}id_/{original}"
    _open = opener or (lambda req: urllib.request.urlopen(req, timeout=60))
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            resp = _open(req)
            # A 302 to the bare root is the archive saying "no capture at that exact url/time".
            final = getattr(resp, "url", url)
            body = resp.read()
            if hasattr(resp, "close"):
                resp.close()
            html = body.decode("utf-8", "replace")
            if final.rstrip("/").endswith("soulandmore.co") and "var meta" not in html:
                _log(f"  soft-redirect for {original} @ {ts}; skipping")
                return None
            os.makedirs(cache_dir, exist_ok=True)
            with open(cp, "w", encoding="utf-8") as fh:
                fh.write(html)
            sleep(REQUEST_SPACING)
            return html
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            code = getattr(exc, "code", None)
            if attempt < MAX_RETRIES:
                sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                continue
            _log(f"  give up {original} @ {ts}: {exc} (code={code})")
            return None
    return None


_META_RE = re.compile(r"var meta = (\{.*?\});", re.S)


def extract_observations(html: str) -> list[dict]:
    """Pull [{product_id, public_title, variant_id, price_minor, sku}] from the var meta blob.

    Handles both shapes: product page ({"product": {...variants...}}) and collection page
    ({"products": [ {...variants...} ]}). Iterates ALL `var meta` matches and uses the one that
    actually carries variants — the 2026 theme puts a decoy {"page":{...}} stub first.
    """
    products = []
    for m in _META_RE.finditer(html):
        try:
            o = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(o.get("product"), dict) and o["product"].get("variants"):
            products = [o["product"]]
        elif isinstance(o.get("products"), list) and any(p.get("variants") for p in o["products"]):
            products = [p for p in o["products"] if p.get("variants")]
        if products:
            break
    obs = []
    for p in products:
        pid = p.get("id")
        if not isinstance(pid, int):
            continue
        for v in p.get("variants", []):
            price = v.get("price")
            if not isinstance(price, int):
                continue
            obs.append({
                "product_id": pid,
                "public_title": v.get("public_title"),
                "variant_id": v.get("id"),
                "price_minor": price,          # already integer minor units in var meta
                "sku": v.get("sku") or "",
            })
    return obs


def _variant_title(public_title) -> str:
    pt = (public_title or "").strip()
    return pt if pt else "Default Title"


def _iso(ts: str) -> str:
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}Z"


def load_live(state_path: str) -> tuple[set[str], dict[str, int], dict[int, set[str]]]:
    """Return (live_variant_keys, {handle: product_id}, {product_id: {variant_key,...}})."""
    if not os.path.exists(state_path):
        return set(), {}, {}
    state = json.load(open(state_path, encoding="utf-8"))
    keys, handles, pid_keys = set(), {}, {}
    for vk, v in state.get("variants", {}).items():
        keys.add(vk)
        handles[v["handle"].lower()] = v["product_id"]
        pid_keys.setdefault(v["product_id"], set()).add(vk)
    return keys, handles, pid_keys


def _handle_of(original: str) -> str | None:
    m = re.search(r"/products/([^/?]+)", original.split("?", 1)[0])
    return m.group(1).lower() if m else None


def backfill_store(store: dict, *, opener=None, sleep=time.sleep) -> dict:
    slug = store["slug"]
    data_dir = os.path.join("data", slug)
    cache_dir = os.path.join(data_dir, ".backfill_cache")
    live_keys, live_handles, live_pid_keys = load_live(os.path.join(data_dir, "state.json"))
    if not live_keys:
        _log(f"{slug}: no state.json — run the collector first; nothing to anchor backfill onto")
        return {"events": 0}

    caps = cdx_captures(store["slug"])
    collections = [c for c in caps if c["kind"] == "collection"]
    products = [c for c in caps if c["kind"] == "product"]
    _log(f"{slug}: {len(caps)} captures in window ({len(collections)} collection, {len(products)} product)")

    # (variant_key, day) -> observation (price kept from the first capture that day)
    observed: dict[tuple[str, str], dict] = {}
    fetches = 0

    def ingest(html: str, ts: str) -> None:
        for o in extract_observations(html):
            vk = f"{o['product_id']}:{_variant_title(o['public_title'])}"
            if vk not in live_keys:
                continue  # archived-only / recreated product — no live chart to attach to
            price = o["price_minor"]
            try:
                assert_sane_minor(price)
            except AssertionError:
                _log(f"  skip out-of-range price {price} for {vk} @ {ts}")
                continue
            key = (vk, ts[:8])
            if key not in observed:
                observed[key] = {
                    "variant_key": vk, "product_id": o["product_id"],
                    "variant_title": _variant_title(o["public_title"]),
                    "variant_id": o["variant_id"], "sku": o["sku"],
                    "price": price, "ts": _iso(ts), "capture": ts,
                }

    # Pass 1: collections first (many products per request).
    for c in collections:
        if fetches >= MAX_FETCHES:
            break
        html = fetch_archived(c["ts"], c["original"], cache_dir, opener=opener, sleep=sleep)
        fetches += 1
        if html:
            ingest(html, c["ts"])

    # Pass 2: product pages, but only where they add coverage. Skip a product capture when its
    # handle isn't currently tracked (delisted — no chart to attach to) or when every live variant
    # of that product is already observed for that capture-day.
    for c in products:
        if fetches >= MAX_FETCHES:
            _log("  hit MAX_FETCHES cap — stopping (partial coverage)")
            break
        handle = _handle_of(c["original"])
        pid = live_handles.get(handle) if handle else None
        if pid is None:
            continue  # not a currently-tracked product
        day = c["ts"][:8]
        wanted = live_pid_keys.get(pid, set())
        if wanted and all((vk, day) in observed for vk in wanted):
            continue  # already fully covered by a collection capture that day
        html = fetch_archived(c["ts"], c["original"], cache_dir, opener=opener, sleep=sleep)
        fetches += 1
        if html:
            ingest(html, c["ts"])

    events = sorted(observed.values(), key=lambda e: (e["ts"], e["variant_key"]))
    out_path = os.path.join(data_dir, "backfill.jsonl")
    with open(out_path, "w", encoding="utf-8") as fh:
        for e in events:
            row = {
                "available": None, "capture": e["capture"], "compare_at": None,
                "event": "observed", "price": e["price"], "product_id": e["product_id"],
                "source": "wayback", "sku": e["sku"], "store": slug, "ts": e["ts"],
                "variant_id": e["variant_id"], "variant_key": e["variant_key"],
                "variant_title": e["variant_title"],
            }
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")

    variants = len({e["variant_key"] for e in events})
    days = len({e["capture"][:8] for e in events})
    _log(f"{slug}: wrote {len(events)} backfill events for {variants} variants across {days} archive days -> {out_path}")
    return {"events": len(events), "variants": variants, "days": days, "fetches": fetches}


def main() -> int:
    stores = load_stores("stores.yml")
    for store in stores:
        try:
            backfill_store(store)
        except Exception as exc:  # noqa: BLE001 - one-off tool, report and continue
            _log(f"{store.get('slug')}: FAILED: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
