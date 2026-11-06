"""Market value from comparable cars (spec section 9).

A cohort is the same generation, market (UK car, UK-registered import, still in Japan) and mod
tier. Its median uses each car's effective price (landed cost for imports) over the last 180 days,
including cars since removed or sold, which is the nearest thing to sold prices. With fewer than
MIN_COMPARABLES the value score falls back to the seed bands and is labelled provisional.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from core.db import DB

MIN_COMPARABLES = 8
WINDOW_DAYS = 180


def cohort_median(db: DB, generation: str, market: str, mod_tier: str, exclude_car: int | None = None
                  ) -> tuple[float | None, int]:
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")
    rows = db.query(
        "SELECT c.id, MIN(COALESCE(li.landed_cost_gbp, li.price_gbp)) AS p FROM cars c "
        "JOIN car_listings cl ON cl.car_id=c.id JOIN listings li ON li.id=cl.listing_id "
        "WHERE c.generation=? AND c.market=? AND c.mod_tier=? AND c.reasons NOT LIKE '%Excluded:%' "
        "AND c.engine_status='factory_turbo' AND li.last_seen>=? AND c.id<>? "
        "GROUP BY c.id HAVING p > 1000",
        (generation, market, mod_tier, since, exclude_car or -1))
    prices = [r["p"] for r in rows]
    if len(prices) < MIN_COMPARABLES:
        return None, len(prices)
    return float(statistics.median(prices)), len(prices)
