"""Price & stock history in a local SQLite database."""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from .alerts import Alert
    from .config import Listing
    from .extract import Result

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id           INTEGER PRIMARY KEY,
    listing_key  TEXT NOT NULL,
    product      TEXT NOT NULL,
    retailer     TEXT NOT NULL,
    url          TEXT,
    ts           TEXT NOT NULL,      -- UTC, ISO 8601
    status       TEXT NOT NULL,      -- ok | blocked | error
    price        REAL,
    currency     TEXT,
    in_stock     INTEGER,            -- 1 / 0 / NULL (unknown)
    availability TEXT,
    title        TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_listing ON checks (listing_key, id);
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY,
    listing_key  TEXT NOT NULL,
    product      TEXT NOT NULL,
    retailer     TEXT NOT NULL,
    ts           TEXT NOT NULL,
    kinds        TEXT NOT NULL,      -- comma separated: in_stock,target,price_drop
    title        TEXT NOT NULL,
    message      TEXT NOT NULL,
    url          TEXT,
    price        REAL,
    currency     TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        try:  # lets the GUI read while the tracker writes
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def record(
        self,
        listing: Listing,
        status: str,
        result: Result | None = None,
        error: str | None = None,
        ts: str | None = None,
    ) -> None:
        r = result
        self.conn.execute(
            "INSERT INTO checks (listing_key, product, retailer, url, ts, status, price, currency,"
            " in_stock, availability, title, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                listing.key,
                listing.product.name,
                listing.retailer_name,
                (r.link if r else None) or listing.url,
                ts or utcnow(),
                status,
                r.price if r else None,
                r.currency if r else None,
                None if (r is None or r.in_stock is None) else int(r.in_stock),
                r.availability if r else None,
                r.title if r else None,
                error,
            ),
        )
        self.conn.commit()

    def record_alert(self, listing: Listing, alert: Alert, ts: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO alerts (listing_key, product, retailer, ts, kinds, title, message, url, price, currency)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (listing.key, listing.product.name, listing.retailer_name, ts or utcnow(), ",".join(alert.kinds),
             alert.title, alert.message, alert.url, alert.data.get("price"), alert.data.get("currency")),
        )
        self.conn.commit()

    def recent_alerts(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def price_points(self, key: str, limit: int = 300) -> list[dict[str, Any]]:
        """Oldest-first points where price or stock changed, plus the latest check (for charts)."""
        points = [dict(r) for r in reversed(self.changes(key, limit))]
        last = self.last_ok(key)
        if last is not None and (not points or points[-1]["ts"] != last["ts"]):
            points.append({"ts": last["ts"], "price": last["price"], "currency": last["currency"],
                           "in_stock": last["in_stock"]})
        return [{"ts": p["ts"], "price": p["price"], "in_stock": p["in_stock"]} for p in points]

    def last_ok(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM checks WHERE listing_key=? AND status='ok' ORDER BY id DESC LIMIT 1", (key,)
        ).fetchone()

    def last_check(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM checks WHERE listing_key=? ORDER BY id DESC LIMIT 1", (key,)
        ).fetchone()

    def stats(self, key: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS checks, MIN(ts) AS first_ts, MIN(price) AS low, MAX(price) AS high,"
            " MAX(CASE WHEN in_stock=1 THEN ts END) AS last_in_stock"
            " FROM checks WHERE listing_key=? AND status='ok'",
            (key,),
        ).fetchone()
        low_ts = None
        if row["low"] is not None:
            low_ts = self.conn.execute(
                "SELECT ts FROM checks WHERE listing_key=? AND status='ok' AND price=? ORDER BY id DESC LIMIT 1",
                (key, row["low"]),
            ).fetchone()["ts"]
        failed = self.conn.execute(
            "SELECT COUNT(*) FROM checks WHERE listing_key=? AND status!='ok'", (key,)
        ).fetchone()[0]
        return {**dict(row), "low_ts": low_ts, "failed": failed}

    def changes(self, key: str, limit: int = 20) -> list[sqlite3.Row]:
        """Successful checks where the price or stock changed (newest first)."""
        return self.conn.execute(
            """
            SELECT ts, price, currency, in_stock, availability, prev_price, prev_stock FROM (
                SELECT ts, price, currency, in_stock, availability, id,
                       LAG(price)    OVER w AS prev_price,
                       LAG(in_stock) OVER w AS prev_stock,
                       ROW_NUMBER()  OVER w AS rn
                FROM checks WHERE listing_key=? AND status='ok'
                WINDOW w AS (ORDER BY id)
            )
            WHERE rn = 1 OR price IS NOT prev_price OR in_stock IS NOT prev_stock
            ORDER BY id DESC LIMIT ?
            """,
            (key, limit),
        ).fetchall()

    def export_csv(self, path: str | Path, keys: Iterable[str] | None = None) -> int:
        sql = "SELECT ts, product, retailer, url, status, price, currency, in_stock, availability, title, error FROM checks"
        params: tuple = ()
        if keys is not None:
            keys = list(keys)
            if not keys:
                keys = [""]
            sql += f" WHERE listing_key IN ({','.join('?' * len(keys))})"
            params = tuple(keys)
        rows = self.conn.execute(sql + " ORDER BY id", params).fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["time_utc", "product", "retailer", "url", "status", "price", "currency", "in_stock",
                        "availability", "title", "error"])
            for r in rows:
                w.writerow([r[k] for k in r.keys()])
        return len(rows)
