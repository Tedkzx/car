"""Precision and recall of the matcher on the labelled set (spec section 3).

Run:  python -m tests.labelled_eval        (prints a report and every miss)

Labels: in (factory SR20DET 200SX/Silvia), out (anything else), check (the ad
does not say enough; must NOT be predicted in). Precision is strict: a
`check` listing predicted in counts as a false positive.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.matcher import classify  # noqa: E402
from core.normalizer import normalise  # noqa: E402

LABELLED = ROOT / "tests" / "labelled" / "listings.jsonl"
NEVER_ALERT = {"parts", "wanted", "scam_template", "fits_spam", "scale_model", "lead_gen", "not_a_car"}


def load() -> list[dict]:
    return [json.loads(line) for line in LABELLED.read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate(rows: list[dict] | None = None) -> dict:
    rows = rows or load()
    tp = fp = fn = 0
    misses, never_alert_hits = [], []
    for row in rows:
        listing = normalise("labelled", {
            "id": row["id"], "url": "", "title": row["title"], "description": row.get("description", ""),
            "year": row.get("year"), "location": row.get("location", ""),
        })
        m = classify(listing)
        truth_in = row["label"] == "in"
        if m.in_scope and truth_in:
            tp += 1
        elif m.in_scope and not truth_in:
            fp += 1
            misses.append(("false positive", row, m))
            if row.get("category") in NEVER_ALERT:
                never_alert_hits.append(row["id"])
        elif truth_in and not m.in_scope:
            fn += 1
            misses.append(("missed", row, m))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "n": len(rows), "real": sum(r["origin"] == "real" for r in rows), "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "never_alert_hits": never_alert_hits, "misses": misses,
    }


def main() -> None:
    r = evaluate()
    print(f"Labelled listings: {r['n']} ({r['real']} real, {r['n'] - r['real']} synthetic)")
    print(f"Precision {r['precision']:.1%}  Recall {r['recall']:.1%}  (TP {r['tp']}, FP {r['fp']}, FN {r['fn']})")
    print(f"Parts / wanted / scam alerts: {len(r['never_alert_hits'])}")
    for kind, row, m in r["misses"]:
        print(f"  {kind}: [{row['id']}] {row['title']!r} -> {m.generation}/{m.engine_status}/{m.excluded}; {m.reasons}")


if __name__ == "__main__":
    main()
