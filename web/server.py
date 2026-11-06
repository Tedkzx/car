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
    "all": "All active",
    "removed": "Removed and sold",
}
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


def car_cards(db: DB, recipes: dict, view: str, budget: dict) -> list[dict]:
    where = {
        "new": "c.in_scope=1 AND c.shortlist_state='new'",
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
        price = main["price_gbp"]
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
            "links": [{"name": recipes[l["source"]].display_name if l["source"] in recipes else l["source"],
                       "url": l["url"], "price": l["price_gbp"], "status": l["status"]} for l in listings],
            "in_scope": bool(c["in_scope"]), "engine_status": c["engine_status"],
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
        counts = {v: len(car_cards(state.db, state.pipeline.recipes, v, cfg["budget"])) for v in ("new", "check", "shortlist")}
        counts["duplicates"] = state.db.one(
            "SELECT COUNT(*) n FROM dup_candidates d JOIN cars a ON a.id=d.car_a JOIN cars b ON b.id=d.car_b "
            "WHERE d.state='pending'")["n"]
        return {"request": request, "cfg": cfg, "banner": b, "views": VIEWS, "counts": counts,
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
        return HTMLResponse("", headers={"HX-Trigger": "carsChanged"})

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
        return tpl.TemplateResponse(request, "sources.html", ctx(request, view="sources",
                                    updates=[u for u in updates if u]))

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
        return tpl.TemplateResponse(request, "settings.html", ctx(
            request, view="settings", recipes=recipes, topic_url=_topic_url(state.cfg()),
            disabled=set(state.cfg()["sources"].get("disabled") or [])))

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
        cfg["alerts"]["maintainer_ntfy_topic"] = re.sub(r"[^A-Za-z0-9_-]", "", str(form.get("maintainer_topic", "")))[:64]
        cfg["target"]["include_180sx"] = form.get("include_180sx") == "on"
        cfg["target"]["alert_swaps"] = form.get("alert_swaps") == "on"
        cfg["sources"]["disabled"] = [n for n in state.pipeline.recipes if form.get(f"src_{n}") != "on"]
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
