"""Facebook Marketplace opt-in. The parser is checked against a SYNTHETIC page in Marketplace's known
tile shape (a real one needs a login); capture a real page with --capture facebook to verify."""
from fastapi.testclient import TestClient

from alerts.notifier import Notifier
from collectors import facebook
from collectors import recipe_engine as engine
from web.server import AppState, create_app

from .test_pipeline import make
from .test_web import FakeScheduler

SYNTHETIC = """<html><body><div role="main">
<a href="/marketplace/item/1234567890/?ref=search&amp;x=1"><div><img src="https://scontent.xx.fbcdn.net/a.jpg">
  <span>£13,995</span><span>1998 Nissan 200SX S14a turbo</span><span>Leeds, United Kingdom</span><span>98K miles</span></div></a>
<a href="/marketplace/item/1234567890/"><span>£13,995</span><span>duplicate tile</span></a>
<a href="/marketplace/item/2222222222/"><div><span>£2,500</span><span>SR20DET engine S14</span><span>Hull, United Kingdom</span></div></a>
<a href="/marketplace/item/3333333333/"><div><span>£16,500 £17,500</span><span>Nissan Silvia S15 Spec-R</span><span>Bristol</span></div></a>
</div></body></html>"""


def test_parse_marketplace_tiles_by_shape_not_class_names():
    items = facebook.parse_results(SYNTHETIC)
    assert [i["id"] for i in items] == ["1234567890", "2222222222", "3333333333"]
    a = items[0]
    assert a["title"] == "1998 Nissan 200SX S14a turbo" and a["price"] == "£13,995"
    assert a["location"] == "Leeds, United Kingdom" and a["mileage"] == "98K miles"
    assert a["url"] == "https://www.facebook.com/marketplace/item/1234567890/"
    assert a["images"] == ["https://scontent.xx.fbcdn.net/a.jpg"]


def test_login_and_checkpoint_pages_are_recognised():
    assert facebook.is_login_or_checkpoint("https://www.facebook.com/login/?next=x", "")
    assert facebook.is_login_or_checkpoint("https://www.facebook.com/checkpoint/1501092823525282/", "")
    assert facebook.is_login_or_checkpoint("https://www.facebook.com/marketplace/", '<form id="login_form">')
    assert not facebook.is_login_or_checkpoint("https://www.facebook.com/marketplace/search/?query=x", SYNTHETIC)


class FakeFB:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def search(self, terms, lo, hi):
        if self.error:
            raise self.error
        return self.result


def test_facebook_is_off_until_switched_on_then_cars_flow_through(monkeypatch):
    p, db, sent, cfg = make({"pistonheads": [(200, "")], "facebook": [(200, "")]})
    p.recipes["facebook"] = engine.load_recipes()["facebook"]
    assert "facebook" not in p.enabled_sources()  # off by default
    cfg["sources"]["facebook"] = True
    assert "facebook" in p.enabled_sources()
    monkeypatch.setattr(facebook, "FacebookBrowser", lambda: FakeFB(facebook.parse_results(SYNTHETIC)))
    res = p.sweep(force=True)
    assert res.per_source["facebook"] == 3
    titles = [a["title"] for a in sent.ntfy]
    assert any("S14a" in t for t in titles)  # the engine-only ad is dropped as parts
    assert db.one("SELECT seller_type FROM listings WHERE source='facebook' LIMIT 1")["seller_type"] == "private"


def test_expired_login_stops_and_tells_the_owner_once_a_day(monkeypatch):
    p, db, sent, cfg = make({"facebook": [(200, "")]})
    p.recipes["facebook"] = engine.load_recipes()["facebook"]
    cfg["sources"]["facebook"] = True
    monkeypatch.setattr(facebook, "FacebookBrowser", lambda: FakeFB(error=engine.BlockedError(facebook.NEEDS_LOGIN)))
    p.sweep(force=True)
    db.execute("UPDATE source_health SET paused_until=NULL")
    p.sweep(force=True)
    owner = [a for a in sent.ntfy if a["title"] == "Facebook needs you to log in"]
    assert len(owner) == 1 and "Settings" in owner[0]["message"]


def test_settings_show_warning_and_toggle(monkeypatch):
    p, db, sent, cfg = make({"pistonheads": [(200, "")]})
    p.recipes["facebook"] = engine.load_recipes()["facebook"]
    state = AppState(db, p, FakeScheduler(), Notifier(lambda: cfg, sent.ntfy_send, sent.toast_send))
    state._cfg["setup_complete"] = True
    c = TestClient(create_app(state))
    page = c.get("/settings").text
    assert "could restrict the account" in page and "Log in to Facebook" in page
    c.post("/settings", data={"facebook_on": "on", "src_pistonheads": "on"})
    assert state.cfg()["sources"]["facebook"] is True and "facebook" not in state.cfg()["sources"]["disabled"]
