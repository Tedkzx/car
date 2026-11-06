import json
from pathlib import Path

import pytest

from collectors import recipe_engine as engine
from core import config, normalizer
from core.matcher import classify
from core.scheduler import Scheduler
from enrich import plate_extract

from .conftest import FIX, FakeFetcher
from .labelled_eval import evaluate

ROOT = Path(__file__).resolve().parent.parent


# ---- matcher: the acceptance bar for milestone 1 -------------------------
def test_matcher_meets_precision_and_recall_on_labelled_set():
    r = evaluate()
    assert r["n"] >= 150
    assert r["precision"] >= 0.95, r["misses"]
    assert r["recall"] >= 0.90, r["misses"]
    assert r["never_alert_hits"] == []


def test_matcher_on_holdout_set():
    rows = [json.loads(l) for l in (ROOT / "tests/labelled/holdout.jsonl").read_text().splitlines() if l]
    r = evaluate(rows)
    assert r["precision"] >= 0.95 and r["recall"] >= 0.90, r["misses"]


@pytest.mark.parametrize("title,gen,engine_status,in_scope", [
    ("Nissan 200SX S13 SR20DET", "S13", "swapped_turbo", False),
    ("Nissan Silvia S15 Spec-S", "S15", "na", False),
    ("Nissan Silvia S15 Spec-R", "S15", "factory_turbo", True),
    ("Nissan 200SX", "S14a", "factory_turbo", True),  # with year 1998 below
])
def test_matcher_examples(title, gen, engine_status, in_scope):
    l = normalizer.normalise("t", {"id": 1, "url": "u", "title": title, "year": 1998 if title == "Nissan 200SX" else None})
    m = classify(l)
    assert (m.generation, m.engine_status, m.in_scope) == (gen, engine_status, in_scope)
    assert m.reasons


def test_180sx_setting():
    l = normalizer.normalise("t", {"id": 1, "url": "u", "title": "Nissan 180SX Type X SR20DET", "year": 1996})
    assert classify(l).excluded == "excluded_180sx"
    assert classify(l, include_180sx=True).excluded is None


# ---- normaliser and plates -----------------------------------------------
def test_normaliser_parses_prices_years_mileage_and_country():
    l = normalizer.normalise("t", {"id": 1, "url": "u", "title": "Nissan S14", "price": "£ 13,995",
                                   "attributes": "1997 | 98,000 miles | Private", "location": "Cape Town, South Africa"})
    assert l.price_gbp == 13995 and l.year == 1997 and l.mileage == 98000
    assert l.seller_type == "private" and l.country == "south africa"
    assert normalizer.parse_money("POA") is None and normalizer.parse_money("12.5k") == 12500


def test_plate_extraction():
    assert plate_extract.extract("Nissan 200SX, reg R123 ABC, MOT")[0] == "R123ABC"
    assert plate_extract.extract("2001 Silvia on Y12 ABC plates")[0] == "Y12ABC"
    assert plate_extract.extract("Nissan Silvia S15 Spec R")[0] is None
    # Engine codes fit the plate pattern but are not plates (this merged four cars once).
    for text in ("1994 Nissan Silvia S14 200SX SR20DET", "CA18DET swap", "RB25DET", "1JZ GTE", "SR20 DET"):
        assert plate_extract.extract(text)[0] is None, text


# ---- recipes -------------------------------------------------------------
def test_verified_recipes_pass_self_test_on_saved_real_pages():
    recipes = engine.load_recipes([ROOT / "recipes"])
    for name in ("pistonheads", "gumtree"):
        ok, msg = engine.self_test(recipes[name])
        assert ok, f"{name}: {msg}"


def test_recipe_fields_from_real_pages():
    recipes = engine.load_recipes([ROOT / "recipes"])
    ph = engine.extract((ROOT / "recipes/fixtures/pistonheads.html").read_text(), recipes["pistonheads"].data)
    first = ph[0]
    assert first["url"].startswith("https://www.pistonheads.com/buy/listing/")
    assert first["price"] and first["year"] and first["location"].endswith("United Kingdom")
    assert "name" not in first and "phone" not in first  # seller details never read
    gt = engine.extract((ROOT / "recipes/fixtures/gumtree.html").read_text(), recipes["gumtree"].data,
                        "https://www.gumtree.com/search")
    assert gt[0]["id"] == "1515568009" and "3,900" in gt[0]["price"] and "Twickenham" in gt[0]["location"]
    assert ".css-" not in gt[0]["price"]  # inline styles stripped


def test_challenge_and_block_pages_are_detected_not_bypassed():
    cc = (FIX / "carandclassic_challenge.html").read_text()
    eb = (FIX / "ebay_blocked.html").read_text()
    assert engine.detect_block(403, cc)
    assert engine.detect_block(403, eb)
    recipes = engine.load_recipes([ROOT / "recipes"])
    with pytest.raises(engine.BlockedError):
        engine.run_recipe(recipes["carandclassic"], config.DEFAULTS, browser=FakeFetcher([(403, cc)]))
    # A browser page that still shows a check after loading stops the collector too.
    for f in ("autotrader_cloudflare.html", "cargurus_verification.html", "cazoo_vercel.html", "theparking_challenge.html"):
        with pytest.raises(engine.BlockedError):
            engine.run_recipe(recipes["autotrader"], config.DEFAULTS, browser=FakeFetcher([(200, (FIX / f).read_text())]))
    with pytest.raises(engine.BlockedError):
        engine.run_recipe(recipes["pistonheads"], config.DEFAULTS, fetcher=FakeFetcher([(429, "slow down")]))


def test_unverified_recipes_have_no_fixture_yet_and_say_so():
    recipes = engine.load_recipes([ROOT / "recipes"])
    for name in ("ebay", "carandclassic"):
        assert recipes[name].status == "unverified"
        assert engine.self_test(recipes[name])[0] is False


def test_downloaded_recipe_overrides_shipped_one(tmp_path):
    (tmp_path / "pistonheads.yaml").write_text("name: pistonheads\ndisplay_name: PH fixed\nstatus: verified\n")
    recipes = engine.load_recipes([tmp_path, ROOT / "recipes"])
    assert recipes["pistonheads"].display_name == "PH fixed" and "gumtree" in recipes


def test_search_urls_expand_terms_and_prices():
    recipes = engine.load_recipes([ROOT / "recipes"])
    urls = engine.search_urls(recipes["ebay"].data, config.DEFAULTS)
    assert any("_udlo=8000" in u and "_udhi=25000" in u and "nissan+200sx" in u for u in urls)


def test_ebay_recipe_reads_current_card_markup():
    """eBay's markup as documented, not a saved page: a check of the recipe wiring only."""
    html = """<ul><li class="s-card"><a class="su-link" href="https://www.ebay.co.uk/itm/1234567890?x=1">
      <div class="s-card__title"><span class="su-styled-text">Nissan 200SX S14a Turbo 1998</span></div></a>
      <span class="s-card__price">£12,995.00</span><img class="s-card__image" src="https://i.ebayimg.com/a.jpg"></li></ul>"""
    recipes = engine.load_recipes([ROOT / "recipes"])
    items = engine.extract(html, recipes["ebay"].data)
    assert items[0]["id"] == "1234567890" and items[0]["title"].startswith("Nissan 200SX")


# ---- config and scheduler ------------------------------------------------
def test_config_creates_long_random_topic_and_round_trips(tmp_path):
    p = tmp_path / "c.yaml"
    cfg = config.load(p)
    assert len(cfg["alerts"]["ntfy_topic"]) >= 20
    cfg["budget"]["max_gbp"] = 19000
    config.save(cfg, p)
    assert config.load(p)["budget"]["max_gbp"] == 19000
    assert config.load(p)["alerts"]["ntfy_topic"] == cfg["alerts"]["ntfy_topic"]


def test_scheduler_jitter_and_missed_run_detection():
    t = [1_800_000_000.0]
    s = Scheduler(lambda: None, lambda: 20, lambda: 20, lambda: "2027-01-15T08:00:00+00:00", clock=lambda: t[0])
    delays = [s.next_delay() for _ in range(200)]
    assert min(delays) >= 16 * 60 and max(delays) <= 24 * 60
    t[0] = 1_800_000_000.0 + 3 * 3600  # long after the last sweep
    from datetime import datetime

    last = datetime.fromtimestamp(1_800_000_000.0).astimezone().isoformat()
    s2 = Scheduler(lambda: None, lambda: 20, lambda: 20, lambda: last, clock=lambda: t[0])
    assert s2.missed_runs() is True
    s3 = Scheduler(lambda: None, lambda: 20, lambda: 20, lambda: None)
    assert s3.missed_runs() is True
