"""Exchange rates for Japan listings, from the free no-key Frankfurter feed (European Central Bank rates).

Rates are fetched at most once a day and cached in the database. The rate used is stored on
every listing so each landed-cost figure can be explained later (spec section 8).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from core.db import DB, now

log = logging.getLogger(__name__)
FEEDS = ("https://api.frankfurter.app/latest?from=GBP&to=JPY,USD,EUR,AUD",
         "https://api.frankfurter.dev/v1/latest?base=GBP&symbols=JPY,USD,EUR,AUD")
# Used only if no rate has ever been fetched: approximate, and labelled as such on the car.
FALLBACK = {"JPY": 213.0, "USD": 1.30, "EUR": 1.18, "AUD": 2.0}


class FX:
    def __init__(self, db: DB, client=None):
        self.db = db
        self._client = client

    def _fetch(self) -> dict | None:
        try:
            if self._client is None:
                import httpx

                self._client = httpx.Client(timeout=20, headers={"User-Agent": "SX200Tracker"})
            for url in FEEDS:
                r = self._client.get(url)
                if r.status_code == 200:
                    data = r.json()
                    return {"rates": data["rates"], "date": data.get("date"), "fetched": now()}
        except Exception as e:
            log.info("exchange rate fetch failed: %s", e)
        return None

    def rates(self) -> dict:
        cached = json.loads(self.db.get_meta("fx_rates") or "null")
        fresh = cached and datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched"]) < timedelta(hours=20)
        if not fresh:
            got = self._fetch()
            if got:
                self.db.set_meta("fx_rates", json.dumps(got))
                cached = got
        if not cached:
            return {"rates": FALLBACK, "date": None, "approximate": True}
        return cached

    def to_gbp(self, amount: float, currency: str) -> tuple[float | None, float | None, str | None]:
        """Returns (GBP amount, units of currency per £1, rate date)."""
        if amount is None:
            return None, None, None
        currency = (currency or "GBP").upper()
        if currency == "GBP":
            return amount, 1.0, None
        r = self.rates()
        per_gbp = r["rates"].get(currency)
        if not per_gbp:
            return None, None, None
        return round(amount / per_gbp, 2), per_gbp, r.get("date") or "approximate"
