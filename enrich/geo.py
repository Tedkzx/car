"""Distance from the owner's postcode, via the free no-key postcodes.io (cached)."""
from __future__ import annotations

import logging
import math
import re
from urllib.parse import quote

from core.db import DB, now

log = logging.getLogger(__name__)
BASE = "https://api.postcodes.io"
FULL_PC = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.I)
OUTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$", re.I)


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


class Geo:
    def __init__(self, db: DB, client=None):
        self.db = db
        self._client = client

    def client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=15, headers={"User-Agent": "SX200Tracker/1.0"})
        return self._client

    def locate(self, query: str) -> tuple[float, float] | None:
        q = re.sub(r"\s+", " ", (query or "").strip())
        q = re.sub(r",?\s*(united kingdom|uk|england|scotland|wales)$", "", q, flags=re.I).strip(" ,")
        if not q:
            return None
        key = q.upper()
        row = self.db.one("SELECT lat, lon FROM geo_cache WHERE query=?", (key,))
        if row:
            return (row["lat"], row["lon"]) if row["lat"] is not None else None
        result = self._lookup(q)
        self.db.execute(
            "INSERT OR REPLACE INTO geo_cache(query, lat, lon, fetched_at) VALUES (?,?,?,?)",
            (key, result[0] if result else None, result[1] if result else None, now()),
        )
        return result

    def _lookup(self, q: str) -> tuple[float, float] | None:
        try:
            if FULL_PC.match(q):
                r = self.client().get(f"{BASE}/postcodes/{quote(q)}")
                if r.status_code == 200:
                    res = r.json()["result"]
                    return res["latitude"], res["longitude"]
            if OUTCODE.match(q.split()[0]) and len(q.split()) == 1:
                r = self.client().get(f"{BASE}/outcodes/{quote(q)}")
                if r.status_code == 200:
                    res = r.json()["result"]
                    return res["latitude"], res["longitude"]
            town = q.split(",")[0].strip()
            r = self.client().get(f"{BASE}/places", params={"q": town, "limit": 1})
            if r.status_code == 200 and r.json().get("result"):
                res = r.json()["result"][0]
                return res["latitude"], res["longitude"]
        except Exception as e:  # distance is a nicety: never fail a sweep over it
            log.info("postcode lookup failed for %r: %s", q, e)
        return None

    def distance(self, home_postcode: str, location: str) -> float | None:
        if not home_postcode or not location:
            return None
        home = self.locate(home_postcode)
        there = self.locate(location)
        if not home or not there:
            return None
        return round(miles(home, there), 1)
