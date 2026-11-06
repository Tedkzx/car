"""Milestone 3 acceptance tests (spec section 12)."""
from core import config
from core.models import Match
from core.normalizer import normalise
from scoring.score import score

from .test_pipeline import make

W = config.DEFAULTS["scoring"]["weights"]


def m(gen="S14a"):
    return Match(gen, "factory_turbo", "uk_200sx", 0.9, True)


def test_stock_car_ranks_above_heavily_modified_car_with_reasons():
    stock = normalise("t", {"id": 1, "url": "u", "title": "Nissan 200SX S14a", "price": 13000,
                            "description": "Completely standard, 2 owners, full service history, cambelt done"})
    heavy = normalise("t", {"id": 2, "url": "u", "title": "Nissan 200SX S14a", "price": 13000,
                            "description": "GTX3076 big turbo, standalone ECU, forged internals, coilovers, drift car"})
    s1, s2 = score(stock, m(), W), score(heavy, m(), W)
    assert s1.score > s2.score + 30
    assert s1.mod_tier == "stock" and s2.mod_tier == "heavy"
    assert "Ad says the car is standard or original" in s1.reasons
    assert "Used for drifting or track days" in s2.reasons


def test_value_switches_from_provisional_after_8_comparables():
    p, db, sent, cfg = make({"pistonheads": [(200, "")]})
    for i in range(7):
        p.process("pistonheads", {"id": f"c{i}", "url": f"https://x/{i}", "title": "Nissan 200SX S14a turbo standard",
                                  "price": 12000 + i * 100, "year": 1998})
    l = normalise("t", {"id": "n", "url": "u", "title": "Nissan 200SX S14a turbo standard", "price": 11000, "year": 1998})
    from scoring.value import cohort_median

    s = score(l, m(), W, market_lookup=lambda g, mk, t: cohort_median(db, g, mk, t))
    assert s.value_provisional and s.comparables == 7
    assert any("provisional" in r for r in s.reasons)
    p.process("pistonheads", {"id": "c7", "url": "https://x/7", "title": "Nissan 200SX S14a turbo standard",
                              "price": 12700, "year": 1998})
    s = score(l, m(), W, market_lookup=lambda g, mk, t: cohort_median(db, g, mk, t))
    assert not s.value_provisional and s.comparables == 8
    assert any("compared with 8 similar cars, typical price £12,350" in r for r in s.reasons)


def test_price_history_page_shows_changes():
    from fastapi.testclient import TestClient

    from alerts.notifier import Notifier
    from web.server import AppState, create_app

    from .test_web import FakeScheduler

    p, db, sent, cfg = make({"pistonheads": [(200, "")]})
    raw = {"id": "1", "url": "https://x/1", "title": "Nissan Silvia S15 Spec-R", "price": 19000, "year": 2001}
    p.process("pistonheads", raw)
    p.process("pistonheads", dict(raw, price=17500))
    state = AppState(db, p, FakeScheduler(), Notifier(lambda: cfg, sent.ntfy_send, sent.toast_send))
    state._cfg["setup_complete"] = True
    html = TestClient(create_app(state)).get("/price-history").text
    assert "£19,000" in html and "£17,500" in html and "-8%" in html
