"""One sweep: collect, normalise, match, merge, score, store, alert (spec section 6).

Every collector runs in isolation: one failing or blocked source never stops
the others, and every failure lands in source_health for the banner.

Milestone 1 merges cars that share a UK plate or chassis number. Image-hash
and probable-duplicate rules arrive in milestone 2.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from alerts.notifier import Notifier, car_alert
from collectors import recipe_engine as engine
from enrich.geo import Geo
from scoring.score import score as score_listing

from . import health
from .db import DB, dumps, loads, now
from .matcher import classify
from .models import Listing
from .normalizer import normalise

log = logging.getLogger(__name__)
REMOVED_AFTER = timedelta(hours=36)
PRICE_DROP = 0.05


@dataclass
class SweepResult:
    started: str
    finished: str = ""
    per_source: dict = field(default_factory=dict)  # name -> found count or error text
    new_listings: int = 0
    new_matches: int = 0
    alerts: int = 0


class Pipeline:
    def __init__(self, db: DB, cfg_getter: Callable[[], dict], notifier: Notifier, recipes: dict | None = None,
                 fetcher_factory: Callable[[], engine.HttpFetcher] | None = None, browser_factory=None,
                 geo: Geo | None = None):
        self.db = db
        self.cfg = cfg_getter
        self.notifier = notifier
        self.recipes = recipes if recipes is not None else engine.load_recipes()
        self.fetcher_factory = fetcher_factory or engine.HttpFetcher
        self.browser_factory = browser_factory
        self.geo = geo or Geo(db)
        self.progress: dict = {"running": False, "done": 0, "total": 0, "current": ""}

    # ------------------------------------------------------------------
    def enabled_sources(self) -> list[str]:
        cfg = self.cfg()
        disabled = set(cfg["sources"].get("disabled") or [])
        out = []
        for name, r in self.recipes.items():
            if name in disabled or not r.enabled_by_default:
                continue
            if name == "facebook" and not cfg["sources"].get("facebook"):
                continue
            out.append(name)
        return out

    def sweep(self, only: list[str] | None = None) -> SweepResult:
        res = SweepResult(started=now())
        names = [n for n in self.enabled_sources() if not only or n in only]
        self.progress = {"running": True, "done": 0, "total": len(names), "current": ""}
        browser = None
        try:
            for name in names:
                self.progress["current"] = self.recipes[name].display_name
                if health.is_paused(self.db, name):
                    res.per_source[name] = "paused"
                    self.progress["done"] += 1
                    continue
                if self.recipes[name].method == "browser" and browser is None and self.browser_factory:
                    browser = self.browser_factory()
                self._run_source(name, res, browser)
                self.progress["done"] += 1
        finally:
            if browser is not None:
                browser.close()
            self.progress.update(running=False, current="")
        res.finished = now()
        self.db.set_meta("last_sweep", res.finished)
        return res

    def _run_source(self, name: str, res: SweepResult, browser) -> None:
        recipe = self.recipes[name]
        cfg = self.cfg()
        t0 = time.monotonic()
        fetcher = self.fetcher_factory()
        raw_file = None

        def save_raw(source: str, html: str) -> str:
            nonlocal raw_file
            from . import paths

            raw_file = paths.data_dir() / "raw" / f"{source}.html"
            raw_file.write_text(html, encoding="utf-8", errors="replace")
            return str(raw_file)

        try:
            raws = engine.run_recipe(recipe, cfg, fetcher=fetcher, browser=browser, save_raw=save_raw)
        except engine.BlockedError as e:
            tripped = health.record_failure(self.db, name, str(e), blocked=True)
            res.per_source[name] = f"blocked: {e}"
            if tripped:
                self.notifier.maintainer(f"{recipe.display_name} paused for 6 hours",
                                         f"Blocked three times in a row: {e}. The recipe may need attention.")
            return
        except Exception as e:  # isolation: any failure stays inside this source
            msg = str(e) if isinstance(e, engine.CollectorError) else "Something went wrong reading this site"
            log.exception("collector %s failed", name)
            health.record_failure(self.db, name, msg, blocked=False)
            res.per_source[name] = f"error: {msg}"
            return
        finally:
            fetcher.close()
        seen_ids = []
        for raw in raws:
            try:
                lid = self.process(name, raw, res, raw_path=str(raw_file) if raw_file else None)
                if lid:
                    seen_ids.append(lid)
            except Exception:
                log.exception("could not process a listing from %s", name)
        health.record_success(self.db, name, len(raws), time.monotonic() - t0)
        res.per_source[name] = len(raws)
        if raws:
            self._mark_removed(name)

    # ------------------------------------------------------------------
    def process(self, source: str, raw: dict, res: SweepResult | None = None, raw_path: str | None = None) -> int | None:
        cfg = self.cfg()
        recipe = self.recipes.get(source)
        rdata = recipe.data if recipe else {}
        listing = normalise(source, raw, default_country=rdata.get("country"))
        if not listing.url:
            return None
        if rdata.get("location_filter") == "uk" and listing.country not in (None, "uk"):
            return None  # e.g. South African cars on Car & Classic
        if listing.currency and listing.currency.upper() != "GBP" and rdata.get("group") != "japan_export":
            return None  # foreign-priced listings outside the import flow (milestone 4)
        match = classify(listing, include_180sx=cfg["target"]["include_180sx"],
                         japan_source=rdata.get("group") == "japan_export")
        lid, is_new, old_price, relisted = self._upsert_listing(listing, raw_path)
        if res and is_new:
            res.new_listings += 1
        if match.excluded == "other_model":
            # Keep noise (Suzuki SX4 and the like) out of the cars table; the listing row stays for first-seen logic.
            return lid
        sc = score_listing(listing, match, cfg["scoring"]["weights"])
        car_id, car_is_new = self._car_for(lid, listing)
        flags = sc.hard_flags + sc.scam_flags
        self.db.execute(
            "UPDATE cars SET generation=?, engine_status=?, market=?, confidence=?, reasons=?, in_scope=?, mod_tier=?, "
            "score=?, score_reasons=?, flags=?, updated_at=? WHERE id=?",
            (match.generation, match.engine_status, match.market, match.confidence,
             dumps(match.reasons + ([f"Excluded: {match.excluded}"] if match.excluded else [])),
             int(match.in_scope), sc.mod_tier, sc.score, dumps(sc.reasons), dumps(flags), now(), car_id),
        )
        if match.in_scope and res and car_is_new:
            res.new_matches += 1
        if cfg["owner"].get("home_postcode") and listing.location_text and is_new:
            d = self.geo.distance(cfg["owner"]["home_postcode"], listing.location_text)
            if d is not None:
                self.db.execute("UPDATE listings SET distance_miles=? WHERE id=?", (d, lid))
        sent = self._maybe_alert(car_id, lid, match, sc, listing, is_new, old_price, relisted)
        if res:
            res.alerts += sent
        return lid

    def _upsert_listing(self, l: Listing, raw_path: str | None) -> tuple[int, bool, float | None, bool]:
        row = self.db.one("SELECT id, price_gbp, status FROM listings WHERE source=? AND source_id=?",
                          (l.source, l.source_id))
        ts = now()
        if row is None:
            cur = self.db.execute(
                "INSERT INTO listings(source, source_id, url, title, description, price_gbp, price_basis, location_text, "
                "postcode, year, reg, reg_confidence, chassis_no, mileage, colour, seller_type, image_urls, "
                "listed_estimate, first_seen, last_seen, status, raw_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (l.source, l.source_id, l.url, l.title, l.description, l.price_gbp, l.price_basis, l.location_text,
                 l.postcode, l.year, l.reg, l.reg_confidence, l.chassis_no, l.mileage, l.colour, l.seller_type,
                 dumps(l.image_urls), l.listed_estimate, ts, ts, "active", raw_path),
            )
            lid = cur.lastrowid
            self.db.execute("INSERT INTO price_history(listing_id, seen_at, price_gbp) VALUES (?,?,?)", (lid, ts, l.price_gbp))
            for u in l.image_urls[:6]:
                self.db.execute("INSERT OR IGNORE INTO images(listing_id, url) VALUES (?,?)", (lid, u))
            return lid, True, None, False
        lid = row["id"]
        relisted = row["status"] in ("removed", "sold")
        self.db.execute(
            "UPDATE listings SET url=?, title=?, description=COALESCE(NULLIF(?, ''), description), price_gbp=?, "
            "location_text=?, year=COALESCE(?, year), mileage=COALESCE(?, mileage), image_urls=?, last_seen=?, "
            "status=?, raw_path=COALESCE(?, raw_path) WHERE id=?",
            (l.url, l.title, l.description, l.price_gbp, l.location_text, l.year, l.mileage, dumps(l.image_urls), ts,
             "relisted" if relisted or row["status"] == "relisted" else "active", raw_path, lid),
        )
        if l.price_gbp != row["price_gbp"]:
            self.db.execute("INSERT INTO price_history(listing_id, seen_at, price_gbp) VALUES (?,?,?)", (lid, ts, l.price_gbp))
        return lid, False, row["price_gbp"], relisted

    def _car_for(self, lid: int, l: Listing) -> tuple[int, bool]:
        row = self.db.one("SELECT car_id FROM car_listings WHERE listing_id=?", (lid,))
        if row:
            return row["car_id"], False
        # Same UK plate or same chassis number on another listing: the same car.
        for col, val in (("reg", l.reg if l.reg_confidence >= 0.7 else None), ("chassis_no", l.chassis_no)):
            if not val:
                continue
            other = self.db.one(
                f"SELECT cl.car_id FROM listings li JOIN car_listings cl ON cl.listing_id=li.id "
                f"WHERE li.{col}=? AND li.id<>? LIMIT 1", (val, lid))
            if other:
                self.db.execute("INSERT INTO car_listings(car_id, listing_id) VALUES (?,?)", (other["car_id"], lid))
                return other["car_id"], False
        ts = now()
        cur = self.db.execute("INSERT INTO cars(created_at, updated_at) VALUES (?,?)", (ts, ts))
        self.db.execute("INSERT INTO car_listings(car_id, listing_id) VALUES (?,?)", (cur.lastrowid, lid))
        return cur.lastrowid, True

    def _mark_removed(self, source: str) -> None:
        cutoff = (datetime.now(timezone.utc) - REMOVED_AFTER).isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE listings SET status='removed' WHERE source=? AND status IN ('active','relisted') AND last_seen < ?",
            (source, cutoff),
        )

    # ------------------------------------------------------------------
    def _maybe_alert(self, car_id, lid, match, sc, listing: Listing, is_new, old_price, relisted) -> int:
        cfg = self.cfg()
        budget = cfg["budget"]
        price = listing.price_gbp
        in_budget = price is not None and budget["min_gbp"] <= price <= budget["max_gbp"]
        state = self.db.one("SELECT shortlist_state FROM cars WHERE id=?", (car_id,))["shortlist_state"]
        if state == "ignored":
            return 0
        eligible = (match.in_scope and in_budget and not sc.hard_flags and not sc.scam_flags
                    and sc.score >= cfg["alerts"]["alert_min_score"])
        if match.engine_status == "swapped_turbo" and not cfg["target"]["alert_swaps"]:
            eligible = False
        kind = None
        prev = self.db.one("SELECT type, price_at_send FROM alerts_sent WHERE car_id=? ORDER BY id DESC LIMIT 1", (car_id,))
        if prev is None and eligible:
            kind = "new"
        elif prev is not None and eligible and relisted:
            kind = "relisted"
        elif prev is not None and eligible and price and prev["price_at_send"] and price <= prev["price_at_send"] * (1 - PRICE_DROP):
            kind = "price_drop"
        if not kind:
            return 0
        lrow = dict(self.db.one("SELECT * FROM listings WHERE id=?", (lid,)))
        lrow["image_list"] = loads(lrow["image_urls"], [])
        car = dict(self.db.one("SELECT * FROM cars WHERE id=?", (car_id,)))
        car["flags_list"] = loads(car["flags"], [])
        sources = [dict(r) | {"source_name": self.recipes[r["source"]].display_name if r["source"] in self.recipes else r["source"]}
                   for r in self.db.query("SELECT li.source FROM car_listings cl JOIN listings li ON li.id=cl.listing_id "
                                          "WHERE cl.car_id=? ORDER BY li.first_seen", (car_id,))]
        alert = car_alert(kind, car, lrow, sources, old_price=prev["price_at_send"] if prev else old_price)
        self.notifier.send(alert)
        self.db.execute("INSERT INTO alerts_sent(car_id, type, sent_at, price_at_send) VALUES (?,?,?,?)",
                        (car_id, kind, now(), price))
        return 1
