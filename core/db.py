"""SQLite storage. Tables follow spec section 6.

Listing content only: no seller names, phone numbers or addresses are ever stored.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    description TEXT,
    price_gbp REAL,
    price_basis TEXT DEFAULT 'retail',
    location_text TEXT,
    postcode TEXT,
    lat REAL,
    lon REAL,
    distance_miles REAL,
    year INTEGER,
    reg TEXT,
    reg_confidence REAL,
    chassis_no TEXT,
    mileage INTEGER,
    colour TEXT,
    seller_type TEXT,
    image_urls TEXT,
    listed_estimate TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    raw_path TEXT,
    UNIQUE(source, source_id)
);
CREATE TABLE IF NOT EXISTS cars (
    id INTEGER PRIMARY KEY,
    generation TEXT,
    engine_status TEXT,
    market TEXT,
    confidence REAL,
    reasons TEXT,
    in_scope INTEGER NOT NULL DEFAULT 0,
    mod_tier TEXT,
    score REAL,
    score_reasons TEXT,
    flags TEXT,
    shortlist_state TEXT DEFAULT 'new',
    owner_notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS car_listings (
    car_id INTEGER NOT NULL REFERENCES cars(id),
    listing_id INTEGER NOT NULL REFERENCES listings(id) UNIQUE,
    PRIMARY KEY (car_id, listing_id)
);
CREATE TABLE IF NOT EXISTS price_history (
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    seen_at TEXT NOT NULL,
    price_gbp REAL
);
CREATE TABLE IF NOT EXISTS images (
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    url TEXT NOT NULL,
    local_path TEXT,
    phash TEXT,
    UNIQUE(listing_id, url)
);
CREATE TABLE IF NOT EXISTS mot_cache (
    reg TEXT PRIMARY KEY,
    fetched_at TEXT,
    vehicle TEXT,
    tests TEXT
);
CREATE TABLE IF NOT EXISTS alerts_sent (
    id INTEGER PRIMARY KEY,
    car_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    price_at_send REAL
);
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    last_success TEXT,
    last_error TEXT,
    last_error_at TEXT,
    found_count INTEGER DEFAULT 0,
    consecutive_blocks INTEGER DEFAULT 0,
    paused_until TEXT,
    last_duration_s REAL
);
CREATE TABLE IF NOT EXISTS geo_cache (
    query TEXT PRIMARY KEY,
    lat REAL,
    lon REAL,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS dup_candidates (
    id INTEGER PRIMARY KEY,
    car_a INTEGER NOT NULL,
    car_b INTEGER NOT NULL,
    reason TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    UNIQUE(car_a, car_b)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_reg ON listings(reg);
CREATE INDEX IF NOT EXISTS idx_listings_chassis ON listings(chassis_no);
"""


# Columns added after milestone 1: existing databases are upgraded in place.
MIGRATIONS = [
    ("source_health", "last_attempt", "TEXT"),
    ("source_health", "zero_runs", "INTEGER DEFAULT 0"),
    ("source_health", "selftest_error", "TEXT"),
    ("listings", "price_original", "REAL"),
    ("listings", "currency", "TEXT DEFAULT 'GBP'"),
    ("listings", "fx_rate", "REAL"),
    ("listings", "fx_date", "TEXT"),
    ("listings", "landed_cost_gbp", "REAL"),
    ("listings", "landed_breakdown", "TEXT"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    """A small thread-safe wrapper. One connection, guarded by a lock."""

    def __init__(self, path: Path | str | None = None):
        self.path = str(path or paths.db_path())
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.conn.commit()

    # -- basic helpers -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


JAPAN_SOURCES = ("goonet", "sbtjapan", "carfromjapan")


def cleanup_misread_import_prices(db: "DB") -> int:
    """Version 0.2.0 could download the Japan recipes and read yen or dollar prices as pounds
    (cars 'costing millions'). Those rows never got price_original; remove them once. The next
    sweep adds the same cars back with converted prices and landed costs."""
    if db.get_meta("cleanup_misread_import_prices") == "done":
        return 0
    marks = ",".join("?" * len(JAPAN_SOURCES))
    ids = [r["id"] for r in db.query(
        f"SELECT id FROM listings WHERE source IN ({marks}) AND price_original IS NULL", JAPAN_SOURCES)]
    for lid in ids:
        row = db.one("SELECT car_id FROM car_listings WHERE listing_id=?", (lid,))
        for table in ("price_history", "images", "car_listings"):
            db.execute(f"DELETE FROM {table} WHERE listing_id=?", (lid,))
        db.execute("DELETE FROM listings WHERE id=?", (lid,))
        if row and not db.one("SELECT 1 FROM car_listings WHERE car_id=?", (row["car_id"],)):
            db.execute("DELETE FROM alerts_sent WHERE car_id=?", (row["car_id"],))
            db.execute("DELETE FROM dup_candidates WHERE car_a=? OR car_b=?", (row["car_id"], row["car_id"]))
            db.execute("DELETE FROM cars WHERE id=?", (row["car_id"],))
    db.set_meta("cleanup_misread_import_prices", "done")
    return len(ids)
