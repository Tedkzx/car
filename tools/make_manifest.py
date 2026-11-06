"""Rebuild recipes/manifest.json after changing a recipe or fixture.

    python tools/make_manifest.py

Running installs download changed recipes listed here (core/updater.py).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.updater import build_manifest  # noqa: E402

out = ROOT / "recipes" / "manifest.json"
out.write_text(json.dumps(build_manifest(ROOT / "recipes"), indent=1, sort_keys=True) + "\n")
print(f"Wrote {out}")
