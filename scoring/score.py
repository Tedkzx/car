"""Stock-first score, 0-100 (spec section 9).

Milestone 1 ships a provisional version: mod tier, use history, engine status,
a seed-band value score, service history and hard / scam flags from the ad
text. Milestone 3 adds rolling medians, price history and the full flag set.
Mileage is stored and shown but never changes the score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.models import Listing, Match


def rx(p: str) -> re.Pattern:
    return re.compile(p, re.I)


STOCK_POS = rx(r"\b(standard|stock|original|unmodified|unmolested|untouched|oem|factory spec|bone stock)\b|"
               r"\b(one|1|two|2) (previous )?(owners?|keepers?)\b")
MOD_HEAVY = rx(r"\bbig turbo\b|\bgtx\d*\b|\bgt\d{2}\b|\bstandalone\b|\bforged\b|\bstroker\b|\bwide ?body\b|"
               r"\b\d{3,4}\s?bhp\b(?!.*\bstandard\b)|\bhybrid turbo\b|\bbuilt (engine|motor)\b")
MOD_MEDIUM = rx(r"\bcoilovers?\b|\baftermarket wheels\b|\bbody ?kit\b|\baero kit\b|\buprated clutch\b|\bpaddle clutch\b|"
                r"\bvertex\b|\bbomex\b|\bbn sports\b|\bwork wheels\b|\brota\b|\bbig wheels\b|\bbucket seats?\b")
MOD_LIGHT = rx(r"\bexhaust\b|\bintake\b|\binduction kit\b|\bfmic\b|\bfront mount\b|\bintercooler\b|\bremap(ped)?\b|"
               r"\bboost controller\b|\bdump valve\b|\bmods?\b|\bmodified\b")
USE_HISTORY = rx(r"\bdrift(ed|ing| car)?\b|\btrack (car|use|day|days)\b|\btracked\b|\bstripped\b|\broll ?cage\b|"
                 r"\bcaged\b|\bhydro handbrake\b|\brace car\b|\bhill ?climb\b")
NOT_TRACKED = rx(r"\bnever (been )?(tracked|drifted)\b")
SERVICE = rx(r"\bcambelt\b|\btiming belt\b|\bwater pump\b|\bstamped (service )?book\b|\bfull (service )?history\b|"
             r"\bfsh\b|\breceipts\b|\bhistory file\b|\bno advisories\b")
HARD_FLAGS = [
    (rx(r"\bcat\s?[snab]\b|\bcategory [snab]\b|\bwrite[\s-]?off\b"), "Recorded as an insurance write-off (Cat S, N, A or B)"),
    (rx(r"\bcrashed\b|\baccident damage(d)?\b|\bfront end damage\b|\bdamaged repairable\b"), "Accident damage mentioned"),
    (rx(r"\bnon[\s-]?runner\b|\bdoes not run\b|\bdoesn'?t run\b|\bspares (or|and|/) repairs?\b|\bspares or repair\b"), "Not running, or sold as spares or repair"),
    (rx(r"\b(?:auction\s+)?grade\s*:?\s*r[a]?\b"), "Auction grade R or RA: the car has had accident repairs"),
    (rx(r"\bneeds? welding\b|\b(bad|heavy|visible|lots of) rust\b|\brotten\b|\brust holes?\b"), "Rust or welding needed"),
]
SCAM_TEXT = rx(r"\bdeposit\b.{0,30}\b(secure|hold|transfer)\b|\bcourier\b|\bshipping\b|\boverseas\b|\babroad\b|"
               r"\bescrow\b|\bpay(ment)? (off|outside)\b|\bwestern union\b|\bbank transfer only\b")

# Seed price bands (GBP) until 8 comparables exist per cohort. Approximate, set by hand.
SEED_MEDIAN = {"S14": 12000, "S14a": 13500, "S15": 18000, "S13": 14000, "unknown": 13000}


@dataclass
class Score:
    score: float
    mod_tier: str
    reasons: list[str] = field(default_factory=list)
    hard_flags: list[str] = field(default_factory=list)
    scam_flags: list[str] = field(default_factory=list)
    value_provisional: bool = True
    comparables: int = 0


def mod_tier(text: str) -> tuple[str, float]:
    if MOD_HEAVY.search(text):
        return "heavy", 0.1
    if MOD_MEDIUM.search(text):
        return "medium", 0.4
    if MOD_LIGHT.search(text) and not re.search(r"\bno mods\b|\bunmodified\b", text, re.I):
        return "light", 0.7
    return "stock", 0.85


def score(listing: Listing, match: Match, weights: dict, market_lookup=None) -> Score:
    """market_lookup(generation, market, mod_tier) -> (median price or None, number of comparables)."""
    text = listing.text
    reasons: list[str] = []

    # Stock-ness and mod tier.
    tier, stock = mod_tier(text)
    if STOCK_POS.search(text) and tier in ("stock", "light"):
        stock = min(1.0, stock + 0.15)
        reasons.append("Ad says the car is standard or original")
    reasons.append({"stock": "No modifications mentioned", "light": "Light modifications (exhaust, intake or remap)",
                    "medium": "Medium modifications (suspension, wheels or body kit)",
                    "heavy": "Heavy modifications (big turbo, engine build or wide body)"}[tier])

    # Use history.
    use = 1.0
    if USE_HISTORY.search(text) and not NOT_TRACKED.search(text):
        use = 0.0
        reasons.append("Used for drifting or track days")

    # Engine status.
    engine = {"factory_turbo": 1.0, "unknown": 0.35, "swapped_turbo": 0.1, "na": 0.0}.get(match.engine_status, 0.3)
    if match.engine_status == "unknown":
        reasons.append("Engine not confirmed, so the score is capped")

    # Value against a seed band (provisional until there are comparables).
    value = 0.5
    median, comparables = (market_lookup(match.generation, match.market, tier) if market_lookup else (None, 0))
    provisional = median is None
    if provisional:
        median = SEED_MEDIAN.get(match.generation, SEED_MEDIAN["unknown"])
    basis = (f"provisional: only {comparables} similar cars seen so far" if provisional
             else f"compared with {comparables} similar cars, typical price £{median:,.0f}")
    price = listing.effective_price
    if price:
        ratio = price / median
        value = max(0.0, min(1.0, 1.25 - ratio * 0.75))
        if ratio < 0.85:
            reasons.append(f"Cheaper than usual for this kind of {match.generation} ({basis})")
        elif ratio > 1.15:
            reasons.append(f"Dearer than usual for this kind of {match.generation} ({basis})")
        else:
            reasons.append(f"About the usual price ({basis})")

    service = 1.0 if SERVICE.search(text) else 0.4
    if service == 1.0:
        reasons.append("Service history or cambelt receipts mentioned")

    mot = 0.5  # neutral: the MOT API is off by default

    total_w = sum(weights.values()) or 1
    raw = (stock * weights.get("stock", 40) + use * weights.get("use_history", 15) + engine * weights.get("engine", 15)
           + value * weights.get("value", 15) + service * weights.get("service", 10) + mot * weights.get("mot", 5))
    s = 100 * raw / total_w
    if match.engine_status == "unknown":
        s = min(s, 50)
    elif match.engine_status in ("swapped_turbo", "na"):
        s = min(s, 30)

    hard = [why for p, why in HARD_FLAGS if p.search(text)]
    scam = []
    if SCAM_TEXT.search(text):
        scam.append("Mentions deposits, couriers, shipping or a seller abroad")
    if price and price < 0.6 * median and match.in_scope and not listing.landed_cost_gbp:
        scam.append(f"Price is far below the usual price for an {match.generation}")
    return Score(round(s), tier, reasons, hard, scam, provisional, comparables)
