"""Best-effort UK registration plate extraction from ad text (spec section 7).

Many ads hide the plate, so this returns (plate, confidence) or (None, 0).
"""
from __future__ import annotations

import re

# Current format (2001 on): AB12 CDE. Letters I, Q, Z are not used in the first two.
CURRENT = re.compile(r"\b([A-HJ-PR-Y][A-HJ-PR-Y])\s?(\d{2})\s?([A-HJ-PR-Z]{3})\b")
# Prefix (1983-2001): A123 BCD. The S14 era cars carry these (L, M, N, P, R, S, T, V, W, X, Y).
PREFIX = re.compile(r"\b([A-HJ-NP-Y])\s?(\d{1,3})\s?([A-HJ-PR-Y]{3})\b")
# Suffix (1963-1983): ABC 123D. Rare for these cars, kept for imports on dateless or age-related plates.
SUFFIX = re.compile(r"\b([A-HJ-PR-Y]{3})\s?(\d{1,3})\s?([A-HJ-NP-Y])\b")

# Words that look like plates but are not.
STOPWORDS = {
    "S14", "S15", "S13", "SR20", "CA18", "FMIC", "ABS", "MOT", "HPI", "FSH", "BHP", "ECU",
}
# Engine and chassis codes that fit a plate pattern ("SR20 DET" looks like a current plate).
ENGINE_CODE = re.compile(r"^(SR|CA|RB|KA|VG|VQ|VR|JZ|FJ|CR|QR|MR|HR|GA|CD|TB|TD|YD|ZD|EJ|BP|FS|KL|SV)\d{2}[A-Z]*$|^\d?JZ")
CONTEXT = re.compile(r"\b(reg(istration)?|plate|reg\.?\s*no|registered as)\b", re.I)


def normalise(plate: str) -> str:
    return re.sub(r"\s+", "", plate.upper())


def extract(text: str) -> tuple[str | None, float]:
    if not text:
        return None, 0.0
    upper = text.upper()
    best: tuple[str | None, float] = (None, 0.0)
    for pattern, base in ((CURRENT, 0.75), (PREFIX, 0.55), (SUFFIX, 0.35)):
        for m in pattern.finditer(upper):
            plate = "".join(m.groups())
            if plate in STOPWORDS or len(plate) < 5 or ENGINE_CODE.match(plate):
                continue
            # A prefix plate must not be a model string like "S15 SPEC" read as S15SPE.
            if pattern is PREFIX and re.fullmatch(r"S1[345].*", plate):
                continue
            conf = base
            window = text[max(0, m.start() - 25) : m.start()]
            if CONTEXT.search(window):
                conf += 0.2
            if conf > best[1]:
                best = (plate, min(conf, 0.95))
    return best
