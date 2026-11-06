"""Daily digest and quiet hours (spec section 10).

Digest: one ntfy message a day at digest_time listing cars inside the budget that did not merit
an instant alert: red flags, engine swaps, low scores, or an engine still to be confirmed. If the
PC was off at that time, the digest goes out on the first check afterwards.

Quiet hours: alerts that arrive between quiet_start and quiet_end wait in a queue and are sent
when quiet hours end.
"""
from __future__ import annotations

import json
from datetime import datetime, time as dtime

from core.db import DB, loads

MAX_LINES = 10


def _hhmm(value) -> dtime | None:
    try:
        h, m = str(value).strip().split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def in_quiet_hours(cfg: dict, now: datetime | None = None) -> bool:
    a = cfg["alerts"]
    start, end = _hhmm(a.get("quiet_start")), _hhmm(a.get("quiet_end"))
    if not start or not end or start == end:
        return False
    t = (now or datetime.now()).time()
    return start <= t < end if start < end else (t >= start or t < end)  # overnight, e.g. 22:00-07:00


def digest_due(db: DB, cfg: dict, now: datetime | None = None) -> bool:
    a = cfg["alerts"]
    if a.get("digest_enabled", True) is False:
        return False
    at = _hhmm(a.get("digest_time") or "08:30")
    now = now or datetime.now()
    if not at or now.time() < at or in_quiet_hours(cfg, now):
        return False
    return db.get_meta("last_digest_date") != now.date().isoformat()


def build_digest(db: DB, cfg: dict, recipes: dict) -> tuple[str, str] | None:
    """Cars first seen since the last digest, inside the budget, that sent no instant alert."""
    since = db.get_meta("last_digest_at") or "1970-01-01"
    budget = cfg["budget"]
    rows = db.query(
        "SELECT c.id, c.in_scope, c.engine_status, c.score, c.flags, c.reasons, li.title, li.url, li.source, "
        "COALESCE(li.landed_cost_gbp, li.price_gbp) AS price, li.landed_cost_gbp FROM cars c "
        "JOIN car_listings cl ON cl.car_id=c.id JOIN listings li ON li.id=cl.listing_id "
        "WHERE li.first_seen >= ? AND li.status IN ('active','relisted') AND c.shortlist_state <> 'ignored' "
        "AND c.reasons NOT LIKE '%Excluded:%' AND NOT EXISTS (SELECT 1 FROM alerts_sent a WHERE a.car_id=c.id) "
        "AND COALESCE(li.landed_cost_gbp, li.price_gbp) BETWEEN ? AND ? ORDER BY c.score DESC",
        (since, budget["min_gbp"], budget["max_gbp"]))
    lines, seen = [], set()
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        flags = loads(r["flags"], [])
        if flags:
            why = flags[0]
        elif r["engine_status"] == "swapped_turbo":
            why = "engine swapped"
        elif r["engine_status"] == "unknown":
            why = "engine not confirmed in the ad"
        elif r["in_scope"] and (r["score"] or 0) < cfg["alerts"]["alert_min_score"]:
            why = f"low score ({r['score']:.0f}/100)"
        else:
            continue  # NA cars and the like are not near-misses
        price = f"£{r['price']:,.0f}" + (" landed" if r["landed_cost_gbp"] else "")
        lines.append(f"• {r['title'][:60]}, {price}: {why}")
    if not lines:
        return None
    n = len(lines)
    body = "\n".join(lines[:MAX_LINES])
    if n > MAX_LINES:
        body += f"\n…and {n - MAX_LINES} more in the tracker."
    body += "\nOpen the tracker on the PC to look at them."
    title = f"Daily digest: {n} car{'s' if n != 1 else ''} worth a look"
    return title, body


def send_digest_if_due(db: DB, cfg: dict, notifier, recipes: dict, now: datetime | None = None) -> bool:
    if not digest_due(db, cfg, now):
        return False
    from core.db import now as utc_now

    from .notifier import Alert

    built = build_digest(db, cfg, recipes)
    stamp = (now or datetime.now()).date().isoformat()
    db.set_meta("last_digest_date", stamp)
    db.set_meta("last_digest_at", utc_now())
    if built:
        notifier.send(Alert("digest", built[0], built[1], None, None, ["newspaper"], 2))
    return bool(built)


def queue_alert(db: DB, alert) -> None:
    q = json.loads(db.get_meta("queued_alerts") or "[]")
    q.append(alert.__dict__)
    db.set_meta("queued_alerts", json.dumps(q))


def take_queue(db: DB) -> list[dict]:
    q = json.loads(db.get_meta("queued_alerts") or "[]")
    db.set_meta("queued_alerts", "[]")
    return q
