"""Configuration: loading, validation, environment-variable expansion and saving."""

from __future__ import annotations

import copy
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

# Hard floor for any check interval, so a typo can't hammer a retailer (and get you banned).
ABSOLUTE_MIN_INTERVAL = 30

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_interval": 300,
    "min_interval": 60,
    "per_host_delay": 5,
    "timeout": 20,
    "jitter": 0.1,
    "max_backoff": 3600,
    "price_drop_min_percent": 1.0,
    "currency": "USD",
    "database": "tracker.db",
    "user_agent": "Mozilla/5.0 (compatible; StockTracker/1.0; personal price tracker)",
}
DURATION_SETTINGS = ("default_interval", "min_interval", "per_host_delay", "timeout", "max_backoff")

RETAILER_TYPES = ("html", "shopify", "json_api")

# Micro Center store IDs (the `storeid=` URL parameter). Stock at Micro Center is per store.
# Source: the open-source streetmerchant project; stores opened since may be missing, in which
# case use the 3-digit ID from the store's page on microcenter.com.
MICROCENTER_STORES: dict[str, str] = {
    "web": "029",  # shippable items only
    "brentwood": "095", "brooklyn": "115", "cambridge": "121", "charlotte": "175",
    "chicago": "151", "columbus": "141", "dallas": "131", "denver": "181", "duluth": "065",
    "fairfax": "081", "flushing": "145", "houston": "155", "madison-heights": "055",
    "marietta": "041", "mayfield-heights": "051", "miami": "185", "north-jersey": "075",
    "overland-park": "191", "parkville": "125", "rockville": "085", "sharonville": "071",
    "st-davids": "061", "st-louis-park": "045", "tustin": "101", "westbury": "171",
    "westmont": "025", "yonkers": "105",
}

# Retailers that work without being defined in the config file. A `retailers:` entry with the
# same key is merged on top, e.g. `bestbuy: {api_key: "..."}` only sets the key.
BUILTIN_RETAILERS: dict[str, dict[str, Any]] = {
    "generic": {
        "name": "Web page",
        "type": "html",
    },
    "shopify": {
        "name": "Shopify store",
        "type": "shopify",
    },
    "bestbuy": {
        "name": "Best Buy",
        "type": "json_api",
        "domains": ["bestbuy.com"],
        "api_key": "${BESTBUY_API_KEY}",
        "url": (
            "https://api.bestbuy.com/v1/products/{sku}.json"
            "?apiKey={api_key}&show=sku,name,salePrice,onlineAvailability,url"
        ),
        "sku_pattern": r"skuId=(\d+)|/(\d{6,8})\.p",
        "price_path": "salePrice",
        "in_stock_path": "onlineAvailability",
        "title_path": "name",
        "link_path": "url",
        "currency": "USD",
        "min_delay": 1,
    },
    "microcenter": {
        "name": "Micro Center",
        "type": "html",
        "domains": ["microcenter.com"],
        "price_selector": "span#pricing",
        "stock_selector": "#pnlInventory",
        "in_stock_text": ["in stock"],
        "out_of_stock_text": ["sold out", "out of stock"],
        "url_params": {"storeid": "{store}"},
        "stores": MICROCENTER_STORES,
        "store": "",  # default store for every Micro Center listing; a listing's `store:` wins
        "currency": "USD",
        "min_delay": 10,
    },
}

HEADER = (
    "# Stock tracker config. Every option is documented in config.example.yaml.\n"
    "# CLI commands rewrite this file, so comments added here are not kept\n"
    "# (the previous version is saved next to it as a .bak file).\n\n"
)

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DURATION_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)?\s*$", re.I
)


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------- helpers


def parse_duration(value: Any, allow_zero: bool = False) -> float:
    """Seconds from 300, "300", "30s", "2m", "1.5h"."""
    if isinstance(value, bool) or value is None:
        raise ConfigError(f"invalid duration {value!r} (use e.g. 90, 30s, 2m, 1h)")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        m = _DURATION_RE.match(str(value))
        if not m:
            raise ConfigError(f"invalid duration {value!r} (use e.g. 90, 30s, 2m, 1h)")
        seconds = float(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[(m.group(2) or "s")[0].lower()]
    if seconds < 0 or (seconds == 0 and not allow_zero):
        raise ConfigError(f"duration must be positive, got {value!r}")
    return seconds


def format_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, s = divmod(s, 60)
        return f"{m}m" + (f" {s}s" if s else "")
    h, rem = divmod(s, 3600)
    return f"{h}h" + (f" {rem // 60}m" if rem >= 60 else "")


def expand_env(value: Any) -> Any:
    """Replace ${VAR} with the environment variable's value (empty string when unset)."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def env_vars_in(value: Any) -> list[str]:
    return _ENV_RE.findall(value) if isinstance(value, str) else []


def _norm_store(value: Any) -> str:
    return re.sub(r"[\s_.]+", "-", str(value).strip().lower()).strip("-")


def resolve_store(retailer: dict[str, Any], value: Any) -> str | None:
    """A store name ("tustin", "St. Davids") or ID ("101") -> the retailer's store ID."""
    if value is None or value == "":
        return None
    name = retailer.get("name", "this retailer")
    stores = {_norm_store(k): str(v) for k, v in (retailer.get("stores") or {}).items()}
    if isinstance(value, bool):
        raise ConfigError(f"store must be a store name or ID, got {value!r}")
    if isinstance(value, int):
        padded = f"{value:03d}"
        if not stores or padded in stores.values():
            return padded if stores else str(value)
        raise ConfigError(
            f"{value} isn't a known {name} store ID. Put IDs in quotes (store: \"025\"), because YAML reads "
            f"025 as an octal number, or use the store name"
        )
    text = str(value).strip()
    if _norm_store(text) in stores:
        return stores[_norm_store(text)]
    if text.isdigit():
        return text
    known = ", ".join(sorted(stores)) or "(none listed)"
    raise ConfigError(f"unknown {name} store {value!r}. Use a store ID or one of: {known}")


def store_label(retailer: dict[str, Any], store_id: str | None) -> str | None:
    if not store_id:
        return None
    names = [k for k, v in (retailer.get("stores") or {}).items() if str(v) == store_id]
    return f"{names[0]} ({store_id})" if names else store_id


def uses_store(retailer: dict[str, Any]) -> bool:
    return bool(retailer.get("stores")) or any("{store}" in str(v) for v in (retailer.get("url_params") or {}).values())


def _opt_price(value: Any, what: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ConfigError(f"{what} must be a number, got {value!r}")
    try:
        return float(str(value).replace(",", "").lstrip("$"))
    except ValueError:
        raise ConfigError(f"{what} must be a number, got {value!r}") from None


# --------------------------------------------------------------------------- runtime model


@dataclass
class Product:
    name: str
    target_price: float | None = None
    max_price: float | None = None
    interval: float = 300
    enabled: bool = True
    listings: list[Listing] = field(default_factory=list)


@dataclass
class Listing:
    product: Product = field(repr=False)
    index: int  # 1-based position inside the product, as shown by `list`
    retailer_key: str
    retailer: dict[str, Any]  # merged retailer config with ${VAR}s expanded
    retailer_raw: dict[str, Any]  # same, unexpanded (used for helpful error messages)
    fields: dict[str, Any]  # the listing's own fields (url, sku, variant, ...), expanded
    interval: float
    enabled: bool = True
    store_id: str | None = None  # resolved store (listing's `store:` or the retailer default)

    @property
    def url(self) -> str | None:
        return self.fields.get("url")

    @property
    def retailer_name(self) -> str:
        name = str(self.retailer.get("name") or self.retailer_key)
        builtin = BUILTIN_RETAILERS.get(self.retailer_key, {})
        if self.retailer_key in ("generic", "shopify") and self.url and name == builtin.get("name"):
            host = (urlparse(self.url).hostname or "").lower()
            return host[4:] if host.startswith("www.") else (host or name)  # e.g. "allbirds.com"
        return name

    @property
    def key(self) -> str:
        """Stable id for price history. Independent of the product name, so renames keep history."""
        f = self.fields
        key = "|".join(
            [self.retailer_key, str(f.get("url") or ""), str(f.get("sku") or ""), str(f.get("variant") or "")]
        )
        return f"{key}|store={self.store_id}" if self.store_id else key

    @property
    def label(self) -> str:
        return f"{self.product.name} @ {self.retailer_name}"

    def describe(self) -> str:
        f = self.fields
        parts = []
        if f.get("label"):
            parts.append(str(f["label"]))
        if f.get("url"):
            parts.append(str(f["url"]))
        if f.get("sku"):
            parts.append(f"sku {f['sku']}")
        if f.get("variant"):
            parts.append(f"variant {f['variant']}")
        if self.store_id:
            default = "" if f.get("store") else ", default"
            parts.insert(0, f"[store {store_label(self.retailer, self.store_id)}{default}]")
        return "  ".join(parts)


# --------------------------------------------------------------------------- config file


class Config:
    def __init__(self, path: str | Path, raw: dict[str, Any]):
        self.path = Path(path)
        self.raw = raw
        self.warnings: list[str] = []
        self._normalize()
        self.validate()

    # ---- load / save

    @classmethod
    def load(cls, path: str | Path) -> Config:
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"{path} not found. Create one with:  python tracker.py init")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{path}: invalid YAML:\n{e}") from None
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: the top level must be a mapping (settings:, products:, ...)")
        return cls(path, raw)

    @classmethod
    def from_file_as(cls, source: str | Path, path: str | Path) -> Config:
        """Load `source` (e.g. the example config) but save to `path`."""
        cfg = cls.load(source)
        cfg.path = Path(path)
        return cfg

    def save(self) -> None:
        self.validate()
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
        text = HEADER + yaml.safe_dump(
            self.raw, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
        )
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)

    def _normalize(self) -> None:
        for key in ("settings", "notifications", "retailers"):
            if self.raw.get(key) is None:
                self.raw[key] = {}
        if self.raw.get("products") is None:
            self.raw["products"] = []
        for p in self.raw["products"]:
            if isinstance(p, dict) and p.get("listings") is None:
                p["listings"] = []

    # ---- settings

    @property
    def settings(self) -> dict[str, Any]:
        s = dict(DEFAULT_SETTINGS)
        s.update(self.raw.get("settings") or {})
        for k in DURATION_SETTINGS:
            try:
                s[k] = parse_duration(s[k], allow_zero=(k == "per_host_delay"))
            except ConfigError as e:
                raise ConfigError(f"settings.{k}: {e}") from None
        s["min_interval"] = max(s["min_interval"], ABSOLUTE_MIN_INTERVAL)
        s["jitter"] = min(max(float(s["jitter"]), 0.0), 0.5)
        s["price_drop_min_percent"] = float(s["price_drop_min_percent"])
        return expand_env(s)

    def database_path(self) -> Path:
        p = Path(str(self.settings["database"])).expanduser()
        return p if p.is_absolute() else self.path.parent / p

    def set_setting(self, key: str, value: Any) -> None:
        if key not in DEFAULT_SETTINGS:
            raise ConfigError(f"unknown setting {key!r}. Settings: {', '.join(DEFAULT_SETTINGS)}")
        if key in DURATION_SETTINGS:
            parse_duration(value, allow_zero=(key == "per_host_delay"))
        old = self.raw["settings"].get(key)
        self.raw["settings"][key] = value
        try:
            self.validate()
        except ConfigError:
            if old is None:
                del self.raw["settings"][key]
            else:
                self.raw["settings"][key] = old
            raise

    # ---- retailers

    def retailers_raw(self) -> dict[str, dict[str, Any]]:
        """Built-in + custom retailers. Custom keys come first so they win domain matching."""
        custom = self.raw.get("retailers") or {}
        merged: dict[str, dict[str, Any]] = {}
        for key, r in custom.items():
            merged[key] = {**copy.deepcopy(BUILTIN_RETAILERS.get(key, {})), **(r or {})}
        for key, r in BUILTIN_RETAILERS.items():
            merged.setdefault(key, copy.deepcopy(r))
        for r in merged.values():
            r.setdefault("type", "html")
        return merged

    def retailer_source(self, key: str) -> str:
        custom = key in (self.raw.get("retailers") or {})
        if key in BUILTIN_RETAILERS:
            return "built-in, customized" if custom else "built-in"
        return "custom"

    def retailer_for_url(self, url: str | None) -> str:
        host = (urlparse(url).hostname or "").lower() if url else ""
        if host:
            for key, r in self.retailers_raw().items():
                for d in r.get("domains") or []:
                    d = str(d).lower().lstrip(".")
                    if host == d or host.endswith("." + d):
                        return key
        return "generic"

    def resolve_retailer(self, listing_raw: dict[str, Any]) -> str:
        key = listing_raw.get("retailer")
        if key:
            if key not in self.retailers_raw():
                raise ConfigError(f"unknown retailer {key!r} (known: {', '.join(self.retailers_raw())})")
            return str(key)
        return self.retailer_for_url(listing_raw.get("url"))

    def add_retailer(self, key: str, fields: dict[str, Any]) -> bool:
        """Create or update a custom retailer. Returns True when it was newly created."""
        if not _KEY_RE.match(key):
            raise ConfigError(f"retailer key {key!r} must be lowercase letters, digits, '-' or '_'")
        retailers = self.raw["retailers"]
        created = key not in retailers
        old = copy.deepcopy(retailers.get(key))
        entry = retailers.setdefault(key, {})
        for k, v in fields.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        if key not in BUILTIN_RETAILERS:
            entry.setdefault("name", key)
            entry.setdefault("type", "html")
        try:
            self.validate()
        except ConfigError:
            if old is None:
                del retailers[key]
            else:
                retailers[key] = old
            raise
        return created

    def remove_retailer(self, key: str) -> None:
        if key not in (self.raw.get("retailers") or {}):
            if key in BUILTIN_RETAILERS:
                raise ConfigError(f"{key!r} is built in and can't be removed")
            raise ConfigError(f"no custom retailer named {key!r}")
        # Listings that only matched by domain fall back to "generic"; explicit references would break.
        users = [str(p.get("name")) for p in self.raw["products"] for l in p.get("listings") or []
                 if isinstance(l, dict) and l.get("retailer") == key]
        if users:
            raise ConfigError(f"retailer {key!r} is used by: {', '.join(sorted(set(users)))}. Remove those listings first.")
        del self.raw["retailers"][key]

    # ---- products

    def products(self, include_disabled: bool = False) -> list[Product]:
        settings = self.settings
        retailers = self.retailers_raw()
        out = []
        for p in self.raw["products"]:
            prod_interval = p.get("interval")
            prod = Product(
                name=str(p["name"]),
                target_price=_opt_price(p.get("target_price"), "target_price"),
                max_price=_opt_price(p.get("max_price"), "max_price"),
                interval=max(parse_duration(prod_interval or settings["default_interval"]), settings["min_interval"]),
                enabled=p.get("enabled", True) is not False,
            )
            for i, l in enumerate(p.get("listings") or [], 1):
                rkey = self.resolve_retailer(l)
                interval = parse_duration(l.get("interval") or prod_interval or settings["default_interval"])
                rexp = expand_env(retailers[rkey])
                store_id = resolve_store(rexp, expand_env(l.get("store") or rexp.get("store"))) if uses_store(rexp) else None
                prod.listings.append(
                    Listing(
                        product=prod,
                        index=i,
                        retailer_key=rkey,
                        retailer=rexp,
                        retailer_raw=retailers[rkey],
                        fields=expand_env(dict(l)),
                        interval=max(interval, settings["min_interval"]),
                        enabled=l.get("enabled", True) is not False,
                        store_id=store_id,
                    )
                )
            if include_disabled or prod.enabled:
                out.append(prod)
        return out

    def listings(self, product: str | None = None) -> list[Listing]:
        """Enabled listings of enabled products, optionally only for one product."""
        products = self.products()
        if product:
            name = str(self.find_product_raw(product)["name"])
            products = [p for p in products if p.name == name]
        return [l for p in products for l in p.listings if l.enabled]

    def find_product_raw(self, name: str) -> dict[str, Any]:
        products = self.raw["products"]
        q = name.strip().lower()
        exact = [p for p in products if str(p.get("name", "")).lower() == q]
        if exact:
            return exact[0]
        partial = [p for p in products if q in str(p.get("name", "")).lower()]
        if len(partial) == 1:
            return partial[0]
        names = ", ".join(repr(str(p.get("name"))) for p in (partial or products)) or "(none yet)"
        if not partial:
            raise ConfigError(f"no product matching {name!r}. Products: {names}")
        raise ConfigError(f"{name!r} matches several products ({names}); use the full name")

    def add_product(
        self,
        name: str,
        target_price: float | None = None,
        max_price: float | None = None,
        interval: Any = None,
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ConfigError("product name can't be empty")
        if any(str(p.get("name", "")).lower() == name.lower() for p in self.raw["products"]):
            raise ConfigError(f"product {name!r} already exists")
        p: dict[str, Any] = {"name": name}
        if target_price is not None:
            p["target_price"] = target_price
        if max_price is not None:
            p["max_price"] = max_price
        if interval is not None:
            parse_duration(interval)
            p["interval"] = interval
        p["listings"] = []
        self.raw["products"].append(p)
        try:
            self.validate()
        except ConfigError:
            self.raw["products"].remove(p)
            raise
        return p

    def edit_product(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply changes; a value of None removes that field (e.g. clears the target price)."""
        p = self.find_product_raw(name)
        old = copy.deepcopy(p)
        for k, v in changes.items():
            if v is None:
                p.pop(k, None)
            else:
                p[k] = v
        try:
            self.validate()
        except ConfigError:
            p.clear()
            p.update(old)
            raise
        return p

    def remove_product(self, name: str) -> str:
        p = self.find_product_raw(name)
        self.raw["products"].remove(p)
        return str(p["name"])

    def add_listing(
        self,
        product: str,
        url: str | None = None,
        retailer: str | None = None,
        sku: str | None = None,
        variant: str | None = None,
        interval: Any = None,
        label: str | None = None,
        store: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        p = self.find_product_raw(product)
        retailers = self.retailers_raw()
        key = retailer or self.retailer_for_url(url)
        if key not in retailers:
            raise ConfigError(f"unknown retailer {key!r} (known: {', '.join(retailers)})")
        r = retailers[key]
        if not sku and url and r.get("sku_pattern"):
            m = re.search(str(r["sku_pattern"]), url)
            if m:
                sku = next((g for g in m.groups() if g), None) or m.group(0)
        if r.get("type") in ("html", "shopify") and not url:
            raise ConfigError(f"{r.get('name', key)} listings need a product page URL")
        if r.get("type") == "json_api" and "{sku}" in str(r.get("url", "")) and not sku:
            raise ConfigError(f"{r.get('name', key)} listings need a SKU (pass --sku; it wasn't found in the URL)")

        listing: dict[str, Any] = {}
        if key != "generic" or retailer:
            listing["retailer"] = key
        if url:
            listing["url"] = url
        if sku:
            listing["sku"] = str(sku)
        if variant:
            listing["variant"] = str(variant)
        if store:
            if not uses_store(r):
                raise ConfigError(f"{r.get('name', key)} doesn't use stores")
            resolve_store(r, store)
            listing["store"] = str(store)
        if label:
            listing["label"] = label
        if interval is not None:
            parse_duration(interval)
            listing["interval"] = interval

        def ident(l: dict[str, Any]) -> tuple:
            rk = self.resolve_retailer(l)
            store_id = resolve_store(retailers[rk], l.get("store") or retailers[rk].get("store")) if uses_store(retailers[rk]) else None
            return (rk, l.get("url"), str(l.get("sku") or ""), str(l.get("variant") or ""), store_id)

        if any(ident(l) == ident(listing) for l in p["listings"]):
            raise ConfigError(f"that listing is already tracked for {p['name']!r}")
        p["listings"].append(listing)
        try:
            self.validate()
        except ConfigError:
            p["listings"].remove(listing)
            raise
        return listing, key

    def remove_listing(self, product: str, number: int) -> dict[str, Any]:
        p = self.find_product_raw(product)
        if not 1 <= number <= len(p["listings"]):
            raise ConfigError(f"{p['name']!r} has {len(p['listings'])} listing(s); pick a number from `list`")
        return p["listings"].pop(number - 1)

    # ---- validation

    def validate(self) -> None:
        errors: list[str] = []
        self.warnings = []
        raw = self.raw

        for section in ("settings", "notifications", "retailers"):
            if not isinstance(raw.get(section), dict):
                errors.append(f"`{section}:` must be a mapping")
        if not isinstance(raw.get("products"), list):
            errors.append("`products:` must be a list (each item starts with '- name: ...')")
        if errors:
            raise ConfigError(f"Problems in {self.path}:\n  - " + "\n  - ".join(errors))

        for k in raw["settings"]:
            if k not in DEFAULT_SETTINGS:
                self.warnings.append(f"settings.{k} is not a known setting (ignored)")
        try:
            settings = self.settings
        except (ConfigError, TypeError, ValueError) as e:
            errors.append(str(e))
            settings = None

        retailers = self.retailers_raw()
        for key, r in (raw.get("retailers") or {}).items():
            where = f"retailers.{key}"
            if not _KEY_RE.match(str(key)):
                errors.append(f"{where}: key must be lowercase letters, digits, '-' or '_'")
            if r is not None and not isinstance(r, dict):
                errors.append(f"{where}: must be a mapping")
                continue
            merged = retailers[key]
            if merged["type"] not in RETAILER_TYPES:
                errors.append(f"{where}.type must be one of {', '.join(RETAILER_TYPES)}")
            if merged["type"] == "json_api":
                if not merged.get("url"):
                    errors.append(f"{where}: json_api retailers need `url` (an API URL template)")
                if not (merged.get("price_path") or merged.get("in_stock_path")):
                    errors.append(f"{where}: json_api retailers need `price_path` and/or `in_stock_path`")
            for lst in ("domains", "in_stock_text", "out_of_stock_text"):
                if lst in merged and not isinstance(merged[lst], list):
                    errors.append(f"{where}.{lst} must be a list, e.g. [\"Sold out\"]")
            for mapping in ("url_params", "stores", "headers"):
                if mapping in merged and not isinstance(merged[mapping], dict):
                    errors.append(f"{where}.{mapping} must be a mapping")
            if merged.get("store") and isinstance(merged.get("stores", {}), dict):
                try:
                    resolve_store(expand_env(merged), expand_env(merged["store"]))
                except ConfigError as e:
                    errors.append(f"{where}.store: {e}")
            if merged.get("sku_pattern"):
                try:
                    re.compile(str(merged["sku_pattern"]))
                except re.error as e:
                    errors.append(f"{where}.sku_pattern: invalid regex: {e}")
            if merged.get("price_regex"):
                try:
                    re.compile(str(merged["price_regex"]))
                except re.error as e:
                    errors.append(f"{where}.price_regex: invalid regex: {e}")

        seen: set[str] = set()
        storeless: dict[str, int] = {}
        for i, p in enumerate(raw["products"], 1):
            where = f"products #{i}"
            if not isinstance(p, dict) or not str(p.get("name") or "").strip():
                errors.append(f"{where}: needs a `name`")
                continue
            where = f"product {p['name']!r}"
            lname = str(p["name"]).lower()
            if lname in seen:
                errors.append(f"{where}: duplicate product name")
            seen.add(lname)
            for k in ("target_price", "max_price"):
                try:
                    _opt_price(p.get(k), k)
                except ConfigError as e:
                    errors.append(f"{where}: {e}")
            if p.get("interval") is not None:
                try:
                    secs = parse_duration(p["interval"])
                    if settings and secs < settings["min_interval"]:
                        self.warnings.append(
                            f"{where}: interval {p['interval']} is below min_interval; "
                            f"using {format_duration(settings['min_interval'])}"
                        )
                except ConfigError as e:
                    errors.append(f"{where}.interval: {e}")
            if not isinstance(p.get("listings"), list):
                errors.append(f"{where}: `listings:` must be a list")
                continue
            for j, l in enumerate(p["listings"], 1):
                lwhere = f"{where} listing #{j}"
                if not isinstance(l, dict):
                    errors.append(f"{lwhere}: must be a mapping (url:, retailer:, sku:, ...)")
                    continue
                try:
                    rkey = self.resolve_retailer(l)
                except ConfigError as e:
                    errors.append(f"{lwhere}: {e}")
                    continue
                rtype = retailers[rkey]["type"]
                url = l.get("url")
                if url and not str(url).startswith(("http://", "https://")):
                    errors.append(f"{lwhere}: url must start with http:// or https://")
                if rtype in ("html", "shopify") and not url:
                    errors.append(f"{lwhere}: needs a `url` (retailer {rkey!r} reads the product page)")
                if l.get("interval") is not None:
                    try:
                        parse_duration(l["interval"])
                    except ConfigError as e:
                        errors.append(f"{lwhere}.interval: {e}")
                r = retailers[rkey]
                if uses_store(r) and isinstance(r.get("stores", {}), dict):
                    try:
                        if resolve_store(expand_env(r), expand_env(l.get("store") or r.get("store"))) is None:
                            storeless[rkey] = storeless.get(rkey, 0) + 1
                    except ConfigError as e:
                        errors.append(f"{lwhere}: {e}")
                elif l.get("store"):
                    errors.append(f"{lwhere}: retailer {rkey!r} doesn't use `store:`")

        for rkey, count in storeless.items():
            name = retailers[rkey].get("name", rkey)
            self.warnings.append(
                f"{name}: {count} listing(s) have no store set, and {name} stock is per store. Set your store "
                f"once with:  python tracker.py add-retailer {rkey} --set store=<name or ID>   "
                f"(see: python tracker.py stores {rkey})"
            )

        if errors:
            raise ConfigError(f"Problems in {self.path}:\n  - " + "\n  - ".join(errors))
