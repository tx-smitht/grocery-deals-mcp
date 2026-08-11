"""SQLite-backed cache and price history.

Two jobs:
  1. Keep Flipp responses on disk so a week's circulars are fetched once.
  2. Accumulate every price we ever see, so "$1.99/lb chicken" can be
     judged against what that item normally costs.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from .config import CACHE_TTL_HOURS, DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key         TEXT PRIMARY KEY,
    body        TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);

-- The live index every query tool reads from. Refilled by a refresh;
-- read-only and network-free at query time, which is what keeps lookups
-- in the single-digit milliseconds.
CREATE TABLE IF NOT EXISTS deals (
    zip         TEXT NOT NULL,
    store       TEXT NOT NULL,
    name        TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    brand       TEXT,
    price       REAL,
    unit        TEXT,
    qualifier   TEXT,
    sale_story  TEXT,
    category    TEXT,
    valid_from  TEXT,
    valid_to    TEXT,
    flyer_id    INTEGER,
    item_id     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_deals_zip ON deals (zip);
CREATE INDEX IF NOT EXISTS idx_deals_store ON deals (store);
CREATE INDEX IF NOT EXISTS idx_deals_key ON deals (item_key);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS price_points (
    merchant    TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    name        TEXT NOT NULL,
    price       REAL NOT NULL,
    unit        TEXT,
    sale_story  TEXT,
    valid_from  TEXT,
    valid_to    TEXT,
    first_seen  REAL NOT NULL,
    PRIMARY KEY (merchant, item_key, price, valid_from)
);

CREATE INDEX IF NOT EXISTS idx_price_item ON price_points (item_key);
CREATE INDEX IF NOT EXISTS idx_price_merchant ON price_points (merchant);
"""

_STOPWORDS = {
    "or", "and", "the", "with", "of", "a", "an", "each", "ea", "pack",
    "select", "varieties", "assorted", "brand", "value", "size",
}


def normalize(name: str) -> str:
    """Collapse an ad headline into a comparable key.

    'Nature's Promise Organic Sweet Potatoes' -> 'natures promise organic
    sweet potatoes'. Sizes and pack counts are dropped so the same product
    matches across weeks when the ad copy changes.
    """
    s = name.lower()
    s = re.sub(r"[®™©]", "", s)
    s = re.sub(r"\b\d+(\.\d+)?\s?(oz|lb|lbs|ct|pk|pt|qt|gal|ml|l|g|kg)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t and t not in _STOPWORDS]
    return " ".join(tokens)


def tokens(name: str) -> set[str]:
    return set(normalize(name).split())


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def cache_get(key: str, ttl_hours: float | None = None) -> Any | None:
    ttl = (CACHE_TTL_HOURS if ttl_hours is None else ttl_hours) * 3600
    with connect() as conn:
        row = conn.execute(
            "SELECT body, fetched_at FROM http_cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    if ttl >= 0 and time.time() - row["fetched_at"] > ttl:
        return None
    try:
        return json.loads(row["body"])
    except json.JSONDecodeError:
        return None


def cache_put(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO http_cache (key, body, fetched_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )


def cache_clear() -> int:
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM http_cache").fetchone()["c"]
        conn.execute("DELETE FROM http_cache")
    return int(n)


def record_prices(items: Iterable[dict]) -> int:
    """Persist deal items into price history. Returns rows newly inserted."""
    rows = []
    now = time.time()
    for it in items:
        price = it.get("price")
        name = (it.get("name") or "").strip()
        merchant = (it.get("store") or "").strip()
        if price is None or not name or not merchant:
            continue
        rows.append(
            (
                merchant,
                normalize(name),
                name,
                float(price),
                it.get("unit"),
                it.get("sale_story"),
                it.get("valid_from"),
                it.get("valid_to"),
                now,
            )
        )
    if not rows:
        return 0
    with connect() as conn:
        before = conn.execute("SELECT COUNT(*) AS c FROM price_points").fetchone()["c"]
        conn.executemany(
            """INSERT OR IGNORE INTO price_points
               (merchant, item_key, name, price, unit, sale_story,
                valid_from, valid_to, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        after = conn.execute("SELECT COUNT(*) AS c FROM price_points").fetchone()["c"]
    return int(after - before)


def history_for(query: str, limit: int = 200) -> list[dict]:
    """Price points whose item_key shares meaningful tokens with `query`."""
    want = tokens(query)
    if not want:
        return []
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM price_points ORDER BY valid_from DESC"
        ).fetchall()

    scored = []
    for r in rows:
        have = set(r["item_key"].split())
        if not have:
            continue
        overlap = len(want & have)
        if overlap == 0:
            continue
        coverage = overlap / len(want)
        if coverage < 0.6:
            continue
        scored.append((coverage, dict(r)))
    scored.sort(key=lambda x: (-x[0], x[1].get("valid_from") or ""))
    return [d for _, d in scored[:limit]]


# --------------------------------------------------------------------------
# Deal index — the hot path
# --------------------------------------------------------------------------

def replace_deals(zip_code: str, items: Iterable[dict]) -> int:
    """Swap in a fresh index for one ZIP. Old rows survive in price_points."""
    rows = [
        (
            zip_code,
            it.get("store"),
            it.get("name"),
            normalize(it.get("name") or ""),
            it.get("brand"),
            it.get("price"),
            it.get("unit"),
            it.get("qualifier"),
            it.get("sale_story"),
            it.get("category"),
            it.get("valid_from"),
            it.get("valid_to"),
            it.get("flyer_id"),
            it.get("item_id"),
        )
        for it in items
        if it.get("store") and it.get("name")
    ]
    with connect() as conn:
        conn.execute("DELETE FROM deals WHERE zip = ?", (zip_code,))
        conn.executemany(
            """INSERT INTO deals
               (zip, store, name, item_key, brand, price, unit, qualifier,
                sale_story, category, valid_from, valid_to, flyer_id, item_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"refreshed_at:{zip_code}", str(time.time())),
        )
    return len(rows)


def index_age_hours(zip_code: str) -> float | None:
    """Hours since this ZIP's last refresh, or None if never refreshed."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"refreshed_at:{zip_code}",)
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM deals WHERE zip = ?", (zip_code,)
        ).fetchone()["c"]
    if row is None or not count:
        return None
    try:
        return (time.time() - float(row["value"])) / 3600
    except ValueError:
        return None


def indexed_stores(zip_code: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT store, COUNT(*) AS items,
                      SUM(CASE WHEN price IS NOT NULL THEN 1 ELSE 0 END) AS priced,
                      MIN(valid_from) AS valid_from, MAX(valid_to) AS valid_to
               FROM deals WHERE zip = ? GROUP BY store ORDER BY store""",
            (zip_code,),
        ).fetchall()
    return [dict(r) for r in rows]


def search_index(
    zip_code: str,
    query: str = "",
    stores: list[str] | None = None,
    max_price: float | None = None,
    priced_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Token-overlap search over the local index. No network."""
    sql = "SELECT * FROM deals"
    where, params = ["zip = ?"], [zip_code]
    if stores:
        where.append("(" + " OR ".join("store LIKE ?" for _ in stores) + ")")
        params += [f"%{s}%" for s in stores]
    if max_price is not None:
        where.append("price IS NOT NULL AND price <= ?")
        params.append(max_price)
    elif priced_only:
        where.append("price IS NOT NULL")
    sql += " WHERE " + " AND ".join(where)

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    want = tokens(query)
    if not want:
        out = [dict(r) for r in rows]
        out.sort(key=lambda d: (d["price"] is None, d["price"] or 0))
        return out[:limit]

    scored = []
    for r in rows:
        have = set(r["item_key"].split())
        overlap = want & have
        if not overlap:
            continue
        # Coverage of the *query* matters most: searching "chicken breast"
        # should rank an exact hit above a 12-word ad headline that happens
        # to contain both words.
        coverage = len(overlap) / len(want)
        brevity = len(overlap) / max(len(have), 1)
        scored.append((coverage + 0.25 * brevity, dict(r)))

    scored.sort(key=lambda x: (-x[0], x[1]["price"] is None, x[1]["price"] or 0))
    return [d for _, d in scored[:limit]]


def history_stats() -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS points,
                      COUNT(DISTINCT item_key) AS items,
                      COUNT(DISTINCT merchant) AS stores,
                      MIN(valid_from) AS earliest,
                      MAX(valid_to) AS latest
               FROM price_points"""
        ).fetchone()
    return dict(row)
