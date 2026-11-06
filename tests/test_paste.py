"""Milestone 5: the "Paste a link" box."""
from fastapi.testclient import TestClient

from alerts.notifier import Notifier
from collectors import pasted
from collectors import recipe_engine as engine
from web.server import AppState, create_app

from .conftest import FIX
from .test_pipeline import make
from .test_web import FakeScheduler


def client(monkeypatch, page=None, status=200):
    p, db, sent, cfg = make({"pistonheads": [(200, "")]})
    p.recipes.update({k: v for k, v in engine.load_recipes().items() if k in ("goonet", "autotrader")})
    if page is not None:
        monkeypatch.setattr(engine.HttpFetcher, "get", lambda self, url: (status, page, url))
    state = AppState(db, p, FakeScheduler(), Notifier(lambda: cfg, sent.ntfy_send, sent.toast_send))
    state._cfg["setup_complete"] = True
    return TestClient(create_app(state)), db, sent


def test_real_pistonheads_ad_is_read_and_judged_without_alerting(monkeypatch):
    html = (FIX / "pistonheads_ad.html").read_text(encoding="utf-8")
    c, db, sent = client(monkeypatch, html)
    assert "Paste the link" in c.get("/").text
    r = c.post("/paste", data={"url": "https://www.pistonheads.com/buy/listing/21285998"}).text
    assert "£19,000" in r and "SILVIA" in r.upper()
    li = db.one("SELECT source, price_gbp, description FROM listings")
    assert li["source"] == "pistonheads" and li["price_gbp"] == 19000 and "<p>" not in li["description"]
    assert sent.ntfy == []  # the owner found it: no alert


def test_blocked_site_asks_the_owner_for_title_and_price(monkeypatch):
    challenge = (FIX / "autotrader_cloudflare.html").read_text(encoding="utf-8")
    c, db, sent = client(monkeypatch, challenge, status=403)
    url = "https://www.autotrader.co.uk/car-details/202611060000001"
    r = c.post("/paste", data={"url": url}).text
    assert "will not let the tracker read the page" in r and 'name="title"' in r
    r = c.post("/paste", data={"url": url, "title": "1998 Nissan 200SX S14a 2.0 Turbo, standard",
                               "price": "£13,995", "details": "2 owners, full history, cambelt done"}).text
    assert "Match, inside your budget" in r and "Add to shortlist" in r
    assert db.one("SELECT source FROM listings")["source"] == "autotrader"
    assert sent.ntfy == []


def test_pasted_import_gets_landed_cost_and_non_matches_say_why(monkeypatch):
    c, db, sent = client(monkeypatch)
    r = c.post("/paste", data={"url": "https://www.goo-net-exchange.com/usedcars/NISSAN/SILVIA/1/",
                               "title": "NISSAN SILVIA SPEC-R", "price": "¥1,800,000"}).text
    assert "landed" in r
    r = c.post("/paste", data={"url": "https://example.com/x", "title": "Nissan Silvia S15 Spec-S auto", "price": "9000"}).text
    assert "Not a match" in r and "Non-turbo" in r
    assert pasted.source_for("https://example.com/x", {}) == "pasted"
