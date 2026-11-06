"""Rebuild recipes/manifest-v3.json and manifest.json after changing a recipe or fixture.

    python tools/make_manifest.py

Running installs download changed recipes listed here (core/updater.py).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.updater import build_manifest  # noqa: E402

# manifest-v3.json: read by 0.3.0 and later; every file, with the app version each needs.
# manifest.json: read by 0.2.x; only what 0.2.x can handle. Keep it frozen at "0.2.0".
for name, version in (("manifest-v3.json", None), ("manifest.json", "0.2.0")):
    out = ROOT / "recipes" / name
    out.write_text(json.dumps(build_manifest(ROOT / "recipes", version), indent=1, sort_keys=True) + "\n")
    print(f"Wrote {out}")
