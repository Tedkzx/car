"""Drives the Microsoft Edge already installed on Windows (spec section 5).

Playwright with channel="msedge" and a persistent profile in the app-data
folder, so no browser download is needed. One page load per search. The page
is loaded as a person would see it; any check, captcha or login wall stops
the collector with a plain message. Nothing here tries to get past one.
"""
from __future__ import annotations

import logging
import random
import threading
import time

from core import paths

log = logging.getLogger(__name__)


class EdgeBrowser:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self._ctx = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._ctx is not None:
            return
        from playwright.sync_api import sync_playwright  # imported lazily: optional at runtime

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(paths.data_dir() / "browser-profile"),
            channel="msedge",
            headless=self.headless,
            locale="en-GB",
        )

    def get(self, url: str) -> tuple[int, str, str]:
        with self._lock:
            self._ensure()
            page = self._ctx.new_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(random.uniform(3, 6))  # let the results render
                return (resp.status if resp else 0), page.content(), page.url
            finally:
                page.close()

    def close(self) -> None:
        with self._lock:
            try:
                if self._ctx is not None:
                    self._ctx.close()
                if self._pw is not None:
                    self._pw.stop()
            except Exception as e:  # closing must never crash the app
                log.debug("browser close: %s", e)
            self._ctx = self._pw = None
