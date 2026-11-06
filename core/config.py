"""config.yaml: defaults from spec section 11, merged with the owner's saved settings."""
from __future__ import annotations

import copy
import secrets
import threading
from pathlib import Path
from typing import Any

import yaml

from . import paths

DEFAULTS: dict[str, Any] = {
    "budget": {"min_gbp": 10000, "max_gbp": 20000},
    "target": {"engine": "SR20DET", "include_180sx": False, "alert_swaps": False},
    "owner": {"home_postcode": ""},
    "alerts": {
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",
        "ntfy_enabled": True,
        "windows_enabled": True,
        "maintainer_ntfy_topic": "",
        "alert_min_score": 60,
        "digest_time": "08:30",
        "quiet_start": None,
        "quiet_end": None,
    },
    "scoring": {
        "weights": {"stock": 40, "use_history": 15, "engine": 15, "value": 15, "service": 10, "mot": 5}
    },
    "imports": {"duty_rate": 0.10, "classic_relief": False, "vat_rate": 0.20, "prep_allowance_gbp": 750},
    "schedule": {"interval_minutes": 20, "jitter_pct": 20},
    "search": {"price_min_gbp": 8000, "price_max_gbp": 25000},
    "sources": {"facebook": False, "mot_api": False, "email_alerts": False, "disabled": []},
    "updates": {"enabled": True, "repo": "Tedkzx/car", "branch": "main"},
    "setup_complete": False,
}

_lock = threading.Lock()


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def new_topic() -> str:
    """A long random ntfy topic. The topic name is the only secret (spec section 7)."""
    return "sx200-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:24].lower()


def load(path: Path | None = None) -> dict:
    path = path or paths.config_path()
    with _lock:
        saved = {}
        if path.exists():
            saved = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = _merge(DEFAULTS, saved)
        if not cfg["alerts"]["ntfy_topic"]:
            cfg["alerts"]["ntfy_topic"] = new_topic()
            _write(path, cfg)
        return cfg


def save(cfg: dict, path: Path | None = None) -> None:
    path = path or paths.config_path()
    with _lock:
        _write(path, cfg)


def _write(path: Path, cfg: dict) -> None:
    tmp = path.with_suffix(".tmp")
    header = "# SX200 Tracker settings. Change these from the Settings page.\n"
    tmp.write_text(header + yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
