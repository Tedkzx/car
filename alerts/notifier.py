"""Builds plain-English alerts and sends them by ntfy and Windows (spec section 10)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from . import ntfy, windows_toast

log = logging.getLogger(__name__)

SAFETY = "Always see the car in person and run an HPI check before paying anything."
IMPORT_NOTE = ("Import: duty may be 0% with an origin statement from the exporter. HMRC must be told (NOVA) "
               "within 14 days of arrival. Use an inspection service in Japan before paying.")
GEN_NAMES = {"S14": "S14", "S14a": "S14a (facelift)", "S15": "S15", "S13": "S13"}
MARKET_NAMES = {"uk_200sx": "UK car", "jdm_registered_in_uk": "Japanese import, UK registered", "jdm_overseas": "In Japan"}
TIER_NAMES = {"stock": "No mods mentioned", "light": "Lightly modified", "medium": "Modified", "heavy": "Heavily modified"}


def money(v: float | None) -> str:
    return f"£{v:,.0f}" if v else "Price not shown"


@dataclass
class Alert:
    kind: str  # new, price_drop, relisted, owner_action, test
    title: str
    message: str
    url: str | None = None
    image: str | None = None
    tags: list[str] = field(default_factory=list)
    priority: int = 3


def car_alert(kind: str, car: dict, listing: dict, sources: list[dict], old_price: float | None = None) -> Alert:
    gen = GEN_NAMES.get(car["generation"], "200SX / Silvia")
    title_bits = {"new": "New", "price_drop": "Price drop", "relisted": "Back on sale"}
    landed = listing.get("landed_cost_gbp")
    if landed:
        price = f"about {money(landed)} landed"
    else:
        price = money(listing["price_gbp"])
    current = listing.get("price_original") if listing.get("price_original") is not None else listing["price_gbp"]
    if kind == "price_drop" and old_price and current:
        cut = 100 * (old_price - current) / old_price
        price = (f"{price}, cut {cut:.0f}%" if landed else f"{money(old_price)} → {money(listing['price_gbp'])}")
    title = f"{title_bits.get(kind, 'Update')}: {gen}, {price}"
    lines = [
        listing["title"],
        f"{MARKET_NAMES.get(car['market'], '')} · {TIER_NAMES.get(car['mod_tier'] or 'stock', '')} · score {car['score']:.0f}/100",
    ]
    if listing.get("year") or listing.get("mileage"):
        bits = [str(listing["year"]) if listing.get("year") else None,
                f"{listing['mileage']:,} miles" if listing.get("mileage") else None]
        lines.append(" · ".join(b for b in bits if b))
    if landed:
        lines.append(f"Asking {money(listing['price_gbp'])} in Japan ({listing.get('currency')} "
                     f"{listing.get('price_original'):,.0f} {listing.get('price_basis') or ''}). Landed estimate includes "
                     f"shipping, 10% duty, VAT, fees and £750 prep.")
    if listing.get("location_text"):
        loc = listing["location_text"]
        if listing.get("distance_miles") is not None:
            loc += f" ({listing['distance_miles']:.0f} miles away)"
        lines.append(loc)
    flags = car.get("flags_list") or []
    if flags:
        lines.append("Watch out: " + "; ".join(flags))
    if len(sources) > 1:
        lines.append("Also listed on: " + ", ".join(s["source_name"] for s in sources[1:]))
    if landed:
        lines.append(IMPORT_NOTE)
    lines.append(SAFETY)
    tags = {"new": ["car"], "price_drop": ["chart_with_downwards_trend"], "relisted": ["repeat"]}.get(kind, [])
    images = listing.get("image_list") or []
    return Alert(kind, title, "\n".join(lines), listing["url"], images[0] if images else None, tags,
                 4 if kind == "new" else 3)


class Notifier:
    def __init__(self, cfg_getter: Callable[[], dict], ntfy_send=ntfy.send, toast_send=windows_toast.send, db=None,
                 clock=None):
        self.cfg = cfg_getter
        self._ntfy = ntfy_send
        self._toast = toast_send
        self.db = db  # when set, alerts during quiet hours wait in a queue
        self._clock = clock

    def send(self, alert: Alert) -> bool:
        from . import digest

        if self.db is not None and alert.kind not in ("test", "owner_action") and \
                digest.in_quiet_hours(self.cfg(), self._clock() if self._clock else None):
            digest.queue_alert(self.db, alert)
            return True
        return self._deliver(alert)

    def flush_quiet_queue(self) -> int:
        """Send alerts held during quiet hours, once they are over."""
        from . import digest

        if self.db is None or digest.in_quiet_hours(self.cfg(), self._clock() if self._clock else None):
            return 0
        held = digest.take_queue(self.db)
        for a in held:
            self._deliver(Alert(**a))
        return len(held)

    def _deliver(self, alert: Alert) -> bool:
        a = self.cfg()["alerts"]
        ok = False
        if a.get("ntfy_enabled", True) and a.get("ntfy_topic"):
            ok = self._ntfy(a.get("ntfy_server", "https://ntfy.sh"), a["ntfy_topic"], alert.title, alert.message,
                            click=alert.url, attach=alert.image, tags=alert.tags, priority=alert.priority) or ok
        if a.get("windows_enabled", True):
            ok = self._toast(alert.title, alert.message.split("\n")[0], alert.url) or ok
        return ok

    def maintainer(self, title: str, message: str) -> bool:
        """Technical problems go to the maintainer's optional topic, never to the owner's phone."""
        a = self.cfg()["alerts"]
        topic = a.get("maintainer_ntfy_topic")
        if not topic:
            return False
        return self._ntfy(a.get("ntfy_server", "https://ntfy.sh"), topic, title, message, tags=["wrench"], priority=2)

    def test(self) -> bool:
        # No click link: the dashboard lives on the PC and cannot open from a phone.
        return self.send(Alert(
            "test", "SX200 Tracker is set up",
            "Phone alerts are working. You will get a message like this when a matching car is listed.",
            None, None, ["white_check_mark"], 3,
        ))
