from pathlib import Path

from alerts.notifier import Notifier
from collectors import recipe_engine as engine
from core import config, health
from core.db import DB
from core.pipeline import Pipeline

from .conftest import FIX, RFIX, FakeFetcher, NoGeo, Sent

ROOT = Path(__file__).resolve().parent.parent


def make(pages_by_source, cfg_over=None, db=None):
    recipes = engine.load_recipes([ROOT / "recipes"])
    recipes = {k: v for k, v in recipes.items() if k in pages_by_source}
    cfg = config.load()
    cfg["owner"]["home_postcode"] = "LE1 1AA"
    for k, v in (cfg_over or {}).items():
        cfg[k].update(v)
    sent = Sent()
    db = db or DB(":memory:")
    current = {}

    def fetcher_factory():
        name = current["name"]
        return FakeFetcher(pages_by_source[name])

    p = Pipeline(db, lambda: cfg, Notifier(lambda: cfg, sent.ntfy_send, sent.toast_send), recipes,
                 fetcher_factory=fetcher_factory, geo=NoGeo())
    orig = p._run_source

    def run_source(name, res, browser):
        current["name"] = name
        return orig(name, res, browser)

    p._run_source = run_source
    return p, db, sent, cfg


def test_sweep_real_pistonheads_page_alerts_nothing_out_of_scope():
    html = (RFIX / "pistonheads.html").read_text(encoding="utf-8")
    p, db, sent, _ = make({"pistonheads": [(200, html)]})
    res = p.sweep()
    assert res.per_source["pistonheads"] >= 6
    # Real page on 6 Nov 2026: two S15s with no trim stated, a 180SX, an LS3 swap, a PS13 'sr 20', a V12.
    # None is a confirmed factory-SR20DET car in budget, so nothing may alert.
    assert sent.ntfy == []
    cars = db.query("SELECT generation, engine_status, in_scope FROM cars")
    assert all(not c["in_scope"] for c in cars)
    s15 = [c for c in cars if c["generation"] == "S15"]
    assert len(s15) == 2 and all(c["engine_status"] == "unknown" for c in s15)


def make_page(title, price, year=1997, loc="Leicester, United Kingdom", aid="900001"):
    html = (RFIX / "pistonheads.html").read_text(encoding="utf-8")
    # Rewrite the first advert into an in-scope car to drive the alert path with real page structure.
    html = html.replace('"headline":"Nissan Silvia S15 "', f'"headline":"{title}"', 1)
    html = html.replace('"id":"20375305"', f'"id":"{aid}"').replace('Advert:20375305', f'Advert:{aid}')
    html = html.replace('"price":30000,', f'"price":{price},', 1)
    return html


def test_new_match_alerts_once_and_price_drop_alerts_again():
    page1 = make_page("Nissan Silvia S15 Spec-R standard", 18500)
    p, db, sent, _ = make({"pistonheads": [(200, page1)]})
    p.sweep()
    assert len(sent.ntfy) == 1
    a = sent.ntfy[0]
    assert a["title"].startswith("New: S15, £18,500")
    assert "HPI" in a["message"] and a["click"].startswith("https://www.pistonheads.com/buy/listing/")
    assert "42 miles away" in a["message"]
    # Same car again: no repeat alert.
    p.sweep()
    assert len(sent.ntfy) == 1
    # Price drops 5%+: one price-drop alert.
    page2 = make_page("Nissan Silvia S15 Spec-R standard", 17000)
    p.fetcher_factory = lambda: FakeFetcher([(200, page2)])
    p.sweep()
    assert len(sent.ntfy) == 2 and sent.ntfy[1]["title"].startswith("Price drop")


def test_out_of_budget_and_flagged_cars_do_not_alert():
    p, db, sent, _ = make({"pistonheads": [(200, make_page("Nissan Silvia S15 Spec-R", 26000))]})
    p.sweep()
    assert sent.ntfy == []
    p2, _, sent2, _ = make({"pistonheads": [(200, make_page("Nissan 200SX S14a turbo Cat S", 12000, aid="900002"))]})
    p2.sweep()
    assert sent2.ntfy == []


def test_blocked_source_backs_off_and_others_keep_running():
    blocked = (FIX / "carandclassic_challenge.html").read_text(encoding="utf-8")
    ph = make_page("Nissan Silvia S15 Spec-R", 18000)
    p, db, sent, _ = make({"pistonheads": [(200, ph)], "gumtree": [(403, blocked)]})
    res = p.sweep()
    assert res.per_source["gumtree"].startswith("blocked")
    assert res.per_source["pistonheads"] >= 1
    assert health.is_paused(db, "gumtree")
    assert len(sent.ntfy) == 1  # the PistonHeads car still alerted
    # Circuit breaker after three blocks in a row.
    db.execute("UPDATE source_health SET paused_until=NULL WHERE source='gumtree'")
    p.sweep(only=["gumtree"])
    db.execute("UPDATE source_health SET paused_until=NULL WHERE source='gumtree'")
    p.sweep(only=["gumtree"])
    row = db.one("SELECT consecutive_blocks FROM source_health WHERE source='gumtree'")
    assert row["consecutive_blocks"] == 3
    label, detail = health.status_of(db.one("SELECT * FROM source_health WHERE source='gumtree'"), "verified", 20)
    assert label == "Needs attention"


def test_crashing_collector_is_isolated(monkeypatch):
    ph = make_page("Nissan Silvia S15 Spec-R", 18000)
    p, db, sent, _ = make({"pistonheads": [(200, ph)], "gumtree": lambda url: (_ for _ in ()).throw(RuntimeError("boom"))})
    res = p.sweep()
    assert res.per_source["gumtree"].startswith("error")
    assert len(sent.ntfy) == 1
    b = health.banner(db, p.recipes, p.enabled_sources(), 20)
    assert b["text"] == "1 of 2 sources working, 1 being repaired"


def test_same_plate_on_two_sources_is_one_car():
    p, db, sent, cfg = make({"pistonheads": [(200, "")]})
    p.recipes["gumtree"] = engine.load_recipes([ROOT / "recipes"])["gumtree"]
    raw = {"id": "a1", "url": "https://example.com/a1", "title": "Nissan 200SX S14a turbo, reg R123 ABC",
           "price": 13000, "year": 1998, "location": "Leicester"}
    p.process("pistonheads", raw)
    p.process("gumtree", dict(raw, id="b2", url="https://example.com/b2"))
    assert db.one("SELECT COUNT(*) n FROM cars")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM car_listings")["n"] == 2
    assert len(sent.ntfy) == 1  # one alert per canonical car


def test_noise_listings_never_become_cars():
    html = (FIX / "pistonheads_keyword.html").read_text(encoding="utf-8")
    p, db, sent, _ = make({"pistonheads": [(200, html)]})
    p.sweep()
    titles = [r["title"] for r in db.query(
        "SELECT li.title FROM cars c JOIN car_listings cl ON cl.car_id=c.id JOIN listings li ON li.id=cl.listing_id")]
    assert not any("Suzuki" in t or "Fiat" in t or "Hyundai" in t for t in titles)
