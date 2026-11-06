import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"
RFIX = ROOT / "recipes" / "fixtures"


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SX200_DATA", str(tmp_path / "appdata"))
    yield tmp_path / "appdata"


class FakeFetcher:
    """Serves saved pages instead of the network. pages: list of (status, html) or a callable(url)."""

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if callable(self.pages):
            status, html = self.pages(url)
        else:
            status, html = self.pages[min(len(self.urls) - 1, len(self.pages) - 1)]
        return status, html, url

    def post(self, url, data):
        """Form paging (Goo-net): later pages are empty in tests."""
        self.urls.append((url, data))
        return 200, "<html></html>", url

    def close(self):
        pass


class Sent:
    def __init__(self):
        self.ntfy = []
        self.toasts = []

    def ntfy_send(self, server, topic, title, message, **kw):
        self.ntfy.append({"server": server, "topic": topic, "title": title, "message": message, **kw})
        return True

    def toast_send(self, title, message, url=None):
        self.toasts.append((title, message, url))
        return True


class NoGeo:
    def distance(self, *a):
        return 42.0
