"""Shared helpers for the price tracker. Stdlib only.

Owned by the lead: money, time, config parsing, and HTTP live here because they are the
cross-cutting surface every other module builds on. The money rules in docs/SCHEMA.md are
enforced here so no other module ever has to touch a float.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# Descriptive User-Agent with a contact address, per the project's polite-client rule and the
# measured fact that library-default UAs (python-requests/node-fetch) get 429 from this store.
USER_AGENT = (
    "SoulAndMore-PriceTracker/1.0 "
    "(+https://github.com/YahiaRagae/Shopify-Price-Tracker-SoulAndMore; nour.free@gmail.com)"
)


# --------------------------------------------------------------------------- money
def to_minor(value) -> int | None:
    """Convert a price to integer minor units (piastres). Never uses float.

    Accepts the decimal *string* the live feed returns ("599.00"), integers already in minor
    units are NOT assumed here (the live feed is always major-unit strings). Handles thousands
    separators ("1,200.00"). Returns None for None/empty (e.g. a null compare_at_price).

    Raises ValueError if the value is not a clean 2-decimal money amount.
    """
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s == "" or s.lower() == "none":
        return None
    try:
        minor = Decimal(s) * 100
    except InvalidOperation as exc:
        raise ValueError(f"not a price: {value!r}") from exc
    if minor != minor.to_integral_value():
        raise ValueError(f"price has sub-piastre precision: {value!r}")
    return int(minor)


def assert_sane_minor(minor: int | None, *, floor: int = 100, ceil: int = 100_000_00) -> None:
    """Guard against a units mixup. A real catalogue price sits well inside [1 EGP, 100k EGP].

    floor=100 piastres (1.00 EGP), ceil=10,000,000 piastres (100,000.00 EGP). None is allowed
    (delisted / no compare-at). Raises AssertionError on an out-of-range value so a 100x bug
    fails loudly instead of drawing a wrong chart.
    """
    if minor is None:
        return
    assert isinstance(minor, int), f"price must be int minor units, got {type(minor)}"
    assert floor <= minor <= ceil, f"price {minor} outside sane range [{floor}, {ceil}]"


# --------------------------------------------------------------------------- time
def now_iso() -> str:
    """Current UTC time as ISO-8601 with a trailing Z, e.g. 2026-07-29T06:00:11Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_of(ts: str) -> str:
    """UTC calendar date YYYY-MM-DD for an ISO-8601 ...Z timestamp produced by now_iso()."""
    # Our timestamps are always UTC with a trailing Z, so the date prefix is authoritative.
    if len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
        return ts[:10]
    # Fallback: parse anything reasonable and normalise to UTC.
    cleaned = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned).astimezone(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- config
def load_stores(path: str) -> list[dict]:
    """Parse the constrained stores.yml (see that file's header). Stdlib only — not full YAML.

    Recognises a top-level `stores:` list whose items are `- key: value` blocks with 2-space
    indented continuation keys. Values may be bare or quoted. Ignores blank/comment lines.
    """
    stores: list[dict] = []
    current: dict | None = None
    in_stores = False
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not line.startswith(" ") and stripped.rstrip() == "stores:":
                in_stores = True
                continue
            if not in_stores:
                continue
            if stripped.startswith("- "):
                current = {}
                stores.append(current)
                stripped = stripped[2:].strip()  # first key sits on the dash line
            if current is None:
                continue
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                current[key.strip()] = _unquote(val.split("#", 1)[0].strip())
    return stores


def _unquote(val: str) -> str:
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    return val


# --------------------------------------------------------------------------- http
class FetchError(RuntimeError):
    """Raised after retries are exhausted or on a non-retryable HTTP error."""


def http_get_json(
    url: str,
    *,
    user_agent: str = USER_AGENT,
    timeout: int = 30,
    retries: int = 3,
    backoff=(5, 15, 45),
    sleep=time.sleep,
    opener=None,
):
    """GET a URL and parse JSON. Retries on 429/5xx with exponential backoff, then raises
    FetchError. Any non-retryable error (e.g. 403/404, malformed JSON) raises FetchError
    immediately. A caller mid-pagination must let this propagate and abort the whole run so no
    partial snapshot is written.

    `opener` (a callable taking a urllib.request.Request and returning a response with .read())
    is injectable for tests; defaults to urllib.request.urlopen.
    """
    _open = opener or (lambda req: urllib.request.urlopen(req, timeout=timeout))
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
        try:
            with _open(req) as resp:
                body = resp.read()
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                sleep(backoff[min(attempt, len(backoff) - 1)])
                continue
            raise FetchError(f"GET {url} failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
            if attempt < retries:
                sleep(backoff[min(attempt, len(backoff) - 1)])
                continue
            raise FetchError(f"GET {url} failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            # A challenge/HTML page instead of JSON — not retryable, fail loudly.
            raise FetchError(f"GET {url} returned non-JSON: {exc}") from exc
    raise FetchError(f"GET {url} failed after {retries} retries: {last_exc}")
