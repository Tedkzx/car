"""The run loop (spec section 5).

Sweep at start, then every interval with random jitter. If the gap since the
last run is more than twice the interval (sleep, hibernate, PC off), sweep at
once. Pause, resume and "check now" come from the tray and the dashboard.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, sweep: Callable[[], object], interval_minutes: Callable[[], float],
                 jitter_pct: Callable[[], float], last_sweep: Callable[[], str | None],
                 clock: Callable[[], float] = time.time):
        self._sweep = sweep
        self._interval = interval_minutes
        self._jitter = jitter_pct
        self._last_sweep = last_sweep
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.paused = False
        self.running_sweep = False
        self.next_run: float | None = None
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run_now(self) -> None:
        self.next_run = self._clock()
        self._wake.set()

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False
        self._wake.set()

    def next_delay(self) -> float:
        base = max(5.0, float(self._interval())) * 60
        j = max(0.0, min(50.0, float(self._jitter()))) / 100
        return base * random.uniform(1 - j, 1 + j)

    def missed_runs(self) -> bool:
        """True after sleep or downtime: more than twice the interval since the last sweep."""
        last = self._last_sweep()
        if not last:
            return True
        try:
            gap = self._clock() - datetime.fromisoformat(last).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return True
        return gap > 2 * float(self._interval()) * 60

    def _loop(self) -> None:
        self.next_run = self._clock()  # catch-up sweep at startup
        last_tick = self._clock()
        while not self._stop.is_set():
            now = self._clock()
            # Wall clock jumped (the PC slept): sweep as soon as we wake.
            if now - last_tick > 120 and self.missed_runs():
                log.info("Resumed after a gap of %.0f s, sweeping now", now - last_tick)
                self.next_run = now
            last_tick = now
            if not self.paused and self.next_run is not None and now >= self.next_run:
                self.running_sweep = True
                try:
                    self._sweep()
                    self.last_error = None
                except Exception as e:  # the loop must survive anything
                    log.exception("sweep failed")
                    self.last_error = str(e)
                finally:
                    self.running_sweep = False
                self.next_run = self._clock() + self.next_delay()
            self._wake.wait(timeout=30)
            self._wake.clear()
