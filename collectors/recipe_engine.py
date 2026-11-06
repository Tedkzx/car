"""Runs a source recipe: fetch search pages, pull out the listings (spec section 5).

A recipe is a YAML file. Extraction strategies are tried in order and the
first that yields listings wins: JSON the page embeds, then JSON-LD, then
CSS selectors. JSON breaks less often than HTML.

Recipes are data, never code: hot-updated recipes can only describe where
to read fields, so a tampered recipe cannot run anything (spec section 13).
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote_plus, urljoin

import yaml
from selectolax.lexbor import LexborHTMLParser

from core import paths

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)
# Strong signs: these only appear on check pages.
CHALLENGE_MARKERS = (
    "just a moment...", "cf-chl", "confirm it's you", "confirm it’s you", "pardon our interruption",
    "verify you are human", "please enable js and disable any ad blocker", "security checkpoint",
    "attention required!", "verification required", "checking your browser",
)
# Weak signs: normal pages also mention these (a reCAPTCHA script on a contact form, Cloudflare's
# background script), so they only count on small pages. Real block pages are a few KB.
WEAK_MARKERS = ("captcha", "are you a robot", "access denied", "unusual traffic", "challenge-platform")
WEAK_MAX_BYTES = 30_000
LOGIN_MARKERS = ("log in to continue", "sign in to continue", "you must log in")


class CollectorError(Exception):
    """Something went wrong; the plain message is safe to show the owner."""


class BlockedError(CollectorError):
    """The site refused us or showed a challenge. Back off, never try to get past it."""


class LayoutChangedError(CollectorError):
    """The page loaded but the recipe found nothing it recognises."""


# ---------------------------------------------------------------------------
# Recipe loading
# ---------------------------------------------------------------------------
@dataclass
class Recipe:
    name: str
    display_name: str
    data: dict
    path: Path

    @property
    def status(self) -> str:
        return self.data.get("status", "unverified")

    @property
    def method(self) -> str:
        return self.data.get("method", "http")

    @property
    def enabled_by_default(self) -> bool:
        return bool(self.data.get("enabled", True))

    @property
    def fixture_path(self) -> Path | None:
        fx = self.data.get("fixture")
        return (self.path.parent / fx) if fx else None


def version_tuple(v: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "0"))[:3]) or (0,)


def compatible(data: dict, app_version: str | None = None) -> bool:
    from core.version import VERSION

    need = data.get("min_app_version")
    return not need or version_tuple(need) <= version_tuple(app_version or VERSION)


def recipe_dirs() -> list[Path]:
    """Downloaded recipes (app data) override the ones shipped in the installer."""
    return [paths.data_dir() / "recipes", paths.bundle_dir() / "recipes"]


def load_recipes(dirs: Iterable[Path] | None = None) -> dict[str, Recipe]:
    found: dict[str, Recipe] = {}
    for d in dirs or recipe_dirs():
        if not d.exists():
            continue
        for f in sorted(d.glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                log.error("Recipe %s is not valid YAML: %s", f, e)
                continue
            name = data.get("name") or f.stem
            if not compatible(data):
                log.info("Recipe %s needs app %s or later; skipped", name, data.get("min_app_version"))
                continue
            if name not in found:  # first directory wins
                found[name] = Recipe(name, data.get("display_name", name), data, f)
    return found


# ---------------------------------------------------------------------------
# Small path language for JSON:  a.b.0.c,  a.*.b (every item),  a.b|first
# {"__ref": "X"} values are resolved against the `refs` root when given.
# ---------------------------------------------------------------------------
def get_path(obj: Any, path: str, refs: dict | None = None) -> Any:
    if path in ("", "."):
        return obj
    parts = path.split(".")
    cur: Any = obj
    for i, part in enumerate(parts):
        cur = _deref(cur, refs)
        if cur is None:
            return None
        if part == "*":
            rest = ".".join(parts[i + 1 :])
            items = cur.values() if isinstance(cur, dict) else cur if isinstance(cur, list) else []
            return [get_path(x, rest, refs) if rest else _deref(x, refs) for x in items]
        if isinstance(cur, list):
            if part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return _deref(cur, refs)


def _deref(value: Any, refs: dict | None) -> Any:
    if refs is not None and isinstance(value, dict) and set(value) == {"__ref"}:
        return refs.get(value["__ref"])
    return value


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _clean_tree(html: str) -> LexborHTMLParser:
    tree = LexborHTMLParser(html)
    for node in tree.css("style, noscript"):
        node.decompose()
    return tree


def _node_text(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.text(separator=" ", strip=True)).strip()


def extract(html: str, recipe: dict, page_url: str = "") -> list[dict]:
    strategies = recipe.get("extract")
    if isinstance(strategies, dict):
        strategies = [strategies]
    tree = _clean_tree(html)
    for strat in strategies or []:
        try:
            items = _run_strategy(tree, html, strat, page_url)
        except Exception as e:  # a broken strategy must not stop the next one
            log.debug("strategy %s failed: %s", strat.get("type"), e)
            continue
        items = [i for i in items if i.get("title") and i.get("url")]
        if items:
            for i in items:
                i["_strategy"] = strat.get("type")
            return items
    return []


def _run_strategy(tree: LexborHTMLParser, html: str, strat: dict, page_url: str) -> list[dict]:
    kind = strat["type"]
    if kind in ("json_script", "json_attr", "json_regex"):
        data = _load_embedded_json(tree, html, strat)
        refs = get_path(data, strat["refs_root"]) if strat.get("refs_root") else None
        container = get_path(data, strat.get("items", ""), refs)
        if isinstance(container, dict):
            prefix = strat.get("key_prefix")
            container = [v for k, v in container.items() if not prefix or k.startswith(prefix)]
        if not isinstance(container, list):
            return []
        out = []
        for raw in container:
            raw = _deref(raw, refs)
            if not isinstance(raw, dict):
                continue
            out.append(_map_json_fields(raw, strat["fields"], refs, page_url))
        return out
    if kind == "jsonld":
        return _jsonld(tree, strat, page_url)
    if kind == "rss":
        return _rss(html, strat)
    if kind == "auto":
        return _auto(tree, html, strat, page_url)
    if kind == "css":
        return _css(tree, strat, page_url)
    raise ValueError(f"unknown strategy {kind}")


def _load_embedded_json(tree: LexborHTMLParser, html: str, strat: dict) -> Any:
    if strat["type"] == "json_script":
        node = tree.css_first(strat["selector"])
        if node is None:
            raise LayoutChangedError("embedded JSON script not found")
        return json.loads(node.text(strip=False))
    if strat["type"] == "json_attr":
        node = tree.css_first(strat["selector"])
        if node is None or not node.attributes.get(strat["attr"]):
            raise LayoutChangedError("embedded JSON attribute not found")
        return json.loads(node.attributes[strat["attr"]])
    m = re.search(strat["regex"], html, re.S)
    if not m:
        raise LayoutChangedError("embedded JSON not found")
    return json.loads(m.group(1))


def _map_json_fields(raw: dict, fields: dict, refs: dict | None, page_url: str) -> dict:
    item: dict[str, Any] = {}
    for name, spec in fields.items():
        if isinstance(spec, str):
            item[name] = get_path(raw, spec, refs)
        elif isinstance(spec, dict) and "template" in spec:
            continue
        elif isinstance(spec, dict) and "path" in spec:
            item[name] = get_path(raw, spec["path"], refs)
        elif isinstance(spec, list):  # first path with a value
            item[name] = next((v for p in spec if (v := get_path(raw, p, refs)) not in (None, "", [])), None)
    _apply_templates(item, fields, page_url)
    return item


def _apply_templates(item: dict, fields: dict, page_url: str) -> None:
    for name, spec in fields.items():
        if isinstance(spec, dict) and "template" in spec:
            try:
                item[name] = spec["template"].format(**{k: ("" if v is None else v) for k, v in item.items()})
            except (KeyError, IndexError):
                item[name] = None
    for key in ("url",):
        if item.get(key):
            item[key] = urljoin(page_url, str(item[key]))
    imgs = item.get("images")
    if isinstance(imgs, str):
        imgs = [imgs]
    if imgs:
        item["images"] = [urljoin(page_url or "https://localhost/", i) if i.startswith("//") or i.startswith("/") else i
                          for i in imgs if isinstance(i, str)]
    for key, spec in fields.items():
        if isinstance(spec, dict) and spec.get("from"):
            src = item.get(spec["from"])
            m = re.search(spec.get("regex", "(.*)"), str(src or ""))
            item[key] = m.group(1) if m else None


def _jsonld(tree: LexborHTMLParser, strat: dict, page_url: str) -> list[dict]:
    wanted = set(strat.get("types", ["Car", "Vehicle", "Product", "Offer"]))
    out: list[dict] = []

    def visit(obj: Any) -> None:
        if isinstance(obj, list):
            for x in obj:
                visit(x)
            return
        if not isinstance(obj, dict):
            return
        t = obj.get("@type")
        types = set(t) if isinstance(t, list) else {t}
        if types & wanted:
            offers = obj.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            image = obj.get("image")
            out.append({
                "title": obj.get("name"),
                "url": urljoin(page_url, obj.get("url") or offers.get("url") or ""),
                "price": offers.get("price") or obj.get("price"),
                "currency": offers.get("priceCurrency"),
                "description": obj.get("description"),
                "year": obj.get("vehicleModelDate") or obj.get("productionDate") or obj.get("modelDate"),
                "mileage": get_path(obj, "mileageFromOdometer.value"),
                "colour": obj.get("color"),
                "images": image if isinstance(image, list) else [image] if image else [],
                "location": get_path(offers, "availableAtOrFrom.address.addressLocality")
                or get_path(obj, "offers.seller.address.addressLocality"),
            })
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in obj:
                visit(obj[key])

    for node in tree.css('script[type="application/ld+json"]'):
        try:
            visit(json.loads(node.text(strip=False)))
        except ValueError:
            continue
    for item in out:
        if not item.get("id") and item.get("url") and strat.get("id_regex"):
            m = re.search(strat["id_regex"], item["url"])
            item["id"] = m.group(1) if m else item["url"]
    return out


def _css(tree: LexborHTMLParser, strat: dict, page_url: str) -> list[dict]:
    containers = strat["items"] if isinstance(strat["items"], list) else [strat["items"]]
    nodes = []
    for sel in containers:
        nodes = tree.css(sel)
        if nodes:
            break
    out = []
    for node in nodes:
        item: dict[str, Any] = {}
        for name, spec in strat["fields"].items():
            if isinstance(spec, dict) and ("template" in spec or "from" in spec):
                continue
            spec = {"css": spec} if isinstance(spec, (str, list)) else spec
            sels = spec["css"] if isinstance(spec["css"], list) else [spec["css"]]
            value: Any = None
            if spec.get("all") and len(sels) > 1:  # collect from every selector, in order
                vals = []
                for sel in sels:
                    for f in (node.css(sel) if sel != ":self" else [node]):
                        v = _attr(f, spec.get("attr")) if spec.get("attr") else _node_text(f)
                        if v:
                            vals.append(v)
                item[name] = vals
                continue
            for sel in sels:
                found = node.css(sel) if sel != ":self" else [node]
                if not found:
                    continue
                if spec.get("all"):
                    vals = [(_attr(f, spec.get("attr")) if spec.get("attr") else _node_text(f)) for f in found]
                    value = [v for v in vals if v]
                else:
                    value = _attr(found[0], spec.get("attr")) if spec.get("attr") else _node_text(found[0])
                if value:
                    break
            if value and spec.get("regex") and isinstance(value, str):
                m = re.search(spec["regex"], value)
                value = m.group(1) if m else None
            item[name] = value
        _apply_templates(item, strat["fields"], page_url)
        out.append(item)
    return out


def _attr(node, attr: str | None) -> str | None:
    if attr is None:
        return _node_text(node)
    return node.attributes.get(attr)


# ---------------------------------------------------------------------------
# RSS feeds (forums). Old threads are dropped by max_age_days.
# ---------------------------------------------------------------------------
def _rss(text: str, strat: dict) -> list[dict]:
    import xml.etree.ElementTree as ET
    from datetime import datetime, timedelta, timezone
    from email.utils import parsedate_to_datetime

    start = text.find("<")
    root = ET.fromstring(text[start:].encode("utf-8"))  # ElementTree never fetches external entities
    max_age = strat.get("max_age_days")
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(max_age)) if max_age else None
    out = []
    for it in root.iter("item"):
        when = it.findtext("pubDate")
        try:
            dt = parsedate_to_datetime(when) if when else None
        except (TypeError, ValueError):
            dt = None
        if cutoff and dt and dt < cutoff:
            continue
        desc_html = it.findtext("description") or ""
        desc = _node_text(LexborHTMLParser(desc_html).body) if desc_html.strip() else ""
        imgs = [i.attributes.get("src") for i in LexborHTMLParser(desc_html).css("img[src]")] if desc_html else []
        link = (it.findtext("link") or "").strip()
        out.append({
            "id": (it.findtext("guid") or link).strip(), "url": link, "title": (it.findtext("title") or "").strip(),
            "description": desc[:4000], "listed": dt.isoformat() if dt else None,
            "images": [i for i in imgs if i and i.startswith("http")][:6],
        })
    return out


# ---------------------------------------------------------------------------
# "auto": for sites not yet seen from the build machine. Reads JSON-LD, then
# looks through any JSON the page embeds for a list of listing-like objects.
# ---------------------------------------------------------------------------
TITLE_KEYS = ("title", "headline", "name", "heading", "displayTitle", "makeModel", "vehicleTitle")
PRICE_KEYS = ("price", "priceValue", "advertisedPrice", "amount", "displayPrice", "priceGbp", "formattedPrice")
URL_KEYS = ("url", "link", "href", "canonicalUrl", "permalink", "advertUrl", "listingUrl", "detailsUrl")
ID_KEYS = ("id", "advertId", "listingId", "adId", "uuid", "stockId", "vehicleId")
YEAR_KEYS = ("year", "registrationYear", "modelYear", "yearOfManufacture", "vehicleYear")
MILEAGE_KEYS = ("mileage", "odometer", "miles", "odometerReading")
LOCATION_KEYS = ("location", "town", "city", "sellerLocation", "area", "locationName", "region")
IMAGE_KEYS = ("images", "imageUrls", "photos", "image", "mainImage", "thumbnail", "imageUrl", "primaryImage")


def _first(d: dict, keys: tuple) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _scalar(v: Any) -> Any:
    if isinstance(v, dict):
        return _first(v, ("value", "amount", "name", "displayValue", "text", "url", "src", "formatted"))
    if isinstance(v, list):
        return _scalar(v[0]) if v else None
    return v


def _looks_like_listing(d: Any) -> bool:
    return isinstance(d, dict) and _first(d, TITLE_KEYS) is not None and _first(d, PRICE_KEYS) is not None


def _auto(tree: LexborHTMLParser, html: str, strat: dict, page_url: str) -> list[dict]:
    items = [i for i in _jsonld(tree, {"types": ["Car", "Vehicle", "Product", "Motorcycle"],
                                       "id_regex": strat.get("id_regex")}, page_url) if i.get("title") and i.get("url")]
    if items:
        return items
    blobs: list[Any] = []
    for node in tree.css('script[type="application/json"], script#__NEXT_DATA__, script[type="application/ld+json"]'):
        try:
            blobs.append(json.loads(node.text(strip=False)))
        except ValueError:
            pass
    for m in re.finditer(r"window\.__[A-Z_]+__\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S):
        try:
            blobs.append(json.loads(m.group(1)))
        except ValueError:
            pass
    best: list = []

    def walk(o: Any, depth: int = 0) -> None:
        nonlocal best
        if depth > 12:
            return
        if isinstance(o, list):
            hits = [x for x in o if _looks_like_listing(x)]
            if len(hits) >= 2 and len(hits) >= 0.6 * len(o) and len(hits) > len(best):
                best = hits
            for x in o[:200]:
                walk(x, depth + 1)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v, depth + 1)

    for b in blobs:
        walk(b)
    out = []
    for d in best:
        item = {
            "id": _scalar(_first(d, ID_KEYS)), "title": _scalar(_first(d, TITLE_KEYS)),
            "price": _scalar(_first(d, PRICE_KEYS)), "year": _scalar(_first(d, YEAR_KEYS)),
            "mileage": _scalar(_first(d, MILEAGE_KEYS)), "location": _scalar(_first(d, LOCATION_KEYS)),
            "description": _scalar(_first(d, ("description", "subtitle", "summary", "attentionGrabber"))),
        }
        imgs = _first(d, IMAGE_KEYS)
        imgs = imgs if isinstance(imgs, list) else [imgs] if imgs else []
        item["images"] = [x for x in (_scalar(i) for i in imgs) if isinstance(x, str) and x.startswith("http")][:6]
        url = _scalar(_first(d, URL_KEYS))
        if not url and strat.get("url_template") and item["id"]:
            url = strat["url_template"].format(id=item["id"])
        item["url"] = urljoin(page_url, str(url)) if url else None
        if item["id"] is None:
            item["id"] = item["url"]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def detect_block(status: int, html: str) -> str | None:
    head = html[:20000].lower()
    if head.lstrip().startswith(("<?xml", "<rss")) and status < 400:
        return None  # a feed: forum posts may mention captchas without being a block page
    weak = len(html) < WEAK_MAX_BYTES and any(m in head for m in WEAK_MARKERS)
    if status in (401, 403, 429, 503) or weak or any(m in head for m in CHALLENGE_MARKERS):
        if status == 429:
            return "The site asked us to slow down"
        return "The site is blocking automatic checks right now"
    if any(m in head for m in LOGIN_MARKERS):
        return "The site wants a login"
    return None


@dataclass
class HttpFetcher:
    """Polite HTTP: one request at a time, randomised delays, ETag caching."""

    min_delay: float = 4.0
    max_delay: float = 9.0
    timeout: float = 30.0
    sleep: Callable[[float], None] = time.sleep
    _client: Any = None
    _validators: dict = field(default_factory=dict)
    _cache: dict = field(default_factory=dict)
    _last_request: float = 0.0

    def client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": BROWSER_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-GB,en;q=0.9",
                },
            )
        return self._client

    def post(self, url: str, data: dict) -> tuple[int, str, str]:
        """For sites that page through results with a form (Goo-net). Same politeness as get()."""
        wait = random.uniform(self.min_delay, self.max_delay) - (time.monotonic() - self._last_request)
        if self._last_request and wait > 0:
            self.sleep(wait)
        try:
            r = self.client().post(url, data=data)
        except Exception as e:
            raise CollectorError(f"Could not reach the site ({type(e).__name__})") from e
        finally:
            self._last_request = time.monotonic()
        return r.status_code, r.text, str(r.url)

    def get(self, url: str) -> tuple[int, str, str]:
        wait = random.uniform(self.min_delay, self.max_delay) - (time.monotonic() - self._last_request)
        if self._last_request and wait > 0:
            self.sleep(wait)
        headers = {}
        if url in self._validators:
            etag, modified = self._validators[url]
            if etag:
                headers["If-None-Match"] = etag
            if modified:
                headers["If-Modified-Since"] = modified
        try:
            r = self.client().get(url, headers=headers)
        except Exception as e:
            raise CollectorError(f"Could not reach the site ({type(e).__name__})") from e
        finally:
            self._last_request = time.monotonic()
        if r.status_code == 304 and url in self._cache:
            return 200, self._cache[url], str(r.url)
        if r.headers.get("etag") or r.headers.get("last-modified"):
            self._validators[url] = (r.headers.get("etag"), r.headers.get("last-modified"))
            self._cache[url] = r.text
        return r.status_code, r.text, str(r.url)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


# ---------------------------------------------------------------------------
# Running a recipe
# ---------------------------------------------------------------------------
def search_urls(recipe: dict, cfg: dict) -> list[str]:
    s = cfg.get("search", {})
    pc = re.sub(r"\s+", "", (cfg.get("owner", {}).get("home_postcode") or "LE1 1AA")).upper()
    values = {"price_min": s.get("price_min_gbp", 8000), "price_max": s.get("price_max_gbp", 25000), "postcode": pc}
    urls: list[str] = []
    for tmpl in recipe.get("searches", []):
        if "{term}" in tmpl:
            for term in recipe.get("terms", []):
                urls.append(tmpl.format(term=quote_plus(term), **values))
        else:
            urls.append(tmpl.format(**values))
    return urls


def run_recipe(recipe: Recipe, cfg: dict, fetcher: HttpFetcher | None = None, browser=None,
               save_raw: Callable[[str, str], str] | None = None) -> list[dict]:
    """Fetch every search page for one source and return raw listing dicts.

    Raises BlockedError at the first sign of blocking, so the caller backs off.
    """
    data = recipe.data
    if data.get("adapter"):
        # Custom Python adapters ship inside the app; a recipe can only name one, never supply code.
        if data["adapter"] == "ebay_api":
            from . import ebay_api

            return ebay_api.run(data, cfg)
        raise CollectorError(f"Unknown adapter {data['adapter']!r}")
    fetcher = fetcher or HttpFetcher()
    results: dict[str, dict] = {}
    pages_ok = 0
    for url in search_urls(data, cfg):
        max_pages = int(get_path(data, "paging.max_pages") or 1)
        for page in range(1, max_pages + 1):
            page_url = url
            form = None
            if page > 1:
                if get_path(data, "paging.method") == "post":
                    step = int(get_path(data, "paging.step") or 20)
                    form = {k: str(v).format(page=page, offset=(page - 1) * step)
                            for k, v in (get_path(data, "paging.form") or {}).items()}
                else:
                    param = get_path(data, "paging.param") or "page"
                    page_url = f"{url}{'&' if '?' in url else '?'}{param}={page}"
            if form is not None and recipe.method != "browser":
                status, html, final_url = fetcher.post(page_url, form)
            elif recipe.method == "browser":
                if browser is None:
                    raise CollectorError("This source needs the Edge browser, which is not available")
                status, html, final_url = browser.get(page_url)
            else:
                status, html, final_url = fetcher.get(page_url)
            problem = detect_block(status, html)
            if problem:
                raise BlockedError(problem)
            if status >= 400:
                raise CollectorError(f"The site answered with an error ({status})")
            items = extract(html, data, final_url)
            if save_raw is not None:
                save_raw(recipe.name, html)
            pages_ok += 1
            for it in items:
                key = str(it.get("id") or it.get("url"))
                results.setdefault(key, it)
            if not items:
                break
    if pages_ok and not results and data.get("expect_results", True) and data.get("strict_empty", False):
        raise LayoutChangedError("The page layout may have changed")
    return list(results.values())


def self_test(recipe: Recipe) -> tuple[bool, str]:
    """Parse the saved real page and check the recipe still finds listings."""
    fx = recipe.fixture_path
    if fx is None or not fx.exists():
        return False, "No saved page to test against yet"
    html = fx.read_text(encoding="utf-8", errors="replace")
    if detect_block(200, html):
        return False, "The saved page is a block or challenge page, not real results"
    items = extract(html, recipe.data, recipe.data.get("fixture_url", ""))
    need = int(recipe.data.get("fixture_min_items", 1))
    if len(items) < need:
        return False, f"Found {len(items)} listings on the saved page, expected at least {need}"
    return True, f"Found {len(items)} listings on the saved page"
