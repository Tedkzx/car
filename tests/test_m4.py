"""Milestone 4 acceptance tests: Japan export sources and landed cost (spec sections 8 and 12)."""
import json
from pathlib import Path

import pytest

from collectors import recipe_engine as engine
from core import config
from core.db import DB
from core.matcher import classify
from core.normalizer import normalise
from enrich.fx import FX
from enrich.landed_cost import landed_cost

from .test_pipeline import make

ROOT = Path(__file__).resolve().parent.parent
IMPORTS = config.DEFAULTS["imports"]


# ---- landed cost within £50 of a hand calculation on 3 sample cars -------------------------
@pytest.mark.parametrize("price,basis,cfg,hand", [
    # £10,000 FOB: CIF 11,600; duty 1,160; VAT 0.2 x 12,760 = 2,552; fees 1,110; prep 750
    (10000, "FOB", IMPORTS, 17172),
    # ¥2,325,000 at 200/£ = £11,625 FOB: CIF 13,225; duty 1,322.50; VAT 2,909.50; fees 1,110; prep 750
    (11625, "FOB", IMPORTS, 19317),
    # £12,000 CIF with an origin statement (0% duty): VAT 2,400; fees 1,110; prep 750. Freight not added twice
    (12000, "CIF", {**IMPORTS, "duty_rate": 0}, 16260),
])
def test_landed_cost_matches_hand_calculation(price, basis, cfg, hand):
    got = landed_cost(price, basis, cfg)
    assert abs(got["landed"] - hand) <= 50, got
    if basis == "CIF":
        assert got["freight"] == 0


class FakeFX:
    def to_gbp(self, amount, currency):
        per = {"JPY": 200.0, "USD": 1.25}[currency]
        return round(amount / per, 2), per, "2026-11-05"


def japan_pipeline(names, pages, cfg_over=None):
    p, db, sent, cfg = make({n: [(200, "")] for n in ["pistonheads"]}, cfg_over)
    recipes = engine.load_recipes([ROOT / "recipes"])
    p.recipes = {n: recipes[n] for n in names}
    p.fx = FakeFX()
    by_name = dict(zip(names, pages))
    current = {}

    from .conftest import FakeFetcher

    def fetcher_factory():
        return FakeFetcher(lambda url: (200, by_name[current["n"]]) if "page=" not in url else (200, "<html></html>"))

    p.fetcher_factory = fetcher_factory
    real = type(p)._run_source

    def run_source(name, res, browser):
        current["n"] = name
        return real(p, name, res, browser)

    p._run_source = run_source
    return p, db, sent, cfg


def test_goonet_real_page_imports_alert_only_inside_landed_budget():
    html = (ROOT / "recipes/fixtures/goonet.html").read_text(encoding="utf-8")
    p, db, sent, cfg = japan_pipeline(["goonet"], [html])
    res = p.sweep()
    assert res.per_source["goonet"] == 20
    rows = db.query("SELECT li.title, li.landed_cost_gbp, li.price_original, li.currency, c.in_scope, c.market "
                    "FROM cars c JOIN car_listings cl ON cl.car_id=c.id JOIN listings li ON li.id=cl.listing_id")
    assert all(r["currency"] == "JPY" and r["market"] == "jdm_overseas" for r in rows)
    scoped = [r for r in rows if r["in_scope"]]
    assert len(scoped) >= 5 and all("SPEC-R" in r["title"].upper() or "K" in r["title"].upper() for r in scoped)
    # Every alert is for an in-scope car whose landed estimate is inside £10-20k; none outside.
    in_budget = [r for r in scoped if 10000 <= r["landed_cost_gbp"] <= 20000]
    assert len(sent.ntfy) == len(in_budget)
    for a in sent.ntfy:
        assert "landed" in a["title"] and "NOVA" in a["message"] and "HPI" in a["message"]


def test_overseas_cars_outside_landed_budget_never_alert():
    html = (ROOT / "recipes/fixtures/goonet.html").read_text(encoding="utf-8")
    p, db, sent, cfg = japan_pipeline(["goonet"], [html], {"budget": {"min_gbp": 10000, "max_gbp": 11000}})
    p.sweep()
    assert sent.ntfy == []


def test_price_drop_on_import_uses_sellers_currency_not_exchange_rate():
    p, db, sent, cfg = japan_pipeline(["goonet"], [""])
    raw = {"id": "1", "url": "https://goo/1", "title": "NISSAN SILVIA SPEC-R", "price": "¥1,800,000",
           "location": "Kanagawa Japan", "attributes": "2001.03 WHITE 90,000 km"}
    p.process("goonet", raw)
    assert len(sent.ntfy) == 1
    p.fx = type("F", (), {"to_gbp": lambda self, a, c: (round(a / 180, 2), 180.0, "2026-11-06")})()  # yen strengthens
    p.process("goonet", raw)
    assert len(sent.ntfy) == 1  # same yen price: no "price drop" from the exchange rate
    p.process("goonet", dict(raw, price="¥1,600,000"))
    assert len(sent.ntfy) == 2 and "cut 11%" in sent.ntfy[1]["title"]


@pytest.mark.parametrize("name,minimum", [("goonet", 15), ("sbtjapan", 30), ("carfromjapan", 20)])
def test_japan_recipes_read_their_saved_real_pages(name, minimum):
    rec = engine.load_recipes([ROOT / "recipes"])[name]
    items = engine.extract(rec.fixture_path.read_text(encoding="utf-8"), rec.data, rec.data["fixture_url"])
    assert len(items) >= minimum
    for it in items:
        l = normalise(name, dict(it, currency=rec.data["currency"]), "japan")
        assert l.price_original and l.currency in ("JPY", "USD") and l.price_gbp is None
        assert l.url.startswith("https://") and all(i.startswith("https://") for i in l.image_urls)


def test_grade_r_is_spec_r_and_chassis_from_model_code():
    l = normalise("t", {"id": 1, "url": "u", "title": "1999 NISSAN SILVIA 2WD", "attributes": "Grade R | Manual",
                        "currency": "USD", "price": "US$ 18,000"}, "japan")
    m = classify(l, japan_source=True)
    assert (m.generation, m.engine_status, m.market) == ("S15", "factory_turbo", "jdm_overseas")
    l = normalise("t", {"id": 1, "url": "u", "title": "1997/3 NISSAN SILVIA",
                        "attributes": "S14 Model Code MT Transmission"}, "japan")
    assert classify(l, japan_source=True).generation in ("S14", "S14a")


def test_goonet_pages_through_its_form():
    rec = engine.load_recipes([ROOT / "recipes"])["goonet"]

    class F:
        def __init__(self):
            self.posts = []

        def get(self, url):
            return 200, (ROOT / "recipes/fixtures/goonet.html").read_text(encoding="utf-8"), url

        def post(self, url, data):
            self.posts.append(data)
            return 200, "<html></html>", url

    f = F()
    engine.run_recipe(rec, config.DEFAULTS, fetcher=f)
    assert f.posts[0] == {"offset": "20", "page": "2", "page_flag": "1"}


def test_fx_rates_are_cached_and_have_a_fallback():
    calls = []

    class C:
        def get(self, url):
            calls.append(url)

            class R:
                status_code = 200

                def json(self):
                    return {"rates": {"JPY": 210.0, "USD": 1.3}, "date": "2026-11-05"}

            return R()

    db = DB(":memory:")
    fx = FX(db, client=C())
    assert fx.to_gbp(2100000, "JPY") == (10000.0, 210.0, "2026-11-05")
    fx.to_gbp(1300, "USD")
    assert len(calls) == 1  # cached for the day

    class Down:
        def get(self, url):
            raise OSError("offline")

    gbp, rate, date = FX(DB(":memory:"), client=Down()).to_gbp(2130000, "JPY")
    assert gbp == 10000.0 and date == "approximate"
