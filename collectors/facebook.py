"""Facebook Marketplace, opt-in (spec sections 2, 4 and 13).

Off by default. The owner turns it on in Settings, reads the warning, and logs in once in the
tracker's own Edge window (its own profile, separate from his normal browser). After that, at
most once an hour, the tracker opens one Marketplace search per term in that profile, reads the
listings shown, and closes the page: what a person scrolling the first screen would see.

Facebook's terms forbid automated collection, so the account could be restricted: a spare
account is safer. Any login page or "confirm it's you" checkpoint stops the collector at once
and the owner gets one plain message. Nothing here tries to get past a check.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser

from core import paths

from .recipe_engine import BlockedError, CollectorError

log = logging.getLogger(__name__)
VEHICLES = "546583916084032"  # Marketplace's Vehicles category
SEARCH = ("https://www.facebook.com/marketplace/{area}search/?query={q}&minPrice={lo}&maxPrice={hi}"
          f"&category_id={VEHICLES}&exact=false")
PROFILE_IN_USE = "The Facebook window is still open. Close it, then try again"


def area_slug(value: str | None) -> str:
    """'nottingham', or a pasted Marketplace link like .../marketplace/nottingham/search/... -> 'nottingham'."""
    v = (value or "").strip().lower()
    m = re.search(r"/marketplace/([a-z0-9-]+)/", v)
    if m and m.group(1) not in ("search", "item", "category", "you", "create"):
        return m.group(1)
    return v if re.fullmatch(r"[a-z0-9-]{2,60}", v) else ""


def search_url(term: str, lo: int, hi: int, area: str = "") -> str:
    slug = area_slug(area)
    return SEARCH.format(area=f"{slug}/" if slug else "", q=quote_plus(term), lo=lo, hi=hi)
LOGIN_URL = "https://www.facebook.com/login"
NEEDS_LOGIN = "Facebook needs you to log in again: Settings, then Facebook, then Log in"


def profile_dir():
    d = paths.data_dir() / "facebook-profile"
    d.mkdir(exist_ok=True)
    return d


def is_login_or_checkpoint(url: str, html: str) -> bool:
    u = (url or "").lower()
    if "/login" in u or "/checkpoint" in u or "two_step_verification" in u:
        return True
    head = (html or "")[:40000].lower()
    return any(m in head for m in ('id="login_form"', 'name="login"', "confirm your identity", "you're temporarily blocked",
                                    "we suspended your account", "help us confirm it's you", "enter the code"))


PRICE = re.compile(r"^(?:£|GBP\s?)\s?[\d,]+(?:\.\d\d)?$|^Free$", re.I)
MILES = re.compile(r"^\s*[\d.,]+\s?[kK]?\s*(?:miles|mi|km)\b", re.I)


def parse_results(html: str) -> list[dict]:
    """Marketplace result tiles are links to /marketplace/item/<id>/. Inside: price, title,
    location, often mileage, each on its own line. Read them by position and shape, not by
    Facebook's generated class names, which change constantly."""
    tree = LexborHTMLParser(html)
    for n in tree.css("script, style"):
        n.decompose()
    out: dict[str, dict] = {}
    for a in tree.css('a[href*="/marketplace/item/"]'):
        m = re.search(r"/marketplace/item/(\d+)", a.attributes.get("href") or "")
        if not m or m.group(1) in out:
            continue
        lines = [ln.strip() for ln in a.text(separator="\n").split("\n") if ln.strip()]
        prices = [ln for ln in lines if PRICE.match(ln)]
        miles = next((ln for ln in lines if MILES.match(ln)), None)
        rest = [ln for ln in lines if ln not in prices and ln != miles]
        title = rest[0] if rest else ""
        location = next((ln for ln in rest[1:] if "," in ln or "United Kingdom" in ln), rest[1] if len(rest) > 1 else "")
        img = a.css_first("img")
        out[m.group(1)] = {
            "id": m.group(1), "url": f"https://www.facebook.com/marketplace/item/{m.group(1)}/",
            "title": title, "price": prices[0] if prices else None, "location": location,
            "mileage": miles, "images": [img.attributes["src"]] if img and img.attributes.get("src") else [],
            "seller_type": "private",
        }
    return [i for i in out.values() if i["title"]]


class FacebookBrowser:
    """Edge with the tracker's own Facebook profile. One page at a time."""

    _lock = threading.Lock()

    def __init__(self, headless: bool = True):
        self.headless = headless

    def _launch(self, headless: bool):
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            ctx = pw.chromium.launch_persistent_context(str(profile_dir()), channel="msedge", headless=headless,
                                                        locale="en-GB", viewport={"width": 1280, "height": 900})
        except Exception as e:
            pw.stop()
            if "exitCode=21" in str(e):  # Chromium's "profile in use": the login window is still open
                raise CollectorError(PROFILE_IN_USE) from e
            raise CollectorError("Microsoft Edge could not be opened on this PC") from e
        return pw, ctx

    def search(self, terms: list[str], lo: int, hi: int, area: str = "") -> list[dict]:
        if not self._lock.acquire(blocking=False):
            raise CollectorError("The Facebook window is open for logging in; trying again later")
        try:
            pw, ctx = self._launch(self.headless)
            try:
                found: dict[str, dict] = {}
                page = ctx.new_page()
                for term in terms:
                    page.goto(search_url(term, lo, hi, area), wait_until="domcontentloaded", timeout=45000)
                    time.sleep(random.uniform(5, 9))  # let the tiles render, as a person would wait
                    html = page.content()
                    if is_login_or_checkpoint(page.url, html):
                        raise BlockedError(NEEDS_LOGIN)
                    for item in parse_results(html):
                        found.setdefault(item["id"], item)
                    time.sleep(random.uniform(8, 15))
                return list(found.values())
            finally:
                ctx.close()
                pw.stop()
        finally:
            self._lock.release()

    def open_login_window(self, timeout_s: int = 900) -> None:
        """A visible Edge window on Facebook's login page. Returns when the owner closes it."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            pw, ctx = self._launch(headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(LOGIN_URL)
                deadline = time.time() + timeout_s
                while time.time() < deadline and ctx.pages:
                    time.sleep(1)
            finally:
                try:
                    ctx.close()
                finally:
                    pw.stop()
        finally:
            self._lock.release()


def run(recipe_data: dict, cfg: dict, browser: FacebookBrowser | None = None) -> list[dict]:
    if not cfg.get("sources", {}).get("facebook"):
        raise CollectorError("Off: Facebook Marketplace is switched off in Settings")
    s = cfg.get("search", {})
    return (browser or FacebookBrowser()).search(recipe_data.get("terms", []), int(s.get("price_min_gbp", 8000)),
                                                 int(s.get("price_max_gbp", 25000)),
                                                 cfg.get("sources", {}).get("facebook_area", ""))
