"""The dashboard's "Paste a link" box (spec sections 4 and 10).

Reads a single advert page the owner found himself and runs it through the same matcher,
scoring and landed-cost rules. Some sites refuse automatic reading (Autotrader, eBay); then
the owner types the title and price and the tracker works from those. Nothing here tries to
get past a block.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser

from . import recipe_engine as engine

PASTED = "pasted"


def source_for(url: str, recipes: dict) -> str:
    """The recipe whose site this link is on, so it inherits currency and market; else 'pasted'."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for name, r in recipes.items():
        for s in r.data.get("searches", []):
            if isinstance(s, str) and "://" in s and urlparse(s).netloc.lower().removeprefix("www.") == host:
                return name
    return PASTED


def _meta(tree: LexborHTMLParser, *names: str) -> str | None:
    for n in names:
        node = tree.css_first(f'meta[property="{n}"]') or tree.css_first(f'meta[name="{n}"]')
        if node and node.attributes.get("content"):
            return node.attributes["content"].strip()
    return None


def read_page(html: str, url: str) -> dict[str, Any]:
    """Best effort: JSON-LD vehicle data, then Open Graph tags, then the page title."""
    tree = LexborHTMLParser(html)
    item: dict[str, Any] = {"id": url, "url": url}
    for got in engine._jsonld(tree, {"types": ["Car", "Vehicle", "Product", "Motorcycle"]}, url):
        if got.get("title"):
            item.update({k: v for k, v in got.items() if v not in (None, "", [])})
            item["url"] = url
            break
    if item.get("description") and "<" in str(item["description"]):
        item["description"] = engine._node_text(LexborHTMLParser(str(item["description"])).body)
    item.setdefault("title", _meta(tree, "og:title", "twitter:title")
                    or (tree.css_first("title").text(strip=True) if tree.css_first("title") else ""))
    desc = _meta(tree, "og:description", "description", "twitter:description")
    if desc and not item.get("description"):
        item["description"] = desc
    if not item.get("price"):
        item["price"] = _meta(tree, "product:price:amount", "og:price:amount")
        cur = _meta(tree, "product:price:currency", "og:price:currency")
        if cur:
            item["currency"] = cur
    if not item.get("images"):
        img = _meta(tree, "og:image", "twitter:image")
        item["images"] = [img] if img else []
    if not item.get("price"):
        for node in tree.css("style, script, noscript, nav, footer"):
            node.decompose()
        text = tree.body.text(separator=" ", strip=True) if tree.body else ""
        m = re.search(r"£\s?(\d{1,3}(?:,\d{3})+|\d{4,6})(?!\s*(?:per|pm|/m|a month|deposit))", text)
        item["price"] = m.group(1) if m else None
        item.setdefault("attributes", text[:600])
    return item


def from_owner(url: str, title: str, price: str, details: str) -> dict[str, Any]:
    return {"id": url, "url": url, "title": title.strip(), "price": price.strip() or None,
            "description": details.strip()[:4000]}


def fetch(url: str, fetcher: engine.HttpFetcher | None = None) -> dict[str, Any]:
    """Raises BlockedError when the site refuses, CollectorError when it cannot be read."""
    if not re.match(r"^https?://", url or ""):
        raise engine.CollectorError("That does not look like a web link")
    f = fetcher or engine.HttpFetcher(min_delay=0, max_delay=0)
    try:
        status, html, final = f.get(url)
    finally:
        if fetcher is None:
            f.close()
    problem = engine.detect_block(status, html)
    if problem:
        raise engine.BlockedError(problem)
    if status >= 400:
        raise engine.CollectorError(f"The site answered with an error ({status}); the ad may have been removed")
    return read_page(html, final)


def describe(item: dict) -> str:
    return json.dumps({k: item.get(k) for k in ("title", "price", "currency")}, ensure_ascii=False)
