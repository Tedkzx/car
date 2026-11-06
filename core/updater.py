"""Recipe hot-update and app auto-update from the project's GitHub repository (spec sections 5, 11, 13).

Recipes: recipes/manifest.json lists every recipe file with its SHA-256. Changed files are
downloaded into %APPDATA%\\SX200Tracker\\recipes\\ (which overrides the shipped copies) only
after their checksum matches. Recipes are data, never code.

App: the latest GitHub release's SX200-Tracker-Setup.exe is downloaded with its .sha256 file,
checked, and installed silently the next time the tracker starts.

Both need the repository (or its releases) to be readable without a login, because the
installer never carries a key. If they are not, the tracker carries on and says so in Sources.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import paths
from .db import DB, now

log = logging.getLogger(__name__)
ALLOWED_RECIPE_FILE = re.compile(r"^(?:[a-z0-9-]+\.yaml|fixtures/[a-z0-9_-]+\.(?:html|xml|json))$")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


class Updater:
    def __init__(self, db: DB, cfg_getter, version: str, client=None):
        self.db = db
        self.cfg = cfg_getter
        self.version = version
        self._client = client

    def client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=30, follow_redirects=True,
                                        headers={"User-Agent": f"SX200Tracker/{self.version}"})
        return self._client

    def _settings(self) -> dict:
        u = self.cfg().get("updates", {})
        return {"repo": u.get("repo", "Tedkzx/car"), "branch": u.get("branch", "main"), "enabled": u.get("enabled", True)}

    # ---- recipes -------------------------------------------------------
    def update_recipes(self) -> int:
        """Returns how many recipe files changed. Never raises."""
        s = self._settings()
        if not s["enabled"]:
            return 0
        base = f"https://raw.githubusercontent.com/{s['repo']}/{s['branch']}/recipes/"
        try:
            r = self.client().get(base + "manifest.json")
            if r.status_code != 200:
                self._status("recipes", f"Scraper fixes not reachable (status {r.status_code})")
                return 0
            manifest = r.json()
        except Exception as e:
            self._status("recipes", f"Scraper fixes not reachable ({type(e).__name__})")
            return 0
        dest = paths.data_dir() / "recipes"
        changed = 0
        for name, digest in (manifest.get("files") or {}).items():
            if not ALLOWED_RECIPE_FILE.match(name):
                log.warning("manifest names an unexpected file %r; ignored", name)
                continue
            local = dest / name
            bundled = paths.bundle_dir() / "recipes" / name
            current = local if local.exists() else bundled
            if current.exists() and sha256(current.read_bytes()) == digest:
                continue
            try:
                data = self.client().get(base + name).content
            except Exception as e:
                log.info("could not download %s: %s", name, e)
                continue
            if sha256(data) != digest:
                log.warning("checksum mismatch for %s; not used", name)
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            changed += 1
        self._status("recipes", f"Scraper fixes checked {now()[:16].replace('T', ' ')}" +
                     (f", {changed} updated" if changed else ""))
        return changed

    # ---- app -----------------------------------------------------------
    def check_app(self) -> str | None:
        """Downloads a newer installer if there is one. Returns its version."""
        s = self._settings()
        if not s["enabled"] or not getattr(sys, "frozen", False) or sys.platform != "win32":
            return None
        try:
            r = self.client().get(f"https://api.github.com/repos/{s['repo']}/releases/latest",
                                  headers={"Accept": "application/vnd.github+json"})
            if r.status_code != 200:
                self._status("app", f"Update check not reachable (status {r.status_code})")
                return None
            rel: dict[str, Any] = r.json()
        except Exception as e:
            self._status("app", f"Update check not reachable ({type(e).__name__})")
            return None
        latest = rel.get("tag_name", "").lstrip("v")
        if version_tuple(latest) <= version_tuple(self.version):
            self._status("app", f"Up to date (version {self.version})")
            return None
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        exe, digest_url = assets.get("SX200-Tracker-Setup.exe"), assets.get("SX200-Tracker-Setup.exe.sha256")
        if not exe or not digest_url:
            return None
        try:
            expected = self.client().get(digest_url).text.split()[0].lower()
            data = self.client().get(exe).content
        except Exception as e:
            log.info("update download failed: %s", e)
            return None
        if sha256(data) != expected:
            log.warning("update checksum mismatch; not used")
            return None
        out = paths.data_dir() / "updates"
        out.mkdir(exist_ok=True)
        (out / "SX200-Tracker-Setup.exe").write_bytes(data)
        (out / "pending.json").write_text(json.dumps({"version": latest, "sha256": expected}))
        self._status("app", f"Version {latest} downloaded; it installs next time the tracker starts")
        return latest

    def apply_pending(self) -> bool:
        """At startup: run a downloaded installer silently and exit. It restarts the tracker."""
        out = paths.data_dir() / "updates"
        pending, exe = out / "pending.json", out / "SX200-Tracker-Setup.exe"
        if not (pending.exists() and exe.exists()) or sys.platform != "win32":
            return False
        info = json.loads(pending.read_text(encoding="utf-8"))
        pending.unlink()
        if version_tuple(info["version"]) <= version_tuple(self.version) or sha256(exe.read_bytes()) != info["sha256"]:
            return False
        subprocess.Popen([str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/update=1"],
                         close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return True

    def _status(self, kind: str, text: str) -> None:
        self.db.set_meta(f"update_status_{kind}", text)


def build_manifest(recipes_dir: Path) -> dict:
    files = {}
    for f in sorted(recipes_dir.rglob("*")):
        rel = f.relative_to(recipes_dir).as_posix()
        if f.is_file() and ALLOWED_RECIPE_FILE.match(rel):
            files[rel] = sha256(f.read_bytes())
    return {"files": files}
