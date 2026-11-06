"""Estimated landed cost of a Japan import (spec section 8).

    CIF    = car + freight + insurance
    duty   = duty_rate × CIF
    VAT    = 0.20 × (CIF + duty)
    landed = CIF + duty + VAT + fees + prep allowance

Every rate is a setting, because duty and fees change. Default duty is 10% (the safe case):
0% may apply to Japan-built cars under the UK-Japan agreement, but only with a statement on
origin from the exporter. Check the UK Trade Tariff (commodity 8703) before relying on it.
Never add freight twice: a CIF price already includes it.
"""
from __future__ import annotations

DEFAULT_FEES = {
    "freight_gbp": 1500,       # RoRo; sources range about £800-2,000
    "insurance_gbp": 100,
    "agent_fees_gbp": 450,     # agent and auction fees
    "port_gbp": 250,           # port handling and customs clearance
    "inland_gbp": 300,         # about £200-400
    "mot_gbp": 55,
    "dvla_gbp": 55,            # first registration: re-check the current fee
    "iva_gbp": 0,              # not needed for cars over 10 years old; all in-scope models are
}


def landed_cost(price_gbp: float, basis: str, imports_cfg: dict) -> dict:
    fees = {**DEFAULT_FEES, **{k: v for k, v in (imports_cfg.get("fees") or {}).items() if v is not None}}
    basis = (basis or "FOB").upper()
    duty_rate = float(imports_cfg.get("duty_rate", 0.10))
    vat_rate = float(imports_cfg.get("vat_rate", 0.20))
    prep = float(imports_cfg.get("prep_allowance_gbp", 750))
    if basis == "CIF":
        cif, freight, insurance = price_gbp, 0.0, 0.0
    else:  # FOB, or a Japanese retail price at the dealer's door
        freight, insurance = float(fees["freight_gbp"]), float(fees["insurance_gbp"])
        cif = price_gbp + freight + insurance
    duty = duty_rate * cif
    vat = vat_rate * (cif + duty)
    other = sum(float(fees[k]) for k in ("agent_fees_gbp", "port_gbp", "inland_gbp", "mot_gbp", "dvla_gbp", "iva_gbp"))
    total = cif + duty + vat + other + prep
    return {
        "car_gbp": round(price_gbp), "basis": basis, "freight": round(freight), "insurance": round(insurance),
        "cif": round(cif), "duty_rate": duty_rate, "duty": round(duty), "vat": round(vat), "fees": round(other),
        "prep": round(prep), "landed": round(total),
    }
