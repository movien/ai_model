"""Turn a retailer's product page or API response into price + stock information.

Retailer types:
  html      Any product page. Reads schema.org structured data (JSON-LD / microdata / meta
            tags) that most shops publish for Google, then optional CSS selectors / regex /
            keywords you configure per retailer.
  shopify   Any Shopify store: reads the store's public /products/<handle>.js endpoint.
  json_api  Any JSON API (e.g. Best Buy's official Products API) using a URL template and
            dotted paths to the price / availability fields.
"""

from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlparse, urlunparse

from bs4 import BeautifulSoup

from .config import env_vars_in
from .fetch import Fetcher, FetchError

if TYPE_CHECKING:
    from .config import Listing

try:  # lxml is faster and more forgiving; html.parser is the stdlib fallback
    import lxml  # noqa: F401

    _HAVE_LXML = True
except ImportError:  # pragma: no cover
    _HAVE_LXML = False

# ("buy now" is left out on purpose: it matches "Buy now, pay later" banners on sold-out pages)
DEFAULT_IN_STOCK_TEXT = ["add to cart", "add to basket", "add to bag", "in stock"]
DEFAULT_OUT_OF_STOCK_TEXT = [
    "out of stock",
    "sold out",
    "not in stock",
    "currently unavailable",
    "temporarily unavailable",
    "no longer available",
    "notify me when available",
    "coming soon",
]

_IN = {"instock", "limitedavailability", "onlineonly", "instoreonly", "preorder", "presale",
       "available", "true", "yes", "1"}
_OUT = {"outofstock", "soldout", "discontinued", "backorder", "oos", "unavailable",
        "notavailable", "false", "no", "0"}

# A price-like number: digits with optional thousands/decimal separators.
_NUM_RE = re.compile(r"\d(?:[\d.,'  ]|\s(?=\d{3}(?!\d)))*")


class ExtractError(Exception):
    pass


@dataclass
class Result:
    price: float | None = None
    in_stock: bool | None = None
    currency: str | None = None
    title: str | None = None
    availability: str | None = None  # raw label, e.g. "InStock", "PreOrder"
    link: str | None = None
    price_source: str | None = None
    stock_source: str | None = None


# --------------------------------------------------------------------------- parsing helpers


def parse_price(value: Any) -> float | None:
    """'$1,999.99' -> 1999.99, '1.999,99 €' -> 1999.99, 199999 -> 199999.0"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM_RE.search(str(value))
    if not m:
        return None
    num = re.sub(r"[\s'  ]", "", m.group(0)).rstrip(".,")
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):  # 1.999,99
            num = num.replace(".", "").replace(",", ".")
        else:  # 1,999.99
            num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        num = f"{head}.{tail}" if (len(tail) == 2 and num.count(",") == 1) else num.replace(",", "")
    elif num.count(".") > 1:  # 1.999.999
        num = num.replace(".", "")
    try:
        return float(num)
    except ValueError:
        return None


def availability_to_bool(value: Any, in_values: list[str] | None = None) -> bool | None:
    """Map schema.org availability URLs, booleans, quantities and common words to True/False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().rstrip("/").rsplit("/", 1)[-1].lower()
    compact = re.sub(r"[\s_\-]", "", v)
    if in_values:
        return compact in {re.sub(r"[\s_\-]", "", x.lower()) for x in in_values}
    if compact in _IN:
        return True
    if compact in _OUT:
        return False
    return None


def get_path(data: Any, path: str | None) -> Any:
    """Dotted path lookup: 'products.0.salePrice'."""
    if not path:
        return None
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def _clean(text: str | None, limit: int = 150) -> str | None:
    if not text:
        return None
    text = " ".join(str(text).split())
    return text[:limit] or None


# --------------------------------------------------------------------------- structured data


def _jsonld_docs(soup: BeautifulSoup) -> list[Any]:
    docs = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        text = (tag.string or tag.get_text() or "").strip()
        text = re.sub(r"^\s*(<!--|//\s*<!\[CDATA\[)", "", text)
        text = re.sub(r"(-->|//\s*\]\]>)\s*$", "", text).strip()
        if not text:
            continue
        try:
            docs.append(json.loads(text, strict=False))
        except json.JSONDecodeError:
            continue
    return docs


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    items = t if isinstance(t, list) else [t]
    return {str(x).rsplit("/", 1)[-1].lower() for x in items if x}


_PRODUCT_TYPES = {"product", "productgroup", "individualproduct", "productmodel", "vehicle", "book"}


def _collect_offers(node: Any, out: list[dict]) -> None:
    """Collect Offer-like dicts without descending into them (so a nested
    priceSpecification isn't mistaken for a separate offer)."""
    if isinstance(node, list):
        for item in node:
            _collect_offers(item, out)
        return
    if not isinstance(node, dict):
        return
    if "aggregateoffer" in _types(node) and node.get("offers"):
        before = len(out)
        _collect_offers(node["offers"], out)  # prefer the individual offers (they carry stock)
        if len(out) > before:
            return
    if any(k in node for k in ("price", "lowPrice", "availability", "priceSpecification")):
        out.append(node)
        return
    for v in node.values():
        _collect_offers(v, out)


def _offers_of(product: dict) -> list[dict]:
    offers: list[dict] = []
    _collect_offers(product.get("offers"), offers)
    for variant in product.get("hasVariant") or []:
        if isinstance(variant, dict):
            _collect_offers(variant.get("offers"), offers)
    return offers


def _offer_price(offer: dict) -> tuple[float | None, str | None]:
    price = offer.get("price")
    if price is None:
        price = offer.get("lowPrice")
    currency = offer.get("priceCurrency")
    spec = offer.get("priceSpecification")
    if price is None and spec:
        for s in spec if isinstance(spec, list) else [spec]:
            if isinstance(s, dict) and s.get("price") is not None:
                price, currency = s["price"], currency or s.get("priceCurrency")
                break
    return parse_price(price), currency


def structured_data(soup: BeautifulSoup) -> Result:
    """Price/stock from schema.org JSON-LD (the format shops publish for Google Shopping)."""
    res = Result()
    for doc in _jsonld_docs(soup):
        for node in _walk(doc):
            if not (_types(node) & _PRODUCT_TYPES):
                continue
            offers = _offers_of(node)
            if not offers:
                res.title = res.title or _clean(node.get("name"))
                continue
            rows = []
            for o in offers:
                price, currency = _offer_price(o)
                label = o.get("availability")
                rows.append((price, availability_to_bool(label), currency, label))
            in_stock_rows = [r for r in rows if r[1] is True]
            priced = [r for r in (in_stock_rows or rows) if r[0] is not None]
            best = min(priced, key=lambda r: r[0]) if priced else (in_stock_rows or rows)[0]
            res.title = _clean(node.get("name")) or res.title
            res.price, res.currency = best[0], best[2]
            if in_stock_rows:
                res.in_stock = True
            elif any(r[1] is False for r in rows):
                res.in_stock = False
            res.availability = str(best[3]).rsplit("/", 1)[-1] if best[3] else None
            res.price_source = "structured data (JSON-LD)" if res.price is not None else None
            res.stock_source = f"structured data (JSON-LD: {res.availability})" if res.in_stock is not None else None
            return res
    return res


def meta_data(soup: BeautifulSoup) -> Result:
    """Price/stock from microdata (itemprop=...) and Open Graph / product meta tags."""
    res = Result()

    def meta(*names: str) -> str | None:
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return str(tag["content"])
        return None

    el = soup.select_one('[itemprop="price"]')
    if el is not None:
        res.price = parse_price(el.get("content") or el.get_text(" ", strip=True))
        if res.price is not None:
            res.price_source = "microdata (itemprop=price)"
    if res.price is None:
        res.price = parse_price(meta("product:price:amount", "og:price:amount"))
        if res.price is not None:
            res.price_source = "meta tag (product:price:amount)"

    cur = soup.select_one('[itemprop="priceCurrency"]')
    res.currency = (cur.get("content") if cur is not None else None) or meta(
        "product:price:currency", "og:price:currency"
    )

    el = soup.select_one('[itemprop="availability"]')
    if el is not None:
        label = el.get("href") or el.get("content") or el.get_text(" ", strip=True)
        res.in_stock = availability_to_bool(label)
        if res.in_stock is not None:
            res.availability = str(label).rsplit("/", 1)[-1]
            res.stock_source = f"microdata (itemprop=availability: {res.availability})"
    if res.in_stock is None:
        label = meta("product:availability", "og:availability")
        res.in_stock = availability_to_bool(label)
        if res.in_stock is not None:
            res.availability = label
            res.stock_source = f"meta tag (product:availability: {label})"

    res.title = _clean(meta("og:title")) or _clean(soup.title.get_text() if soup.title else None)
    return res


def _keyword_stock(text: str, in_words: list[str], out_words: list[str]) -> tuple[bool | None, str | None]:
    low = " ".join(text.split()).lower()
    for w in out_words:
        if str(w).lower() in low:
            return False, str(w)
    for w in in_words:
        if str(w).lower() in low:
            return True, str(w)
    return None, None


def parse_html(html: str, retailer: dict[str, Any]) -> Result:
    """Extract from a product page. Specific config (selectors/regex) beats structured data,
    which beats meta tags, which beat page-wide keywords."""
    soup = BeautifulSoup(html, "lxml") if _HAVE_LXML else BeautifulSoup(html, "html.parser")
    res = Result()
    in_words = retailer.get("in_stock_text") or DEFAULT_IN_STOCK_TEXT
    out_words = retailer.get("out_of_stock_text") or DEFAULT_OUT_OF_STOCK_TEXT

    # 1. Explicit per-retailer selectors / regex.
    sel = retailer.get("price_selector")
    if sel:
        el = soup.select_one(sel)
        if el is not None:
            res.price = parse_price(el.get("content") or el.get("data-price") or el.get_text(" ", strip=True))
            if res.price is not None:
                res.price_source = f"selector {sel!r}"
    rx = retailer.get("price_regex")
    if res.price is None and rx:
        m = re.search(str(rx), html, re.S)
        if m:
            res.price = parse_price(m.group(1) if m.groups() else m.group(0))
            if res.price is not None:
                res.price_source = f"regex {rx!r}"
    stock_sel = retailer.get("stock_selector")
    if stock_sel:
        el = soup.select_one(stock_sel)
        if el is not None:
            res.in_stock, word = _keyword_stock(el.get_text(" ", strip=True), in_words, out_words)
            if res.in_stock is not None:
                res.stock_source = f"selector {stock_sel!r} contains {word!r}"

    # 2. Structured data, 3. microdata / meta tags.
    layers = []
    if retailer.get("structured_data", True) is not False:
        layers.append(structured_data(soup))
    layers.append(meta_data(soup))
    for layer in layers:
        if res.price is None and layer.price is not None:
            res.price, res.price_source = layer.price, layer.price_source
            res.currency = layer.currency or res.currency
        if res.in_stock is None and layer.in_stock is not None:
            res.in_stock, res.stock_source, res.availability = layer.in_stock, layer.stock_source, layer.availability
        res.currency = res.currency or layer.currency
        res.title = res.title or layer.title

    # 4. Last resort: keywords anywhere in the visible page text.
    if res.in_stock is None and retailer.get("keyword_fallback", True) is not False:
        for tag in soup(["script", "style", "noscript", "template", "svg"]):
            tag.decompose()
        res.in_stock, word = _keyword_stock(soup.get_text(" "), in_words, out_words)
        if res.in_stock is not None:
            res.stock_source = f"page text contains {word!r}"
    return res


# --------------------------------------------------------------------------- shopify


def shopify_endpoint(url: str) -> tuple[str, str | None]:
    """https://shop.com/products/rtx-5090?variant=123 -> (https://shop.com/products/rtx-5090.js, '123')"""
    p = urlparse(url)
    path = re.sub(r"\.(js|json)$", "", p.path.rstrip("/"))
    if "/products/" not in path:
        raise ExtractError("Shopify URLs look like https://store.com/products/<product-name>")
    variant = (parse_qs(p.query).get("variant") or [None])[0]
    return urlunparse((p.scheme, p.netloc, path + ".js", "", "", "")), variant


def parse_shopify(data: dict[str, Any], variant: str | None = None) -> Result:
    variants = [v for v in data.get("variants") or [] if isinstance(v, dict)]
    chosen = variants
    if variant:
        want = str(variant).strip().lower()
        chosen = [v for v in variants if str(v.get("id")) == want] or [
            v for v in variants if want in str(v.get("title", "")).lower()
        ]
        if not chosen:
            names = ", ".join(repr(v.get("title")) for v in variants[:10])
            raise ExtractError(f"variant {variant!r} not found. Variants: {names}")

    def money(v: Any) -> float | None:
        # The .js endpoint gives integer cents; tolerate "1999.99" strings too.
        if isinstance(v, int) and not isinstance(v, bool):
            return v / 100
        return parse_price(v)

    res = Result(title=_clean(data.get("title")), price_source="Shopify product data")
    if chosen:
        available = [v for v in chosen if v.get("available")]
        pool = [money(v.get("price")) for v in (available or chosen)]
        pool = [p for p in pool if p is not None]
        res.price = min(pool) if pool else None
        res.in_stock = bool(available)
        if variant and len(chosen) == 1 and chosen[0].get("title") not in (None, "Default Title"):
            res.title = _clean(f"{res.title} ({chosen[0]['title']})")
    else:
        res.price = money(data.get("price"))
        res.in_stock = data.get("available") if isinstance(data.get("available"), bool) else None
    res.availability = None if res.in_stock is None else ("available" if res.in_stock else "sold out")
    res.stock_source = "Shopify product data" if res.in_stock is not None else None
    return res


# --------------------------------------------------------------------------- json api


def parse_json_api(data: Any, retailer: dict[str, Any]) -> Result:
    res = Result()
    if retailer.get("price_path"):
        res.price = parse_price(get_path(data, retailer["price_path"]))
        if res.price is not None:
            res.price_source = f"API field {retailer['price_path']!r}"
    if retailer.get("in_stock_path"):
        raw = get_path(data, retailer["in_stock_path"])
        res.in_stock = availability_to_bool(raw, retailer.get("in_stock_values"))
        if res.in_stock is not None:
            res.availability = str(raw)
            res.stock_source = f"API field {retailer['in_stock_path']!r} = {raw!r}"
    res.title = _clean(get_path(data, retailer.get("title_path")))
    link = get_path(data, retailer.get("link_path"))
    res.link = str(link) if link else None
    cur = get_path(data, retailer.get("currency_path"))
    res.currency = str(cur) if cur else None
    return res


def _api_url(listing: Listing) -> str:
    template = str(listing.retailer.get("url") or "")
    values = {**listing.retailer, **listing.fields}
    names = [name for _, name, _, _ in string.Formatter().parse(template) if name]
    missing = [n for n in names if values.get(n) in (None, "")]
    if missing:
        hints = []
        for n in missing:
            env = env_vars_in(listing.retailer_raw.get(n)) or env_vars_in(listing.fields.get(n))
            hints.append(f"set the {env[0]} environment variable" if env else f"add `{n}:` to the listing or retailer")
        raise ExtractError(f"{listing.retailer_name} needs {', '.join(missing)}: {'; '.join(hints)}")
    try:
        return template.format_map({n: quote(str(values[n]), safe="") for n in names})
    except (ValueError, KeyError, IndexError) as e:
        raise ExtractError(f"bad API url template {template!r}: {e}") from None


# --------------------------------------------------------------------------- entry point


def extract(listing: Listing, fetcher: Fetcher) -> Result:
    r = listing.retailer
    rtype = r.get("type", "html")
    headers = {str(k): str(v) for k, v in (r.get("headers") or {}).items()}
    if r.get("user_agent"):
        headers["User-Agent"] = str(r["user_agent"])
    delay = r.get("min_delay")

    if rtype == "html":
        resp = fetcher.get(listing.url, headers=headers, min_delay=delay)
        res = parse_html(resp.text, r)
    elif rtype == "shopify":
        endpoint, url_variant = shopify_endpoint(listing.url)
        resp = fetcher.get(endpoint, headers=headers, min_delay=delay)
        try:
            data = resp.json()
        except ValueError:
            raise ExtractError("not a Shopify product (the .js endpoint didn't return JSON)") from None
        res = parse_shopify(data, listing.fields.get("variant") or url_variant)
    elif rtype == "json_api":
        resp = fetcher.get(_api_url(listing), headers=headers, min_delay=delay)
        try:
            data = resp.json()
        except ValueError:
            raise ExtractError("the API didn't return JSON") from None
        res = parse_json_api(data, r)
    else:
        raise ExtractError(f"unknown retailer type {rtype!r}")

    if res.price is None and res.in_stock is None:
        raise ExtractError(
            "no price or stock info found. Run `python tracker.py test-url <url>` and add a "
            "price_selector / stock_selector for this retailer (see README)"
        )
    res.currency = res.currency or r.get("currency")
    res.link = res.link or listing.url
    return res


__all__ = ["Result", "ExtractError", "FetchError", "extract", "parse_html", "parse_price", "parse_shopify",
           "parse_json_api", "availability_to_bool", "get_path"]
