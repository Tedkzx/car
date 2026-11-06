"""Where the app keeps its files.

On Windows everything lives in %APPDATA%\\SX200Tracker\\ (spec section 11).
SX200_DATA overrides it, which the tests and developers use.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "SX200Tracker"


def data_dir() -> Path:
    override = os.environ.get("SX200_DATA")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    else:
        base = Path.home() / ".sx200tracker"
    base.mkdir(parents=True, exist_ok=True)
    for sub in ("logs", "recipes", "raw", "browser-profile"):
        (base / sub).mkdir(exist_ok=True)
    return base


def bundle_dir() -> Path:
    """Folder holding the files shipped with the app (recipes, templates).

    PyInstaller unpacks them into sys._MEIPASS; in development it is the repo root.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def db_path() -> Path:
    return data_dir() / "tracker.db"


def config_path() -> Path:
    return data_dir() / "config.yaml"


def log_path() -> Path:
    return data_dir() / "logs" / "tracker.log"
