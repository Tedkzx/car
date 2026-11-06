"""The local dashboard and setup page: FastAPI, plain HTML and HTMX (spec section 10).

Large text, one screen, no jargon. Only listens on 127.0.0.1.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from alerts.notifier import GEN_NAMES, MARKET_NAMES, TIER_NAMES
from core import config as config_mod
from core import health, paths, winsys
from core import dedupe
from core.db import DB, loads

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
VIEWS = {
    "new": "New",
    "check": "Needs a look",
    "shortlist": "Shortlist",
    "all": "All matches, any price",
    "japan": "In Japan, any price",
    "removed": "Removed and sold",
}
MAIN_VIEWS = ("new", "check", "shortlist")
MOT_URL = "https://www.check-mot.service.gov.uk/results?registration={reg}"  # confirm by hand: spec section 15


class AppState:
    def __init__(self, db: DB, pipeline, scheduler, notifier, cfg_path: Path | None = None):
        self.db = db
        self.pipeline = pipeline
        self.scheduler = scheduler
        self.notifier = notifier
        self.cfg_path = cfg_path
        self._cfg = config_mod.load(cfg_path)

    def cfg(self) -> dict:
        return self._cfg

    def save(self, cfg: dict) -> None:
        self._cfg = cfg
        config_mod.save(cfg, self.cfg_path)


def _ago(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        d = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if d < timedelta(minutes=1):
        return "just now"
    if d < timedelta(hours=1):
        return f"{int(d.total_seconds() // 60)} min ago"
    if d < timedelta(days=1):
        return f"{int(d.total_seconds() // 3600)} h ago"
    return f"{d.days} days ago"


SPECIAL_SOURCES = {"pasted": "Pasted link"}


def source_name(recipes: dict, name: str) -> str:
    return recipes[name].display_name if name in recipes else SPECIAL_SOURCES.get(name, name)


def car_cards(db: DB, recipes: dict, view: str, budget: dict) -> list[dict]:
    where = {
        "new": "c.in_scope=1 AND c.shortlist_state='new'",
        "japan": "c.market='jdm_overseas' AND c.shortlist_state<>'ignored' AND c.reasons NOT LIKE '%Excluded:%'",
        "check": "c.in_scope=0 AND c.engine_status='unknown' AND c.shortlist_state<>'ignored' "
                 "AND c.reasons NOT LIKE '%Excluded:%'",
        "shortlist": "c.shortlist_state='shortlisted'",
        "all": "c.shortlist_state<>'ignored' AND c.reasons NOT LIKE '%Excluded:%'",
        "removed": "1=1",
    }[view]
    status = "IN ('removed','sold')" if view == "removed" else "IN ('active','relisted')"
    rows = db.query(
        f"SELECT c.*, MIN(li.first_seen) AS first_seen FROM cars c JOIN car_listings cl ON cl.car_id=c.id "
        f"JOIN listings li ON li.id=cl.listing_id WHERE {where} AND li.status {status} "
        f"GROUP BY c.id ORDER BY c.score DESC, first_seen DESC LIMIT 200"
    )
    cards = []
    for c in rows:
        listings = [dict(r) for r in db.query(
            "SELECT li.* FROM car_listings cl JOIN listings li ON li.id=cl.listing_id WHERE cl.car_id=? "
            "ORDER BY li.first_seen", (c["id"],))]
        main = listings[0]
        images = loads(main["image_urls"], [])
        price = main["landed_cost_gbp"] or main["price_gbp"]
        if view in ("new", "check") and not (price is not None and budget["min_gbp"] <= price <= budget["max_gbp"]):
            continue  # the main tabs only show cars inside the budget; everything else is under More
        cards.append({
            "id": c["id"], "title": main["title"], "price": price,
            "in_budget": price is not None and budget["min_gbp"] <= price <= budget["max_gbp"],
            "generation": GEN_NAMES.get(c["generation"], "Not sure"), "market": MARKET_NAMES.get(c["market"], ""),
            "mod_tier": TIER_NAMES.get(c["mod_tier"] or "stock", ""), "score": c["score"] or 0,
            "reasons": loads(c["reasons"], []), "score_reasons": loads(c["score_reasons"], []),
            "flags": loads(c["flags"], []), "state": c["shortlist_state"], "notes": c["owner_notes"] or "",
            "image": images[0] if images else None, "year": main["year"], "mileage": main["mileage"],
            "location": main["location_text"], "distance": main["distance_miles"], "reg": main["reg"],
            "mot_url": MOT_URL.format(reg=quote(main["reg"])) if main["reg"] else None,
            "first_seen": _ago(c["first_seen"]),
            "links": [{"name": source_name(recipes, l["source"]),
                       "url": l["url"], "price": l["price_gbp"], "status": l["status"]} for l in listings],
            "in_scope": bool(c["in_scope"]), "engine_status": c["engine_status"],
            "landed": loads(main["landed_breakdown"]) if main["landed_cost_gbp"] else None,
            "asking_gbp": main["price_gbp"], "price_original": main["price_original"], "currency": main["currency"],
            "fx_date": main["fx_date"],
            "value_note": next((r for r in loads(c["score_reasons"], []) if "usual" in r), None),
        })
    return cards


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="SX200 Tracker", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    tpl = Jinja2Templates(directory=str(HERE / "templates"))
    tpl.env.filters["money"] = lambda v: f"£{v:,.0f}" if v else "Price not shown"
    tpl.env.filters["miles"] = lambda v: f"{v:,}" if v else ""

    def ctx(request: Request, **kw: Any) -> dict:
        cfg = state.cfg()
        b = health.banner(state.db, state.pipeline.recipes, state.pipeline.enabled_sources(),
                          cfg["schedule"]["interval_minutes"])
        counts = {v: len(car_cards(state.db, state.pipeline.recipes, v, cfg["budget"])) for v in ("new", "check", "shortlist", "japan")}
        counts["duplicates"] = state.db.one(
            "SELECT COUNT(*) n FROM dup_candidates d JOIN cars a ON a.id=d.car_a JOIN cars b ON b.id=d.car_b "
            "WHERE d.state='pending'")["n"]
        return {"request": request, "cfg": cfg, "banner": b, "views": VIEWS, "main_views": MAIN_VIEWS, "counts": counts,
                "paused": state.scheduler.paused if state.scheduler else False,
                "last_sweep": _ago(state.db.get_meta("last_sweep")), **kw}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, view: str = "new"):
        if not state.cfg().get("setup_complete"):
            return RedirectResponse("/setup", status_code=303)
        view = view if view in VIEWS else "new"
        cards = car_cards(state.db, state.pipeline.recipes, view, state.cfg()["budget"])
        return tpl.TemplateResponse(request, "dashboard.html", ctx(request, view=view, cards=cards))

    @app.get("/cars", response_class=HTMLResponse)
    def cars_partial(request: Request, view: str = "new"):
        view = view if view in VIEWS else "new"
        cards = car_cards(state.db, state.pipeline.recipes, view, state.cfg()["budget"])
        return tpl.TemplateResponse(request, "_cards.html", {"request": request, "cards": cards, "view": view})

    @app.post("/cars/{car_id}/{action}", response_class=HTMLResponse)
    def car_action(request: Request, car_id: int, action: str, notes: str = Form("")):
        if action == "shortlist":
            state.db.execute("UPDATE cars SET shortlist_state='shortlisted' WHERE id=?", (car_id,))
        elif action == "ignore":
            state.db.execute("UPDATE cars SET shortlist_state='ignored' WHERE id=?", (car_id,))
        elif action == "restore":
            state.db.execute("UPDATE cars SET shortlist_state='new' WHERE id=?", (car_id,))
        elif action == "notes":
            state.db.execute("UPDATE cars SET owner_notes=? WHERE id=?", (notes[:2000], car_id))
            return HTMLResponse('<span class="saved">Saved</span>')
        msg = {"shortlist": '<span class="saved">On your shortlist</span>'}.get(action, "")
        return HTMLResponse(msg, headers={"HX-Trigger": "carsChanged"})

    @app.get("/banner", response_class=HTMLResponse)
    def banner_partial(request: Request):
        return tpl.TemplateResponse(request, "_banner.html", ctx(request))

    @app.post("/check-now", response_class=HTMLResponse)
    def check_now(request: Request):
        if state.scheduler:
            state.scheduler.resume()
            state.scheduler.run_now()
        return HTMLResponse('<span class="saved">Checking all sites now</span>')

    @app.get("/progress", response_class=HTMLResponse)
    def progress(request: Request):
        p = state.pipeline.progress
        return tpl.TemplateResponse(request, "_progress.html", {"request": request, "p": p,
                                    "started": state.db.get_meta("last_sweep") is not None})

    # ---- setup: at most three questions --------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        cfg = state.cfg()
        return tpl.TemplateResponse(request, "setup.html", ctx(request, topic_url=_topic_url(cfg)))

    @app.post("/setup")
    def setup_save(request: Request, postcode: str = Form(""), budget_min: str = Form("10000"),
                   budget_max: str = Form("20000"), phone: str = Form("yes")):
        cfg = state.cfg()
        cfg["owner"]["home_postcode"] = _clean_postcode(postcode)
        lo, hi = _money(budget_min, 10000), _money(budget_max, 20000)
        cfg["budget"] = {"min_gbp": min(lo, hi), "max_gbp": max(lo, hi)}
        cfg["alerts"]["ntfy_enabled"] = phone != "skip"
        cfg["setup_complete"] = True
        state.save(cfg)
        winsys.set_start_at_login(True)
        state.notifier.test()
        if state.scheduler:
            state.scheduler.run_now()
        return RedirectResponse("/first-run", status_code=303)

    @app.get("/first-run", response_class=HTMLResponse)
    def first_run(request: Request):
        return tpl.TemplateResponse(request, "first_run.html", ctx(
            request, p=state.pipeline.progress, started=state.db.get_meta("last_sweep") is not None))

    @app.get("/qr.svg")
    def qr():
        import qrcode
        import qrcode.image.svg

        img = qrcode.make(_topic_url(state.cfg()), image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
        buf = io.BytesIO()
        img.save(buf)
        return Response(buf.getvalue(), media_type="image/svg+xml")

    @app.post("/test-alert", response_class=HTMLResponse)
    def test_alert():
        ok = state.notifier.test()
        return HTMLResponse('<span class="saved">Test alert sent. Check your phone.</span>' if ok else
                            '<span class="warn">Could not send. Is the PC online?</span>')

    # ---- sources and settings -----------------------------------------
    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        updates = [state.db.get_meta("update_status_recipes"), state.db.get_meta("update_status_app")]
        relevant = {r["source"]: r["n"] for r in state.db.query(
            "SELECT li.source, COUNT(*) n FROM listings li JOIN car_listings cl ON cl.listing_id=li.id "
            "WHERE li.status IN ('active','relisted') GROUP BY li.source")}
        from core.version import VERSION

        return tpl.TemplateResponse(request, "sources.html", ctx(request, view="sources", relevant=relevant,
                                    updates=[u for u in updates if u], version=VERSION))

    @app.get("/everything", response_class=HTMLResponse)
    def everything(request: Request, show: str = "active"):
        """Every 200SX / Silvia / 180SX the tracker has seen, with its verdict and why."""
        status = "('active','relisted')" if show == "active" else "('active','relisted','removed','sold')"
        rows = state.db.query(
            f"SELECT c.id, c.in_scope, c.engine_status, c.generation, c.reasons, c.score, li.title, "
            f"COALESCE(li.landed_cost_gbp, li.price_gbp) AS price_gbp, li.landed_cost_gbp, "
            f"li.year, li.url, li.source, li.status, li.first_seen FROM cars c JOIN car_listings cl ON cl.car_id=c.id "
            f"JOIN listings li ON li.id=cl.listing_id WHERE li.status IN {status} ORDER BY c.in_scope DESC, "
            f"li.first_seen DESC LIMIT 500")
        budget = state.cfg()["budget"]
        items = []
        for r in rows:
            reasons = loads(r["reasons"], [])
            excluded = next((x.split(": ", 1)[1] for x in reasons if x.startswith("Excluded: ")), None)
            why = [x for x in reasons if not x.startswith("Excluded: ")]
            if r["in_scope"]:
                verdict, cls = "Match", "good"
                if r["price_gbp"] and not budget["min_gbp"] <= r["price_gbp"] <= budget["max_gbp"]:
                    verdict = "Match, outside budget"
            elif not excluded and r["engine_status"] == "unknown":
                verdict, cls = "Needs a look", "warn"
            else:
                verdict, cls = "Not a match", "bad"
            items.append({**dict(r), "verdict": verdict, "cls": cls, "why": why[-1] if why else "",
                          "source_name": source_name(state.pipeline.recipes, r["source"])})
        return tpl.TemplateResponse(request, "everything.html", ctx(request, view="everything", items=items, show=show))

    @app.post("/update-check", response_class=HTMLResponse)
    def update_check():
        from core.updater import Updater
        from core.version import VERSION

        upd = Updater(state.db, state.cfg, VERSION)
        got = upd.check_app()
        if got:
            return HTMLResponse(f'<span class="saved">Version {got} downloaded.</span> '
                                '<form method="post" action="/update-install" style="display:inline">'
                                '<button class="primary">Install now</button></form>')
        return HTMLResponse(f'<span class="note">{state.db.get_meta("update_status_app") or "No update found."}</span>')

    @app.post("/update-install", response_class=HTMLResponse)
    def update_install():
        import os
        import threading

        from core.updater import Updater
        from core.version import VERSION

        if not Updater(state.db, state.cfg, VERSION).apply_pending():
            return HTMLResponse('<p>No downloaded update to install. Press "Check for updates" first.</p>')
        # The installer replaces the program and restarts it; this copy steps aside.
        threading.Timer(2.0, lambda: os._exit(0)).start()
        return HTMLResponse("<p>Installing the update. The tracker restarts by itself in about a minute; "
                            "then reload this page.</p>")

    @app.post("/paste", response_class=HTMLResponse)
    def paste(request: Request, url: str = Form(""), title: str = Form(""), price: str = Form(""),
              details: str = Form("")):
        from collectors import pasted
        from collectors import recipe_engine as engine

        url = url.strip()
        recipes = state.pipeline.recipes
        src = pasted.source_for(url, recipes)
        if title.strip():
            raw = pasted.from_owner(url or f"manual:{title.strip()}", title, price, details)
        else:
            try:
                raw = pasted.fetch(url)
            except engine.CollectorError as e:
                blocked = isinstance(e, engine.BlockedError)
                return tpl.TemplateResponse(request, "_paste_result.html", {
                    "request": request, "ask": True, "url": url, "site": source_name(recipes, src),
                    "why": ("That site will not let the tracker read the page." if blocked else str(e))})
        lid = state.pipeline.process(src, raw, alert=False)
        row = state.db.one("SELECT c.*, COALESCE(li.landed_cost_gbp, li.price_gbp) AS eff, li.landed_cost_gbp, li.title "
                           "FROM listings li LEFT JOIN car_listings cl ON cl.listing_id=li.id "
                           "LEFT JOIN cars c ON c.id=cl.car_id WHERE li.id=?", (lid,)) if lid else None
        budget = state.cfg()["budget"]
        verdict, reasons = "Not a Nissan 200SX or Silvia", []
        if row and row["id"]:
            reasons = loads(row["reasons"], [])
            excluded = next((x.split(": ", 1)[1] for x in reasons if x.startswith("Excluded: ")), None)
            reasons = [x for x in reasons if not x.startswith("Excluded: ")] + loads(row["flags"], [])
            if row["in_scope"]:
                inside = row["eff"] is not None and budget["min_gbp"] <= row["eff"] <= budget["max_gbp"]
                verdict = "Match, inside your budget" if inside else "Match, but outside your budget"
            elif not excluded and row["engine_status"] == "unknown":
                verdict = "Needs a look: the ad does not confirm the engine"
            else:
                verdict = "Not a match"
        return tpl.TemplateResponse(request, "_paste_result.html", {
            "request": request, "ask": False, "verdict": verdict, "reasons": reasons, "row": row,
            "car_id": row["id"] if row else None})

    @app.get("/more", response_class=HTMLResponse)
    def more(request: Request):
        return tpl.TemplateResponse(request, "more.html", ctx(request, view="more"))

    @app.get("/price-history", response_class=HTMLResponse)
    def price_history(request: Request):
        rows = state.db.query(
            "SELECT li.id, li.title, li.url, li.source, li.status, li.currency, c.in_scope FROM listings li "
            "JOIN car_listings cl ON cl.listing_id=li.id JOIN cars c ON c.id=cl.car_id "
            "WHERE c.reasons NOT LIKE '%Excluded:%' AND "
            "(SELECT COUNT(DISTINCT price_gbp) FROM price_history ph WHERE ph.listing_id=li.id) > 1 "
            "ORDER BY (SELECT MAX(seen_at) FROM price_history ph WHERE ph.listing_id=li.id) DESC LIMIT 200")
        items = []
        for r in rows:
            points, last = [], None
            for ph in state.db.query("SELECT seen_at, price_gbp FROM price_history WHERE listing_id=? ORDER BY seen_at",
                                     (r["id"],)):
                if ph["price_gbp"] != last:
                    points.append({"date": ph["seen_at"][:10], "price": ph["price_gbp"]})
                    last = ph["price_gbp"]
            first, latest = points[0]["price"], points[-1]["price"]
            change = (latest - first) / first * 100 if first and latest else None
            items.append({**dict(r), "points": points, "change": change,
                          "source_name": source_name(state.pipeline.recipes, r["source"])})
        return tpl.TemplateResponse(request, "price_history.html", ctx(request, view="prices", items=items))

    @app.get("/duplicates", response_class=HTMLResponse)
    def duplicates(request: Request):
        rows = state.db.query(
            "SELECT d.* FROM dup_candidates d JOIN cars a ON a.id=d.car_a JOIN cars b ON b.id=d.car_b "
            "WHERE d.state='pending' ORDER BY d.id")
        budget = state.cfg()["budget"]
        all_cards = {c["id"]: c for v in ("all", "removed") for c in car_cards(state.db, state.pipeline.recipes, v, budget)}
        pairs = [{"id": r["id"], "reason": r["reason"], "a": all_cards.get(r["car_a"]), "b": all_cards.get(r["car_b"])}
                 for r in rows]
        pairs = [p for p in pairs if p["a"] and p["b"]]
        return tpl.TemplateResponse(request, "duplicates.html", ctx(request, view="duplicates", pairs=pairs))

    @app.post("/duplicates/{dup_id}/{action}")
    def duplicate_action(dup_id: int, action: str):
        row = state.db.one("SELECT * FROM dup_candidates WHERE id=?", (dup_id,))
        if row and action == "merge":
            dedupe.merge_cars(state.db, row["car_a"], row["car_b"])
            state.db.execute("UPDATE dup_candidates SET state='merged' WHERE id=?", (dup_id,))
        elif row and action == "different":
            state.db.execute("UPDATE dup_candidates SET state='different' WHERE id=?", (dup_id,))
        return RedirectResponse("/duplicates", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        recipes = state.pipeline.recipes
        from collectors import ebay_api

        cid, _secret = ebay_api.get_keys()
        return tpl.TemplateResponse(request, "settings.html", ctx(
            request, view="settings", recipes=recipes, topic_url=_topic_url(state.cfg()),
            disabled=set(state.cfg()["sources"].get("disabled") or []),
            ebay_key=(cid[:6] + "…") if cid else None))

    @app.post("/facebook-login", response_class=HTMLResponse)
    def facebook_login():
        import threading

        from collectors.facebook import FacebookBrowser

        threading.Thread(target=FacebookBrowser(headless=False).open_login_window, name="facebook-login",
                         daemon=True).start()
        return HTMLResponse('<span class="saved">An Edge window is opening on Facebook. Log in there (tick '
                            '"remember me" if asked), then close that window. The tracker keeps the login.</span>')

    @app.post("/ebay-test", response_class=HTMLResponse)
    def ebay_test():
        from collectors import ebay_api

        cid, secret = ebay_api.get_keys()
        if not (cid and secret):
            return HTMLResponse('<span class="warn">No eBay key saved yet.</span>')
        try:
            api = ebay_api.EbayBrowse(cid, secret)
            n = len(api.search("nissan 200sx", 1000, 100000))
            return HTMLResponse(f'<span class="saved">The key works. eBay returned {n} listings for "nissan 200sx".</span>')
        except Exception as e:
            return HTMLResponse(f'<span class="warn">{e}</span>')

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        cfg = state.cfg()
        cfg["owner"]["home_postcode"] = _clean_postcode(str(form.get("postcode", "")))
        lo, hi = _money(form.get("budget_min"), cfg["budget"]["min_gbp"]), _money(form.get("budget_max"), cfg["budget"]["max_gbp"])
        cfg["budget"] = {"min_gbp": min(lo, hi), "max_gbp": max(lo, hi)}
        cfg["alerts"]["alert_min_score"] = max(0, min(100, int(_money(form.get("min_score"), 60))))
        cfg["alerts"]["ntfy_enabled"] = form.get("ntfy_enabled") == "on"
        cfg["alerts"]["windows_enabled"] = form.get("windows_enabled") == "on"
        cfg["alerts"]["digest_enabled"] = form.get("digest_enabled") == "on"
        for key in ("digest_time", "quiet_start", "quiet_end"):
            v = str(form.get(key, "")).strip()
            cfg["alerts"][key] = v if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v) else (
                "08:30" if key == "digest_time" else None)
        cfg["alerts"]["maintainer_ntfy_topic"] = re.sub(r"[^A-Za-z0-9_-]", "", str(form.get("maintainer_topic", "")))[:64]
        cfg["target"]["include_180sx"] = form.get("include_180sx") == "on"
        cfg["sources"]["facebook"] = form.get("facebook_on") == "on"
        cfg["target"]["alert_swaps"] = form.get("alert_swaps") == "on"
        cfg["sources"]["disabled"] = [n for n in state.pipeline.recipes
                                      if n != "facebook" and form.get(f"src_{n}") != "on"]
        from collectors import ebay_api

        if form.get("ebay_remove") == "on":
            ebay_api.set_keys("", "")
        elif str(form.get("ebay_client_id", "")).strip() and str(form.get("ebay_client_secret", "")).strip():
            ebay_api.set_keys(str(form["ebay_client_id"]).strip(), str(form["ebay_client_secret"]).strip())
        if form.get("new_topic") == "on":
            cfg["alerts"]["ntfy_topic"] = config_mod.new_topic()
        state.save(cfg)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/pause", response_class=HTMLResponse)
    def pause():
        if state.scheduler:
            if state.scheduler.paused:
                state.scheduler.resume()
            else:
                state.scheduler.pause()
        return RedirectResponse("/", status_code=303)

    @app.post("/problem-report")
    def problem_report():
        """Zip the logs and source health (no personal data) for the maintainer."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in (paths.data_dir() / "logs").glob("*.log*"):
                z.write(f, f"logs/{f.name}")
            rows = [dict(r) for r in state.db.query("SELECT * FROM source_health")]
            z.writestr("source_health.txt", "\n".join(str(r) for r in rows))
            cfg = state.cfg()
            safe = {k: v for k, v in cfg.items() if k not in ("owner", "alerts")}
            z.writestr("settings.txt", str(safe))
        name = f"sx200-problem-report-{datetime.now():%Y%m%d-%H%M}.zip"
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app


def _topic_url(cfg: dict) -> str:
    return f"{cfg['alerts'].get('ntfy_server', 'https://ntfy.sh').rstrip('/')}/{cfg['alerts']['ntfy_topic']}"


def _clean_postcode(pc: str) -> str:
    pc = re.sub(r"[^A-Za-z0-9 ]", "", pc or "").upper().strip()
    return re.sub(r"\s+", " ", pc)[:8]


def _money(value: Any, default: float) -> float:
    try:
        text = str(value).replace(",", "").replace("£", "").strip().lower()
        n = float(text[:-1]) * 1000 if text.endswith("k") else float(text)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default
