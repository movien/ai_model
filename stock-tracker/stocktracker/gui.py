"""Local web GUI:  python tracker.py gui   ->  http://127.0.0.1:8765

The server listens on 127.0.0.1 only. Every API call must carry a random token that is
embedded in the page at launch, so other websites open in your browser can't drive it.
The tracker runs in a background thread while the GUI is open (it *is* the monitor).
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .alerts import Alert
from .config import (
    DEFAULT_SETTINGS,
    NOTIFY_FIELDS,
    NOTIFY_FLAGS,
    RETAILER_TYPES,
    TEXT_SETTINGS,
    Config,
    ConfigError,
    Listing,
    coerce_value,
    expand_env,
    parse_duration,
    resolve_store,
    store_label,
    uses_store,
)
from .engine import Outcome, Tracker
from .extract import ExtractError, extract, parse_price
from .fetch import BlockedError, Fetcher, FetchError
from .notify import build_notifiers, send_all
from .storage import Store

WEB_DIR = Path(__file__).resolve().parent / "web"
log = logging.getLogger("stocktracker")
MAX_BODY = 1_000_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- background monitor


class Monitor:
    """Runs the tracker's scheduler in a thread; the GUI can pause it or ask for checks now."""

    def __init__(self, config_path: Path, watching: bool = True, on_outcome: Callable[[Outcome], None] | None = None):
        self.config_path = config_path
        self.watching = watching
        self.on_outcome = on_outcome
        self.tracker: Tracker | None = None
        self.error: str | None = None
        self.activity: deque[dict[str, Any]] = deque(maxlen=150)
        self._forced: set[str] = set()
        self._force_all = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="tracker", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._shutdown.set()
        self._wake.set()

    def set_watching(self, on: bool) -> None:
        self.watching = bool(on)
        self._wake.set()

    def check_now(self, keys: list[str] | None = None) -> None:
        with self._lock:
            if keys is None:
                self._force_all = True
            else:
                self._forced.update(keys)
        self._wake.set()

    def pending(self) -> set[str] | None:
        with self._lock:
            return None if self._force_all else set(self._forced)

    def next_in(self, key: str) -> float | None:
        t = self.tracker
        if not t or key not in t.due:
            return None
        return max(0.0, t.due[key] - time.monotonic())

    def current_key(self) -> str | None:
        t = self.tracker
        cur = t.current if t else None
        return cur.key if cur else None

    def _record(self, out: Outcome) -> None:
        r = out.result
        self.activity.appendleft({
            "ts": _now_iso(), "key": out.listing.key, "label": out.listing.label, "status": out.status,
            "price": r.price if r else None, "currency": r.currency if r else None,
            "in_stock": r.in_stock if r else None, "error": out.error,
            "alert": out.alert.kinds if out.alert else None, "next_in": out.next_in,
        })
        if self.on_outcome:
            try:
                self.on_outcome(out)
            except Exception:  # noqa: BLE001
                log.debug("on_outcome failed", exc_info=True)

    def _ensure_tracker(self) -> Tracker | None:
        if self.tracker is not None:
            return self.tracker
        try:
            cfg = Config.load(self.config_path)
        except ConfigError as e:
            self.error = str(e)
            return None
        self.error = None
        # SQLite connections belong to one thread, so the tracker's store is opened here.
        self.tracker = Tracker(cfg, Store(cfg.database_path()))
        return self.tracker

    def _loop(self) -> None:
        while not self._shutdown.is_set():
            self._wake.clear()
            wait = 5.0
            t = self._ensure_tracker()
            if t is not None:
                try:
                    with self._lock:
                        forced, force_all = set(self._forced), self._force_all
                        self._forced.clear()
                        self._force_all = False
                    t.reload_if_changed()
                    keys: set[str] = set()
                    if forced or force_all:
                        keys = {l.key for l in t.config.listings()} if force_all else forced
                        now = time.monotonic()
                        for k in keys:
                            t.due[k] = now - 0.001  # due right away
                    if self.watching:
                        wait = t.run_step(on_outcome=self._record, stop=self._shutdown.is_set)
                    elif keys:  # paused: only the checks someone asked for
                        wait = t.run_step(on_outcome=self._record, only=keys, stop=self._shutdown.is_set)
                    self.error = None
                except Exception as e:  # noqa: BLE001 - keep the GUI alive
                    log.exception("tracker loop error")
                    self.error = f"{e.__class__.__name__}: {e}"
            self._wake.wait(timeout=min(max(wait, 0.05), 5.0) if self.watching else 5.0)
        if self.tracker:
            self.tracker.store.close()


# --------------------------------------------------------------------------- application / API


def _secret_view(value: Any) -> dict[str, Any]:
    text = "" if value is None else str(value)
    if "${" in text:
        return {"value": text, "is_set": True}
    return {"value": "", "is_set": bool(text)}


class App:
    def __init__(self, config_path: str | Path, watching: bool = True, on_outcome=None):
        self.config_path = Path(config_path)
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.monitor = Monitor(self.config_path, watching, on_outcome)

    # ---- helpers

    def load(self) -> Config:
        return Config.load(self.config_path)

    def mutate(self, fn: Callable[[Config], Any]) -> Any:
        with self.lock:
            cfg = self.load()
            result = fn(cfg)
            cfg.save()
            return result

    # ---- read endpoints

    def state(self) -> dict[str, Any]:
        mon = self.monitor
        base: dict[str, Any] = {
            "server_time": _now_iso(),
            "monitor": {"watching": mon.watching, "checking": mon.current_key(), "error": mon.error,
                        "pending": sorted(mon.pending() or []), "pending_all": mon.pending() is None},
            "activity": list(mon.activity)[:60],
            "config_path": str(self.config_path),
        }
        try:
            cfg = self.load()
        except ConfigError as e:
            return {**base, "config_error": str(e)}
        settings = cfg.settings
        store = Store(cfg.database_path())
        try:
            products = []
            for p in cfg.products(include_disabled=True):
                products.append({
                    "name": p.name, "target_price": p.target_price, "max_price": p.max_price,
                    "interval": p.interval, "interval_raw": cfg.find_product_raw(p.name).get("interval"),
                    "enabled": p.enabled,
                    "listings": [self._listing_state(l, store, cfg) for l in p.listings],
                })
            alerts = [dict(r) for r in store.recent_alerts(40)]
        finally:
            store.close()
        return {
            **base,
            "products": products,
            "alerts": alerts,
            "warnings": cfg.warnings,
            "retailers": self._retailers(cfg),
            "settings": {
                "raw": {k: cfg.raw["settings"].get(k, v) for k, v in DEFAULT_SETTINGS.items()},
                "effective_default_interval": max(settings["default_interval"], settings["min_interval"]),
                "min_interval": settings["min_interval"],
                "currency": settings["currency"],
            },
            "notifications": self._notifications(cfg),
        }

    def _listing_state(self, l: Listing, store: Store, cfg: Config) -> dict[str, Any]:
        last = store.last_check(l.key)
        ok = last if (last is not None and last["status"] == "ok") else store.last_ok(l.key)
        changes = store.changes(l.key, 40)
        change = None
        for c in changes:
            if c["price"] is not None and c["prev_price"] is not None and c["price"] != c["prev_price"]:
                change = {"diff": c["price"] - c["prev_price"], "pct": (c["price"] - c["prev_price"]) / c["prev_price"] * 100,
                          "ts": c["ts"]}
                break
        stats = store.stats(l.key)
        return {
            "key": l.key, "index": l.index, "enabled": l.enabled,
            "retailer_key": l.retailer_key, "retailer_name": l.retailer_name,
            "retailer_type": l.retailer.get("type", "html"),
            "url": l.url, "link": (ok["url"] if ok and ok["url"] else None) or l.url,
            "label": l.fields.get("label"), "sku": l.fields.get("sku"), "variant": l.fields.get("variant"),
            "store": l.fields.get("store"), "store_id": l.store_id, "store_label": store_label(l.retailer, l.store_id),
            "store_is_default": bool(l.store_id and not l.fields.get("store")),
            "interval": l.interval, "interval_raw": l.fields.get("interval"),
            "last": dict(last) if last else None,
            "last_ok": dict(ok) if ok else None,
            "change": change,
            "spark": [c["price"] for c in reversed(changes) if c["price"] is not None][-30:],
            "stats": {"low": stats["low"], "high": stats["high"], "last_in_stock": stats["last_in_stock"],
                      "checks": stats["checks"], "failed": stats["failed"]},
            "next_in": self.monitor.next_in(l.key),
        }

    def _retailers(self, cfg: Config) -> list[dict[str, Any]]:
        out = []
        for key, r in cfg.retailers_raw().items():
            rexp = expand_env(r)
            try:
                default_store = resolve_store(rexp, rexp.get("store")) if uses_store(rexp) else None
            except ConfigError:
                default_store = None
            out.append({
                "key": key, "name": r.get("name", key), "type": r.get("type", "html"),
                "domains": r.get("domains") or [], "source": cfg.retailer_source(key),
                "custom": dict(cfg.raw["retailers"].get(key) or {}),
                "fields": {k: r.get(k) for k in ("price_selector", "price_regex", "stock_selector", "in_stock_text",
                                                 "out_of_stock_text", "currency", "min_delay", "url", "price_path",
                                                 "in_stock_path", "title_path", "link_path", "sku_pattern")
                           if r.get(k) not in (None, "", [])},
                "needs_api_key": r.get("type") == "json_api" and "{api_key}" in str(r.get("url", ""))
                and not rexp.get("api_key"),
                "uses_store": uses_store(r),
                "stores": [{"name": n, "id": str(i)} for n, i in (r.get("stores") or {}).items()],
                "default_store": default_store,
                "default_store_label": store_label(r, default_store),
            })
        return out

    def _notifications(self, cfg: Config) -> dict[str, Any]:
        raw = cfg.raw.get("notifications") or {}
        view: dict[str, Any] = {f: raw.get(f, f != "desktop") is not False for f in NOTIFY_FLAGS}
        view["desktop"] = bool(raw.get("desktop"))
        for channel, fields in NOTIFY_FIELDS.items():
            section = raw.get(channel) or {}
            view[channel] = {}
            for f in fields:
                name = f.rstrip("*")
                view[channel][name] = _secret_view(section.get(name)) if f.endswith("*") else {
                    "value": "" if section.get(name) is None else str(section.get(name))}
        view["active"] = [n.name for n in build_notifiers(raw)]
        return view

    def history(self, product: str | None) -> dict[str, Any]:
        cfg = self.load()
        store = Store(cfg.database_path())
        try:
            products = cfg.products(include_disabled=True)
            if product:
                name = cfg.find_product_raw(product)["name"]
                products = [p for p in products if p.name == name]
            series = []
            for p in products:
                for l in p.listings:
                    label = l.retailer_name + (f" · {l.fields['label']}" if l.fields.get("label") else "")
                    if l.store_id:
                        label += f" · {store_label(l.retailer, l.store_id)}"
                    series.append({"key": l.key, "label": label, "points": store.price_points(l.key)})
            return {"series": series, "target_price": products[0].target_price if len(products) == 1 else None,
                    "max_price": products[0].max_price if len(products) == 1 else None}
        finally:
            store.close()

    # ---- write endpoints (each returns a dict merged into {"ok": true})

    @staticmethod
    def _price(value: Any) -> float | None:
        if value in (None, ""):
            return None
        price = parse_price(value)
        if price is None:
            raise ConfigError(f"not a price: {value!r}")
        return price

    @staticmethod
    def _interval(value: Any) -> Any:
        if value in (None, ""):
            return None
        parse_duration(value)
        return coerce_value(value) if isinstance(value, str) else value

    def act_watch(self, body: dict) -> dict:
        self.monitor.set_watching(bool(body.get("on")))
        return {"watching": self.monitor.watching}

    def act_check(self, body: dict) -> dict:
        keys = body.get("keys")
        if body.get("product"):
            cfg = self.load()
            name = cfg.find_product_raw(body["product"])["name"]
            keys = [l.key for p in cfg.products(include_disabled=True) if p.name == name for l in p.listings]
        self.monitor.check_now(list(keys) if keys else None)
        return {}

    def act_product_add(self, body: dict) -> dict:
        name = str(body.get("name") or "").strip()

        def fn(cfg: Config) -> None:
            cfg.add_product(name, self._price(body.get("target_price")), self._price(body.get("max_price")),
                            self._interval(body.get("interval")))
        self.mutate(fn)
        return {"name": name}

    def act_product_edit(self, body: dict) -> dict:
        changes: dict[str, Any] = {}
        c = body.get("changes") or {}
        if "name" in c and str(c["name"]).strip():
            changes["name"] = str(c["name"]).strip()
        for k in ("target_price", "max_price"):
            if k in c:
                changes[k] = self._price(c[k])
        if "interval" in c:
            changes["interval"] = self._interval(c["interval"])
        if "enabled" in c:
            changes["enabled"] = None if c["enabled"] else False
        p = self.mutate(lambda cfg: cfg.edit_product(str(body.get("name")), changes))
        return {"name": p["name"]}

    def act_product_remove(self, body: dict) -> dict:
        return {"name": self.mutate(lambda cfg: cfg.remove_product(str(body.get("name"))))}

    def act_listing_add(self, body: dict) -> dict:
        def fn(cfg: Config) -> str:
            cfg.add_listing(
                str(body.get("product")), url=(body.get("url") or "").strip() or None,
                retailer=body.get("retailer") or None, sku=body.get("sku") or None,
                variant=body.get("variant") or None, interval=self._interval(body.get("interval")),
                label=body.get("label") or None, store=body.get("store") or None,
            )
            return str(cfg.find_product_raw(str(body.get("product")))["name"])
        name = self.mutate(fn)
        cfg = self.load()
        listing = next(p for p in cfg.products(include_disabled=True) if p.name == name).listings[-1]
        self.monitor.check_now([listing.key])
        return {"key": listing.key}

    def act_listing_edit(self, body: dict) -> dict:
        changes = dict(body.get("changes") or {})
        if "interval" in changes:
            changes["interval"] = self._interval(changes["interval"])
        if "enabled" in changes:
            changes["enabled"] = None if changes["enabled"] else False
        self.mutate(lambda cfg: cfg.edit_listing(str(body.get("product")), int(body.get("number")), changes))
        return {}

    def act_listing_remove(self, body: dict) -> dict:
        self.mutate(lambda cfg: cfg.remove_listing(str(body.get("product")), int(body.get("number"))))
        return {}

    def act_test_url(self, body: dict) -> dict:
        cfg = self.load()
        url = str(body.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ConfigError("enter a full product URL starting with https://")
        overrides = {k: v for k, v in (body.get("overrides") or {}).items() if v not in (None, "", [])}
        listing = cfg.adhoc_listing(url, body.get("retailer") or None, body.get("sku") or None,
                                    body.get("variant") or None, body.get("store") or None, overrides)
        info = {"retailer_key": listing.retailer_key, "retailer_name": listing.retailer_name,
                "store_label": store_label(listing.retailer, listing.store_id)}
        try:
            res = extract(listing, Fetcher(cfg.settings))
        except BlockedError as e:
            return {**info, "status": "blocked", "error": str(e)}
        except (FetchError, ExtractError) as e:
            return {**info, "status": "error", "error": str(e)}
        return {**info, "status": "ok", "title": res.title, "price": res.price,
                "currency": res.currency or listing.retailer.get("currency") or cfg.settings["currency"],
                "in_stock": res.in_stock, "price_source": res.price_source, "stock_source": res.stock_source,
                "link": res.link}

    def act_retailer_save(self, body: dict) -> dict:
        key = str(body.get("key") or "").strip().lower()
        fields: dict[str, Any] = {}
        for k, v in (body.get("fields") or {}).items():
            if k not in ("name", "type", "domains", "price_selector", "price_regex", "stock_selector", "in_stock_text",
                         "out_of_stock_text", "currency", "min_delay", "user_agent", "url", "api_key", "price_path",
                         "in_stock_path", "title_path", "link_path", "sku_pattern", "store", "structured_data"):
                raise ConfigError(f"retailers have no field {k!r}")
            if isinstance(v, str):
                v = v.strip()
            if k in ("domains", "in_stock_text", "out_of_stock_text") and isinstance(v, str):
                v = [x.strip() for x in v.split(",") if x.strip()]
            if k == "min_delay" and v not in (None, ""):
                v = float(v)
            if k == "type" and v not in RETAILER_TYPES:
                raise ConfigError(f"type must be one of {', '.join(RETAILER_TYPES)}")
            fields[k] = None if v in ("", []) else v
        created = self.mutate(lambda cfg: cfg.add_retailer(key, fields))
        return {"created": created}

    def act_retailer_remove(self, body: dict) -> dict:
        self.mutate(lambda cfg: cfg.remove_retailer(str(body.get("key"))))
        return {}

    def act_settings(self, body: dict) -> dict:
        def fn(cfg: Config) -> None:
            for k, v in (body.get("settings") or {}).items():
                cfg.set_setting(k, v if k in TEXT_SETTINGS else coerce_value(v))
        self.mutate(fn)
        return {}

    def act_notifications(self, body: dict) -> dict:
        self.mutate(lambda cfg: cfg.set_notifications(body.get("changes") or {}))
        return {}

    def act_test_notify(self, body: dict) -> dict:
        cfg = self.load()
        notifiers = build_notifiers(cfg.raw.get("notifications"))
        alert = Alert(kinds=["in_stock"], title="Test alert from your stock tracker",
                      message="If you can read this, notifications work.")
        return {"results": send_all(notifiers, alert)}

    ACTIONS = {
        "/api/watch": "act_watch", "/api/check": "act_check",
        "/api/product/add": "act_product_add", "/api/product/edit": "act_product_edit",
        "/api/product/remove": "act_product_remove",
        "/api/listing/add": "act_listing_add", "/api/listing/edit": "act_listing_edit",
        "/api/listing/remove": "act_listing_remove", "/api/test-url": "act_test_url",
        "/api/retailer/save": "act_retailer_save", "/api/retailer/remove": "act_retailer_remove",
        "/api/settings": "act_settings", "/api/notifications": "act_notifications",
        "/api/test-notify": "act_test_notify",
    }


# --------------------------------------------------------------------------- HTTP server


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    csp = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

    class Handler(BaseHTTPRequestHandler):
        server_version = "StockTracker"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("gui: " + fmt, *args)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _guard(self) -> bool:
            if not self._host_ok():
                self._send(403, b"Forbidden", "text/plain")
                return False
            if self.path.startswith("/api/") and not secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                self._json(403, {"error": "invalid or missing token; reload the page"})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__API_TOKEN__", app.token)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                           {"Content-Security-Policy": csp, "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})
            elif url.path == "/api/state":
                self._json(200, app.state())
            elif url.path == "/api/history":
                try:
                    self._json(200, app.history((parse_qs(url.query).get("product") or [None])[0]))
                except ConfigError as e:
                    self._json(400, {"error": str(e)})
            elif url.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._send(404, b"Not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            name = App.ACTIONS.get(urlparse(self.path).path)
            if not name:
                self._json(404, {"error": "unknown action"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ConfigError("request too large")
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    raise ConfigError("expected a JSON object")
                result = getattr(app, name)(body)
                self._json(200, {"ok": True, **(result or {})})
            except (ConfigError, ValueError, KeyError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("gui action %s failed", name)
                self._json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys

        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionError, TimeoutError)):  # browser closed a connection; harmless
            log.debug("gui: connection from %s dropped: %s", client_address, err)
            return
        log.exception("gui: error handling request from %s", client_address)


def create_server(app: App, port: int = 8765, tries: int = 10) -> ThreadingHTTPServer:
    last_error: OSError | None = None
    for p in range(port, port + tries + 1) if port else [0]:
        try:
            return _Server(("127.0.0.1", p), make_handler(app))
        except OSError as e:
            last_error = e
    raise ConfigError(f"ports {port}-{port + tries} are all busy ({last_error}); pick another with --port")


def serve(config_path: Path, port: int = 8765, open_browser: bool = True, watching: bool = True,
          on_outcome: Callable[[Outcome], None] | None = None) -> None:
    Config.load(config_path)  # fail early with a readable error
    app = App(config_path, watching=watching, on_outcome=on_outcome)
    server = create_server(app, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    app.monitor.start()
    print(f"Stock tracker GUI running at {url}")
    print("Keep this window open (it is also the tracker). Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, webbrowser.open, (url,)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        app.monitor.stop()
        server.server_close()
