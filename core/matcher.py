"""Classifies a listing (spec section 3). The job is precision.

Alert only on factory-SR20DET 200SX and Silvia cars. Keep swaps, NA cars,
parts, wanted ads, scale models, lead-gen and scam templates out. Every
decision carries plain-English reasons so it can be explained to the owner.
"""
from __future__ import annotations

import re

from .models import Listing, Match

F = re.I


def rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, F)


# ---- model identification -------------------------------------------------
MODEL_200SX = rx(r"\b200\s?-?\s?sx\b|\b200sx")
MODEL_SILVIA = rx(r"\bsil[vb]?ia\b|\bsilva\b|\bsilvea\b")
MODEL_180 = rx(r"\b180\s?-?sx\b|\bsileighty\b|\bonevia\b|\brps13\b|\bkps13\b")
CHASSIS_S15 = rx(r"\b(c?s15)\b|\bs-15\b")
CHASSIS_S14A = rx(r"\bs14\s?a\b|\bs14\s?kouki\b|\bkouki\s?s14\b")
CHASSIS_S14 = rx(r"\b(c?s14)\b|\bs-14\b")
CHASSIS_S13 = rx(r"\b(p?s13)\b|\bs-13\b")
CHASSIS_S12 = rx(r"\bs12\b")
SR20DET = rx(r"\bsr\s?20\s?-?\s?det\b|\bsr20\s?turbo\b")
SR20_BARE = rx(r"\bsr\s?20\b(?!\s?-?\s?(de\b|det|ve))")
SR20DE_NA = rx(r"\bsr\s?20\s?-?\s?de\b(?!t)|\bsr20ve\b")
CA18DET = rx(r"\bca\s?18\s?-?\s?det\b|\bca18\b")
TURBO_WORD = rx(r"\bturbo\b|\bturbocharged\b")
NON_TURBO = rx(r"\bnon[\s-]?turbo\b|\bnaturally aspirated\b|\bn/a engine\b|\bno turbo\b")
SPEC_R = rx(r"\bspec[\s-]?r\b|\bspecr\b")
SPEC_S = rx(r"\bspec[\s-]?s\b|\bspecs\b(?!\w)")
KS_TRIM = rx(r"\bk['’`]?s\b(?!\s*(?:miles|km))|\bks\s+(?:type|aero|turbo)")
QS_TRIM = rx(r"\bq['’`]s\b|\bqs\b|\bj['’`]s\b|\bvarietta\b")
KOUKI = rx(r"\bkouki\b|\bfacelift\b")
ZENKI = rx(r"\bzenki\b|\bpre[\s-]?facelift\b")
IMPORT_WORDS = rx(r"\bimport(ed)?\b|\bjdm\b|\bjapanese\b|\bjapan\b|\bjap\b|\bauction sheet\b|\bauction grade\b")

# ---- swaps and other engines ----------------------------------------------
SWAP_WORDS = rx(
    r"\bswap(ped)?\b|\bconver(ted|sion)\b|\bsr20\s?(det\s?)?conversion\b|\bre-?engined\b|"
    r"\bwith (an? )?s1[345] sr20(det)?\b|\bs15 (sr20det|engine|motor) fitted\b|\bengine transplant\b"
)
OTHER_ENGINE = rx(
    r"\bls[1-7]\b|\bv8\b|\bv12\b|\bv6\b|\b[12]jz(-?gte)?\b|\brb2[56](det)?\b|\brb30\b|\bk2[04]\b|\b4age\b|"
    r"\bvq3[57]\b|\bka24(de)?\b|\bsr22\b|\bcoyote\b|\b13b\b|\bvg30\b|\b5\.3\b"
)

# ---- hard excludes --------------------------------------------------------
WANTED = rx(
    r"\bwanted\b|\bwtb\b|\bwant to buy\b|\blooking for\b|\bsearching for\b|\bin search of\b|\biso\b|"
    r"\bcash waiting\b|\bwe buy\b|\bbuying (all|any)\b|\bsell your\b"
)
PART_NOUNS = (
    r"engine|motor|gearbox|gear box|transmission|ecu|loom|wiring loom|wheels?|alloys?|rims?|tyres?|kit|bodykit|body kit|"
    r"bumper|bonnet|wing|spoiler|headlights?|tail ?lights?|lights?|seats?|interior|dash|dashboard|turbocharger|"
    r"exhaust|manifold|intercooler|front mount|fmic|radiator|coilovers?|suspension|diff|differential|clutch|flywheel|"
    r"brakes?|calipers?|hub|driveshafts?|propshaft|door|doors|shell|panel|mirror|steering wheel|"
    r"badge|emblem|brochure|keyring|key ring|poster|cover|mats|decal|stickers?"
)
PART_PREFIX = (
    r"(?:(?:genuine|oem|used|new|jdm|rare|original|kouki|zenki|front|rear|full|complete|pair of|set of|"
    r"4x|x4|1x|2x|5 speed|6 speed|sr20(?:det)?|ca18(?:det)?)[\s,/-]+)*"
)
# The ad's title starts with a part ("SR20DET engine", "Turbo for SR20DET", "Coilovers to fit ...").
PARTS_HEAD = rx(rf"^{PART_PREFIX}(?:{PART_NOUNS}|turbo)\b(?!\s*(?:coupe|car|\d))")
# The title ends with a part ("Nissan Silvia S15 wheels").
PARTS_TAIL = rx(rf"\b(?:{PART_NOUNS})[\s.!)]*$")
CAR_CUES = rx(r"\bmot\b|\bowners?\b|\bdrives\b|\bdriven\b|\bdaily\b|\bv5c?\b|\bservice history\b|\bfsh\b|"
              r"\bcambelt\b|\bstamped\b|\bkeys\b|\bmileage\b|\bregistered\b|\bgarage(d| kept)\b|\bruns\b|"
              r"\bhistory\b|\bcoupe\b|\bhpi\b|\b(19|20)\d\d\b")
PARTS_ANY = rx(r"\bbreaking\b|\bfor parts\b|\bparts only\b|\bspares only\b|\bpart ex(change)? only\b|\bstripping\b|"
               r"\bshell only\b|\brolling shell\b|\bengine only\b|\bengine (and|&) gearbox\b|"
               r"\bcomplete engine\b|\bengine for sale\b|\bstarter motor\b|\balternator\b|\bfuel pump\b|\binjectors?\b|"
               r"\bhaynes\b|\bworkshop manual\b|\bowners? (hand)?book\b|\bowner'?s manual\b")
NEGATED = rx(r"\b(never|not|no|wasn'?t|isn'?t|hasn'?t)\s+(been\s+|an?\s+)?(swapped|swap|converted|conversion|import(ed)?|jdm|modified|tracked|drifted)\b")
FITS_SPAM = rx(r"\bfits?\b.{0,40}\b(200\s?sx|silvia|s1[345])\b|\bsuitable for\b|\bto fit\b|\bcompatible with\b|"
               r"\bfor (nissan )?(200\s?sx|silvia|s1[345])\b")
SCALE_MODEL = rx(r"\bdie-?cast\b|\b1[:/]\s?(18|24|43|64|10|12)\b|\bscale model\b|\bmodel car\b|\btamiya\b|"
                 r"\bhot wheels\b|\bkyosho\b|\btomica\b|\bmatchbox\b|\brc car\b|\bradio control(led)?\b|\blego\b|"
                 r"\bplayset\b|\btoy\b|\bmodel kit\b|\bresin model\b|\bigniton model\b|\bignition model\b|\bautoart\b")
LEAD_GEN = rx(r"\bbad credit\b|\bguaranteed (car )?finance\b|\bapply now\b|\blease deals?\b|\bcar finance for\b|"
              r"\bno credit check\b|\bclick (the )?link\b|\bget approved\b")
SCAM_TEMPLATE = rx(
    r"\b(currently|presently) (abroad|overseas|deployed|offshore|on (an )?oil rig)\b|\bmilitary\b|\barmy\b|"
    r"\bebay (vehicle purchase )?protection\b|\bshipping (company|agent)\b|\bescrow\b|\bdelivered to your door\b|"
    r"\bcontact me (at|on|via) \S+@|\bemail me (at|on)\b|\bpayment via\b|\bwestern union\b|\bmoneygram\b|"
    r"\bdeposit (to|via)\b|\bholding deposit\b|\bcourier fee\b|\bgift card\b|\bi'?m (selling|letting) it for my\b|"
    r"\bdeceased (husband|son|father)\b"
)
NOT_A_CAR = rx(r"\bstorage\b|\bgarage (to let|for rent)\b|\bparking space\b|\bjob\b|\bmechanic needed\b|\bnumber plate\b|"
               r"\bprivate plate\b|\bcherished plate\b|\bregistration (number )?for sale\b")

SOLD = rx(r"^\s*[\[(]?\s*sold\b|\bnow sold\b|\bsorry,? (now )?sold\b|\bsold,? sorry\b|\bsale agreed\b|^\s*sold\s*$")

UK_200SX_YEARS = range(1989, 2001)
SILVIA_YEARS = {"S13": range(1988, 1994), "S14": range(1993, 1999), "S15": range(1999, 2003)}


def _has(p: re.Pattern, text: str) -> bool:
    return bool(p.search(text))


def classify(listing: Listing, include_180sx: bool = False, japan_source: bool = False) -> Match:
    title = (listing.title or "").strip()
    text = NEGATED.sub(" ", listing.text)
    reasons: list[str] = []

    def out(code: str, why: str, generation: str = "unknown", engine: str = "unknown") -> Match:
        return Match(generation, engine, _market(listing, text, japan_source), 0.95, False, code, reasons + [why])

    # 1. Is this a 200SX or Silvia at all?
    is_200sx = _has(MODEL_200SX, text)
    is_silvia = _has(MODEL_SILVIA, text)
    chassis_hit = any(_has(p, text) for p in (CHASSIS_S15, CHASSIS_S14, CHASSIS_S14A, CHASSIS_S13))
    nissan = _has(rx(r"\bnissan\b"), text)
    is_180 = _has(MODEL_180, text)
    if not (is_200sx or is_silvia or is_180 or (chassis_hit and (nissan or _has(SR20DET, text)))):
        return out("other_model", "Not a Nissan 200SX or Silvia")

    # 2. Hard excludes, strongest first.
    status = listing.extra.get("sale_status", "")
    if _has(SOLD, title) or _has(SOLD, status) or _has(SOLD, listing.description[:200]):
        return out("sold", "The ad says the car is already sold")
    if _has(SCALE_MODEL, text):
        return out("scale_model", "A scale model or toy, not a car")
    if _has(WANTED, title) or _has(rx(r"^\s*(wanted|wtb)\b"), text):
        return out("wanted", "A wanted ad, not a car for sale")
    if _has(NOT_A_CAR, title):
        return out("not_a_car", "Not a car for sale (storage, plates or similar)")
    if _has(LEAD_GEN, text):
        return out("lead_gen", "A finance or lease advert, not a real car")
    if _has(SCAM_TEMPLATE, text):
        return out("scam_template", "Wording matches known scam templates")
    if _is_parts(title, text):
        return out("parts", "Parts or an engine for sale, not a whole car")
    if is_180 and not include_180sx:
        return out("excluded_180sx", "180SX, Sileighty or Onevia, excluded by your settings", "S13")

    # 3. Generation.
    year = listing.year
    gen = "unknown"
    if _has(CHASSIS_S15, text) or (is_silvia and _has(SPEC_R, text) and not _has(CHASSIS_S14, text)):
        gen = "S15"
        reasons.append("S15 chassis named" if _has(CHASSIS_S15, text) else "Silvia Spec-R named, so an S15")
    elif _has(CHASSIS_S14A, text) or (_has(CHASSIS_S14, text) and _has(KOUKI, text)):
        gen = "S14a"
        reasons.append("S14a (facelift) named")
    elif _has(CHASSIS_S14, text):
        gen = "S14a" if (year and year >= 1997 and not _has(ZENKI, text)) else "S14"
        reasons.append("S14 chassis named" + (f", {year} so a facelift S14a" if gen == "S14a" else ""))
    elif _has(CHASSIS_S13, text) or is_180:
        gen = "S13"
        reasons.append("180SX family (S13 platform), included by your settings" if is_180 else "S13 chassis named")
    elif _has(CHASSIS_S12, text) or (year and year < 1988):
        return out("old_generation", "An older S12 or earlier model", "S12")
    elif year:
        if is_200sx and not is_silvia:
            if 1989 <= year <= 1993:
                gen = "S13"
            elif 1994 <= year <= 1996:
                gen = "S14"
            elif 1997 <= year <= 2000:
                gen = "S14a"
            if gen != "unknown":
                reasons.append(f"200SX from {year}, so an {gen}")
        elif is_silvia:
            for g, years in SILVIA_YEARS.items():
                if year in years:
                    gen = g
                    reasons.append(f"Silvia from {year}, so probably an {g}")
                    break
    if gen == "unknown" and year and year > 2002:
        return out("other_model", f"A {year} car, too new to be a 200SX or Silvia")

    market = _market(listing, text, japan_source)

    # 4. Engine status.
    swap = _has(SWAP_WORDS, text)
    other_engine = _has(OTHER_ENGINE, text)
    sr20det = _has(SR20DET, text)
    engine = "unknown"
    conf = 0.5
    if other_engine:
        m = OTHER_ENGINE.search(text)
        engine = "swapped_turbo"
        reasons.append(f"Mentions a non-standard engine ({m.group(0)}), so treated as a swap")
    elif swap:
        engine = "swapped_turbo"
        reasons.append("Ad says the engine was swapped or converted")
    elif gen == "S13" and market == "uk_200sx" and sr20det:
        engine = "swapped_turbo"
        reasons.append("UK S13 200SX cars came with the CA18DET, so an SR20DET means a swap")
    elif _has(SR20DE_NA, text) or _has(NON_TURBO, text) or _has(QS_TRIM, text) or (gen == "S15" and _has(SPEC_S, text)):
        engine = "na"
        reasons.append("Non-turbo model (SR20DE, Q's, J's, Spec-S or Varietta)")
    elif gen == "S13" and _has(CA18DET, text):
        return out("ca18det", "An S13 with the CA18DET engine, outside the SR20DET target", "S13", "unknown")
    elif gen in ("S14", "S14a") and market == "uk_200sx":
        engine = "factory_turbo"
        conf = 0.85 if sr20det or _has(TURBO_WORD, text) else 0.75
        reasons.append("Every UK S14 200SX was an SR20DET turbo")
    elif gen == "S15":
        if _has(SPEC_R, text) or sr20det or _has(TURBO_WORD, text):
            engine = "factory_turbo"
            conf = 0.9 if _has(SPEC_R, text) else 0.8
            reasons.append("Spec-R or SR20DET turbo named")
        else:
            reasons.append("S15 trim not stated: could be a non-turbo Spec-S, needs checking")
    elif gen in ("S14", "S14a"):
        if _has(KS_TRIM, text) or sr20det or _has(TURBO_WORD, text):
            engine = "factory_turbo"
            conf = 0.85
            reasons.append("K's trim or SR20DET turbo named")
        elif is_200sx and not _has(IMPORT_WORDS, text) and listing.country in (None, "uk"):
            engine = "factory_turbo"
            conf = 0.7
            reasons.append("Sold as a 200SX, which was always a turbo")
        else:
            reasons.append("Silvia with no engine or trim stated, needs checking")
    elif gen == "S13":
        if is_180 and (sr20det or _has(TURBO_WORD, text) or _has(rx(r"\btype\s?[xrs]\b"), text)) and (year is None or year >= 1991):
            engine = "factory_turbo"
            conf = 0.7
            reasons.append("1991-on 180SX turbo models came with the SR20DET")
        elif is_silvia and _has(KS_TRIM, text) and sr20det and (year is None or year >= 1991):
            engine = "factory_turbo"
            conf = 0.75
            reasons.append("Late S13 Silvia K's with factory SR20DET")
        elif sr20det or _has(SR20_BARE, text):
            engine = "unknown"
            reasons.append("S13 with an SR20: factory K's not confirmed, could be a swap")
        elif market == "uk_200sx":
            return out("ca18det", "UK S13 200SX cars came with the CA18DET, outside the SR20DET target", "S13")
        else:
            reasons.append("S13 engine not confirmed")
    else:
        if sr20det or _has(TURBO_WORD, text):
            reasons.append("Generation not clear from the ad")
        else:
            reasons.append("Generation and engine not clear from the ad")

    if _has(rx(r"\bsix[\s-]speed\b|\b6[\s-]speed\b"), text) and gen in ("S13", "S14", "S14a") and engine == "factory_turbo":
        reasons.append("Six-speed gearbox is not original on this model, so it may be modified")
        conf -= 0.1

    in_scope = engine == "factory_turbo" and gen in ("S13", "S14", "S14a", "S15")
    if in_scope:
        if is_200sx or is_silvia:
            conf += 0.05
        if year and gen in ("S14", "S14a") and market == "uk_200sx" and year not in UK_200SX_YEARS:
            conf -= 0.2
            reasons.append(f"Year {year} is unusual for a UK 200SX")
    elif engine == "unknown":
        conf = 0.4
    else:
        conf = 0.85
    return Match(gen, engine, market, round(max(0.05, min(conf, 0.99)), 2), in_scope, None, reasons)


def _is_parts(title: str, text: str) -> bool:
    if _has(PARTS_ANY, title) or _has(PARTS_HEAD, title):
        return True
    if _has(FITS_SPAM, title) and not _has(rx(r"\b(19|20)\d\d\b"), title):
        return True
    if _has(PARTS_TAIL, title):
        # "Nissan 200sx S14 Coilovers, FMIC" is a modified car when the ad reads like a car ad.
        return not _has(CAR_CUES, text)
    return False


def _market(listing: Listing, text: str, japan_source: bool) -> str:
    if japan_source or listing.country == "japan":
        return "jdm_overseas"
    if _has(rx(r"\buk (car|model|supplied|spec)\b|\bnot an? import\b"), listing.text):
        return "uk_200sx" if not _has(MODEL_SILVIA, text) else "jdm_registered_in_uk"
    if _has(MODEL_SILVIA, text) or _has(CHASSIS_S15, text) or _has(IMPORT_WORDS, text):
        return "jdm_registered_in_uk"
    return "uk_200sx"
