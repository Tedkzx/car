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
CHALLENGE_MARKERS = (
    "just a moment...", "cf-chl", "challenge-platform", "captcha", "are you a robot", "confirm it's you",
    "confirm it’s you", "pardon our interruption", "access denied", "unusual traffic", "verify you are human",
    "please enable js and disable any ad blocker",
)
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
# Fetching
# ---------------------------------------------------------------------------
def detect_block(status: int, html: str) -> str | None:
    head = html[:20000].lower()
    if status in (401, 403, 429, 503) or any(m in head for m in CHALLENGE_MARKERS):
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
    values = {"price_min": s.get("price_min_gbp", 8000), "price_max": s.get("price_max_gbp", 25000)}
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
    fetcher = fetcher or HttpFetcher()
    results: dict[str, dict] = {}
    pages_ok = 0
    for url in search_urls(data, cfg):
        max_pages = int(get_path(data, "paging.max_pages") or 1)
        for page in range(1, max_pages + 1):
            page_url = url
            if page > 1:
                param = get_path(data, "paging.param") or "page"
                page_url = f"{url}{'&' if '?' in url else '?'}{param}={page}"
            if recipe.method == "browser":
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
