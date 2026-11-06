"""The common listing record every collector's output is normalised into."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str = ""
    description: str = ""
    price_gbp: float | None = None
    price_basis: str = "retail"  # retail, FOB, CIF
    currency: str = "GBP"
    location_text: str = ""
    postcode: str | None = None
    country: str | None = None
    year: int | None = None
    reg: str | None = None
    reg_confidence: float = 0.0
    chassis_no: str | None = None
    mileage: int | None = None
    colour: str | None = None
    seller_type: str | None = None  # private, trade
    image_urls: list[str] = field(default_factory=list)
    listed_estimate: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """All free text the matcher and scorer read."""
        parts = [self.title, self.description]
        for key in ("subtitle", "attributes", "model", "engine", "trim"):
            if self.extra.get(key):
                parts.append(str(self.extra[key]))
        return " \n".join(p for p in parts if p)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Match:
    generation: str  # S13, S14, S14a, S15, unknown
    engine_status: str  # factory_turbo, swapped_turbo, na, unknown
    market: str  # uk_200sx, jdm_registered_in_uk, jdm_overseas
    confidence: float
    in_scope: bool
    excluded: str | None = None  # reason code when hard-excluded (wanted, parts, scam...)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
