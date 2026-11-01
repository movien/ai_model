"""Command-line interface:  python tracker.py <command> [options]"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .alerts import Alert, fmt_price
from .config import (
    DEFAULT_SETTINGS,
    RETAILER_TYPES,
    Config,
    ConfigError,
    Listing,
    TEXT_SETTINGS,
    coerce_value,
    expand_env,
    format_duration,
    parse_duration,
    resolve_store,
    store_label,
    uses_store,
)
from .engine import Outcome, Tracker
from .extract import ExtractError, extract, parse_price
from .fetch import BlockedError, Fetcher, FetchError
from .notify import build_notifiers, send_all
from .storage import Store, parse_ts

TOOL_DIR = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = TOOL_DIR / "config.example.yaml"
log = logging.getLogger("stocktracker")


# --------------------------------------------------------------------------- output helpers


class _Style:
    def __init__(self) -> None:
        self.enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        if self.enabled and os.name == "nt":
            os.system("")  # turns on ANSI colors in the classic Windows console

    def _wrap(self, code: str) -> Callable[[str], str]:
        return lambda text: f"\033[{code}m{text}\033[0m" if self.enabled else text

    def __getattr__(self, name: str) -> Callable[[str], str]:
        codes = {"bold": "1", "dim": "2", "red": "31", "green": "1;32", "yellow": "33", "cyan": "36"}
        if name not in codes:
            raise AttributeError(name)
        return self._wrap(codes[name])


S = _Style()


def _trunc(text: Any, width: int) -> str:
    text = str(text)
    return text if len(text) <= width else text[: width - 3] + "..."


def print_table(headers: list[str], rows: list[list[Any]], indent: str = "") -> None:
    """Cells are plain values or (text, style_fn) tuples; widths ignore color codes."""

    def plain(c: Any) -> str:
        return c[0] if isinstance(c, tuple) else str(c)

    widths = [max(len(plain(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    print(indent + "  ".join(S.bold(h.ljust(w)) for h, w in zip(headers, widths)).rstrip())
    for r in rows:
        cells = [c[1](c[0].ljust(w)) if isinstance(c, tuple) else str(c).ljust(w) for c, w in zip(r, widths)]
        print(indent + "  ".join(cells).rstrip())


def status_cell(status: str, in_stock: Any) -> tuple[str, Callable[[str], str]]:
    if status == "blocked":
        return "BLOCKED", S.yellow
    if status == "error":
        return "ERROR", S.yellow
    if in_stock in (True, 1):
        return "IN STOCK", S.green
    if in_stock in (False, 0):
        return "out of stock", S.red
    return "stock unknown", S.dim


def change_text(price: float | None, prev: float | None, currency: str | None) -> str:
    if price is None or prev is None or abs(price - prev) < 0.005:
        return ""
    diff = price - prev
    sign = "+" if diff > 0 else "-"
    return f"{sign}{fmt_price(abs(diff), currency)} ({sign}{abs(diff) / prev * 100:.1f}%)"


def ago(ts: str | None) -> str:
    if not ts:
        return "never"
    secs = (datetime.now(timezone.utc) - parse_ts(ts)).total_seconds()
    if secs < 60:
        return "just now"
    return f"{format_duration(secs).split()[0]} ago" if secs < 86400 else f"{int(secs // 86400)}d ago"


def local_time(ts: str | None) -> str:
    return parse_ts(ts).astimezone().strftime("%Y-%m-%d %H:%M") if ts else "-"


# --------------------------------------------------------------------------- shared plumbing


def config_path(args: argparse.Namespace) -> Path:
    return Path(args.config or os.environ.get("STOCK_TRACKER_CONFIG") or TOOL_DIR / "config.yaml")


def load_config(args: argparse.Namespace) -> Config:
    cfg = Config.load(config_path(args))
    for w in cfg.warnings:
        log.warning("%s", w)
    return cfg


def open_store(cfg: Config) -> Store:
    return Store(cfg.database_path())


def price_arg(value: str) -> float | None:
    if value.strip().lower() in ("none", "off", "clear", "-"):
        return None
    price = parse_price(value)
    if price is None:
        raise argparse.ArgumentTypeError(f"not a price: {value!r}")
    return price


def duration_arg(value: str) -> str | float:
    try:
        parse_duration(value)
    except ConfigError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    if value.replace(".", "", 1).isdigit():  # plain seconds
        return int(float(value)) if float(value).is_integer() else float(value)
    return value


_scalar = coerce_value


def outcome_details(out: Outcome) -> str:
    if out.error:
        return out.error
    r = out.result
    bits = []
    if r and r.title:
        bits.append(r.title)
    if out.alert:
        bits.insert(0, "ALERT: " + ", ".join(out.alert.kinds))
    return " | ".join(bits)


# --------------------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> int:
    path = config_path(args)
    if path.exists() and not args.force:
        raise ConfigError(f"{path} already exists (use --force to overwrite it; a .bak copy is kept)")
    cfg = Config.from_file_as(EXAMPLE_CONFIG, path)
    cfg.save()
    print(f"Created {path}")
    print("Next steps:")
    print("  python tracker.py list                      # see what's tracked")
    print('  python tracker.py add-listing "NVIDIA RTX 5090" <product-page-url>')
    print("  python tracker.py check                     # check everything once")
    print("  python tracker.py run                       # keep watching and alert on changes")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    products = cfg.products(include_disabled=True)
    if not products:
        print('No products yet. Add one:  python tracker.py add-product "NVIDIA RTX 5090" <url>')
        return 0
    store = open_store(cfg)
    default = max(cfg.settings["default_interval"], cfg.settings["min_interval"])
    for p in products:
        bits = [f"checked every {format_duration(p.interval)}"]
        if p.target_price is not None:
            bits.append(f"target {fmt_price(p.target_price, cfg.settings['currency'])}")
        if p.max_price is not None:
            bits.append(f"max {fmt_price(p.max_price, cfg.settings['currency'])}")
        state = "" if p.enabled else S.yellow("  [disabled]")
        print(f"{S.bold(p.name)}{state}  {S.dim(', '.join(bits))}")
        if not p.listings:
            print(S.dim(f'  (no listings; add one with: python tracker.py add-listing "{p.name}" <url>)'))
        for l in p.listings:
            last = store.last_check(l.key)
            if last is None:
                state_txt = S.dim("never checked")
            else:
                text, style = status_cell(last["status"], last["in_stock"])
                price = fmt_price(last["price"], last["currency"]) if last["price"] is not None else ""
                state_txt = f"{style(text)} {price}  {S.dim(ago(last['ts']))}"
                if last["error"]:
                    state_txt += "  " + S.yellow(_trunc(last["error"], 90))
            extra = []
            if l.interval != p.interval:
                extra.append(f"every {format_duration(l.interval)}")
            if not l.enabled:
                extra.append("disabled")
            extra_txt = S.dim(f"  ({', '.join(extra)})") if extra else ""
            print(f"  #{l.index}  {l.retailer_name} [{l.retailer_key}]{extra_txt}  {S.dim(_trunc(l.describe(), 90))}")
            print(f"       {state_txt}")
    store.close()
    print(S.dim(f"\nDefault refresh rate: every {format_duration(default)}.  Config: {cfg.path}"))
    return 0


def cmd_retailers(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    rows = []
    for key, r in cfg.retailers_raw().items():
        rows.append([key, r.get("name", key), r.get("type", "html"), ", ".join(r.get("domains") or []) or "-",
                     cfg.retailer_source(key)])
    print_table(["KEY", "NAME", "TYPE", "DOMAINS", "SOURCE"], rows)
    print(S.dim("\nListings pick a retailer by `retailer:` or by matching the URL's domain; otherwise 'generic'."))
    return 0


def _try_listing(listing: Listing, cfg: Config) -> None:
    """Fetch once and show what was found, so you know a new listing works."""
    print(S.dim("  Testing it now..."))
    try:
        res = extract(listing, Fetcher(cfg.settings))
    except BlockedError as e:
        print(S.yellow(f"  Blocked: {e}"))
        print("  The listing is saved, but this site rejects automated checks. Use the retailer's API if it")
        print("  has one, or track the product at a different retailer.")
        return
    except (FetchError, ExtractError) as e:
        print(S.yellow(f"  Problem: {e}"))
        return
    text, style = status_cell("ok", res.in_stock)
    currency = res.currency or cfg.settings["currency"]
    print(f"  Found: {style(text)}  {fmt_price(res.price, currency)}  {S.dim(res.title or '')}")


def cmd_add_product(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    cfg.add_product(args.name, args.target_price, args.max_price, args.interval)
    added = [cfg.add_listing(args.name, url=u, retailer=args.retailer) for u in args.urls]
    cfg.save()
    print(f"Added product {S.bold(args.name)}" + (f" with {len(added)} listing(s)" if added else ""))
    if not args.no_test:
        for listing in cfg.listings(args.name):
            print(f"  #{listing.index} {listing.retailer_name}: {_trunc(listing.describe(), 80)}")
            _try_listing(listing, cfg)
    if not added:
        print(f'Now add where to buy it:  python tracker.py add-listing "{args.name}" <product-page-url>')
    return 0


def cmd_add_listing(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    _, key = cfg.add_listing(args.product, url=args.url, retailer=args.retailer, sku=args.sku,
                             variant=args.variant, interval=args.interval, label=args.label, store=args.store)
    cfg.save()
    raw = cfg.find_product_raw(args.product)
    product = next(p for p in cfg.products(include_disabled=True) if p.name == raw["name"])
    listing = product.listings[-1]
    print(f"Added listing #{listing.index} to {S.bold(product.name)} via {listing.retailer_name} [{key}]")
    if not args.no_test:
        _try_listing(listing, cfg)
    return 0


def cmd_edit_product(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    changes: dict[str, Any] = {}
    if args.rename:
        changes["name"] = args.rename
    for opt in ("target_price", "max_price", "interval"):
        if getattr(args, opt) is not _UNSET:
            changes[opt] = getattr(args, opt)
    if args.enable:
        changes["enabled"] = None
    if args.disable:
        changes["enabled"] = False
    if not changes:
        raise ConfigError("nothing to change; see: python tracker.py edit-product --help")
    p = cfg.edit_product(args.name, changes)
    cfg.save()
    print(f"Updated {S.bold(p['name'])}")
    return 0


def cmd_remove_product(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    name = cfg.remove_product(args.name)
    cfg.save()
    print(f"Removed {name} (its price history stays in the database)")
    return 0


def cmd_remove_listing(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    removed = cfg.remove_listing(args.product, args.number)
    cfg.save()
    print(f"Removed listing #{args.number}: {removed.get('url') or removed.get('sku') or removed}")
    return 0


def _retailer_fields(args: argparse.Namespace) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    simple = {
        "name": "name", "type": "type", "currency": "currency", "user_agent": "user_agent",
        "price_selector": "price_selector", "price_regex": "price_regex", "stock_selector": "stock_selector",
        "api_url": "url", "price_path": "price_path", "in_stock_path": "in_stock_path",
        "title_path": "title_path", "link_path": "link_path", "sku_pattern": "sku_pattern",
    }
    for opt, key in simple.items():
        value = getattr(args, opt, None)
        if value is not None:
            fields[key] = value
    for opt, key in (("domain", "domains"), ("in_stock_text", "in_stock_text"), ("out_of_stock_text", "out_of_stock_text")):
        if getattr(args, opt, None):
            fields[key] = list(getattr(args, opt))
    if getattr(args, "min_delay", None) is not None:
        fields["min_delay"] = args.min_delay
    if getattr(args, "no_structured_data", False):
        fields["structured_data"] = False
    for h in getattr(args, "header", None) or []:
        if ":" not in h:
            raise ConfigError(f"--header must look like 'Name: value', got {h!r}")
        name, value = h.split(":", 1)
        fields.setdefault("headers", {})[name.strip()] = value.strip()
    for kv in getattr(args, "set", None) or []:
        if "=" not in kv:
            raise ConfigError(f"--set must look like key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        fields[k.strip()] = None if v == "" else _scalar(v)
    return fields


def cmd_add_retailer(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    fields = _retailer_fields(args)
    if not fields and args.key in cfg.raw["retailers"]:
        raise ConfigError("nothing to change; pass options like --domain, --price-selector, --set key=value")
    created = cfg.add_retailer(args.key, fields)
    cfg.save()
    r = cfg.retailers_raw()[args.key]
    print(f"{'Added' if created else 'Updated'} retailer {S.bold(args.key)} ({r.get('name')}, type {r.get('type')})")
    if r.get("type") == "html":
        print(S.dim("Tip: try it on a product page first:  python tracker.py test-url <url> --retailer " + args.key))
    return 0


def cmd_remove_retailer(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    cfg.remove_retailer(args.key)
    cfg.save()
    print(f"Removed retailer {args.key}")
    return 0


def cmd_interval(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    secs = parse_duration(args.every)
    floor = cfg.settings["min_interval"]
    if args.product is None:
        cfg.set_setting("default_interval", args.every)
        target = "Default refresh rate"
    else:
        p = cfg.find_product_raw(args.product)
        if args.listing is None:
            cfg.edit_product(p["name"], {"interval": args.every})
            target = f"{p['name']}"
        else:
            if not 1 <= args.listing <= len(p["listings"]):
                raise ConfigError(f"{p['name']!r} has {len(p['listings'])} listing(s)")
            p["listings"][args.listing - 1]["interval"] = args.every
            target = f"{p['name']} listing #{args.listing}"
    cfg.save()
    print(f"{target}: every {format_duration(max(secs, floor))}")
    if secs < floor:
        print(S.yellow(f"(raised to the minimum of {format_duration(floor)}; see `settings min_interval`)"))
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    if args.key is None:
        current = cfg.raw["settings"]
        rows = []
        for k, default in DEFAULT_SETTINGS.items():
            value = current.get(k, default)
            rows.append([k, _trunc(value, 60), "" if k in current else S.dim("(default)")])
        print_table(["SETTING", "VALUE", ""], rows)
        return 0
    if args.value is None:
        print(cfg.raw["settings"].get(args.key, DEFAULT_SETTINGS.get(args.key, "(unknown setting)")))
        return 0
    # Numbers and booleans become numbers/booleans; durations like "2m" stay text.
    value: Any = args.value if args.key in TEXT_SETTINGS else _scalar(args.value)
    cfg.set_setting(args.key, value)
    cfg.save()
    print(f"{args.key} = {value}")
    return 0


def _make_tracker(cfg: Config) -> Tracker:
    return Tracker(cfg, open_store(cfg))


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    listings = cfg.listings(args.product)
    if not listings:
        print("Nothing to check. Add products with add-product / add-listing (or enable disabled ones).")
        return 0
    tracker = _make_tracker(cfg)
    print(S.dim(f"Checking {len(listings)} listing(s)..."))
    outs = tracker.check_all(args.product, send_alerts=not args.no_alerts)
    rows = []
    for o in outs:
        r = o.result
        cur = r.currency if r else None
        rows.append([
            _trunc(o.listing.product.name, 28),
            _trunc(o.listing.retailer_name, 18),
            status_cell(o.status, r.in_stock if r else None),
            fmt_price(r.price, cur) if r else "-",
            change_text(r.price if r else None, o.prev_price, cur),
            _trunc(outcome_details(o), 70),
        ])
    print_table(["PRODUCT", "RETAILER", "STATUS", "PRICE", "CHANGE", "DETAILS"], rows)
    tracker.store.close()
    return 0


def _run_line(o: Outcome) -> str:
    r = o.result
    text, style = status_cell(o.status, r.in_stock if r else None)
    cur = r.currency if r else None
    price = fmt_price(r.price, cur) if r and r.price is not None else ""
    change = change_text(r.price if r else None, o.prev_price, cur)
    nxt = S.dim(f"next in {format_duration(o.next_in)}") if o.next_in else ""
    err = S.yellow(_trunc(o.error, 90)) if o.error else ""
    stamp = S.dim(datetime.now().strftime("%H:%M:%S"))
    return "  ".join(x for x in [stamp, _trunc(o.listing.label, 45).ljust(45), style(text.ljust(13)),
                                  price.ljust(11), change, nxt, err] if x)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    tracker = _make_tracker(cfg)
    n = len(cfg.listings(args.product))
    channels = ", ".join(x.name for x in tracker.notifiers) or "none"
    print(f"Watching {n} listing(s). Alerts go to: {channels}. Press Ctrl+C to stop.")
    print(S.dim(f"Edits to {cfg.path} (or CLI changes from another terminal) are picked up automatically."))

    def on_outcome(o: Outcome) -> None:
        if args.quiet and o.status == "ok" and not o.alert:
            return
        print(_run_line(o), flush=True)

    try:
        tracker.run(args.product, on_outcome=on_outcome)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        tracker.store.close()
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import serve

    path = config_path(args)
    if not path.exists():
        Config.from_file_as(EXAMPLE_CONFIG, path).save()
        print(f"Created {path} from the example config")
    for w in Config.load(path).warnings:
        log.warning("%s", w)

    def on_outcome(o: Outcome) -> None:
        if not args.quiet or o.alert or o.status != "ok":
            print(_run_line(o), flush=True)

    serve(path, port=args.port, open_browser=not args.no_browser, watching=not args.paused, on_outcome=on_outcome)
    return 0


def cmd_test_url(args: argparse.Namespace) -> int:
    path = config_path(args)
    cfg = Config.load(path) if path.exists() else Config(path, {})
    overrides = _retailer_fields(args)
    listing = cfg.adhoc_listing(args.url, args.retailer, args.sku, args.variant, args.store, overrides)
    key, raw, retailer, store_id = listing.retailer_key, listing.retailer_raw, listing.retailer, listing.store_id
    print(f"Retailer:  {listing.retailer_name} [{key}], type {raw.get('type', 'html')}")
    if uses_store(retailer):
        print(f"Store:     {store_label(retailer, store_id) or S.yellow('none set (pass --store, see `stores`)')}")
    try:
        res = extract(listing, Fetcher(cfg.settings))
    except BlockedError as e:
        print(S.yellow(f"BLOCKED:   {e}"))
        print("This site rejects automated requests, and this tool doesn't try to get around that.")
        print("Options: use the retailer's official API (see README), or track the product elsewhere.")
        return 1
    except (FetchError, ExtractError) as e:
        print(S.yellow(f"Problem:   {e}"))
        return 1
    text, style = status_cell("ok", res.in_stock)
    currency = res.currency or raw.get("currency") or cfg.settings["currency"]
    print(f"Title:     {res.title or '-'}")
    print(f"Price:     {fmt_price(res.price, currency)}   {S.dim('from ' + res.price_source if res.price_source else 'not found')}")
    print(f"Stock:     {style(text)}   {S.dim('from ' + res.stock_source if res.stock_source else 'not found')}")
    if res.link and res.link != args.url:
        print(f"Link:      {res.link}")
    if res.price is None or res.in_stock is None:
        print(S.dim("\nMissing something? Try --price-selector / --stock-selector / --in-stock-text / --out-of-stock-text"))
        print(S.dim("here, then save what works with:  python tracker.py add-retailer <key> --domain <site> [same options]"))
    elif overrides:
        print(S.dim("\nWorks. Save it with:  python tracker.py add-retailer <key> --domain <site> [the same options]"))
    return 0


def cmd_stores(args: argparse.Namespace) -> int:
    path = config_path(args)
    cfg = Config.load(path) if path.exists() else Config(path, {})
    retailers = {k: r for k, r in cfg.retailers_raw().items() if uses_store(r)}
    if args.retailer:
        if args.retailer not in retailers:
            raise ConfigError(f"{args.retailer!r} doesn't use stores. Retailers with stores: {', '.join(retailers) or 'none'}")
        retailers = {args.retailer: retailers[args.retailer]}
    for key, r in retailers.items():
        default = resolve_store(expand_env(r), expand_env(r.get("store")))
        print(f"{S.bold(r.get('name', key))} [{key}]  default store: {store_label(r, default) or S.yellow('not set')}")
        rows = [[name, sid, S.green("<- default") if sid == default else ""] for name, sid in (r.get("stores") or {}).items()]
        if rows:
            print_table(["STORE", "ID", ""], rows, indent="  ")
        print(S.dim(f"\n  Set the default:      python tracker.py add-retailer {key} --set store=<name or ID>"))
        print(S.dim("  Or per listing:       python tracker.py add-listing <product> <url> --store <name or ID>"))
        print(S.dim("  A store missing here? Use its 3-digit ID from the store page on the retailer's site.\n"))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    products = cfg.products(include_disabled=True)
    if args.product:
        name = cfg.find_product_raw(args.product)["name"]
        products = [p for p in products if p.name == name]
    store = open_store(cfg)
    printed = False
    for p in products:
        for l in p.listings:
            printed = True
            print(f"\n{S.bold(l.label)}  {S.dim(_trunc(l.describe(), 80))}")
            st = store.stats(l.key)
            if not st["checks"]:
                print(S.dim("  no successful checks yet"))
                continue
            last = store.last_ok(l.key)
            cur = last["currency"]
            text, style = status_cell("ok", last["in_stock"])
            print(f"  now  {style(text)} {fmt_price(last['price'], cur)}   "
                  f"low {fmt_price(st['low'], cur)} ({local_time(st['low_ts'])})   high {fmt_price(st['high'], cur)}")
            print(S.dim(f"  last in stock: {local_time(st['last_in_stock']) if st['last_in_stock'] else 'never seen'}   "
                        f"{st['checks']} check{'s' if st['checks'] != 1 else ''} since {local_time(st['first_ts'])}"
                        + (f", {st['failed']} failed" if st["failed"] else "")))
            rows = []
            for c in store.changes(l.key, args.limit):
                rows.append([local_time(c["ts"]), fmt_price(c["price"], c["currency"]),
                             change_text(c["price"], c["prev_price"], c["currency"]),
                             status_cell("ok", c["in_stock"])])
            if rows:
                print()
                print_table(["WHEN", "PRICE", "CHANGE", "STOCK"], rows, indent="  ")
    if not printed:
        print("No listings yet.")
    store.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    keys = None
    if args.product:
        keys = [l.key for p in cfg.products(include_disabled=True)
                if p.name == cfg.find_product_raw(args.product)["name"] for l in p.listings]
    store = open_store(cfg)
    n = store.export_csv(args.file, keys)
    store.close()
    print(f"Wrote {n} check(s) to {args.file}")
    return 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    notifiers = build_notifiers(cfg.raw.get("notifications"))
    if not notifiers:
        print("No notification channels are enabled (see `notifications:` in the config).")
        return 1
    alert = Alert(kinds=["in_stock"], title="Test alert from your stock tracker",
                  message="If you can read this, notifications work.", url=None)
    results = send_all(notifiers, alert)
    for name, err in results.items():
        print(f"  {name:<9} {S.green('OK') if err is None else S.red('FAILED: ' + err)}")
    if set(results) == {"console"}:
        print(S.dim("Only the console is set up. For phone alerts, set notifications.ntfy.topic (see README)."))
    return 0 if all(e is None for e in results.values()) else 1


# --------------------------------------------------------------------------- argument parser

_UNSET = object()


def _add_html_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("page reading options (type html)")
    g.add_argument("--price-selector", help="CSS selector of the price element, e.g. '.price-current'")
    g.add_argument("--price-regex", help="regex with one group capturing the price, run on the page HTML")
    g.add_argument("--stock-selector", help="CSS selector whose text is checked for the stock keywords")
    g.add_argument("--in-stock-text", action="append", metavar="TEXT", help="text meaning in stock (repeatable)")
    g.add_argument("--out-of-stock-text", action="append", metavar="TEXT", help="text meaning sold out (repeatable)")
    g.add_argument("--no-structured-data", action="store_true", help="ignore the page's schema.org data")
    g.add_argument("--user-agent", help="User-Agent header for this retailer")
    g.add_argument("--header", action="append", metavar="'NAME: VALUE'", help="extra HTTP header (repeatable)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracker.py",
        description="Track stock and prices for any product at any retailer.",
        epilog="Run `python tracker.py <command> --help` for a command's options.",
    )
    parser.add_argument("--config", help="config file (default: config.yaml next to tracker.py, or $STOCK_TRACKER_CONFIG)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, func: Callable, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_, description=help_)
        p.set_defaults(func=func)
        return p

    p = add("init", cmd_init, "create config.yaml from the example (RTX 5090 starter)")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")

    add("list", cmd_list, "show products, listings and their last status")
    add("retailers", cmd_retailers, "show available retailers (built-in and yours)")

    p = add("check", cmd_check, "check everything once and print a table")
    p.add_argument("--product", help="only this product")
    p.add_argument("--no-alerts", action="store_true", help="record results but don't send alerts")

    p = add("run", cmd_run, "keep checking on each listing's refresh rate; alert on changes")
    p.add_argument("--product", help="only this product")
    p.add_argument("-q", "--quiet", action="store_true", help="only print alerts, errors and blocks")

    p = add("gui", cmd_gui, "open the web dashboard (also runs the tracker while it's open)")
    p.add_argument("--port", type=int, default=8765, help="local port (default 8765; the next free one is used)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    p.add_argument("--paused", action="store_true", help="start with automatic checking off")
    p.add_argument("-q", "--quiet", action="store_true", help="only print alerts and problems in the terminal")

    p = add("add-product", cmd_add_product, "start tracking a new product")
    p.add_argument("name")
    p.add_argument("urls", nargs="*", metavar="URL", help="product page URL(s) at one or more retailers")
    p.add_argument("--target-price", type=price_arg, help="alert when the price is at or below this")
    p.add_argument("--max-price", type=price_arg, help="ignore in-stock offers above this (e.g. scalper prices)")
    p.add_argument("--interval", type=duration_arg, help="refresh rate for this product, e.g. 90s, 2m, 1h")
    p.add_argument("--retailer", help="retailer key for the URLs (default: detect from the domain)")
    p.add_argument("--no-test", action="store_true", help="don't fetch the URLs once to verify them")

    p = add("add-listing", cmd_add_listing, "add a retailer listing (where to buy) to a product")
    p.add_argument("product")
    p.add_argument("url", nargs="?", help="product page URL (optional for API retailers that use --sku)")
    p.add_argument("--retailer", help="retailer key (default: detect from the URL's domain, else 'generic')")
    p.add_argument("--sku", help="retailer SKU / item id (API retailers such as bestbuy)")
    p.add_argument("--variant", help="Shopify variant id or name, e.g. 'OC Edition'")
    p.add_argument("--interval", type=duration_arg, help="refresh rate just for this listing")
    p.add_argument("--label", help="a note shown in `list`, e.g. 'ASUS Astral OC'")
    p.add_argument("--store", help="store name or ID for retailers with per-store stock (Micro Center)")
    p.add_argument("--no-test", action="store_true", help="don't fetch the listing once to verify it")

    p = add("edit-product", cmd_edit_product, "change a product's prices, refresh rate, name, or pause it")
    p.add_argument("name")
    p.add_argument("--rename", metavar="NEW_NAME")
    p.add_argument("--target-price", type=price_arg, default=_UNSET, help="new target price, or 'none'")
    p.add_argument("--max-price", type=price_arg, default=_UNSET, help="new max price, or 'none'")
    p.add_argument("--interval", type=lambda v: None if v.lower() == "none" else duration_arg(v), default=_UNSET,
                   help="new refresh rate, or 'none' for the default")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--disable", action="store_true", help="pause tracking this product")
    g.add_argument("--enable", action="store_true", help="resume tracking")

    p = add("remove-product", cmd_remove_product, "stop tracking a product")
    p.add_argument("name")

    p = add("remove-listing", cmd_remove_listing, "remove one listing (numbers are shown by `list`)")
    p.add_argument("product")
    p.add_argument("number", type=int)

    p = add("interval", cmd_interval, "set the refresh rate: default, per product, or per listing")
    p.add_argument("every", type=duration_arg, help="e.g. 90s, 2m, 15m, 1h")
    p.add_argument("--product", help="set it for this product only")
    p.add_argument("--listing", type=int, metavar="N", help="with --product: only listing #N")

    p = add("add-retailer", cmd_add_retailer, "add (or update) a retailer and how to read its pages")
    p.add_argument("key", help="short id, e.g. newegg, microcenter, mystore")
    p.add_argument("--name", help="display name, e.g. 'Micro Center'")
    p.add_argument("--type", choices=RETAILER_TYPES, help="html (default), shopify or json_api")
    p.add_argument("--domain", action="append", metavar="DOMAIN", help="URLs on this domain use this retailer (repeatable)")
    p.add_argument("--currency", help="currency code when the site doesn't say, e.g. USD, CAD, EUR")
    p.add_argument("--min-delay", type=float, metavar="SECONDS", help="minimum gap between requests to this site")
    _add_html_options(p)
    g = p.add_argument_group("API options (type json_api)")
    g.add_argument("--api-url", help="URL template, e.g. 'https://api.example.com/items/{sku}?key={api_key}'")
    g.add_argument("--price-path", help="dotted path to the price in the JSON, e.g. 'data.price'")
    g.add_argument("--in-stock-path", help="dotted path to availability, e.g. 'data.inStock'")
    g.add_argument("--title-path", help="dotted path to the product name")
    g.add_argument("--link-path", help="dotted path to the product page URL")
    g.add_argument("--sku-pattern", help="regex that pulls the SKU out of a product URL")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="set any other field, e.g. api_key=... (repeatable)")

    p = add("remove-retailer", cmd_remove_retailer, "remove a custom retailer")
    p.add_argument("key")

    p = add("settings", cmd_settings, "show or change global settings")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")

    p = add("test-url", cmd_test_url, "fetch one product page and show what the tracker can read from it")
    p.add_argument("url")
    p.add_argument("--retailer", help="retailer key to use (default: detect from the domain)")
    p.add_argument("--sku")
    p.add_argument("--variant")
    p.add_argument("--store", help="store name or ID (Micro Center)")
    _add_html_options(p)

    p = add("stores", cmd_stores, "list store names/IDs for retailers with per-store stock (Micro Center)")
    p.add_argument("retailer", nargs="?")

    p = add("history", cmd_history, "price history: lows, highs, last time in stock, changes")
    p.add_argument("product", nargs="?")
    p.add_argument("--limit", type=int, default=15, help="how many changes to show per listing")

    p = add("export", cmd_export, "export every check to a CSV file")
    p.add_argument("file")
    p.add_argument("--product")

    add("test-notify", cmd_test_notify, "send a test alert to every enabled notification channel")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # never crash on characters the console can't show
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
