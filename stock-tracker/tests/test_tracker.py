"""Tests: python -m unittest discover -s tests   (run from the stock-tracker folder)

Everything runs against a local HTTP server; no real retailer is contacted."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from stocktracker import cli, notify  # noqa: E402
from stocktracker.alerts import evaluate, fmt_price  # noqa: E402
from stocktracker.config import Config, ConfigError, Listing, Product, parse_duration  # noqa: E402
from stocktracker.engine import Tracker  # noqa: E402
from stocktracker.extract import (  # noqa: E402
    ExtractError,
    Result,
    availability_to_bool,
    extract,
    parse_html,
    parse_price,
    parse_shopify,
)
from stocktracker.fetch import BlockedError, Fetcher, FetchError  # noqa: E402
from stocktracker.notify import ConsoleNotifier, Notifier, send_all  # noqa: E402
from stocktracker.storage import Store  # noqa: E402


def jsonld(obj: dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


PAGES: dict[str, tuple[int, str, str, dict]] = {}  # path -> (status, content-type, body, headers)


def page(path: str, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8", **headers):
    PAGES[path] = (status, ctype, body, headers)


class Handler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        Handler.hits.append(self.path)
        status, ctype, body, headers = PAGES.get(self.path, (404, "text/html", "<h1>Not found</h1>", {}))
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"
threading.Thread(target=SERVER.serve_forever, daemon=True).start()

SETTINGS = {"timeout": 5, "per_host_delay": 0, "user_agent": "test"}

# Tests must never reach real notification services, even if the machine has credentials set.
for _var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL", "SMTP_PASSWORD", "BESTBUY_API_KEY"):
    os.environ.pop(_var, None)
BLOCKED_POSTS: list[str] = []


def _no_external_post(url, *args, **kwargs):
    BLOCKED_POSTS.append(url)
    raise AssertionError(f"test tried to send a real notification to {url}")


notify.requests.post = _no_external_post
notify.smtplib = None  # any email attempt fails immediately

# --------------------------------------------------------------------------- fixtures

page("/jsonld-in-stock", f"""<html><head><title>Shop</title>
{jsonld({"@context": "https://schema.org", "@type": "Product", "name": "GeForce RTX 5090 Founders Edition",
         "offers": {"@type": "Offer", "price": "1999.99", "priceCurrency": "USD",
                    "availability": "https://schema.org/InStock"}})}
</head><body><div class="related">Other card - Out of stock</div></body></html>""")

page("/graph-variants", f"""<html><head>
{jsonld({"@context": "https://schema.org", "@graph": [
    {"@type": "BreadcrumbList", "itemListElement": []},
    {"@type": "ProductGroup", "name": "RTX 5090 Gaming OC", "hasVariant": [
        {"@type": "Product", "name": "A", "offers": {"@type": "Offer", "price": 2199, "availability": "OutOfStock"}},
        {"@type": "Product", "name": "B", "offers": {"@type": "Offer", "price": 2399, "availability": "InStock",
                                                     "priceCurrency": "USD"}}]}]})}
</head><body></body></html>""")

page("/aggregate", jsonld({"@type": "Product", "name": "Agg", "offers": {
    "@type": "AggregateOffer", "lowPrice": "1500", "highPrice": "4000", "priceCurrency": "USD",
    "offers": [{"@type": "Offer", "price": "4000", "availability": "http://schema.org/InStock"},
               {"@type": "Offer", "price": "1500", "availability": "http://schema.org/OutOfStock"}]}}))

page("/price-spec", jsonld({"@type": "Product", "name": "Spec", "offers": {
    "@type": "Offer", "availability": "https://schema.org/OutOfStock",
    "priceSpecification": [{"@type": "UnitPriceSpecification", "price": 2099.0, "priceCurrency": "EUR"}]}}))

page("/broken-jsonld", """<script type="application/ld+json">{ this is not json </script>
<script type="application/ld+json">{"@type": "Product", "name": "Ok", "offers": {"price": "10.50",
 "availability": "SoldOut"}}</script>""")

page("/microdata", """<div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">Card</h1>
<span itemprop="price" content="1899.00">$1,899.00</span><meta itemprop="priceCurrency" content="CAD">
<link itemprop="availability" href="https://schema.org/OutOfStock"></div>""")

page("/meta", """<html><head><meta property="og:title" content="Meta Card">
<meta property="product:price:amount" content="1.749,00"><meta property="product:price:currency" content="EUR">
<meta property="product:availability" content="in stock"></head></html>""")

page("/keywords-sold-out", "<html><body><h1>Card</h1><p>$2,049.99</p><button>Sold Out</button></body></html>")
page("/keywords-buy", "<html><body><h1>Card</h1><button>Add to Cart</button></body></html>")
page("/nothing", "<html><head><title>Hello</title></head><body>Just a blog post.</body></html>")

page("/selectors", f"""<html><head>{jsonld({"@type": "Product", "name": "Stale",
    "offers": {"price": "2500", "availability": "OutOfStock"}})}</head>
<body><span class="price-now">$1,849.99</span><div id="buy"><button>Add to cart</button></div></body></html>""")

page("/cloudflare", "<html><head><title>Just a moment...</title></head><body>cf-chl-widget</body></html>",
     status=403, Server="cloudflare")
page("/rate-limited", "slow down", status=429, ctype="text/plain", Retry_After="120")

SHOPIFY = {"title": "RTX 5090 Master", "available": True, "variants": [
    {"id": 111, "title": "Standard", "available": False, "price": 219999},
    {"id": 222, "title": "OC Edition", "available": True, "price": 239999}]}
page("/products/rtx-5090-master.js", json.dumps(SHOPIFY), ctype="application/javascript")

page("/api/v1/products/6614151.json?apiKey=SECRET123&show=all",
     json.dumps({"sku": 6614151, "name": "NVIDIA GeForce RTX 5090 32GB", "salePrice": 1999.99,
                 "onlineAvailability": False, "url": "https://example.com/rtx5090"}),
     ctype="application/json")
page("/api/bad-key?apiKey=SECRET123", json.dumps({"error": "invalid apiKey=SECRET123"}), status=403, ctype="application/json")

API_RETAILER = {"name": "Test API", "type": "json_api", "api_key": "${TEST_API_KEY}",
                "url": BASE + "/api/v1/products/{sku}.json?apiKey={api_key}&show=all",
                "price_path": "salePrice", "in_stock_path": "onlineAvailability",
                "title_path": "name", "link_path": "url"}


def make_listing(url=None, retailer=None, fields=None, product=None):
    retailer = retailer or {"type": "html"}
    f = {"url": url} if url else {}
    f.update(fields or {})
    return Listing(product=product or Product(name="RTX 5090"), index=1, retailer_key="t",
                   retailer=retailer, retailer_raw=retailer, fields=f, interval=60)


def fetcher():
    return Fetcher(SETTINGS, sleep=lambda s: None)


# --------------------------------------------------------------------------- unit tests


class TestParsing(unittest.TestCase):
    def test_parse_price(self):
        cases = {"$1,999.99": 1999.99, "1.999,99 €": 1999.99, "€ 1 999,99": 1999.99, "1999": 1999.0,
                 "$2,000": 2000.0, "19,99": 19.99, "CA$2,349.00 CAD": 2349.0, "Now: 1,849.99!": 1849.99,
                 "1.234.567": 1234567.0, 1999: 1999.0, "free": None, None: None, "": None}
        for text, expected in cases.items():
            self.assertEqual(parse_price(text), expected, text)

    def test_availability(self):
        self.assertTrue(availability_to_bool("https://schema.org/InStock"))
        self.assertTrue(availability_to_bool("PreOrder"))
        self.assertTrue(availability_to_bool("in stock"))
        self.assertFalse(availability_to_bool("http://schema.org/OutOfStock"))
        self.assertFalse(availability_to_bool("SOLD_OUT"))
        self.assertFalse(availability_to_bool(0))
        self.assertTrue(availability_to_bool(3))
        self.assertIsNone(availability_to_bool("maybe"))
        self.assertTrue(availability_to_bool("Limited", in_values=["limited", "available"]))

    def test_durations(self):
        self.assertEqual(parse_duration("2m"), 120)
        self.assertEqual(parse_duration("1.5h"), 5400)
        self.assertEqual(parse_duration(45), 45)
        self.assertEqual(parse_duration("30 sec"), 30)
        for bad in ("soon", "-5", 0, True):
            with self.assertRaises(ConfigError):
                parse_duration(bad)

    def test_fmt_price(self):
        self.assertEqual(fmt_price(1999.5, "USD"), "$1,999.50")
        self.assertEqual(fmt_price(1999.5, "CHF"), "1,999.50 CHF")
        self.assertEqual(fmt_price(None, "USD"), "-")


class TestExtract(unittest.TestCase):
    def get(self, path, retailer=None):
        return extract(make_listing(BASE + path, retailer), fetcher())

    def test_jsonld_beats_page_keywords(self):
        r = self.get("/jsonld-in-stock")
        self.assertEqual((r.price, r.in_stock, r.currency), (1999.99, True, "USD"))
        self.assertEqual(r.title, "GeForce RTX 5090 Founders Edition")
        self.assertIn("JSON-LD", r.stock_source)

    def test_product_group_prefers_in_stock_variant(self):
        r = self.get("/graph-variants")
        self.assertEqual((r.price, r.in_stock, r.title), (2399.0, True, "RTX 5090 Gaming OC"))

    def test_aggregate_offer_uses_individual_offers(self):
        r = self.get("/aggregate")
        self.assertEqual((r.price, r.in_stock), (4000.0, True))

    def test_price_specification(self):
        r = self.get("/price-spec")
        self.assertEqual((r.price, r.in_stock, r.currency), (2099.0, False, "EUR"))

    def test_broken_jsonld_block_is_skipped(self):
        r = self.get("/broken-jsonld")
        self.assertEqual((r.price, r.in_stock), (10.5, False))

    def test_microdata(self):
        r = self.get("/microdata")
        self.assertEqual((r.price, r.in_stock, r.currency), (1899.0, False, "CAD"))

    def test_meta_tags(self):
        r = self.get("/meta")
        self.assertEqual((r.price, r.in_stock, r.currency, r.title), (1749.0, True, "EUR", "Meta Card"))

    def test_keyword_fallback(self):
        self.assertFalse(self.get("/keywords-sold-out").in_stock)
        self.assertTrue(self.get("/keywords-buy").in_stock)

    def test_selectors_override_structured_data(self):
        r = self.get("/selectors", {"type": "html", "price_selector": ".price-now", "stock_selector": "#buy",
                                    "in_stock_text": ["Add to cart"]})
        self.assertEqual((r.price, r.in_stock), (1849.99, True))
        self.assertIn("selector", r.price_source)

    def test_price_regex(self):
        r = parse_html('<script>var data = {"amount": 1777.25};</script>',
                       {"type": "html", "price_regex": r'"amount":\s*([\d.]+)'})
        self.assertEqual(r.price, 1777.25)

    def test_nothing_found_is_an_error(self):
        with self.assertRaises(ExtractError):
            self.get("/nothing")

    def test_bot_wall_is_reported_not_bypassed(self):
        with self.assertRaises(BlockedError) as ctx:
            self.get("/cloudflare")
        self.assertIn("Cloudflare", str(ctx.exception))

    def test_rate_limit_retry_after(self):
        with self.assertRaises(BlockedError) as ctx:
            self.get("/rate-limited")
        self.assertEqual(ctx.exception.retry_after, 120)

    def test_404(self):
        with self.assertRaises(FetchError) as ctx:
            self.get("/missing")
        self.assertNotIsInstance(ctx.exception, BlockedError)
        self.assertIn("404", str(ctx.exception))

    def test_shopify_any_variant(self):
        r = extract(make_listing(BASE + "/products/rtx-5090-master", {"type": "shopify"}), fetcher())
        self.assertEqual((r.price, r.in_stock), (2399.99, True))

    def test_shopify_specific_variant(self):
        r = extract(make_listing(BASE + "/products/rtx-5090-master?variant=111", {"type": "shopify"}), fetcher())
        self.assertEqual((r.price, r.in_stock), (2199.99, False))
        self.assertEqual(r.title, "RTX 5090 Master (Standard)")
        with self.assertRaises(ExtractError):
            parse_shopify(SHOPIFY, variant="Ti Super")

    def test_json_api(self):
        os.environ["TEST_API_KEY"] = "SECRET123"
        try:
            from stocktracker.config import expand_env

            r = extract(make_listing(retailer=expand_env(API_RETAILER), fields={"sku": "6614151"}), fetcher())
        finally:
            del os.environ["TEST_API_KEY"]
        self.assertEqual((r.price, r.in_stock, r.title, r.link),
                         (1999.99, False, "NVIDIA GeForce RTX 5090 32GB", "https://example.com/rtx5090"))

    def test_json_api_missing_key_names_env_var(self):
        listing = make_listing(fields={"sku": "1"})
        listing.retailer = {**API_RETAILER, "api_key": ""}
        listing.retailer_raw = API_RETAILER
        with self.assertRaises(ExtractError) as ctx:
            extract(listing, fetcher())
        self.assertIn("TEST_API_KEY", str(ctx.exception))

    def test_api_errors_hide_keys(self):
        with self.assertRaises(FetchError) as ctx:
            fetcher().get(BASE + "/api/bad-key?apiKey=SECRET123")
        self.assertNotIn("SECRET123", str(ctx.exception))
        self.assertIn("403", str(ctx.exception))


class TestAlerts(unittest.TestCase):
    settings = {"price_drop_min_percent": 1.0}

    def setUp(self):
        self.product = Product(name="RTX 5090", target_price=2000, max_price=2800)
        self.listing = make_listing("https://shop.example/5090", product=self.product)

    def ev(self, prev, price, in_stock):
        prev_row = None if prev is None else {"price": prev[0], "in_stock": None if prev[1] is None else int(prev[1])}
        return evaluate(self.listing, prev_row, Result(price=price, in_stock=in_stock, currency="USD"), self.settings)

    def test_restock(self):
        a = self.ev((2199, False), 2199, True)
        self.assertEqual(a.kinds, ["in_stock"])
        self.assertIn("IN STOCK", a.title)
        self.assertIn("https://shop.example/5090", a.message)

    def test_first_check_in_stock_alerts(self):
        self.assertEqual(self.ev(None, 2199, True).kinds, ["in_stock"])

    def test_no_repeat_alerts(self):
        self.assertIsNone(self.ev((2199, True), 2199, True))
        self.assertIsNone(self.ev((2199, False), 2199, False))

    def test_scalper_price_ignored_until_reasonable(self):
        self.assertIsNone(self.ev((None, False), 3999, True))
        self.assertIsNone(self.ev((3999, True), 3899, True))  # no price-drop alert above max_price
        self.assertEqual(self.ev((3999, True), 2499, True).kinds, ["in_stock", "price_drop"])

    def test_target_price(self):
        a = self.ev((2100, True), 1999, True)
        self.assertEqual(a.kinds, ["target", "price_drop"])
        self.assertIsNone(self.ev((1999, True), 1999, True))

    def test_small_drop_ignored(self):
        self.assertIsNone(self.ev((2500, True), 2499, True))

    def test_out_of_stock_drop_ignored(self):
        self.assertIsNone(self.ev((2500, False), 1900, False))

    def test_failing_channel_does_not_stop_others(self):
        class Broken(Notifier):
            name = "broken"
            secrets = ("hunter2",)

            def send(self, alert):
                raise RuntimeError("bad token hunter2")

        out = io.StringIO()
        results = send_all([Broken(), ConsoleNotifier(stream=out)], self.ev(None, 1999, True))
        self.assertIn("***", results["broken"])
        self.assertNotIn("hunter2", results["broken"])
        self.assertIsNone(results["console"])
        self.assertIn("IN STOCK", out.getvalue())


# --------------------------------------------------------------------------- config, engine, CLI


def write_config(dirpath: Path, products: list, retailers: dict | None = None) -> Path:
    path = dirpath / "config.yaml"
    path.write_text(yaml.safe_dump({
        "settings": {"per_host_delay": 0, "timeout": 5, "min_interval": 30, "default_interval": 60},
        "notifications": {"console": False},
        "retailers": retailers or {},
        "products": products,
    }), encoding="utf-8")
    return path


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = write_config(self.tmp, [])

    def test_add_and_detect_retailers(self):
        cfg = Config.load(self.path)
        cfg.add_retailer("mystore", {"name": "My Store", "domains": ["mystore.example"], "currency": "CAD"})
        cfg.add_product("RTX 5090", target_price=1999, interval="2m")
        _, key = cfg.add_listing("rtx", url="https://www.mystore.example/item/5090")
        self.assertEqual(key, "mystore")
        listing, key = cfg.add_listing("RTX 5090", url="https://www.bestbuy.com/site/rtx-5090/6614151.p?skuId=6614151")
        self.assertEqual((key, listing["sku"]), ("bestbuy", "6614151"))
        _, key = cfg.add_listing("RTX 5090", url="https://unknown.example/p/1")
        self.assertEqual(key, "generic")
        with self.assertRaises(ConfigError):
            cfg.add_listing("RTX 5090", url="https://unknown.example/p/1")  # duplicate
        cfg.save()

        again = Config.load(self.path)
        listings = again.listings()
        self.assertEqual([l.retailer_key for l in listings], ["mystore", "bestbuy", "generic"])
        self.assertTrue(all(l.interval == 120 for l in listings))
        self.assertTrue((self.tmp / "config.yaml.bak").exists())

    def test_min_interval_floor(self):
        cfg = Config.load(write_config(self.tmp, [{"name": "X", "interval": "5s", "listings": [{"url": "https://a.b/c"}]}]))
        self.assertEqual(cfg.listings()[0].interval, 30)

    def test_validation_errors(self):
        bad = write_config(self.tmp, [{"name": "X", "listings": [{"retailer": "nope"}, {"retailer": "generic"}]}],
                           retailers={"weird": {"type": "ftp"}})
        with self.assertRaises(ConfigError) as ctx:
            Config.load(bad)
        msg = str(ctx.exception)
        self.assertIn("unknown retailer 'nope'", msg)
        self.assertIn("needs a `url`", msg)
        self.assertIn("type must be one of", msg)

    def test_failed_edit_is_rolled_back(self):
        cfg = Config.load(self.path)
        cfg.add_product("A")
        with self.assertRaises(ConfigError):
            cfg.edit_product("A", {"interval": "whenever"})
        self.assertNotIn("interval", cfg.find_product_raw("A"))

    def test_env_expansion(self):
        os.environ["ST_TEST_TOPIC"] = "abc"
        try:
            path = self.tmp / "c.yaml"
            path.write_text("notifications:\n  ntfy: {topic: '${ST_TEST_TOPIC}'}\n", encoding="utf-8")
            from stocktracker.notify import build_notifiers

            names = [n.name for n in build_notifiers(Config.load(path).raw["notifications"])]
            self.assertEqual(names, ["console", "ntfy"])
        finally:
            del os.environ["ST_TEST_TOPIC"]


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = write_config(self.tmp, [{"name": "RTX 5090", "target_price": 2000, "listings": [
            {"url": BASE + "/stock-flip"}, {"url": BASE + "/cloudflare"}]}])
        page("/stock-flip", jsonld({"@type": "Product", "name": "Flip",
                                    "offers": {"price": "2100", "availability": "OutOfStock"}}))

    def test_check_records_history_and_alerts(self):
        cfg = Config.load(self.path)
        sent = []

        class Capture(Notifier):
            name = "capture"

            def send(self, alert):
                sent.append(alert)

        store = Store(cfg.database_path())
        tracker = Tracker(cfg, store, fetcher=fetcher(), notifiers=[Capture()])
        outs = tracker.check_all()
        self.assertEqual([o.status for o in outs], ["ok", "blocked"])
        self.assertEqual(sent, [])

        page("/stock-flip", jsonld({"@type": "Product", "name": "Flip",
                                    "offers": {"price": "1999", "availability": "InStock"}}))
        outs = tracker.check_all()
        self.assertEqual(outs[0].alert.kinds, ["in_stock", "target", "price_drop"])
        self.assertEqual(len(sent), 1)
        self.assertIsNone(tracker.check_all()[0].alert)  # no repeat

        key = cfg.listings()[0].key
        st = store.stats(key)
        self.assertEqual((st["checks"], st["low"], st["high"]), (3, 1999.0, 2100.0))
        self.assertIsNotNone(st["last_in_stock"])
        self.assertEqual(len(store.changes(key)), 2)
        csv_path = self.tmp / "out.csv"
        self.assertEqual(store.export_csv(csv_path), 6)

    def test_run_schedules_and_backs_off(self):
        cfg = Config.load(self.path)
        now = [1000.0]
        tracker = Tracker(cfg, Store(cfg.database_path()), fetcher=fetcher(), notifiers=[],
                          sleep=lambda s: now.__setitem__(0, now[0] + s))
        seen = []
        tracker.run(on_outcome=seen.append, max_checks=6, clock=lambda: now[0])
        ok = [o for o in seen if o.status == "ok"]
        blocked = [o for o in seen if o.status == "blocked"]
        self.assertTrue(all(54 <= o.next_in <= 66 for o in ok))  # 60s +/- 10% jitter
        waits = [o.next_in for o in blocked]
        self.assertTrue(all(b > a for a, b in zip(waits, waits[1:])), waits)  # exponential backoff
        self.assertGreater(len(ok), len(blocked))

    def test_config_reload(self):
        cfg = Config.load(self.path)
        tracker = Tracker(cfg, Store(cfg.database_path()), fetcher=fetcher(), notifiers=[])
        data = yaml.safe_load(self.path.read_text())
        data["products"].append({"name": "Other", "listings": [{"url": BASE + "/keywords-buy"}]})
        self.path.write_text(yaml.safe_dump(data))
        os.utime(self.path, (1, 1))
        self.assertTrue(tracker.reload_if_changed())
        self.assertEqual(len(tracker.config.listings()), 3)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "config.yaml"

    def run_cli(self, *args) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", str(self.path), *args])
        return code, out.getvalue() + err.getvalue()

    def test_full_workflow(self):
        code, out = self.run_cli("init")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_cli("settings", "per_host_delay", "0")[0], 0)
        self.assertEqual(self.run_cli("settings", "notifications")[0], 0)  # prints "(unknown setting)"
        code, out = self.run_cli("add-product", "Steam Deck", BASE + "/jsonld-in-stock", "--target-price", "$2,100",
                                 "--interval", "10m")
        self.assertEqual(code, 0, out)
        self.assertIn("IN STOCK", out)  # the URL was test-fetched
        code, out = self.run_cli("add-retailer", "teststore", "--domain", "127.0.0.1", "--price-selector", ".price-now",
                                 "--in-stock-text", "Add to cart")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("add-listing", "steam", BASE + "/selectors", "--no-test")
        self.assertIn("teststore", out)
        code, out = self.run_cli("interval", "15m", "--product", "Steam Deck", "--listing", "2")
        self.assertIn("listing #2: every 15m", out)
        code, out = self.run_cli("edit-product", "Steam Deck", "--max-price", "3000")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_cli("edit-product", "NVIDIA", "--disable")[0], 0)

        code, out = self.run_cli("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Best Buy", out)  # the RTX product was disabled
        self.assertIn("$1,849.99", out)
        code, out = self.run_cli("list")
        self.assertIn("[disabled]", out)
        self.assertIn("every 15m", out)
        code, out = self.run_cli("history", "steam")
        self.assertIn("low $1,849.99", out)
        code, out = self.run_cli("export", str(self.tmp / "h.csv"), "--product", "steam")
        self.assertIn("Wrote 2 check(s)", out)
        code, out = self.run_cli("remove-listing", "steam", "2")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("remove-retailer", "teststore")
        self.assertEqual(code, 0, out)

        self.assertEqual(BLOCKED_POSTS, [])
        cfg = yaml.safe_load(self.path.read_text())
        steam = next(p for p in cfg["products"] if p["name"] == "Steam Deck")
        self.assertEqual((steam["target_price"], steam["max_price"], steam["interval"]), (2100.0, 3000.0, "10m"))

    def test_test_url_with_overrides(self):
        code, out = self.run_cli("test-url", BASE + "/selectors", "--price-selector", ".price-now")
        self.assertEqual(code, 0, out)
        self.assertIn("$1,849.99", out)
        self.assertIn("selector", out)
        code, out = self.run_cli("test-url", BASE + "/cloudflare")
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", out)

    def test_errors_are_friendly(self):
        code, out = self.run_cli("list")
        self.assertEqual(code, 2)
        self.assertIn("init", out)
        self.run_cli("init")
        code, out = self.run_cli("add-listing", "nope", "https://x.example/p")
        self.assertEqual(code, 2)
        self.assertIn("no product matching", out)


class TestMicroCenter(unittest.TestCase):
    """Store selection and page reading. The fixture copies the markup the built-in selectors
    expect (#pnlInventory / span#pricing); it is not a live Micro Center page."""

    @classmethod
    def setUpClass(cls):
        def mc_page(inventory: str, related: str) -> str:
            return f"""<html><body><h1>NVIDIA GeForce RTX 5090</h1>
<span id="pricing" itemprop="price" content="1999.99">$1,999.99</span>
<div id="pnlInventory"><span class="inventoryCnt">{inventory}</span> at the store</div>
<div class="related">{related}</div></body></html>"""

        page("/product/690450/rtx-5090?storeid=101", mc_page("5 NEW IN STOCK", "Other card: Sold Out"))
        page("/product/690450/rtx-5090?storeid=061", mc_page("SOLD OUT", "Another card: 2 in stock"))

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def mc_config(self, store=None, listings=None) -> Config:
        retailers = {"microcenter": {"domains": ["127.0.0.1"], **({"store": store} if store is not None else {})}}
        return Config.load(write_config(self.tmp, [{"name": "RTX 5090", "listings": listings or []}], retailers))

    def test_resolve_store(self):
        from stocktracker.config import BUILTIN_RETAILERS, resolve_store

        mc = BUILTIN_RETAILERS["microcenter"]
        self.assertEqual(resolve_store(mc, "tustin"), "101")
        self.assertEqual(resolve_store(mc, "St. Davids"), "061")
        self.assertEqual(resolve_store(mc, "Mayfield Heights"), "051")
        self.assertEqual(resolve_store(mc, "Phoenix"), "205")
        self.assertEqual(resolve_store(mc, "201"), "201")  # a store newer than the built-in list
        self.assertEqual(resolve_store(mc, 101), "101")
        self.assertEqual(resolve_store(mc, 25), "025")
        self.assertIsNone(resolve_store(mc, ""))
        with self.assertRaises(ConfigError):
            resolve_store(mc, "gotham")
        with self.assertRaises(ConfigError) as ctx:
            resolve_store(mc, 21)  # what YAML makes of an unquoted 025
        self.assertIn("quotes", str(ctx.exception))

    def test_store_in_url_and_stock(self):
        cfg = self.mc_config(store="tustin", listings=[
            {"url": BASE + "/product/690450/rtx-5090"},
            {"url": BASE + "/product/690450/rtx-5090", "store": "st-davids"}])
        tustin, st_davids = cfg.listings()
        self.assertEqual((tustin.retailer_key, tustin.store_id, st_davids.store_id), ("microcenter", "101", "061"))
        self.assertNotEqual(tustin.key, st_davids.key)  # separate price history per store

        r = extract(tustin, fetcher())
        self.assertEqual((r.price, r.in_stock), (1999.99, True))
        self.assertIn("pnlInventory", r.stock_source)
        self.assertTrue(r.link.endswith("?storeid=101"))
        r = extract(st_davids, fetcher())
        self.assertEqual((r.price, r.in_stock), (1999.99, False))

    def test_page_url(self):
        from stocktracker.extract import page_url

        cfg = self.mc_config(listings=[
            {"retailer": "microcenter", "url": "https://www.microcenter.com/product/1/x?ob=1"},
            {"retailer": "microcenter", "url": "https://www.microcenter.com/product/1/x?storeid=029", "store": "dallas"}])
        no_store, dallas = cfg.listings()
        self.assertEqual(page_url(no_store), "https://www.microcenter.com/product/1/x?ob=1")
        self.assertEqual(page_url(dallas), "https://www.microcenter.com/product/1/x?storeid=131")

    def test_missing_store_warns(self):
        cfg = self.mc_config(listings=[{"url": BASE + "/product/690450/rtx-5090"}])
        self.assertTrue(any("no store set" in w for w in cfg.warnings))
        self.assertFalse(self.mc_config(store="dallas", listings=[{"url": BASE + "/p"}]).warnings)

    def test_bad_stores_are_config_errors(self):
        with self.assertRaises(ConfigError):
            self.mc_config(store="gotham")
        with self.assertRaises(ConfigError):
            self.mc_config(listings=[{"url": BASE + "/p", "store": "nowhere"}])
        with self.assertRaises(ConfigError):  # store on a retailer that has no stores
            Config.load(write_config(self.tmp, [{"name": "X", "listings": [{"url": "https://a.example/p", "store": "1"}]}]))

    def test_cli_store_option(self):
        path = write_config(self.tmp, [{"name": "RTX 5090", "listings": []}])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            cli.main(["--config", str(path), "add-retailer", "microcenter", "--set", "store=025"])
            cli.main(["--config", str(path), "add-listing", "rtx", "https://www.microcenter.com/product/1/x",
                      "--store", "tustin", "--no-test"])
            cli.main(["--config", str(path), "add-listing", "rtx", "https://www.microcenter.com/product/1/x", "--no-test"])
            cli.main(["--config", str(path), "stores"])
        data = yaml.safe_load(path.read_text())
        self.assertEqual(data["retailers"]["microcenter"]["store"], "025")  # kept as text, not octal
        self.assertEqual([l.get("store") for l in data["products"][0]["listings"]], ["tustin", None])
        self.assertIn("westmont (025)", out.getvalue())


class TestGUI(unittest.TestCase):
    """The local web GUI's API, served on a random port against the fixture pages."""

    def setUp(self):
        import requests

        from stocktracker.gui import App, create_server

        self.tmp = Path(tempfile.mkdtemp())
        self.path = write_config(self.tmp, [{"name": "RTX 5090", "target_price": 2000,
                                             "listings": [{"url": BASE + "/jsonld-in-stock"}]}])
        self.app = App(self.path, watching=False)
        self.server = create_server(self.app, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.app.monitor.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.http = requests.Session()
        self.http.trust_env = False
        self.http.headers["X-Token"] = self.app.token

    def tearDown(self):
        self.http.close()
        self.app.monitor.stop()
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        r = self.http.get(self.base + path, timeout=10)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def post(self, path, body=None, status=200):
        r = self.http.post(self.base + path, json=body or {}, timeout=20)
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def wait_for(self, cond, timeout=10.0):
        import time

        end = time.time() + timeout
        while time.time() < end:
            state = self.get("/api/state")
            if cond(state):
                return state
            time.sleep(0.1)
        self.fail("condition not reached")

    def test_page_and_security(self):
        page_html = self.http.get(self.base + "/", timeout=5)
        self.assertEqual(page_html.status_code, 200)
        self.assertIn(self.app.token, page_html.text)
        self.assertNotIn("__API_TOKEN__", page_html.text)
        self.assertIn("frame-ancestors 'none'", page_html.headers["Content-Security-Policy"])
        no_token = self.http.get(self.base + "/api/state", headers={"X-Token": "wrong"}, timeout=5)
        self.assertEqual(no_token.status_code, 403)
        rebinding = self.http.get(self.base + "/api/state", headers={"Host": "evil.example"}, timeout=5)
        self.assertEqual(rebinding.status_code, 403)
        self.assertEqual(self.http.post(self.base + "/api/nope", json={}, timeout=5).status_code, 404)

    def test_full_flow(self):
        state = self.get("/api/state")
        self.assertEqual(state["products"][0]["name"], "RTX 5090")
        self.assertFalse(state["monitor"]["watching"])

        # check now while paused -> result and alert recorded
        self.post("/api/check", {"product": "RTX 5090"})
        state = self.wait_for(lambda s: s["products"][0]["listings"][0]["last"] is not None)
        listing = state["products"][0]["listings"][0]
        self.assertEqual((listing["last"]["status"], listing["last"]["price"], listing["last"]["in_stock"]), ("ok", 1999.99, 1))
        self.assertEqual(state["alerts"][0]["kinds"], "in_stock,target")
        self.assertEqual(state["activity"][0]["status"], "ok")

        hist = self.get("/api/history?product=rtx")
        self.assertEqual(hist["target_price"], 2000)
        self.assertEqual(hist["series"][0]["points"][-1]["price"], 1999.99)

        # products and listings
        self.post("/api/product/add", {"name": "Steam Deck", "target_price": "$399", "interval": "10m"})
        r = self.post("/api/listing/add", {"product": "Steam Deck", "url": BASE + "/keywords-buy", "label": "OLED"})
        self.wait_for(lambda s: any(l["key"] == r["key"] and l["last"] for p in s["products"] for l in p["listings"]))
        self.post("/api/product/edit", {"name": "Steam Deck", "changes": {"target_price": "", "max_price": "550", "enabled": False}})
        self.post("/api/listing/edit", {"product": "Steam Deck", "number": 1, "changes": {"label": "OLED 1TB", "interval": "15m"}})
        deck = next(p for p in self.get("/api/state")["products"] if p["name"] == "Steam Deck")
        self.assertEqual((deck["target_price"], deck["max_price"], deck["enabled"]), (None, 550.0, False))
        self.assertEqual((deck["listings"][0]["label"], deck["listings"][0]["interval_raw"]), ("OLED 1TB", "15m"))
        err = self.post("/api/listing/add", {"product": "Steam Deck", "url": "not a url"}, status=400)
        self.assertIn("http", err["error"])

        # test-url, retailers, stores, settings, notifications
        t = self.post("/api/test-url", {"url": BASE + "/selectors", "overrides": {"price_selector": ".price-now", "stock_selector": "#buy"}})
        self.assertEqual((t["status"], t["price"], t["in_stock"]), ("ok", 1849.99, True))
        self.assertEqual(self.post("/api/test-url", {"url": BASE + "/cloudflare"})["status"], "blocked")
        self.assertTrue(self.post("/api/retailer/save", {"key": "teststore", "fields": {
            "name": "Test Store", "domains": "shop.example, store.example", "out_of_stock_text": "Sold out, Gone"}})["created"])
        self.post("/api/retailer/save", {"key": "microcenter", "fields": {"store": "phoenix"}})
        state = self.get("/api/state")
        mc = next(x for x in state["retailers"] if x["key"] == "microcenter")
        self.assertEqual(mc["default_store_label"], "phoenix (205)")
        ts = next(x for x in state["retailers"] if x["key"] == "teststore")
        self.assertEqual(ts["domains"], ["shop.example", "store.example"])
        self.post("/api/retailer/save", {"key": "microcenter", "fields": {"store": "gotham"}}, status=400)
        self.post("/api/retailer/remove", {"key": "teststore"})

        self.post("/api/settings", {"settings": {"default_interval": "2m", "price_drop_min_percent": "2.5"}})
        self.post("/api/settings", {"settings": {"default_interval": "whenever"}}, status=400)
        self.assertEqual(self.get("/api/state")["settings"]["raw"]["price_drop_min_percent"], 2.5)

        self.assertEqual(self.post("/api/test-notify")["results"], {})  # console is off in tests
        self.post("/api/notifications", {"changes": {"desktop": True, "discord": {"webhook_url": "https://discord.example/hook/SECRET"}}})
        n = self.get("/api/state")["notifications"]
        self.assertEqual(n["discord"]["webhook_url"], {"value": "", "is_set": True})  # secret not sent back
        self.assertIn("discord", n["active"])
        self.post("/api/notifications", {"changes": {"sms": {"to": "1"}}}, status=400)

        # watching on -> the monitor schedules listings by itself
        self.post("/api/watch", {"on": True})
        self.wait_for(lambda s: s["monitor"]["watching"] and s["products"][0]["listings"][0]["next_in"] is not None)

        self.post("/api/listing/remove", {"product": "Steam Deck", "number": 1})
        self.post("/api/product/remove", {"name": "Steam Deck"})
        self.assertEqual([p["name"] for p in self.get("/api/state")["products"]], ["RTX 5090"])
        self.assertEqual(BLOCKED_POSTS, [])

    def test_config_error_is_reported(self):
        self.path.write_text("products: {not: a list}\n", encoding="utf-8")
        state = self.get("/api/state")
        self.assertIn("must be a list", state["config_error"])


if __name__ == "__main__":
    unittest.main()
