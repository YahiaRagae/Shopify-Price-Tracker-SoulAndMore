"""Tests for the collector: the pure diff core, the fetch wrapper, and persistence.

Runs with `python -m unittest tests.test_collect -v` (also collectable by pytest).
The primary fixture is a real captured live feed: 183 products / 318 variants.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request

from tracker import collect, replay
from tracker.common import FetchError

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "collect")
LIVE_FIXTURE = os.path.join(FIXTURE_DIR, "soulandmore_products_page1.json")

STORE = "soulandmore.co"
BASE_URL = "https://soulandmore.co"
STORE_CONF = {"slug": STORE, "base_url": BASE_URL, "currency": "EGP"}

TS1 = "2026-07-29T06:00:11Z"
TS2 = "2026-07-30T06:00:09Z"
TS3 = "2026-07-31T06:00:07Z"
TS4 = "2026-08-01T06:00:05Z"
TS5 = "2026-08-02T06:00:03Z"

# The known-good anchor row from docs/SCHEMA.md §2.
WM_HANDLE = "white-musk-splash"
WM_PRODUCT_ID = 7042570879142
WM_KEY = f"{WM_PRODUCT_ID}:Default Title"
WM_PRICE = 19900
WM_COMPARE_AT = 35000

BUNDLE_HANDLE = "his-hers-bundle"

PRODUCT_COUNT = 183
VARIANT_COUNT = 318


def load_feed() -> list[dict]:
    """A fresh deep copy of the captured feed, so a test may mutate it freely."""
    with open(LIVE_FIXTURE, encoding="utf-8") as fh:
        return copy.deepcopy(json.load(fh)["products"])


def find_product(products: list[dict], handle: str) -> dict:
    for product in products:
        if product["handle"] == handle:
            return product
    raise AssertionError(f"fixture has no product with handle {handle!r}")


def run(prev_state: dict, products: list[dict], ts: str, run_count: int):
    return collect.process(
        prev_state, products, ts, run_count, store=STORE, base_url=BASE_URL
    )


def by_key(events: list[dict]) -> dict[str, dict]:
    return {event["variant_key"]: event for event in events}


# --------------------------------------------------------------------------- fake network
class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for urllib.request.urlopen: maps ?page=N to a payload or an exception."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.urls: list[str] = []

    def __call__(self, request):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.urls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        page = int(query["page"][0])
        outcome = self.pages.get(page, [])
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse({"products": outcome})


def no_sleep(_seconds):
    """Injected in place of time.sleep so retries/backoff cost nothing."""


class CollectTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="collect-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.history, self.state_path = collect.store_paths(STORE, data_dir=self.tmp)


# --------------------------------------------------------------------------- 1. first run
class TestFirstRun(CollectTestCase):
    def test_first_run_lists_every_variant(self):
        products = load_feed()
        events, state = run({}, products, TS1, 1)

        self.assertEqual(len(events), VARIANT_COUNT)
        self.assertEqual({e["event"] for e in events}, {"listed"})
        self.assertEqual(len(state["variants"]), VARIANT_COUNT)
        self.assertEqual(len({e["product_id"] for e in events}), PRODUCT_COUNT)

        for event in events:
            self.assertIsNone(event["prev_price"], event["variant_key"])
            self.assertIsNone(event["prev_compare_at"], event["variant_key"])
            self.assertIsNone(event["prev_available"], event["variant_key"])
            self.assertEqual(event["store"], STORE)
            self.assertEqual(event["ts"], TS1)
            self.assertIsInstance(event["price"], int)
            self.assertIsInstance(event["product_id"], int)
            self.assertIsInstance(event["variant_id"], int)
            self.assertIsInstance(event["sku"], str)

        self.assertEqual(state["store"], STORE)
        self.assertEqual(state["generated_at"], TS1)
        self.assertEqual(state["run_count"], 1)

    def test_white_musk_anchor_row(self):
        events, state = run({}, load_feed(), TS1, 1)
        event = by_key(events)[WM_KEY]

        self.assertEqual(event["event"], "listed")
        self.assertEqual(event["price"], WM_PRICE)
        self.assertEqual(event["compare_at"], WM_COMPARE_AT)
        self.assertIs(event["available"], True)
        self.assertEqual(event["handle"], WM_HANDLE)
        self.assertEqual(event["variant_title"], "Default Title")
        self.assertEqual(event["product_title"], "White Musk Body Splash")
        self.assertEqual(event["vendor"], "soulandmore")
        self.assertTrue(event["image"].startswith("https://cdn.shopify.com/"))

        entry = state["variants"][WM_KEY]
        self.assertEqual(entry["price"], WM_PRICE)
        self.assertEqual(entry["compare_at"], WM_COMPARE_AT)
        self.assertIs(entry["available"], True)
        self.assertEqual(entry["misses"], 0)
        self.assertIs(entry["delisted"], False)
        self.assertEqual(entry["first_seen"], TS1)
        self.assertEqual(entry["last_seen"], TS1)
        self.assertEqual(entry["url"], f"{BASE_URL}/products/{WM_HANDLE}")

    def test_null_sku_and_missing_compare_at_normalise_cleanly(self):
        _, state = run({}, load_feed(), TS1, 1)
        # The live feed has null skus and null compare_at_price rows; neither may crash and
        # neither may become the string "None".
        skus = {entry["sku"] for entry in state["variants"].values()}
        self.assertIn("", skus)
        self.assertNotIn("None", skus)
        self.assertIn(None, {entry["compare_at"] for entry in state["variants"].values()})


# --------------------------------------------------------------------------- 2. no-op run
class TestIdenticalSecondRun(CollectTestCase):
    def test_identical_feed_emits_nothing(self):
        _, state1 = run({}, load_feed(), TS1, 1)
        events, state2 = run(state1, load_feed(), TS2, 2)

        self.assertEqual(events, [])
        self.assertEqual(state2["run_count"], 2)
        self.assertEqual(state2["generated_at"], TS2)
        self.assertEqual(len(state2["variants"]), VARIANT_COUNT)
        # Only the run bookkeeping moved.
        for key, entry in state2["variants"].items():
            self.assertEqual(entry["misses"], 0, key)
            self.assertEqual(entry["first_seen"], TS1, key)
            self.assertEqual(entry["last_seen"], TS2, key)
            self.assertEqual(entry["price"], state1["variants"][key]["price"], key)


# --------------------------------------------------------------------------- 3. price drop
class TestPriceDrop(CollectTestCase):
    def test_single_price_drop_emits_one_change(self):
        _, state1 = run({}, load_feed(), TS1, 1)

        products = load_feed()
        find_product(products, WM_HANDLE)["variants"][0]["price"] = "149.00"
        events, state2 = run(state1, products, TS2, 2)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event"], "change")
        self.assertEqual(event["variant_key"], WM_KEY)
        self.assertEqual(event["prev_price"], WM_PRICE)
        self.assertEqual(event["price"], 14900)
        self.assertEqual(event["prev_compare_at"], WM_COMPARE_AT)
        self.assertEqual(event["compare_at"], WM_COMPARE_AT)
        self.assertIs(event["prev_available"], True)
        self.assertIs(event["available"], True)
        self.assertEqual(event["ts"], TS2)

        self.assertEqual(state2["variants"][WM_KEY]["price"], 14900)
        self.assertEqual(state2["variants"][WM_KEY]["last_seen"], TS2)
        self.assertEqual(state2["variants"][WM_KEY]["first_seen"], TS1)

    def test_compare_at_and_availability_also_trigger_change(self):
        _, state1 = run({}, load_feed(), TS1, 1)

        products = load_feed()
        variant = find_product(products, WM_HANDLE)["variants"][0]
        variant["compare_at_price"] = None
        variant["available"] = False
        events, _ = run(state1, products, TS2, 2)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "change")
        self.assertEqual(events[0]["price"], WM_PRICE)
        self.assertIsNone(events[0]["compare_at"])
        self.assertEqual(events[0]["prev_compare_at"], WM_COMPARE_AT)
        self.assertIs(events[0]["available"], False)
        self.assertIs(events[0]["prev_available"], True)


# --------------------------------------------------------------------------- 4. delisting
class TestDelistDebounce(CollectTestCase):
    @staticmethod
    def feed_without_white_musk() -> list[dict]:
        return [p for p in load_feed() if p["handle"] != WM_HANDLE]

    def test_delisted_only_on_the_fourth_consecutive_miss(self):
        _, state = run({}, load_feed(), TS1, 1)
        baseline_prices = {k: v["price"] for k, v in state["variants"].items()}

        shrunk = self.feed_without_white_musk()
        for miss, ts in enumerate((TS2, TS3, TS4), start=1):
            events, state = run(state, shrunk, ts, miss + 1)
            self.assertEqual(events, [], f"no event may fire on miss {miss}")
            entry = state["variants"][WM_KEY]
            self.assertEqual(entry["misses"], miss)
            self.assertIs(entry["delisted"], False)
            self.assertEqual(entry["price"], WM_PRICE, "price is held until delisting")
            self.assertEqual(entry["last_seen"], TS1, "last_seen freezes at the last sighting")

        events, state = run(state, shrunk, TS5, 5)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event"], "delisted")
        self.assertEqual(event["variant_key"], WM_KEY)
        self.assertIsNone(event["price"])
        self.assertIsNone(event["compare_at"])
        self.assertIsNone(event["available"])
        self.assertEqual(event["prev_price"], WM_PRICE)
        self.assertEqual(event["prev_compare_at"], WM_COMPARE_AT)
        self.assertIs(event["prev_available"], True)
        self.assertEqual(event["ts"], TS5)
        self.assertEqual(event["handle"], WM_HANDLE)

        entry = state["variants"][WM_KEY]
        self.assertIs(entry["delisted"], True)
        self.assertEqual(entry["misses"], 4)
        self.assertEqual(entry["last_seen"], TS1)
        self.assertEqual(entry["first_seen"], TS1)

        # Nothing else moved: the key is kept, every other variant is still live and priced.
        self.assertEqual(len(state["variants"]), VARIANT_COUNT)
        for key, other in state["variants"].items():
            if key == WM_KEY:
                continue
            self.assertEqual(other["misses"], 0, key)
            self.assertIs(other["delisted"], False, key)
            self.assertEqual(other["price"], baseline_prices[key], key)
            self.assertEqual(other["last_seen"], TS5, key)

    def test_delisted_is_emitted_once_and_relisting_emits_a_fresh_listed(self):
        _, state = run({}, load_feed(), TS1, 1)
        shrunk = self.feed_without_white_musk()
        for index, ts in enumerate((TS2, TS3, TS4, TS5), start=2):
            events, state = run(state, shrunk, ts, index)
        self.assertEqual(len(events), 1)

        # A fifth absent run must not repeat the delisted event.
        events, state = run(state, shrunk, "2026-08-03T06:00:00Z", 6)
        self.assertEqual(events, [])
        self.assertEqual(state["variants"][WM_KEY]["misses"], 5)

        # Back in the feed -> a fresh `listed`, original first_seen preserved.
        events, state = run(state, load_feed(), "2026-08-04T06:00:00Z", 7)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "listed")
        self.assertEqual(events[0]["variant_key"], WM_KEY)
        self.assertEqual(events[0]["price"], WM_PRICE)
        self.assertIsNone(events[0]["prev_price"])
        entry = state["variants"][WM_KEY]
        self.assertIs(entry["delisted"], False)
        self.assertEqual(entry["misses"], 0)
        self.assertEqual(entry["first_seen"], TS1)
        self.assertEqual(entry["last_seen"], "2026-08-04T06:00:00Z")

    def test_mass_disappearance_trips_the_sanity_guard(self):
        _, state = run({}, load_feed(), TS1, 1)
        truncated = load_feed()[:50]  # ~80 variants, far under 90% of 318
        with self.assertRaises(collect.SanityError):
            run(state, truncated, TS2, 2)

    def test_sanity_guard_allows_a_small_genuine_shrink(self):
        _, state = run({}, load_feed(), TS1, 1)
        products = load_feed()
        dropped = {p["id"] for p in products if len(p["variants"]) == 1}
        dropped = set(sorted(dropped)[:10])
        keep = [p for p in products if p["id"] not in dropped]
        events, _ = run(state, keep, TS2, 2)
        self.assertEqual(events, [])  # 308/318 is above the 90% floor: just misses

    def test_sanity_guard_is_skipped_on_the_first_run(self):
        events, _ = run({}, load_feed()[:5], TS1, 1)
        self.assertTrue(events)


# --------------------------------------------------------------------------- 5. fetch
class TestFetch(CollectTestCase):
    def test_pagination_stops_on_the_first_empty_page(self):
        feed = load_feed()
        opener = FakeOpener({1: feed[:100], 2: feed[100:], 3: []})
        sleeps: list[float] = []
        products = collect.fetch_all_products(
            BASE_URL, opener=opener, sleep=sleeps.append
        )

        self.assertEqual(len(products), PRODUCT_COUNT)
        self.assertEqual(len(opener.urls), 3)
        self.assertIn("limit=250&page=1", opener.urls[0])
        self.assertIn("page=3", opener.urls[2])
        self.assertEqual(sleeps, [collect.PAGE_SLEEP_SECONDS, collect.PAGE_SLEEP_SECONDS])

    def test_failure_on_page_two_raises_and_writes_nothing(self):
        feed = load_feed()
        opener = FakeOpener(
            {
                1: feed,
                2: urllib.error.URLError("connection reset by peer"),
                3: [],
            }
        )

        with self.assertRaises(FetchError):
            collect.fetch_all_products(BASE_URL, opener=opener, sleep=no_sleep)

        # Now the same failure at the collect_store level: neither data file may appear.
        with self.assertRaises(FetchError):
            collect.collect_store(
                STORE_CONF, TS1, data_dir=self.tmp, opener=opener, sleep=no_sleep
            )
        self.assertFalse(os.path.exists(self.history), "history.jsonl must not be created")
        self.assertFalse(os.path.exists(self.state_path), "state.json must not be created")

    def test_failure_on_page_two_leaves_existing_files_untouched(self):
        feed = load_feed()
        good = FakeOpener({1: feed, 2: []})
        collect.collect_store(
            STORE_CONF, TS1, data_dir=self.tmp, opener=good, sleep=no_sleep
        )
        with open(self.history, encoding="utf-8") as fh:
            history_before = fh.read()
        with open(self.state_path, encoding="utf-8") as fh:
            state_before = fh.read()

        broken = FakeOpener({1: feed[:100], 2: urllib.error.URLError("boom")})
        with self.assertRaises(FetchError):
            collect.collect_store(
                STORE_CONF, TS2, data_dir=self.tmp, opener=broken, sleep=no_sleep
            )

        with open(self.history, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), history_before)
        with open(self.state_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), state_before)

    def test_non_json_payload_raises_fetch_error(self):
        class HtmlOpener:
            def __call__(self, request):
                class _Resp:
                    def read(self_inner):
                        return b"<html>are you a robot?</html>"

                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *exc):
                        return False

                return _Resp()

        with self.assertRaises(FetchError):
            collect.fetch_all_products(BASE_URL, opener=HtmlOpener(), sleep=no_sleep)


# --------------------------------------------------------------------------- 6. multi-variant
class TestMultiVariantProduct(CollectTestCase):
    def test_bundle_yields_one_event_per_variant(self):
        products = load_feed()
        bundle = find_product(products, BUNDLE_HANDLE)
        expected = len(bundle["variants"])
        self.assertGreater(expected, 1, "fixture bundle should be multi-variant")

        events, state = run({}, products, TS1, 1)
        bundle_events = [e for e in events if e["product_id"] == int(bundle["id"])]
        keys = {e["variant_key"] for e in bundle_events}

        self.assertEqual(len(bundle_events), expected)
        self.assertEqual(len(keys), expected, "variants must not collapse into one key")
        self.assertEqual(
            keys,
            {f"{bundle['id']}:{v['title'].strip()}" for v in bundle["variants"]},
        )
        for event in bundle_events:
            self.assertEqual(event["handle"], BUNDLE_HANDLE)
            self.assertIn(event["variant_key"], state["variants"])

        # Changing one variant of the bundle must move only that variant.
        target = bundle["variants"][0]
        target["price"] = "499.00"
        changed, _ = run(state, products, TS2, 2)
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["variant_key"], f"{bundle['id']}:{target['title'].strip()}")


# --------------------------------------------------------------------------- 7. persistence
class TestPersistence(CollectTestCase):
    def test_history_is_valid_jsonl_with_sorted_keys(self):
        events, state = run({}, load_feed(), TS1, 1)
        collect.append_events(self.history, events)

        with open(self.history, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertTrue(raw.endswith("\n"))
        lines = raw.splitlines()
        self.assertEqual(len(lines), VARIANT_COUNT)

        for lineno, line in enumerate(lines, 1):
            parsed = json.loads(line)  # must not raise
            pairs = json.loads(line, object_pairs_hook=lambda p: p)
            keys = [k for k, _ in pairs]
            self.assertEqual(keys, sorted(keys), f"line {lineno} keys are not sorted")
            self.assertEqual(tuple(keys), collect.EVENT_KEYS, f"line {lineno} key set")
            self.assertEqual(parsed["store"], STORE)
            # Byte-for-byte: sorted keys, compact separators, ensure_ascii=False.
            self.assertEqual(
                line,
                json.dumps(
                    parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ),
                f"line {lineno} is not canonically serialised",
            )

    def test_append_is_append_and_empty_events_do_not_touch_the_file(self):
        events, state = run({}, load_feed(), TS1, 1)
        collect.append_events(self.history, events[:2])
        collect.append_events(self.history, events[2:4])
        with open(self.history, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 4)

        collect.append_events(self.history, [])
        with open(self.history, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 4)

        missing, _ = collect.store_paths("never-written", data_dir=self.tmp)
        collect.append_events(missing, [])
        self.assertFalse(os.path.exists(missing))

    def test_write_state_is_atomic_and_readable(self):
        _, state = run({}, load_feed(), TS1, 1)
        collect.write_state(self.state_path, state)

        with open(self.state_path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn(".tmp", os.listdir(os.path.dirname(self.state_path)))
        reloaded = json.loads(raw)
        self.assertEqual(reloaded, state)
        self.assertIn('\n  "generated_at"', raw, "pretty-printed with 2-space indent")
        top_level = [k for k, _ in json.loads(raw, object_pairs_hook=lambda p: p)]
        self.assertEqual(top_level, sorted(top_level))

        self.assertEqual(collect.load_state(self.state_path), state)
        self.assertEqual(collect.load_state(os.path.join(self.tmp, "nope.json")), {})

    def test_collect_store_round_trip(self):
        feed = load_feed()
        opener = FakeOpener({1: feed, 2: []})
        summary = collect.collect_store(
            STORE_CONF, TS1, data_dir=self.tmp, opener=opener, sleep=no_sleep
        )
        self.assertEqual(summary["run_count"], 1)
        self.assertEqual(summary["events"], VARIANT_COUNT)
        self.assertEqual(summary["variants"], VARIANT_COUNT)

        with open(self.history, encoding="utf-8") as fh:
            first_run_log = fh.read()

        # A second, identical run: state moves, the log does not.
        summary2 = collect.collect_store(
            STORE_CONF, TS2, data_dir=self.tmp, opener=FakeOpener({1: feed, 2: []}),
            sleep=no_sleep,
        )
        self.assertEqual(summary2["run_count"], 2)
        self.assertEqual(summary2["events"], 0)
        with open(self.history, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), first_run_log)
        state = collect.load_state(self.state_path)
        self.assertEqual(state["run_count"], 2)
        self.assertEqual(state["generated_at"], TS2)


# --------------------------------------------------------------------------- replay
class TestReplay(CollectTestCase):
    def test_replay_reconstructs_state_after_change_and_delist(self):
        feed = load_feed()
        collect.collect_store(
            STORE_CONF, TS1, data_dir=self.tmp, opener=FakeOpener({1: feed, 2: []}),
            sleep=no_sleep,
        )

        dropped = load_feed()
        find_product(dropped, BUNDLE_HANDLE)["variants"][0]["price"] = "499.00"
        dropped = [p for p in dropped if p["handle"] != WM_HANDLE]
        for ts in (TS2, TS3, TS4, TS5):
            collect.collect_store(
                STORE_CONF, ts, data_dir=self.tmp,
                opener=FakeOpener({1: dropped, 2: []}), sleep=no_sleep,
            )

        replayed = replay.replay(self.history)
        state = collect.load_state(self.state_path)

        self.assertEqual(set(replayed), set(state["variants"]))
        self.assertEqual(replay.compare_with_state(replayed, state), [])
        self.assertIs(replayed[WM_KEY]["delisted"], True)
        self.assertIsNone(replayed[WM_KEY]["price"])
        self.assertEqual(replayed[WM_KEY]["delisted_at"], TS5)
        self.assertEqual(replayed[WM_KEY]["first_seen"], TS1)

        bundle_id = find_product(load_feed(), BUNDLE_HANDLE)["id"]
        bundle_key = f"{bundle_id}:{find_product(load_feed(), BUNDLE_HANDLE)['variants'][0]['title'].strip()}"
        self.assertEqual(replayed[bundle_key]["price"], 49900)
        self.assertEqual(replayed[bundle_key]["events"], 2)

    def test_replay_of_a_missing_log_is_empty(self):
        self.assertEqual(replay.replay(os.path.join(self.tmp, "nothing.jsonl")), {})

    def test_replay_rejects_a_change_without_a_listing(self):
        events, _ = run({}, load_feed(), TS1, 1)
        orphan = dict(events[0], event="change")
        collect.append_events(self.history, [orphan])
        with self.assertRaises(ValueError):
            replay.replay(self.history)

    def test_compare_with_state_reports_a_drift(self):
        events, state = run({}, load_feed(), TS1, 1)
        collect.append_events(self.history, events)
        replayed = replay.replay(self.history)
        state["variants"][WM_KEY]["price"] = 1
        problems = replay.compare_with_state(replayed, state)
        self.assertEqual(len(problems), 1)
        self.assertIn(WM_KEY, problems[0])


# --------------------------------------------------------------------------- money guards
class TestMoneyGuards(CollectTestCase):
    def test_prices_are_integers_in_minor_units(self):
        _, state = run({}, load_feed(), TS1, 1)
        for key, entry in state["variants"].items():
            self.assertIsInstance(entry["price"], int, key)
            self.assertNotIsInstance(entry["price"], bool, key)
            if entry["compare_at"] is not None:
                self.assertIsInstance(entry["compare_at"], int, key)

    def test_an_insane_price_aborts_the_run(self):
        products = load_feed()
        find_product(products, WM_HANDLE)["variants"][0]["price"] = "0.00"
        with self.assertRaises(AssertionError):
            run({}, products, TS1, 1)

    def test_zero_compare_at_becomes_null(self):
        # The captured feed carries 18 rows with compare_at_price "0.00" (Shopify's other
        # spelling of "unset"); they must land as null, never as 0.
        _, state = run({}, load_feed(), TS1, 1)
        self.assertNotIn(0, {e["compare_at"] for e in state["variants"].values()})

        products = load_feed()
        find_product(products, WM_HANDLE)["variants"][0]["compare_at_price"] = "0.00"
        _, state = run({}, products, TS1, 1)
        self.assertIsNone(state["variants"][WM_KEY]["compare_at"])

    def test_a_malformed_price_aborts_the_run(self):
        products = load_feed()
        find_product(products, WM_HANDLE)["variants"][0]["price"] = "199,00 EGP"
        with self.assertRaises(ValueError):
            run({}, products, TS1, 1)


# --------------------------------------------------------------------------- entry points
STORES_YML = """stores:
  - slug: soulandmore.co
    base_url: https://soulandmore.co
    currency: EGP
"""


class TestEntryPoints(CollectTestCase):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.tmp, "stores.yml"), "w", encoding="utf-8") as fh:
            fh.write(STORES_YML)
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.cwd)
        # main() resolves data/<slug>/ relative to the cwd.
        self.history, self.state_path = collect.store_paths(STORE, data_dir=collect.DATA_DIR)

    def test_main_returns_zero_and_writes_both_files(self):
        feed = load_feed()
        with unittest.mock.patch.object(
            collect, "fetch_all_products", return_value=feed
        ), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(collect.main(), 0)

        self.assertIn("318 variants", out.getvalue())
        self.assertTrue(os.path.exists(self.history))
        self.assertTrue(os.path.exists(self.state_path))
        state = collect.load_state(self.state_path)
        self.assertEqual(state["store"], STORE)
        self.assertEqual(state["run_count"], 1)

    def test_main_exits_non_zero_and_writes_nothing_when_the_fetch_fails(self):
        with unittest.mock.patch.object(
            collect, "fetch_all_products", side_effect=FetchError("HTTP 429")
        ), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                collect.main()

        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(os.path.exists(self.history))
        self.assertFalse(os.path.exists(self.state_path))

    def test_main_exits_non_zero_when_the_sanity_guard_trips(self):
        feed = load_feed()
        with unittest.mock.patch.object(
            collect, "fetch_all_products", return_value=feed
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collect.main(), 0)
        with open(self.history, encoding="utf-8") as fh:
            log_after_first_run = fh.read()

        with unittest.mock.patch.object(
            collect, "fetch_all_products", return_value=feed[:20]
        ), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                collect.main()

        self.assertEqual(ctx.exception.code, 1)
        with open(self.history, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), log_after_first_run, "truncated feed must not append")
        self.assertEqual(collect.load_state(self.state_path)["run_count"], 1)

    def test_replay_cli_summarises_and_verifies(self):
        feed = load_feed()
        with unittest.mock.patch.object(
            collect, "fetch_all_products", return_value=feed
        ), contextlib.redirect_stdout(io.StringIO()):
            collect.main()

        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = replay.main([STORE])
        self.assertEqual(code, 0)
        self.assertIn("variants     : 318 (318 live, 0 delisted)", out.getvalue())
        self.assertIn("state.json   : OK", out.getvalue())

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(replay.main([]), 2)
            self.assertEqual(replay.main(["no-such-store"]), 1)


if __name__ == "__main__":
    unittest.main()
