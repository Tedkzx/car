"""SX200 Tracker entry point: tray icon, scheduler and the local dashboard.

    SX200Tracker.exe                 start (opens the dashboard; setup on first run)
    SX200Tracker.exe --background    start quietly (used at Windows login)
    SX200Tracker.exe --selftest      test every recipe against its saved page
    SX200Tracker.exe --capture NAME  save a real search page for a recipe (maintainer)
    SX200Tracker.exe --sweep-once    run one sweep, print the result and exit (maintainer)
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import socket
import sys
import threading
import time
import webbrowser

from core import paths

VERSION = "0.3.0"
PREFERRED_PORT = 8765
log = logging.getLogger("sx200")


def setup_logging(verbose: bool = False) -> None:
    handler = logging.handlers.RotatingFileHandler(paths.log_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        root.addHandler(logging.StreamHandler())
    logging.getLogger("httpx").setLevel(logging.WARNING)


def free_port(preferred: int) -> int:
    for port in (preferred, *range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build():
    from alerts.notifier import Notifier
    from collectors.browser_edge import EdgeBrowser
    from core.db import DB
    from core.dedupe import default_image_fetcher
    from core.pipeline import Pipeline
    from core.scheduler import Scheduler
    from web.server import AppState

    db = DB()
    holder: dict = {}
    notifier = Notifier(lambda: holder["state"].cfg())
    pipeline = Pipeline(db, lambda: holder["state"].cfg(), notifier, browser_factory=EdgeBrowser,
                        image_fetcher=default_image_fetcher())
    scheduler = Scheduler(
        sweep=pipeline.sweep,
        interval_minutes=lambda: holder["state"].cfg()["schedule"]["interval_minutes"],
        jitter_pct=lambda: holder["state"].cfg()["schedule"]["jitter_pct"],
        last_sweep=lambda: db.get_meta("last_sweep"),
    )
    state = AppState(db, pipeline, scheduler, notifier)
    holder["state"] = state
    return state


def run_tray(url: str, state, stop: threading.Event) -> None:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as e:  # no tray available (e.g. a server): keep running headless
        log.info("No tray icon: %s", e)
        stop.wait()
        return

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 18, 60, 46), radius=10, fill=(31, 95, 191, 255))
    d.ellipse((12, 38, 26, 52), fill=(20, 20, 20, 255))
    d.ellipse((38, 38, 52, 52), fill=(20, 20, 20, 255))

    def label(_item=None) -> str:
        s = state.scheduler
        return "Paused" if s.paused else ("Checking sites…" if s.running_sweep else "Watching for cars")

    def toggle(icon, _item):
        state.scheduler.resume() if state.scheduler.paused else state.scheduler.pause()
        icon.update_menu()

    def quit_app(icon, _item):
        stop.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: label(), None, enabled=False),
        pystray.MenuItem("Open", lambda icon, item: webbrowser.open(url), default=True),
        pystray.MenuItem("Check now", lambda icon, item: state.scheduler.run_now()),
        pystray.MenuItem(lambda item: "Resume" if state.scheduler.paused else "Pause", toggle),
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("SX200Tracker", img, "SX200 Tracker", menu)
    icon.run()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="SX200Tracker", description="Finds Nissan 200SX and Silvia cars for sale.")
    ap.add_argument("--background", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--capture", metavar="SOURCE")
    ap.add_argument("--sweep-once", action="store_true")
    ap.add_argument("--no-browser", action="store_true", help="do not open the dashboard")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=VERSION)
    if sys.stdout is None:  # the windowed build has no console; keep print() and argparse safe
        sys.stdout = sys.stderr = open(paths.data_dir() / "logs" / "console.log", "a", encoding="utf-8")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)

    if args.selftest:
        from collectors import recipe_engine as engine

        bad = 0
        for name, r in engine.load_recipes().items():
            ok, msg = engine.self_test(r)
            bad += (not ok) and r.status == "verified"
            print(f"{'OK  ' if ok else 'FAIL'} {name:<16} [{r.status}] {msg}")
        return 1 if bad else 0
    if args.capture:
        from collectors.capture import capture

        print(capture(args.capture))
        return 0

    from core.winsys import SingleInstance

    lock = SingleInstance(paths.data_dir() / "tracker.lock")
    if not lock.acquire():
        # Already running: just open the dashboard of the running copy.
        port_file = paths.data_dir() / "port.txt"
        if port_file.exists() and not args.background:
            webbrowser.open(f"http://127.0.0.1:{port_file.read_text(encoding="utf-8").strip()}/")
        return 0

    from core.updater import Updater

    state = build()
    if Updater(state.db, state.cfg, VERSION).apply_pending():
        log.info("Installing a downloaded update; the installer restarts the tracker")
        lock.release()
        return 0
    if args.sweep_once:
        res = state.pipeline.sweep(force=True)
        for name, v in res.per_source.items():
            print(f"{name:<16} {v}")
        print(f"new listings {res.new_listings}, new matches {res.new_matches}, alerts {res.alerts}")
        return 0

    import uvicorn

    from web.server import create_app

    port = free_port(PREFERRED_PORT)
    (paths.data_dir() / "port.txt").write_text(str(port))
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="warning",
                                           log_config=None))
    threading.Thread(target=server.run, name="web", daemon=True).start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    log.info("SX200 Tracker %s running at %s", VERSION, url)

    # First run: the setup page; afterwards only open the dashboard when started by hand.
    if not state.cfg().get("setup_complete"):
        if not args.no_browser:
            webbrowser.open(url + "setup")
    else:
        state.scheduler.start()
        if not args.background and not args.no_browser:
            webbrowser.open(url)

    stop = threading.Event()
    starter = threading.Thread(target=_start_after_setup, args=(state, stop), daemon=True)
    starter.start()
    threading.Thread(target=maintenance, args=(state, stop), name="maintenance", daemon=True).start()
    try:
        run_tray(url, state, stop)
    except KeyboardInterrupt:
        pass
    stop.set()
    state.scheduler.stop()
    server.should_exit = True
    lock.release()
    return 0


def maintenance(state, stop: threading.Event) -> None:
    """Scraper fixes every 6 hours, recipe self-tests and the app update check once a day."""
    from collectors import recipe_engine as engine
    from core import health
    from core.updater import Updater

    upd = Updater(state.db, state.cfg, VERSION)
    last_recipes = last_daily = 0.0
    while not stop.is_set():
        t = time.time()
        try:
            if t - last_recipes > 6 * 3600:
                last_recipes = t
                if upd.update_recipes():
                    state.pipeline.reload_recipes()
                    log.info("Scraper fixes applied")
            if t - last_daily > 24 * 3600:
                last_daily = t
                for name, r in state.pipeline.recipes.items():
                    ok, msg = engine.self_test(r)
                    failed = (not ok) and r.status == "verified"
                    health.record_selftest(state.db, name, msg if failed else None)
                    if failed:
                        state.notifier.maintainer(f"{r.display_name} self-test failed", msg)
                upd.check_app()
        except Exception:
            log.exception("maintenance step failed")
        stop.wait(600)


def _start_after_setup(state, stop: threading.Event) -> None:
    """The scheduler starts once the three setup questions are answered."""
    while not stop.is_set():
        if state.cfg().get("setup_complete"):
            if state.scheduler._thread is None:
                state.scheduler.start()
            return
        stop.wait(1)


if __name__ == "__main__":
    sys.exit(main())
