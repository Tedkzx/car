"""Milestone 2 acceptance tests (spec section 12)."""
import io
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from collectors import recipe_engine as engine
from core import config, health, paths
from core.db import DB
from core.updater import Updater, build_manifest, sha256

from .conftest import FIX, FakeFetcher
from .test_pipeline import make
from .test_web import FakeScheduler

ROOT = Path(__file__).resolve().parent.parent


def png(seed: int) -> bytes:
    im = Image.new("RGB", (256, 192), (240, 240, 240))
    d = ImageDraw.Draw(im)
    for i in range(12):
        x = (seed * 37 + i * 53) % 220
        y = (seed * 17 + i * 29) % 160
        d.rectangle((x, y, x + 30, y + 25), fill=((seed * 50 + i * 20) % 255, (i * 40) % 255, (seed * 90) % 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def raw(id_, url, title, price, images, **kw):
    return {"id": id_, "url": url, "title": title, "price": price, "images": images, **kw}


# ---- the same car from two or more sources shows as one car with every link -----------------
def test_photo_match_merges_cars_across_sources():
    p, db, sent, _ = make({"pistonheads": [(200, "")]})
    p.recipes.update({k: v for k, v in engine.load_recipes([ROOT / "recipes"]).items() if k in ("gumtree", "exchangeandmart")})
    imgs = {"https://a/1.jpg": png(1), "https://a/2.jpg": png(2), "https://b/1.jpg": png(1), "https://b/2.jpg": png(2),
            "https://c/1.jpg": png(7), "https://c/2.jpg": png(8)}
    p.image_fetcher = imgs.get
    p.process("pistonheads", raw("1", "https://ph/1", "Nissan Silvia S15 Spec-R", 18000, ["https://a/1.jpg", "https://a/2.jpg"], year=2000))
    p.process("gumtree", raw("2", "https://gt/2", "S15 Spec R silvia", 17950, ["https://b/1.jpg", "https://b/2.jpg"], year=2000))
    p.process("exchangeandmart", raw("3", "https://em/3", "Nissan Silvia S15 Spec-R", 18200, ["https://c/1.jpg", "https://c/2.jpg"], year=2001))
    cars = db.query("SELECT car_id, COUNT(*) n FROM car_listings GROUP BY car_id ORDER BY n DESC")
    assert [c["n"] for c in cars] == [2, 1]  # first two merged by photos, third is a different car
    assert len(sent.ntfy) == 2  # one alert per canonical car


def test_probable_duplicate_is_asked_not_merged_and_owner_can_merge():
    p, db, sent, cfg = make({"pistonheads": [(200, "")]})
    p.recipes.update({k: v for k, v in engine.load_recipes([ROOT / "recipes"]).items() if k == "gumtree"})
    p.process("pistonheads", raw("1", "https://ph/1", "Nissan 200SX S14a turbo", 13000, [], year=1998,
                                 colour="Black", location="Leicester, United Kingdom", mileage=90500))
    p.process("gumtree", raw("2", "https://gt/2", "Nissan 200sx turbo 1998", 12500, [], year=1998,
                             colour="black", location="Leicester", mileage=91000))
    assert db.one("SELECT COUNT(*) n FROM cars")["n"] == 2
    dup = db.one("SELECT * FROM dup_candidates")
    assert dup and dup["state"] == "pending"
    from alerts.notifier import Notifier
    from web.server import AppState, create_app

    state = AppState(db, p, FakeScheduler(), Notifier(lambda: cfg, lambda *a, **k: True, lambda *a, **k: True))
    state._cfg["setup_complete"] = True
    c = TestClient(create_app(state))
    page = c.get("/duplicates").text
    assert "Is this the same car?" in page and "Same car?" in c.get("/").text
    c.post(f"/duplicates/{dup['id']}/merge")
    assert db.one("SELECT COUNT(*) n FROM cars")["n"] == 1
    card = c.get("/?view=all").text
    assert "Open on PistonHeads" in card and "Open on Gumtree" in card


# ---- a fixed recipe pushed to the repository reaches a running install ----------------------
class FakeHTTP:
    def __init__(self, files):
        self.files = files

    def get(self, url, **kw):
        class R:
            pass

        r = R()
        body = self.files.get(url)
        r.status_code = 200 if body is not None else 404
        r.content = body if isinstance(body, bytes) else (body or "").encode()
        r.text = r.content.decode()
        r.json = lambda: json.loads(r.text)
        return r


def test_recipe_hot_update_downloads_checked_files_only():
    base = "https://raw.githubusercontent.com/Tedkzx/car/main/recipes/"
    fixed = (ROOT / "recipes/gumtree.yaml").read_text(encoding="utf-8").replace("display_name: Gumtree", "display_name: Gumtree (fixed)").encode()
    evil = b"name: x\n"
    manifest = {"files": {"gumtree.yaml": sha256(fixed), "pistonheads.yaml": "0" * 64, "../app.py": sha256(evil)}}
    http = FakeHTTP({base + "manifest-v3.json": json.dumps(manifest), base + "gumtree.yaml": fixed,
                     base + "pistonheads.yaml": b"tampered", base + "../app.py": evil})
    db = DB(":memory:")
    up = Updater(db, lambda: config.DEFAULTS, "0.2.0", client=http)
    assert up.update_recipes() == 1  # gumtree only: pistonheads failed its checksum, ../app.py is not a recipe path
    recipes = engine.load_recipes()
    assert recipes["gumtree"].display_name == "Gumtree (fixed)"
    assert recipes["pistonheads"].display_name == "PistonHeads"
    assert not (paths.data_dir() / "app.py").exists()
    assert "1 updated" in db.get_meta("update_status_recipes")


def test_unreachable_update_source_is_reported_not_fatal():
    db = DB(":memory:")
    up = Updater(db, lambda: config.DEFAULTS, "0.2.0", client=FakeHTTP({}))
    assert up.update_recipes() == 0
    assert "not reachable" in db.get_meta("update_status_recipes")


def test_manifests_are_up_to_date():
    for name, version in (("manifest-v3.json", None), ("manifest.json", "0.2.0")):
        saved = json.loads((ROOT / "recipes" / name).read_text(encoding="utf-8"))
        assert saved == build_manifest(ROOT / "recipes", version), "run: python tools/make_manifest.py"


def test_old_app_versions_never_receive_recipes_they_cannot_handle():
    """0.2.0 read ¥ and US$ prices as pounds; the Japan recipes must not reach it."""
    legacy = json.loads((ROOT / "recipes/manifest.json").read_text(encoding="utf-8"))["files"]
    for name in ("goonet.yaml", "sbtjapan.yaml", "carfromjapan.yaml", "fixtures/goonet.html"):
        assert name not in legacy
    v3 = json.loads((ROOT / "recipes/manifest-v3.json").read_text(encoding="utf-8"))
    assert v3["requires"]["goonet.yaml"] == "0.3.0"
    # The 0.3.0 updater skips files that need a newer app, and the loader ignores them too.
    base = "https://raw.githubusercontent.com/Tedkzx/car/main/recipes/"
    future = b"name: future\nmin_app_version: 9.0.0\nsearches: [x]\nextract: []\n"
    http = FakeHTTP({base + "manifest-v3.json": json.dumps({"files": {"future.yaml": sha256(future)},
                                                             "requires": {"future.yaml": "9.0.0"}}),
                     base + "future.yaml": future})
    assert Updater(DB(":memory:"), lambda: config.DEFAULTS, "0.3.0", client=http).update_recipes() == 0
    (paths.data_dir() / "recipes" / "future.yaml").write_bytes(future)
    assert "future" not in engine.load_recipes()


# ---- scheduling, health -----------------------------------------------------------------------
def test_browser_sources_respect_their_minimum_interval():
    p, db, sent, _ = make({"pistonheads": [(200, "")], "carandclassic": [(200, "")]})
    browser = FakeFetcher([(200, "<html><body><ul></ul></body></html>")])
    p.browser_factory = lambda: browser
    p.sweep()
    p.sweep()
    assert len(browser.urls) == len(engine.search_urls(p.recipes["carandclassic"].data, config.DEFAULTS))  # ran once only
    res = p.sweep(force=True)
    assert res.per_source["carandclassic"] == 0


def test_verified_source_finding_nothing_repeatedly_needs_attention():
    p, db, sent, cfg = make({"pistonheads": [(200, "<html></html>")]})
    for _ in range(health.ZERO_RUNS_ALERT):
        p.sweep()
    b = health.banner(db, p.recipes, p.enabled_sources(), 20)
    assert b["sources"][0]["label"] == "Needs attention"


def test_milestone1_database_upgrades_in_place(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE source_health (source TEXT PRIMARY KEY, last_success TEXT, last_error TEXT, "
                      "last_error_at TEXT, found_count INTEGER, consecutive_blocks INTEGER, paused_until TEXT, "
                      "last_duration_s REAL); INSERT INTO source_health(source) VALUES ('gumtree');")
    con.commit()
    con.close()
    db = DB(path)
    cols = {r[1] for r in db.query("PRAGMA table_info(source_health)")}
    assert {"last_attempt", "zero_runs", "selftest_error"} <= cols
    assert db.one("SELECT source FROM source_health")["source"] == "gumtree"


# ---- new strategies and sources -----------------------------------------------------------------
def test_rss_feed_and_sold_detection_on_real_forum_feed():
    rec = engine.load_recipes([ROOT / "recipes"])["350z-uk"]
    xml = rec.fixture_path.read_text(encoding="utf-8")
    items = engine._rss(xml, {"type": "rss"})
    assert len(items) == 25 and all(i["url"].startswith("https://www.350z-uk.com/") for i in items)
    from core.matcher import classify
    from core.normalizer import normalise

    l = normalise("t", {"id": 1, "url": "u", "title": "[SOLD] Nissan 200SX S14 turbo", "year": 1997})
    assert classify(l).excluded == "sold"
    l = normalise("t", {"id": 1, "url": "u", "title": "Nissan 200SX S14 turbo", "sale_status": "Sold", "year": 1997})
    assert classify(l).excluded == "sold"
    l = normalise("t", {"id": 1, "url": "u", "title": "My 200SX", "description": "S14a turbo, 1998. Asking £12,750 ono"})
    assert l.price_gbp == 12750


def test_real_importer_page_finds_the_in_scope_cars():
    from core.matcher import classify
    from core.normalizer import normalise

    rec = engine.load_recipes([ROOT / "recipes"])["japimportsuk"]
    items = engine.extract(rec.fixture_path.read_text(encoding="utf-8"), rec.data, "")
    scoped = [i["title"] for i in items if classify(normalise("japimportsuk", i, "uk")).in_scope]
    assert len(scoped) == 3 and all("SR20DET" in t for t in scoped)


def test_auto_strategy_reads_jsonld_and_embedded_json():
    ld = """<script type="application/ld+json">{"@type":"ItemList","itemListElement":[{"@type":"ListItem","item":
      {"@type":"Car","name":"Nissan 200SX S14a","url":"https://x/car-details/123","vehicleModelDate":"1998",
       "offers":{"price":"12995","priceCurrency":"GBP"}}}]}</script>"""
    items = engine.extract(ld, {"extract": [{"type": "auto", "id_regex": r"/car-details/(\d+)"}]})
    assert items[0]["title"] == "Nissan 200SX S14a" and items[0]["price"] == "12995"
    nxt = {"props": {"pageProps": {"results": {"listings": [
        {"advertId": "9", "title": "Nissan Silvia S15 Spec-R", "price": {"value": 18500}, "location": {"name": "Leeds"},
         "images": [{"url": "https://img/1.jpg"}]},
        {"advertId": "10", "title": "Nissan 200SX", "price": {"value": 9000}, "images": []}]}}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nxt)}</script>'
    items = engine.extract(html, {"extract": [{"type": "auto", "url_template": "https://x/ad/{id}"}]})
    assert [i["url"] for i in items] == ["https://x/ad/9", "https://x/ad/10"]
    assert items[0]["price"] == 18500 and items[0]["location"] == "Leeds" and items[0]["images"] == ["https://img/1.jpg"]


def test_every_recipe_loads_and_has_a_method():
    recipes = engine.load_recipes([ROOT / "recipes"])
    assert len(recipes) >= 12
    for r in recipes.values():
        assert r.method in ("http", "browser", "api") and r.data.get("searches") and r.data.get("extract")
        ok, msg = engine.self_test(r)
        if r.status == "verified":
            assert ok, f"{r.name}: {msg}"
