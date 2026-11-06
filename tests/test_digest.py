"""Daily digest and quiet hours (spec section 10)."""
from datetime import datetime

from alerts import digest
from alerts.notifier import Alert, Notifier

from .test_pipeline import make


def test_quiet_hours_overnight_and_daytime():
    cfg = {"alerts": {"quiet_start": "22:00", "quiet_end": "07:00"}}
    assert digest.in_quiet_hours(cfg, datetime(2026, 11, 6, 23, 30))
    assert digest.in_quiet_hours(cfg, datetime(2026, 11, 7, 6, 59))
    assert not digest.in_quiet_hours(cfg, datetime(2026, 11, 7, 7, 0))
    assert not digest.in_quiet_hours({"alerts": {"quiet_start": None, "quiet_end": None}}, datetime(2026, 11, 7, 3, 0))


def test_alerts_in_quiet_hours_wait_then_send():
    p, db, sent, cfg = make({"pistonheads": [(200, "")]})
    cfg["alerts"].update(quiet_start="22:00", quiet_end="07:00")
    now = [datetime(2026, 11, 6, 23, 0)]
    n = Notifier(lambda: cfg, sent.ntfy_send, sent.toast_send, db=db, clock=lambda: now[0])
    p.notifier = n
    p.process("pistonheads", {"id": "1", "url": "https://x/1", "title": "Nissan Silvia S15 Spec-R", "price": 18000, "year": 2001})
    assert sent.ntfy == [] and db.one("SELECT COUNT(*) n FROM alerts_sent")["n"] == 1
    assert n.flush_quiet_queue() == 0  # still quiet
    now[0] = datetime(2026, 11, 7, 7, 5)
    assert n.flush_quiet_queue() == 1
    assert sent.ntfy[0]["title"].startswith("New: S15")
    assert n.send(Alert("test", "t", "m")) and len(sent.ntfy) == 2


def test_daily_digest_lists_in_budget_near_misses_once_a_day():
    p, db, sent, cfg = make({"pistonheads": [(200, "")]})
    p.process("pistonheads", {"id": "1", "url": "https://x/1", "title": "Nissan Silvia S15", "price": 15000, "year": 2001})
    p.process("pistonheads", {"id": "2", "url": "https://x/2", "title": "Nissan 200SX S14a turbo Cat S", "price": 9500, "year": 1998})
    p.process("pistonheads", {"id": "3", "url": "https://x/3", "title": "Nissan 200SX S14 1JZ swap", "price": 12000, "year": 1996})
    p.process("pistonheads", {"id": "4", "url": "https://x/4", "title": "Nissan Silvia S15 Spec-S", "price": 12000, "year": 2000})
    p.process("pistonheads", {"id": "5", "url": "https://x/5", "title": "Nissan 200SX S14a turbo Cat N", "price": 14000, "year": 1998})
    n = Notifier(lambda: cfg, sent.ntfy_send, sent.toast_send, db=db)
    morning = datetime(2026, 11, 7, 8, 40)
    assert not digest.send_digest_if_due(db, cfg, n, p.recipes, now=datetime(2026, 11, 7, 8, 0))  # too early
    assert digest.send_digest_if_due(db, cfg, n, p.recipes, now=morning)
    msg = sent.ntfy[-1]
    assert msg["title"] == "Daily digest: 3 cars worth a look"
    assert "engine not confirmed" in msg["message"] and "engine swapped" in msg["message"]
    assert "S14a turbo Cat S" not in msg["message"]  # £9,500 is under the budget
    assert "Spec-S" not in msg["message"]  # non-turbo is not a near-miss
    assert "write-off" in msg["message"]  # the Cat N car, red-flagged
    assert not digest.send_digest_if_due(db, cfg, n, p.recipes, now=datetime(2026, 11, 7, 18, 0))  # once a day
    # PC off at 08:30 the next day: the digest goes out on the first check afterwards, with only new cars.
    db.execute("UPDATE listings SET first_seen='2026-11-06T09:00:00+00:00'")  # yesterday's cars
    p.process("pistonheads", {"id": "6", "url": "https://x/6", "title": "Nissan Silvia S14", "price": 16000, "year": 1996})
    assert digest.send_digest_if_due(db, cfg, n, p.recipes, now=datetime(2026, 11, 8, 14, 0))
    assert sent.ntfy[-1]["title"] == "Daily digest: 1 car worth a look"
