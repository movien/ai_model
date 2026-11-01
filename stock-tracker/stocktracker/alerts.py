"""Decide when a check is worth an alert.

Alerts fire on *transitions*, compared with the previous successful check, so you get one
message when something happens instead of one per check:

  in_stock    became buyable: in stock, and at or under the product's max_price (if set)
  target      price fell to or below the product's target_price (while not out of stock)
  price_drop  price fell by at least settings.price_drop_min_percent (while not out of stock,
              and ignoring prices above max_price)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Listing
    from .extract import Result

_SYMBOLS = {"USD": "$", "CAD": "C$", "AUD": "A$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹"}


def fmt_price(price: float | None, currency: str | None = None) -> str:
    if price is None:
        return "-"
    cur = (currency or "").upper()
    decimals = 0 if cur in ("JPY", "KRW") else 2
    num = f"{price:,.{decimals}f}"
    if cur in _SYMBOLS:
        return f"{_SYMBOLS[cur]}{num}"
    return f"{num} {cur}".strip()


@dataclass
class Alert:
    kinds: list[str]
    title: str
    message: str
    url: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


def _buyable(in_stock: bool | None, price: float | None, max_price: float | None) -> bool:
    return in_stock is True and (max_price is None or price is None or price <= max_price)


def evaluate(listing: Listing, prev: sqlite3.Row | dict | None, cur: Result, settings: dict[str, Any]) -> Alert | None:
    product = listing.product
    prev_price = prev["price"] if prev is not None else None
    prev_stock = None if prev is None or prev["in_stock"] is None else bool(prev["in_stock"])
    kinds: list[str] = []
    lines: list[str] = []
    money = lambda p: fmt_price(p, cur.currency)  # noqa: E731

    if _buyable(cur.in_stock, cur.price, product.max_price) and not (
        prev is not None and _buyable(prev_stock, prev_price, product.max_price)
    ):
        kinds.append("in_stock")
        price_part = f" for {money(cur.price)}" if cur.price is not None else ""
        lines.append(f"In stock at {listing.retailer_name}{price_part}.")

    if cur.price is not None and cur.in_stock is not False:
        tp = product.target_price
        if tp is not None and cur.price <= tp:
            was_at_target = prev_price is not None and prev_price <= tp and prev_stock is not False
            if not was_at_target:
                kinds.append("target")
                lines.append(f"At or below your target price of {money(tp)}.")
        over_max = product.max_price is not None and cur.price > product.max_price
        if prev_price and cur.price < prev_price and not over_max:
            drop = (prev_price - cur.price) / prev_price * 100
            if drop >= settings.get("price_drop_min_percent", 0):
                kinds.append("price_drop")
                lines.append(f"Price dropped {drop:.1f}% from {money(prev_price)} to {money(cur.price)}.")

    if not kinds:
        return None

    headline = {"in_stock": "IN STOCK", "target": "Target price", "price_drop": "Price drop"}[kinds[0]]
    title = f"{headline}: {product.name} at {listing.retailer_name}"
    if cur.price is not None:
        title += f" - {money(cur.price)}"
    if cur.title and cur.title.lower() != product.name.lower():
        lines.append(cur.title)
    url = cur.link or listing.url
    if url:
        lines.append(url)
    return Alert(
        kinds=kinds,
        title=title,
        message="\n".join(lines),
        url=url,
        data={
            "product": product.name,
            "retailer": listing.retailer_name,
            "price": cur.price,
            "currency": cur.currency,
            "previous_price": prev_price,
            "in_stock": cur.in_stock,
            "target_price": product.target_price,
            "max_price": product.max_price,
        },
    )
