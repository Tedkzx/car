"""Maintainer tool: save one real search page as a recipe fixture.

    SX200Tracker.exe --capture ebay

Run it on the owner's PC, where the home connection sees the site the way a
person does. It saves the page to %APPDATA%\\SX200Tracker\\recipes\\fixtures\\,
strips phone numbers and e-mail addresses, and runs the recipe against it.
If the site shows a check or block page, it says so and saves nothing.
"""
from __future__ import annotations

import re

from core import config, paths

from . import recipe_engine as engine

PHONE = re.compile(r"(?<!\d)(?:\+44\s?7\d{3}|07\d{3})\s?\d{3}\s?\d{3}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def scrub(html: str) -> str:
    return EMAIL.sub("redacted@example.com", PHONE.sub("00000000000", html))


def capture(source: str) -> str:
    recipes = engine.load_recipes()
    if source not in recipes:
        return f"No recipe called {source!r}. Known: {', '.join(sorted(recipes))}"
    recipe = recipes[source]
    if source == "facebook":
        return _capture_facebook()
    urls = engine.search_urls(recipe.data, config.load())
    if not urls:
        return "That recipe has no search pages."
    if recipe.method == "browser":
        from .browser_edge import EdgeBrowser

        b = EdgeBrowser()
        try:
            status, html, final = b.get(urls[0])
        finally:
            b.close()
    else:
        f = engine.HttpFetcher()
        try:
            status, html, final = f.get(urls[0])
        finally:
            f.close()
    problem = engine.detect_block(status, html)
    if problem:
        return f"{recipe.display_name}: {problem} (status {status}). Nothing saved."
    out_dir = paths.data_dir() / "recipes" / "fixtures"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{source}.html"
    out.write_text(scrub(html), encoding="utf-8")
    items = engine.extract(html, recipe.data, final)
    return (
        f"Saved {out} ({len(html):,} characters). The recipe found {len(items)} listings on it.\n"
        "Send this file to the maintainer with a problem report if the number looks wrong."
    )


def _capture_facebook() -> str:
    """Saves one Marketplace results page from the tracker's logged-in Facebook profile."""
    from .facebook import FacebookBrowser, is_login_or_checkpoint, parse_results, search_url

    area = config.load().get("sources", {}).get("facebook_area", "")
    fb = FacebookBrowser()
    pw, ctx = fb._launch(headless=True)
    try:
        page = ctx.new_page()
        page.goto(search_url("nissan 200sx", 1000, 50000, area), wait_until="domcontentloaded")
        page.wait_for_timeout(8000)
        html, url = page.content(), page.url
    finally:
        ctx.close()
        pw.stop()
    if is_login_or_checkpoint(url, html):
        return "Facebook wants a login. Use Settings > Facebook > Log in first. Nothing saved."
    out_dir = paths.data_dir() / "recipes" / "fixtures"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "facebook.html"
    out.write_text(scrub(html), encoding="utf-8")
    return f"Saved {out}. The tracker read {len(parse_results(html))} listings from it. Send this file to the maintainer."
