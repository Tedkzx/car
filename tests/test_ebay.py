"""eBay through the official Browse API (no scraping, no detection workarounds)."""
import json

from fastapi.testclient import TestClient

from collectors import ebay_api
from collectors import recipe_engine as engine
from core import config, health

from .test_pipeline import ROOT, make

SAMPLE = {"itemSummaries": [
    {"itemId": "v1|123456789|0", "title": "Nissan 200SX S14a 2.0 Turbo 1998 standard",
     "price": {"value": "13995.00", "currency": "GBP"}, "itemWebUrl": "https://www.ebay.co.uk/itm/123456789",
     "image": {"imageUrl": "https://i.ebayimg.com/a.jpg"}, "additionalImages": [{"imageUrl": "https://i.ebayimg.com/b.jpg"}],
     "itemLocation": {"postalCode": "LE1", "country": "GB"}, "condition": "Used", "buyingOptions": ["FIXED_PRICE"],
     "seller": {"username": "someone", "feedbackScore": 10}},
    {"itemId": "v1|987|0", "title": "SR20DET engine S14", "price": {"value": "2500.00", "currency": "GBP"},
     "itemWebUrl": "https://www.ebay.co.uk/itm/987", "itemLocation": {"country": "GB"}},
]}


class FakeEbay:
    def __init__(self, token_status=200, search_status=200):
        self.token_status, self.search_status = token_status, search_status
        self.calls = []

    def post(self, url, data=None, headers=None):
        self.calls.append(("post", url, headers))

        class R:
            status_code = self.token_status

            def json(self):
                return {"access_token": "tok", "expires_in": 7200}

        return R()

    def get(self, url, params=None, headers=None):
        self.calls.append(("get", url, params, headers))

        class R:
            status_code = self.search_status

            def json(self):
                return SAMPLE

        return R()


def test_browse_api_request_and_mapping():
    ebay_api.EbayBrowse._token_cache.clear()
    http = FakeEbay()
    items = ebay_api.EbayBrowse("app-id", "cert-id", client=http).search("nissan 200sx", 8000, 25000)
    post, get = http.calls
    assert post[1] == ebay_api.TOKEN_URL and post[2]["Authorization"].startswith("Basic ")
    assert get[2]["category_ids"] == "9801" and "itemLocationCountry:GB" in get[2]["filter"]
    assert get[3]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB" and get[3]["Authorization"] == "Bearer tok"
    assert items[0]["url"] == "https://www.ebay.co.uk/itm/123456789" and items[0]["price"] == "13995.00"
    assert items[0]["images"] == ["https://i.ebayimg.com/a.jpg", "https://i.ebayimg.com/b.jpg"]
    assert items[0]["location"] == "LE1, United Kingdom"
    assert "someone" not in json.dumps(items)  # seller details are never read


def test_bad_key_gives_a_plain_message():
    ebay_api.EbayBrowse._token_cache.clear()
    try:
        ebay_api.EbayBrowse("x", "y", client=FakeEbay(token_status=401)).search("a", 1, 2)
    except engine.CollectorError as e:
        assert "did not accept the key" in str(e)
    else:
        raise AssertionError("expected an error")


def test_without_a_key_ebay_shows_off_not_a_fault(monkeypatch):
    monkeypatch.setattr(ebay_api, "get_keys", lambda: (None, None))
    p, db, sent, cfg = make({"pistonheads": [(200, "")], "ebay": [(200, "")]})
    res = p.sweep(force=True)
    assert res.per_source["ebay"] == "off"
    b = health.banner(db, p.recipes, p.enabled_sources(), 20)
    ebay = next(s for s in b["sources"] if s["name"] == "ebay")
    assert ebay["label"] == "Off" and "free eBay key" in ebay["detail"]
    assert b["text"] == "1 of 1 sources working"  # an Off source is not counted as a fault


def test_with_a_key_ebay_cars_flow_through_the_matcher_and_alert(monkeypatch):
    ebay_api.EbayBrowse._token_cache.clear()
    monkeypatch.setattr(ebay_api, "get_keys", lambda: ("app-id", "cert-id"))
    http = FakeEbay()
    real = ebay_api.EbayBrowse.__init__
    monkeypatch.setattr(ebay_api.EbayBrowse, "__init__", lambda self, a, b, client=None: real(self, a, b, client=http))
    p, db, sent, cfg = make({"ebay": [(200, "")]})
    res = p.sweep(force=True)
    assert res.per_source["ebay"] == 2
    assert len(sent.ntfy) == 1 and "S14a" in sent.ntfy[0]["title"]  # the engine-only ad is dropped as parts


def test_settings_saves_key_in_credential_store(monkeypatch):
    saved = {}
    monkeypatch.setattr(ebay_api, "set_keys", lambda a, b: saved.update(id=a, secret=b) or True)
    monkeypatch.setattr(ebay_api, "get_keys", lambda: (saved.get("id"), saved.get("secret")))
    from alerts.notifier import Notifier
    from web.server import AppState, create_app

    from .test_web import FakeScheduler

    p, db, sent, cfg = make({"pistonheads": [(200, "")]})
    state = AppState(db, p, FakeScheduler(), Notifier(lambda: cfg, sent.ntfy_send, sent.toast_send))
    state._cfg["setup_complete"] = True
    c = TestClient(create_app(state))
    assert "developer.ebay.com" in c.get("/settings").text
    c.post("/settings", data={"ebay_client_id": " APPID-123 ", "ebay_client_secret": "CERT-456", "src_pistonheads": "on"})
    assert saved == {"id": "APPID-123", "secret": "CERT-456"}
    page = c.get("/settings").text
    assert "An eBay key is saved (APPID-" in page and "CERT-456" not in page
    assert "CERT-456" not in (config.paths.config_path().read_text(encoding="utf-8"))
