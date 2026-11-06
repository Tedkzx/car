"""Turns a collector's raw dict into a Listing (spec section 6, pipeline step 2)."""
from __future__ import annotations

import re
from typing import Any

from enrich import plate_extract

from .models import Listing

POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?\b")
UK_WORDS = ("united kingdom", "uk", "england", "scotland", "wales", "northern ireland", "great britain", "gb")
NON_UK_COUNTRIES = (
    "south africa", "australia", "new zealand", "united states", "usa", "canada", "germany", "netherlands",
    "belgium", "france", "spain", "italy", "ireland", "republic of ireland", "sweden", "norway", "denmark",
    "poland", "switzerland", "austria", "portugal", "japan", "united arab emirates", "dubai", "malta", "cyprus",
)


def parse_money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).replace(",", "")
    if re.search(r"\b(poa|price on application|offers?|swap)\b", text, re.I) and not re.search(r"\d{3,}", text):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k)?\b", text, re.I)
    if not m:
        return None
    amount = float(m.group(1))
    if m.group(2):
        amount *= 1000
    return amount if amount > 0 else None


def parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k)?", text, re.I)
    if not m:
        return None
    n = float(m.group(1))
    if m.group(2):
        n *= 1000
    return int(n)


def parse_year(value: Any, fallback_text: str = "") -> int | None:
    for candidate in (value, fallback_text):
        if candidate is None:
            continue
        m = re.search(r"\b(19[89]\d|20[0-2]\d)\b", str(candidate))
        if m:
            return int(m.group(1))
    return None


def parse_mileage(value: Any, fallback_text: str = "") -> int | None:
    if value not in (None, ""):
        n = parse_int(value)
        if n is not None:
            if re.search(r"\bkm\b", str(value), re.I):
                n = int(n * 0.621371)
            return n
    m = re.search(r"(\d[\d,]{2,7})\s*(miles|mi\b|km\b)", fallback_text or "", re.I)
    if m:
        n = int(m.group(1).replace(",", ""))
        return int(n * 0.621371) if m.group(2).lower().startswith("km") else n
    return None


def find_postcode(text: str) -> str | None:
    if not text:
        return None
    m = POSTCODE.search(text.upper())
    if m and m.group(2):
        return f"{m.group(1)} {m.group(2)}"
    return None


def country_of(location: str) -> str | None:
    loc = (location or "").lower()
    if not loc:
        return None
    for c in NON_UK_COUNTRIES:
        if re.search(rf"\b{re.escape(c)}\b", loc):
            return c
    for w in UK_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", loc):
            return "uk"
    return None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " | ".join(str(v) for v in value if v)
    return re.sub(r"\s+", " ", str(value)).strip()


def normalise(source: str, raw: dict, default_country: str | None = None) -> Listing:
    title = clean_text(raw.get("title"))
    description = clean_text(raw.get("description"))
    attributes = clean_text(raw.get("attributes"))
    blob = " ".join([title, description, attributes])
    location = clean_text(raw.get("location"))
    images = raw.get("images") or []
    if isinstance(images, str):
        images = [images]
    listing = Listing(
        source=source,
        source_id=str(raw.get("id") or raw.get("url")),
        url=str(raw.get("url") or ""),
        title=title,
        description=description,
        price_gbp=parse_money(raw.get("price")),
        price_basis=str(raw.get("price_basis") or "retail"),
        currency=str(raw.get("currency") or "GBP"),
        location_text=location,
        postcode=find_postcode(location),
        year=parse_year(raw.get("year"), blob),
        mileage=parse_mileage(raw.get("mileage"), blob),
        colour=clean_text(raw.get("colour")) or None,
        seller_type=(clean_text(raw.get("seller_type")).lower() or None),
        image_urls=[str(u) for u in images if u][:10],
        listed_estimate=clean_text(raw.get("listed")) or None,
        chassis_no=clean_text(raw.get("chassis_no")) or None,
        extra={"attributes": attributes} if attributes else {},
    )
    listing.country = country_of(location) or default_country
    if listing.seller_type:
        if "trade" in listing.seller_type or "dealer" in listing.seller_type:
            listing.seller_type = "trade"
        elif "private" in listing.seller_type:
            listing.seller_type = "private"
    if not listing.seller_type and re.search(r"\bprivate\b", attributes, re.I):
        listing.seller_type = "private"
    plate, conf = plate_extract.extract(" ".join([title, description]))
    if raw.get("reg"):
        plate, conf = plate_extract.normalise(str(raw["reg"])), 0.95
    listing.reg, listing.reg_confidence = plate, conf
    return listing
