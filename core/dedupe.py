"""Merging one car seen on several sites into one canonical car (spec section 6).

Rules, in order:
1. Same UK plate: merge automatically.
2. Same chassis or frame number: merge automatically.
3. Perceptual image hash match (Hamming distance 6 or less) on at least two images: merge automatically.
4. Same generation and colour, price within 10%, same town or postcode district, mileage within 1,000:
   a probable duplicate. Ask the owner in the dashboard; never merge on this alone.
Rules 1 and 2 run when a listing first gets a car (pipeline._car_for). This module does 3 and 4.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Callable

from .db import DB, now

log = logging.getLogger(__name__)
HASH_DISTANCE = 6
IMAGES_PER_LISTING = 3


def default_image_fetcher() -> Callable[[str], bytes | None]:
    import httpx

    client = httpx.Client(timeout=15, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SX200Tracker"})

    def fetch(url: str) -> bytes | None:
        try:
            r = client.get(url)
            if r.status_code == 200 and len(r.content) < 8_000_000:
                return r.content
        except Exception as e:
            log.debug("image fetch failed %s: %s", url, e)
        return None

    return fetch


def phash_bytes(data: bytes) -> str | None:
    try:
        import imagehash
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return str(imagehash.phash(im.convert("RGB")))
    except Exception as e:
        log.debug("could not hash image: %s", e)
        return None


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def hash_listing_images(db: DB, listing_id: int, fetch: Callable[[str], bytes | None]) -> list[str]:
    rows = db.query("SELECT url, phash FROM images WHERE listing_id=? LIMIT ?", (listing_id, IMAGES_PER_LISTING))
    hashes = []
    for r in rows:
        h = r["phash"]
        if not h:
            data = fetch(r["url"])
            h = phash_bytes(data) if data else None
            if h:
                db.execute("UPDATE images SET phash=? WHERE listing_id=? AND url=?", (h, listing_id, r["url"]))
        if h:
            hashes.append(h)
    return hashes


def merge_cars(db: DB, keep: int, drop: int) -> None:
    if keep == drop:
        return
    db.execute("UPDATE car_listings SET car_id=? WHERE car_id=?", (keep, drop))
    db.execute("UPDATE alerts_sent SET car_id=? WHERE car_id=?", (keep, drop))
    db.execute("UPDATE dup_candidates SET state='merged' WHERE (car_a=? OR car_b=?)", (drop, drop))
    row = db.one("SELECT shortlist_state, owner_notes FROM cars WHERE id=?", (drop,))
    if row:
        # Keep the owner's shortlist and notes if they were on the car being folded in.
        if row["shortlist_state"] == "shortlisted":
            db.execute("UPDATE cars SET shortlist_state='shortlisted' WHERE id=?", (keep,))
        if row["owner_notes"]:
            db.execute("UPDATE cars SET owner_notes=TRIM(COALESCE(owner_notes,'') || ' ' || ?) WHERE id=?",
                       (row["owner_notes"], keep))
    db.execute("DELETE FROM cars WHERE id=?", (drop,))


def _district(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*\d[A-Z]{2}\b", text.upper())
    if m:
        return m.group(1)
    town = re.split(r",", text)[0].strip().lower()
    return town or None


def check(db: DB, car_id: int, listing_id: int, fetch: Callable[[str], bytes | None] | None) -> int | None:
    """Run rules 3 and 4 for a newly seen listing. Returns the car it was merged into, if any."""
    me = db.one("SELECT * FROM listings WHERE id=?", (listing_id,))
    car = db.one("SELECT generation FROM cars WHERE id=?", (car_id,))
    if me is None or car is None:
        return None
    others = db.query(
        "SELECT c.id AS car_id, li.* FROM cars c JOIN car_listings cl ON cl.car_id=c.id "
        "JOIN listings li ON li.id=cl.listing_id WHERE c.id<>? AND li.source<>? AND li.status IN ('active','relisted') "
        "AND (c.generation=? OR c.generation='unknown' OR ?='unknown')",
        (car_id, me["source"], car["generation"], car["generation"]))
    if not others:
        return None
    # Rule 3: matching photos.
    if fetch is not None:
        mine = hash_listing_images(db, listing_id, fetch)
        if len(mine) >= 2:
            for o in others:
                theirs = hash_listing_images(db, o["id"], fetch)
                matched = sum(1 for a in mine if any(hamming(a, b) <= HASH_DISTANCE for b in theirs))
                if matched >= 2:
                    merge_cars(db, o["car_id"], car_id)
                    return o["car_id"]
    # Rule 4: probable duplicate, ask the owner.
    for o in others:
        if not (me["price_gbp"] and o["price_gbp"]):
            continue
        if abs(me["price_gbp"] - o["price_gbp"]) > 0.10 * max(me["price_gbp"], o["price_gbp"]):
            continue
        if (me["colour"] or "").lower() != (o["colour"] or "").lower() or not me["colour"]:
            continue
        if not _district(me["postcode"] or me["location_text"]) or \
                _district(me["postcode"] or me["location_text"]) != _district(o["postcode"] or o["location_text"]):
            continue
        if me["mileage"] and o["mileage"] and abs(me["mileage"] - o["mileage"]) > 1000:
            continue
        a, b = sorted((car_id, o["car_id"]))
        db.execute("INSERT OR IGNORE INTO dup_candidates(car_a, car_b, reason, created_at) VALUES (?,?,?,?)",
                   (a, b, "Same colour and area, similar price and mileage", now()))
    return None
