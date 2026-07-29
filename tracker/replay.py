"""Rebuild current per-variant state by folding the append-only event log.

history.jsonl is the durable source of truth (docs/SCHEMA.md §1); state.json is a projection
of it. This module recomputes that projection from the log alone, which makes it useful for
(a) verifying the collector's state.json and (b) rebuilding state.json if it is ever lost.

What it can and cannot reconstruct:
  * price / compare_at / available / delisted — exactly, they only change on an event.
  * first_seen — the ts of the first `listed` event for the key.
  * last_seen — the ts of the last event in which the variant was present. This is a LOWER
    BOUND on state.json's last_seen: the collector refreshes last_seen on every run the
    variant is in the feed, and a quiet run writes no event.
  * misses — NOT reconstructible (miss counters are operational, never logged).
  * display metadata — as of the last event for that key (the collector refreshes it silently
    every run, so state.json can be newer).

Stdlib only. `python -m tracker.replay <store-slug>` prints a summary and, when state.json
exists, verifies the value fields against it.
"""
from __future__ import annotations

import json
import os
import sys

from tracker.collect import DATA_DIR, store_paths

# Fields that the log fully determines, hence the ones a verification run may compare.
COMPARABLE_FIELDS = ("price", "compare_at", "available", "delisted")


def apply_event(variants: dict[str, dict], event: dict) -> None:
    """Fold one history line into the running projection (mutates `variants`)."""
    key = event["variant_key"]
    ts = event["ts"]
    kind = event["event"]
    entry = variants.get(key)

    if kind == "listed":
        # A re-listing keeps the original first_seen — same rule the collector applies.
        first_seen = entry["first_seen"] if entry else ts
        variants[key] = {
            "product_id": event["product_id"],
            "variant_id": event["variant_id"],
            "variant_key": key,
            "sku": event["sku"],
            "handle": event["handle"],
            "product_title": event["product_title"],
            "variant_title": event["variant_title"],
            "vendor": event["vendor"],
            "product_type": event["product_type"],
            "image": event["image"],
            "price": event["price"],
            "compare_at": event["compare_at"],
            "available": event["available"],
            "delisted": False,
            "first_seen": first_seen,
            "last_seen": ts,
            "delisted_at": None,
            "events": (entry["events"] + 1) if entry else 1,
        }
        return

    if entry is None:
        raise ValueError(f"{kind} event for {key} before any listed event (ts={ts})")

    if kind == "change":
        entry.update(
            {
                "product_id": event["product_id"],
                "variant_id": event["variant_id"],
                "sku": event["sku"],
                "handle": event["handle"],
                "product_title": event["product_title"],
                "variant_title": event["variant_title"],
                "vendor": event["vendor"],
                "product_type": event["product_type"],
                "image": event["image"],
                "price": event["price"],
                "compare_at": event["compare_at"],
                "available": event["available"],
                "delisted": False,
                "last_seen": ts,
            }
        )
    elif kind == "delisted":
        # last_seen stays put: the variant was already gone for DELIST_AFTER_MISSES runs.
        entry.update(
            {
                "price": None,
                "compare_at": None,
                "available": None,
                "delisted": True,
                "delisted_at": ts,
            }
        )
    else:
        raise ValueError(f"unknown event type {kind!r} for {key} (ts={ts})")

    entry["events"] += 1


def replay(history_path: str) -> dict[str, dict]:
    """Fold history.jsonl into {variant_key: entry}. A missing log replays to {}."""
    variants: dict[str, dict] = {}
    if not os.path.exists(history_path):
        return variants
    with open(history_path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{history_path}:{lineno}: not valid JSON: {exc}") from exc
            try:
                apply_event(variants, event)
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{history_path}:{lineno}: {exc}") from exc
    return variants


def compare_with_state(replayed: dict[str, dict], state: dict) -> list[str]:
    """Return human-readable differences between a replay and a state.json's variants.

    Only COMPARABLE_FIELDS are checked — see the module docstring for why the rest can
    legitimately differ.
    """
    problems: list[str] = []
    stored = state.get("variants") or {}
    for key in sorted(set(replayed) | set(stored)):
        if key not in stored:
            problems.append(f"{key}: in history but missing from state.json")
            continue
        if key not in replayed:
            problems.append(f"{key}: in state.json but never appears in history")
            continue
        for field in COMPARABLE_FIELDS:
            want = replayed[key].get(field)
            got = stored[key].get(field)
            if want != got:
                problems.append(f"{key}.{field}: history says {want!r}, state.json says {got!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m tracker.replay <store-slug>", file=sys.stderr)
        return 2
    slug = argv[0]
    history_path, state_path = store_paths(slug, data_dir=DATA_DIR)
    if not os.path.exists(history_path):
        print(f"no history log at {history_path}", file=sys.stderr)
        return 1

    variants = replay(history_path)
    delisted = sum(1 for e in variants.values() if e["delisted"])
    events = sum(e["events"] for e in variants.values())
    products = {e["product_id"] for e in variants.values()}
    print(f"store        : {slug}")
    print(f"history      : {history_path}")
    print(f"events       : {events}")
    print(f"products     : {len(products)}")
    print(f"variants     : {len(variants)} ({len(variants) - delisted} live, {delisted} delisted)")
    if variants:
        first = min(e["first_seen"] for e in variants.values())
        last = max(e["last_seen"] for e in variants.values())
        print(f"observed     : {first} .. {last}")

    if not os.path.exists(state_path):
        print(f"state.json   : absent ({state_path}) — nothing to verify")
        return 0

    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)
    problems = compare_with_state(variants, state)
    if problems:
        print(f"state.json   : MISMATCH — {len(problems)} difference(s)", file=sys.stderr)
        for problem in problems[:20]:
            print(f"  - {problem}", file=sys.stderr)
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more", file=sys.stderr)
        return 1
    print(f"state.json   : OK — matches the replayed log on {', '.join(COMPARABLE_FIELDS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
