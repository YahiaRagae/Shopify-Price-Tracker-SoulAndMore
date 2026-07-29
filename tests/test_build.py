"""Tests for tracker.build — does docs/data.json match docs/SCHEMA.md §3?

Runs against tests/fixtures/build/, a miniature of the collector's real output covering the
cases the site has to survive: a multi-change series, an availability-only change, a
multi-variant product whose cheapest variant is out of stock, a delisted variant, and a
single-point day-one series.

    python3 -m unittest tests.test_build -v
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr

from tracker import build

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "build")
STORES_YML = os.path.join(FIXTURES, "stores.yml")

WM = "7042570879142:Default Title"          # White Musk — 3 price changes + 1 stock-only change
ALP = "15438886535334:Destiny / Alp"        # bundle variant, in stock, dropped once
NOIR = "15438886535334:Velvet Noir / Oud"   # bundle variant, cheaper but out of stock
BRIDAL = "8395926306982:Default Title"      # delisted


def run_build(data_dir: str = FIXTURES, stores: str = STORES_YML):
    """Run the real CLI entry point into a temp file and return (payload, raw_text, stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "data.json")
        err = io.StringIO()
        with redirect_stderr(err):
            rc = build.main(["--stores", stores, "--data-dir", data_dir, "--out", out])
        assert rc == 0, f"build exited {rc}"
        with open(out, encoding="utf-8") as fh:
            raw = fh.read()
    return json.loads(raw), raw, err.getvalue()


def variants_by_key(payload):
    return {v["variant_key"]: v for p in payload["products"] for v in p["variants"]}


def products_by_id(payload):
    return {p["product_id"]: p for p in payload["products"]}


class TestEnvelope(unittest.TestCase):
    """§3 top level."""

    @classmethod
    def setUpClass(cls):
        cls.payload, cls.raw, cls.stderr = run_build()

    def test_top_level_keys_are_exactly_the_schema_set(self):
        self.assertEqual(
            list(self.payload),
            ["store", "generated_at", "currency", "product_count", "variant_count", "products"],
        )

    def test_store_currency_and_generated_at(self):
        self.assertEqual(self.payload["store"], "soulandmore.co")
        self.assertEqual(self.payload["currency"], "EGP")
        # generated_at is carried through from state.json — it drives the freshness banner.
        self.assertEqual(self.payload["generated_at"], "2026-07-30T06:00:07Z")

    def test_counts_are_computed_not_copied(self):
        self.assertEqual(self.payload["product_count"], 3)
        self.assertEqual(self.payload["variant_count"], 4)
        self.assertEqual(self.payload["product_count"], len(self.payload["products"]))
        self.assertEqual(
            self.payload["variant_count"],
            sum(len(p["variants"]) for p in self.payload["products"]),
        )

    def test_products_ordered_by_title(self):
        titles = [p["title"] for p in self.payload["products"]]
        self.assertEqual(
            titles,
            ["Bridal Satin Pillow Case", "His & Hers Bundle", "White Musk Body Splash"],
        )

    def test_output_is_compact(self):
        expected = json.dumps(self.payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        self.assertEqual(self.raw, expected)
        self.assertEqual(self.raw.count("\n"), 1, "compact JSON must be a single line")

    def test_every_price_is_int_minor_units_never_float(self):
        money_keys = {"price", "compare_at", "low", "high", "min_price"}
        seen = 0
        for product in self.payload["products"]:
            for key in money_keys & set(product):
                val = product[key]
                if val is not None:
                    self.assertIsInstance(val, int, f"{key} must be int minor units")
                    self.assertNotIsInstance(val, bool)
                    seen += 1
            for variant in product["variants"]:
                for key in money_keys & set(variant):
                    val = variant[key]
                    if val is not None:
                        self.assertIsInstance(val, int, f"{key} must be int minor units")
                        seen += 1
                for day, price in variant["series"]:
                    self.assertRegex(day, r"^\d{4}-\d{2}-\d{2}$")
                    self.assertIsInstance(price, int, "series prices must be int minor units")
                    seen += 1
        self.assertGreater(seen, 0)


class TestVariantShape(unittest.TestCase):
    """§3 variant objects: series, low/high, first_day/last_day."""

    @classmethod
    def setUpClass(cls):
        cls.payload, cls.raw, cls.stderr = run_build()
        cls.variants = variants_by_key(cls.payload)

    def test_variant_keys_present(self):
        self.assertEqual(set(self.variants), {WM, ALP, NOIR, BRIDAL})

    def test_variant_keys_are_exactly_the_schema_set(self):
        self.assertEqual(
            list(self.variants[WM]),
            ["variant_key", "variant_id", "sku", "variant_title", "price", "compare_at",
             "available", "delisted", "low", "high", "first_day", "last_day", "series"],
        )

    def test_series_is_one_point_per_price_change(self):
        v = self.variants[WM]
        self.assertEqual(
            v["series"],
            [["2026-06-14", 27000], ["2026-07-15", 20000], ["2026-07-29", 19900]],
        )

    def test_availability_only_change_is_not_a_series_point(self):
        # The fixture's last White Musk event is a `change` at an unchanged 19900 that only
        # flipped `available` to false. It must not read as a price change.
        self.assertEqual(len(self.variants[WM]["series"]), 3)
        self.assertEqual(self.variants[WM]["series"][-1], ["2026-07-29", 19900])

    def test_low_high_from_observed_prices_only(self):
        v = self.variants[WM]
        self.assertEqual(v["low"], 19900)
        self.assertEqual(v["high"], 27000)
        # compare_at is seller-controlled and must never leak into low/high.
        self.assertEqual(v["compare_at"], 35000)
        self.assertNotEqual(v["high"], v["compare_at"])

    def test_first_day_and_last_day(self):
        v = self.variants[WM]
        self.assertEqual(v["first_day"], "2026-06-14")   # first series day
        self.assertEqual(v["last_day"], "2026-07-30")    # day_of(state last_seen)

    def test_current_fields_come_from_state(self):
        v = self.variants[WM]
        self.assertEqual(v["price"], 19900)
        self.assertEqual(v["variant_id"], 41292060065958)
        self.assertEqual(v["sku"], "6223009681108")
        self.assertEqual(v["variant_title"], "Default Title")
        self.assertIs(v["available"], False)
        self.assertIs(v["delisted"], False)

    def test_single_point_day_one_series(self):
        v = self.variants[NOIR]
        self.assertEqual(v["series"], [["2026-07-27", 54900]])
        self.assertEqual(v["low"], 54900)
        self.assertEqual(v["high"], 54900)
        self.assertEqual(v["first_day"], "2026-07-27")

    def test_delisted_variant_keeps_history_and_gains_no_point(self):
        v = self.variants[BRIDAL]
        # The `delisted` event carries price=null and must not become a series point.
        self.assertEqual(v["series"], [["2026-06-14", 22000]])
        self.assertIs(v["delisted"], True)
        self.assertEqual(v["last_day"], "2026-07-05")

    def test_delisted_variant_has_null_current_values(self):
        # Contract: a delisted variant carries null price/compare_at/available (SCHEMA §1/§3).
        v = self.variants[BRIDAL]
        self.assertIsNone(v["price"])
        self.assertIsNone(v["compare_at"])
        self.assertIsNone(v["available"])

    def test_delisted_variant_keeps_observed_low_and_high(self):
        # low/high come from history and are unaffected by the current price being null.
        v = self.variants[BRIDAL]
        self.assertEqual(v["low"], 22000)
        self.assertEqual(v["high"], 22000)
        self.assertEqual(v["first_day"], "2026-06-14")


class TestProductShape(unittest.TestCase):
    """§3 product objects: min_price, on_sale, available, display metadata."""

    @classmethod
    def setUpClass(cls):
        cls.payload, cls.raw, cls.stderr = run_build()
        cls.products = products_by_id(cls.payload)

    def test_product_keys_are_exactly_the_schema_set(self):
        self.assertEqual(
            list(self.products[7042570879142]),
            ["product_id", "handle", "title", "vendor", "product_type", "url", "image",
             "min_price", "on_sale", "available", "variants"],
        )

    def test_variants_grouped_by_product_id(self):
        bundle = self.products[15438886535334]
        self.assertEqual({v["variant_key"] for v in bundle["variants"]}, {ALP, NOIR})

    def test_min_price_prefers_available_variants(self):
        # Velvet Noir is cheaper (54900) but out of stock, so the shown price is Destiny's.
        bundle = self.products[15438886535334]
        self.assertEqual(bundle["min_price"], 59900)
        self.assertIs(bundle["available"], True)

    def test_min_price_falls_back_to_all_variants_when_none_available(self):
        # Fully delisted: current price is null, so min_price falls back to the last price we
        # actually observed rather than dropping the product out of every price sort.
        bridal = self.products[8395926306982]
        self.assertEqual(bridal["min_price"], 22000)
        self.assertIs(bridal["available"], False)
        self.assertIsNone(bridal["variants"][0]["price"])

    def test_on_sale_is_relative_to_the_variants_own_high(self):
        self.assertIs(self.products[7042570879142]["on_sale"], True)    # 19900 < 27000
        self.assertIs(self.products[15438886535334]["on_sale"], True)   # 59900 < 62000
        # Flat price ever since we first saw it — not on sale, even though compare_at exists
        # elsewhere in the fixture. on_sale must never be derived from compare_at.
        self.assertIs(self.products[8395926306982]["on_sale"], False)

    def test_display_metadata_and_image(self):
        wm = self.products[7042570879142]
        self.assertEqual(wm["handle"], "white-musk-splash")
        self.assertEqual(wm["title"], "White Musk Body Splash")
        self.assertEqual(wm["vendor"], "soulandmore")
        self.assertEqual(wm["product_type"], "Body Splash")
        self.assertEqual(wm["url"], "https://soulandmore.co/products/white-musk-splash")
        # No width param — the site appends its own.
        self.assertIn("white-musk.jpg", wm["image"])
        self.assertNotIn("width=", wm["image"])

    def test_url_comes_from_state(self):
        self.assertEqual(
            self.products[8395926306982]["url"],
            "https://soulandmore.co/products/bridal-satin-pillow-case",
        )

    def test_empty_image_is_preserved_not_invented(self):
        self.assertEqual(self.products[8395926306982]["image"], "")


class TestRobustness(unittest.TestCase):
    """A missing input is a warning, never a crash — the site always gets valid JSON."""

    def test_missing_store_data_yields_empty_products(self):
        with tempfile.TemporaryDirectory() as empty:
            payload, raw, stderr = run_build(data_dir=empty)
        self.assertEqual(payload["products"], [])
        self.assertEqual(payload["product_count"], 0)
        self.assertEqual(payload["variant_count"], 0)
        self.assertEqual(payload["store"], "soulandmore.co")
        self.assertEqual(payload["currency"], "EGP")
        self.assertRegex(payload["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIn("WARNING", stderr)
        json.loads(raw)  # still parseable

    def test_missing_stores_yml_yields_valid_empty_payload(self):
        with tempfile.TemporaryDirectory() as empty:
            payload, raw, stderr = run_build(
                data_dir=empty, stores=os.path.join(empty, "nope.yml")
            )
        self.assertEqual(payload["products"], [])
        self.assertIn("WARNING", stderr)

    def test_corrupt_history_lines_are_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = os.path.join(tmp, "soulandmore.co")
            os.makedirs(store_dir)
            src = os.path.join(FIXTURES, "soulandmore.co")
            with open(os.path.join(src, "history.jsonl"), encoding="utf-8") as fh:
                lines = fh.readlines()
            with open(os.path.join(store_dir, "history.jsonl"), "w", encoding="utf-8") as fh:
                fh.write("{not json at all\n")
                fh.writelines(lines)
                fh.write("\n")
            with open(os.path.join(src, "state.json"), encoding="utf-8") as fh:
                state = fh.read()
            with open(os.path.join(store_dir, "state.json"), "w", encoding="utf-8") as fh:
                fh.write(state)
            payload, _, stderr = run_build(data_dir=tmp)
        self.assertEqual(payload["product_count"], 3)
        self.assertEqual(variants_by_key(payload)[WM]["series"][0], ["2026-06-14", 27000])
        self.assertIn("not valid JSON", stderr)

    def test_state_without_history_reconstructs_a_single_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = os.path.join(tmp, "soulandmore.co")
            os.makedirs(store_dir)
            with open(os.path.join(FIXTURES, "soulandmore.co", "state.json"), encoding="utf-8") as fh:
                state = fh.read()
            with open(os.path.join(store_dir, "state.json"), "w", encoding="utf-8") as fh:
                fh.write(state)
            payload, _, stderr = run_build(data_dir=tmp)
        v = variants_by_key(payload)[WM]
        self.assertEqual(v["series"], [["2026-06-14", 19900]])  # first_seen day + current price
        self.assertEqual(v["low"], 19900)
        self.assertEqual(v["high"], 19900)
        self.assertIn("no history log", stderr)


class TestDelistedRules(unittest.TestCase):
    """The delisted contract: null current values, but the observed history still counts."""

    @staticmethod
    def entry(key, product_id, **over):
        base = {
            "product_id": product_id,
            "variant_key": key,
            "variant_id": 1,
            "sku": "",
            "handle": "p",
            "product_title": "Product",
            "variant_title": key.split(":", 1)[1],
            "vendor": "",
            "product_type": "",
            "image": "",
            "price": 9000,
            "compare_at": None,
            "available": True,
            "delisted": False,
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-05T00:00:00Z",
        }
        base.update(over)
        return base

    @staticmethod
    def payload(entries, series, base_url="https://example.test"):
        store = {"slug": "shop", "currency": "EGP", "base_url": base_url}
        state = {"generated_at": "2026-01-01T00:00:00Z", "variants": entries}
        err = io.StringIO()
        with redirect_stderr(err):
            out = build.build_payload(store, state, series)
        return out, err.getvalue()

    # A variant that was cut from 5000 to 4000, then delisted.
    GONE = {"1:a": [["2026-01-01", 5000], ["2026-01-03", 4000]]}

    def test_min_price_uses_last_observed_price_when_current_is_null(self):
        entries = {"1:a": self.entry("1:a", 1, price=None, compare_at=None,
                                     available=None, delisted=True)}
        out, _ = self.payload(entries, self.GONE)
        product = out["products"][0]
        variant = product["variants"][0]
        self.assertIsNone(variant["price"], "delisted variants keep a null current price")
        self.assertEqual(variant["low"], 4000)   # history is unaffected by the null price
        self.assertEqual(variant["high"], 5000)
        self.assertEqual(product["min_price"], 4000, "falls back to the last observed price")

    def test_delisted_variant_is_never_on_sale(self):
        # Its last observed price (4000) IS below its observed high (5000) — but something you
        # can no longer buy is not on sale, so on_sale must ignore the fallback price.
        entries = {"1:a": self.entry("1:a", 1, price=None, compare_at=None,
                                     available=None, delisted=True)}
        out, _ = self.payload(entries, self.GONE)
        self.assertIs(out["products"][0]["on_sale"], False)
        self.assertIs(out["products"][0]["available"], False)

    def test_available_variant_beats_a_cheaper_delisted_one_for_min_price(self):
        entries = {
            "1:a": self.entry("1:a", 1, price=None, compare_at=None, available=None, delisted=True),
            "1:b": self.entry("1:b", 1, price=9000),
        }
        series = dict(self.GONE, **{"1:b": [["2026-01-01", 9000]]})
        out, _ = self.payload(entries, series)
        self.assertEqual(out["products"][0]["min_price"], 9000)
        self.assertIs(out["products"][0]["available"], True)

    def test_a_live_variant_below_its_own_high_is_on_sale(self):
        entries = {"1:b": self.entry("1:b", 1, price=4000)}
        out, _ = self.payload(entries, {"1:b": [["2026-01-01", 5000], ["2026-01-03", 4000]]})
        self.assertIs(out["products"][0]["on_sale"], True)

    def test_url_falls_back_to_base_url_and_handle_when_state_lacks_one(self):
        entries = {"1:b": self.entry("1:b", 1)}
        entries["1:b"].pop("url", None)
        out, _ = self.payload(entries, {"1:b": [["2026-01-01", 9000]]})
        self.assertEqual(out["products"][0]["url"], "https://example.test/products/p")

    def test_url_from_state_wins_over_the_fallback(self):
        entries = {"1:b": self.entry("1:b", 1, url="https://example.test/products/real-handle")}
        out, _ = self.payload(entries, {"1:b": [["2026-01-01", 9000]]})
        self.assertEqual(out["products"][0]["url"], "https://example.test/products/real-handle")

    def test_delisted_with_no_history_is_not_reconstructed(self):
        # Nothing observed and no current price: leave the series empty rather than invent one.
        entries = {"1:a": self.entry("1:a", 1, price=None, compare_at=None,
                                     available=None, delisted=True)}
        out, _ = self.payload(entries, {})
        variant = out["products"][0]["variants"][0]
        self.assertEqual(variant["series"], [])
        self.assertIsNone(variant["low"])
        self.assertIsNone(variant["high"])
        self.assertIsNone(out["products"][0]["min_price"])


class TestUnits(unittest.TestCase):
    """Direct unit coverage of the two rules that are easy to get subtly wrong."""

    def test_collect_series_collapses_consecutive_equal_prices(self):
        events = [
            {"event": "listed", "variant_key": "1:x", "ts": "2026-01-01T00:00:00Z", "price": 1000},
            {"event": "change", "variant_key": "1:x", "ts": "2026-01-02T00:00:00Z", "price": 1000},
            {"event": "change", "variant_key": "1:x", "ts": "2026-01-03T00:00:00Z", "price": 900},
            {"event": "change", "variant_key": "1:x", "ts": "2026-01-04T00:00:00Z", "price": 1000},
        ]
        self.assertEqual(
            build.collect_series(events)["1:x"],
            # keeps the FIRST day at each price; a price may legitimately recur later
            [["2026-01-01", 1000], ["2026-01-03", 900], ["2026-01-04", 1000]],
        )

    def test_collect_series_orders_by_ts_and_ignores_delisted(self):
        events = [
            {"event": "change", "variant_key": "1:x", "ts": "2026-01-03T00:00:00Z", "price": 900},
            {"event": "listed", "variant_key": "1:x", "ts": "2026-01-01T00:00:00Z", "price": 1000},
            {"event": "delisted", "variant_key": "1:x", "ts": "2026-01-09T00:00:00Z", "price": None},
        ]
        self.assertEqual(
            build.collect_series(events)["1:x"],
            [["2026-01-01", 1000], ["2026-01-03", 900]],
        )

    def test_float_price_is_coerced_and_warned_never_left_a_float(self):
        events = [{"event": "listed", "variant_key": "1:x", "ts": "2026-01-01T00:00:00Z",
                   "price": 1000.0}]
        err = io.StringIO()
        with redirect_stderr(err):
            points = build.collect_series(events)["1:x"]
        self.assertEqual(points, [["2026-01-01", 1000]])
        self.assertIsInstance(points[0][1], int)
        self.assertIn("float", err.getvalue())


if __name__ == "__main__":
    unittest.main()
