"""Per-source health, backoff and the circuit breaker (spec section 5).

Owner-facing words only: Working, Slow, Needs attention, Being set up, Paused.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import DB, now

BREAKER_BLOCKS = 3
BREAKER_PAUSE = timedelta(hours=6)
FIRST_BACKOFF = timedelta(minutes=30)


def _dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _ensure(db: DB, source: str) -> None:
    db.execute("INSERT OR IGNORE INTO source_health(source) VALUES (?)", (source,))


ZERO_RUNS_ALERT = 4


def record_attempt(db: DB, source: str) -> None:
    _ensure(db, source)
    db.execute("UPDATE source_health SET last_attempt=? WHERE source=?", (now(), source))


def due(db: DB, source: str, min_interval_minutes: float | None) -> bool:
    """Browser and forum sources run less often than the main loop (spec section 5)."""
    if not min_interval_minutes:
        return True
    row = db.one("SELECT last_attempt FROM source_health WHERE source=?", (source,))
    last = _dt(row["last_attempt"]) if row else None
    return not last or datetime.now(timezone.utc) - last >= timedelta(minutes=float(min_interval_minutes) * 0.95)


def record_success(db: DB, source: str, found: int, duration_s: float) -> bool:
    """Returns True when a source that used to find listings has found none several runs in a row."""
    _ensure(db, source)
    row = db.one("SELECT found_count, zero_runs FROM source_health WHERE source=?", (source,))
    zero_runs = (row["zero_runs"] or 0) + 1 if found == 0 else 0
    db.execute(
        "UPDATE source_health SET last_success=?, found_count=?, consecutive_blocks=0, paused_until=NULL, "
        "last_duration_s=?, zero_runs=? WHERE source=?",
        (now(), found, round(duration_s, 1), zero_runs, source),
    )
    return zero_runs == ZERO_RUNS_ALERT


def record_selftest(db: DB, source: str, error: str | None) -> None:
    _ensure(db, source)
    db.execute("UPDATE source_health SET selftest_error=? WHERE source=?", (error, source))


def record_failure(db: DB, source: str, message: str, blocked: bool) -> bool:
    """Returns True when this failure tripped the circuit breaker."""
    _ensure(db, source)
    row = db.one("SELECT consecutive_blocks FROM source_health WHERE source=?", (source,))
    blocks = (row["consecutive_blocks"] or 0) + 1 if blocked else (row["consecutive_blocks"] or 0)
    paused_until = None
    tripped = False
    if blocked:
        if blocks >= BREAKER_BLOCKS:
            pause = BREAKER_PAUSE
            tripped = blocks == BREAKER_BLOCKS
        else:
            pause = FIRST_BACKOFF * (2 ** (blocks - 1))
        paused_until = (datetime.now(timezone.utc) + pause).isoformat(timespec="seconds")
    db.execute(
        "UPDATE source_health SET last_error=?, last_error_at=?, consecutive_blocks=?, paused_until=? WHERE source=?",
        (message, now(), blocks, paused_until, source),
    )
    return tripped


def is_paused(db: DB, source: str) -> bool:
    row = db.one("SELECT paused_until FROM source_health WHERE source=?", (source,))
    until = _dt(row["paused_until"]) if row else None
    return bool(until and until > datetime.now(timezone.utc))


def status_of(row, recipe_status: str, interval_minutes: int, expect_results: bool = True) -> tuple[str, str]:
    """(label, plain-English detail) for one source."""
    if row is None or (not row["last_success"] and not row["last_error"]):
        if recipe_status != "verified":
            return "Being set up", "Not checked on this computer yet"
        return "Waiting", "Not checked yet"
    success = _dt(row["last_success"])
    error = _dt(row["last_error_at"])
    paused = _dt(row["paused_until"])
    nowdt = datetime.now(timezone.utc)
    if paused and paused > nowdt:
        if (row["consecutive_blocks"] or 0) >= BREAKER_BLOCKS:
            return "Needs attention", f"{row['last_error']}. Paused until {paused.astimezone():%H:%M}, the maintainer has been told"
        return "Slow", f"{row['last_error']}. Trying again at {paused.astimezone():%H:%M}"
    if error and (not success or error > success):
        if (row["last_error"] or "").startswith("Off:"):
            return "Off", row["last_error"][4:].strip()
        return "Needs attention", row["last_error"] or "Last check failed"
    if row["selftest_error"]:
        return "Needs attention", f"Daily self-test failed: {row['selftest_error']}"
    if recipe_status == "verified" and expect_results and (row["zero_runs"] or 0) >= ZERO_RUNS_ALERT:
        return "Needs attention", "Has found nothing for a while; the site may have changed"
    if success and nowdt - success > timedelta(minutes=max(interval_minutes, 60) * 4):
        return "Slow", "Not checked recently"
    if recipe_status != "verified" and not row["found_count"]:
        return "Being set up", "Working, but has not found any cars yet"
    return "Working", f"Found {row['found_count']} listings last time"


def banner(db: DB, recipes: dict, enabled: list[str], interval_minutes: int) -> dict:
    rows = {r["source"]: r for r in db.query("SELECT * FROM source_health")}
    sources = []
    for name in enabled:
        rec = recipes[name]
        label, detail = status_of(rows.get(name), rec.status, interval_minutes, rec.data.get("expect_results", True))
        sources.append({
            "name": name, "display_name": rec.display_name, "label": label, "detail": detail,
            "last_success": rows[name]["last_success"] if name in rows else None,
        })
    off = sum(s["label"] == "Off" for s in sources)
    working = sum(s["label"] in ("Working", "Being set up", "Waiting") for s in sources)
    repairing = len(sources) - working - off
    text = f"{working} of {len(sources) - off} sources working"
    if repairing:
        text += f", {repairing} unavailable right now"
    return {"text": text, "ok": repairing == 0, "sources": sources}
