"""eBay UK through eBay's official Browse API (optional, needs a free developer key).

The owner pastes an App ID (client ID) and Cert ID (client secret) into Settings once; they are
kept in Windows Credential Manager, never in config files or the installer (spec section 11).
Without them this source is simply off.

API: OAuth 2.0 client credentials -> application token (about 2 hours), then
GET /buy/browse/v1/item_summary/search on the EBAY_GB marketplace, Cars category (9801).
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

from .recipe_engine import BlockedError, CollectorError

log = logging.getLogger(__name__)
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
KEYRING_SERVICE = "SX200Tracker"


def get_keys() -> tuple[str | None, str | None]:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, "ebay_client_id"), keyring.get_password(KEYRING_SERVICE, "ebay_client_secret")
    except Exception as e:  # no keyring backend available
        log.info("keyring unavailable: %s", e)
        return None, None


def set_keys(client_id: str, secret: str) -> bool:
    try:
        import keyring

        if client_id and secret:
            keyring.set_password(KEYRING_SERVICE, "ebay_client_id", client_id.strip())
            keyring.set_password(KEYRING_SERVICE, "ebay_client_secret", secret.strip())
        else:
            for name in ("ebay_client_id", "ebay_client_secret"):
                try:
                    keyring.delete_password(KEYRING_SERVICE, name)
                except Exception:
                    pass
        return True
    except Exception as e:
        log.warning("could not save eBay key: %s", e)
        return False


class EbayBrowse:
    _token_cache: dict[str, tuple[str, float]] = {}

    def __init__(self, client_id: str, secret: str, client=None):
        self.client_id, self.secret = client_id, secret
        self._client = client

    def http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=30, headers={"User-Agent": "SX200Tracker"})
        return self._client

    def token(self) -> str:
        cached = self._token_cache.get(self.client_id)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        basic = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
        r = self.http().post(TOKEN_URL, data={"grant_type": "client_credentials", "scope": SCOPE},
                             headers={"Authorization": f"Basic {basic}",
                                      "Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code in (400, 401):
            raise CollectorError("eBay did not accept the key. Check the App ID and Cert ID in Settings")
        if r.status_code != 200:
            raise CollectorError(f"eBay sign-in failed ({r.status_code})")
        body = r.json()
        self._token_cache[self.client_id] = (body["access_token"], time.time() + int(body.get("expires_in", 7200)))
        return body["access_token"]

    def search(self, term: str, price_min: float, price_max: float, category: str = "9801", limit: int = 100) -> list[dict]:
        params = {
            "q": term, "category_ids": category, "limit": str(limit), "sort": "newlyListed",
            "filter": f"price:[{int(price_min)}..{int(price_max)}],priceCurrency:GBP,itemLocationCountry:GB",
        }
        r = self.http().get(SEARCH_URL, params=params, headers={
            "Authorization": f"Bearer {self.token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            "X-EBAY-C-ENDUSERCTX": "contextualLocation=country=GB"})
        if r.status_code == 429:
            raise BlockedError("eBay's daily API allowance is used up; trying again later")
        if r.status_code == 401:
            self._token_cache.pop(self.client_id, None)
            raise CollectorError("eBay key expired or was withdrawn. Check it in Settings")
        if r.status_code != 200:
            raise CollectorError(f"eBay answered with an error ({r.status_code})")
        return [to_raw(i) for i in r.json().get("itemSummaries", [])]


def to_raw(i: dict[str, Any]) -> dict:
    """An item summary in the shape the normaliser expects. Seller details are not read."""
    loc = i.get("itemLocation") or {}
    images = [x.get("imageUrl") for x in [i.get("image") or {}] + (i.get("additionalImages") or []) if x.get("imageUrl")]
    price = i.get("price") or {}
    return {
        "id": i.get("itemId"), "title": i.get("title"), "price": price.get("value"),
        "currency": price.get("currency", "GBP"), "url": i.get("itemWebUrl"),
        "description": i.get("shortDescription") or "", "images": images,
        "location": ", ".join(x for x in (loc.get("city"), loc.get("postalCode"), "United Kingdom"
                                           if loc.get("country") == "GB" else loc.get("country")) if x),
        "attributes": " | ".join(x for x in [i.get("condition"), ", ".join(i.get("buyingOptions") or [])] if x),
    }


def run(recipe_data: dict, cfg: dict, client=None) -> list[dict]:
    client_id, secret = get_keys()
    if not (client_id and secret):
        raise CollectorError("Off: add a free eBay key in Settings to use eBay")
    api = EbayBrowse(client_id, secret, client=client)
    s = cfg.get("search", {})
    out: dict[str, dict] = {}
    for term in recipe_data.get("terms", []):
        for item in api.search(term, s.get("price_min_gbp", 8000), s.get("price_max_gbp", 25000)):
            out.setdefault(str(item["id"]), item)
    return list(out.values())
