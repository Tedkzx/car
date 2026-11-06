# SX200 Tracker

Finds factory-SR20DET Nissan 200SX and Silvia cars for sale in the UK, ranks them stock-first, and alerts the owner's phone. Built from the "Nissan 200SX Listing Tracker: Build Spec" (6 Nov 2026). Version 0.5.0: milestones 1 and 2, milestone 3's market-based scoring, the Japan export part of milestone 4, and eBay through its official API.

## For the owner (under 5 minutes)

1. Download `SX200-Tracker-Setup.exe` and run it. If Windows shows a blue warning, click **More info**, then **Run anyway**.
2. Answer three questions: postcode (optional), budget, phone alerts.
3. Install the free **ntfy** app on your phone and scan the code on the screen. No account is needed.

That's all. It starts by itself when the PC turns on. Open it from the car icon next to the clock.

## For the maintainer

### Get the installer built

There is no Windows machine in the build environment, so a GitHub Actions workflow builds the installer:

1. Create a GitHub repository and push this folder to it.
2. Every push to `main` runs the tests on Linux and Windows, bundles the app with PyInstaller, builds `SX200-Tracker-Setup.exe` with Inno Setup, installs it silently on the Windows runner, and self-tests the installed copy. The installer is attached to the run as an artifact.
3. To release a version: on GitHub, Releases → Draft a new release → new tag `v` + the version in `core/version.py` (for example `v0.5.1`) → Publish. The build attaches the installer and its checksum to that release in about 15 minutes; running copies then update themselves (or press "Check for updates now" on the Sources page). A tag that does not match `core/version.py` fails the build on purpose.

### Commands

```
SX200Tracker-cli.exe --selftest          test every recipe against its saved page
SX200Tracker-cli.exe --capture ebay      save a real search page from the owner's connection
SX200Tracker-cli.exe --sweep-once        run one sweep of every source and print what each returned
python tools/make_manifest.py            after changing a recipe: lets running installs download it
```

In development: `python app.py --no-browser`, `python -m pytest -q`, `python -m tests.labelled_eval`.

### Where things live

`%APPDATA%\SX200Tracker\`: `tracker.db` (SQLite), `config.yaml`, `logs\`, `recipes\` (downloaded recipes override the shipped ones; milestone 2 fills this from the repository), `browser-profile\` (Edge, for sites that need it).

### Project layout

```
app.py          entry point: tray icon, scheduler, local web server, maintainer commands
core/           config, db, models, normalizer, matcher, pipeline, health, scheduler, winsys
collectors/     recipe_engine.py (YAML recipes: embedded JSON, then JSON-LD, then CSS), browser_edge.py, capture.py
recipes/        one YAML per source, fixtures/ (saved real pages), SOURCES.md
enrich/         plate_extract.py, geo.py (postcodes.io)
scoring/        score.py (provisional; milestone 3 completes it)
alerts/         ntfy.py, windows_toast.py, notifier.py
web/            FastAPI + Jinja2 + HTMX dashboard and setup page
installer/      PyInstaller spec, Inno Setup script
tests/          labelled listings, holdout set, unit, pipeline and web tests
```

## Milestone 1 acceptance status

| Check | Status |
| --- | --- |
| Database, config, matcher, recipe engine | Done, tested |
| Matcher precision ≥ 95%, recall ≥ 90%, zero parts / wanted / scam alerts | 100% / 100% on 159 labelled listings and on a 28-listing holdout set, 0 junk alerts. **Only 18 of the 159 are real listings**; the rest are synthetic and were written alongside the matcher, so they overstate accuracy. The spec's bar needs 150+ real labelled ads (maintainer task) |
| PistonHeads scraper | Verified on the live site, saved page, self-test passes |
| Gumtree scraper | Verified on the live site, saved page, self-test passes |
| eBay UK scraper | Recipe written, **not verified**: eBay refused the cloud build machine. Run `--capture ebay` on the owner's PC |
| Car & Classic scraper | Recipe written, **not verified**: Cloudflare check on the cloud build machine, which the tracker never bypasses. Run `--capture carandclassic` on the owner's PC |
| ntfy phone alerts | Verified: JSON publish to ntfy.sh with no account, £ signs intact |
| Windows notifications, tray icon, Startup entry | Written; can only be tested on Windows |
| Dashboard and three-question setup | Done, tested, screenshots checked at desktop and phone widths |
| Installer | Inno Setup script + CI workflow written; the same PyInstaller spec builds and self-tests on Linux. **Not yet built on Windows** |
| First real alert within 10 minutes on a clean Windows 11 machine | **Not yet tested** (needs Windows). Note: no in-scope car was for sale on PistonHeads or Gumtree on 6 Nov 2026, so the first alert depends on the market; use Settings, "Send a test alert" to prove delivery |

### Before trusting it

- Collect 150+ real labelled listings into `tests/labelled/listings.jsonl` (`origin: real`) and rerun `python -m tests.labelled_eval`.
- Confirm the gov.uk MOT link format by hand with one plate (`web/server.py`, `MOT_URL`). It could not be reached from the build machine.
- The seed price bands in `scoring/score.py` are rough guesses until milestone 3's rolling medians.

## Milestone 2 status

| Check | Status |
| --- | --- |
| New sources | Exchange & Mart, JDM Imports UK and the 350Z-UK forum verified on live pages. Autotrader, CarGurus, Cazoo (formerly Motors), Preloved and theparking.eu added as Edge sources, unverified because they block cloud machines. eBay and Car & Classic moved to Edge after they refused plain requests from the owner's home connection too. ClassicCarsForSale has closed (redirects to an auction house). See `recipes/SOURCES.md` |
| Same car on two sites shows once with every link | Done: plate, chassis number, matching photos (perceptual hash, 2+ images within distance 6), and a "Same car?" page for probable duplicates. Tested |
| Simulated block: backoff, circuit breaker, banner | Done in milestone 1, still tested. Added: a verified source that finds nothing four runs in a row shows "Needs attention" |
| Daily recipe self-tests | Done |
| Scraper fixes reach running installs without a reinstall | Done and tested with a fake server: `recipes/manifest.json` + SHA-256 per file, only recipe files accepted. **Needs a public repository** (see below) |
| App auto-update | Done: checks the latest GitHub release daily, verifies the installer's SHA-256, installs silently at next start and restarts. **Needs public releases** |
| Browser sources every 30-60 minutes | Done, per-recipe `min_interval_minutes` |

## Milestone 3 status (scoring)

| Check | Status |
| --- | --- |
| Every card shows its score reasons in plain words | Done |
| A stock car outranks a heavily modified one | Done, tested |
| Value score switches from provisional to median-based after 8 comparables | Done, tested. Cohort = generation + market + mod tier, last 180 days, removed and sold cars included |
| Price history | "Price changes" tab |
| Shortlist, ignore, notes | Done since milestone 1 |
| Still provisional | Seed price bands (used until 8 comparables) are rough guesses; condition and scam flags read the ad text only (the MOT API is off) |

"New" lists only matches inside the budget (landed cost for imports). Matches outside it stay under All active, In Japan and Everything found.

## Milestone 5 status

| Check | Status |
| --- | --- |
| "Paste a link" box (on the New tab) | Done. Reads the ad page (JSON-LD, Open Graph, price in the text) and runs the matcher, flags, score and landed cost. If the site refuses automatic reading (Autotrader, eBay), the owner types the title and price instead. Pasted cars never alert |
| Facebook opt-in | Not built. Facebook Marketplace and group ads can go through the paste box meanwhile |
| Forums | 350Z-UK (RSS). Driftworks blocked; Zilvia is US-based |

## Milestone 4 status (Japan exports)

| Check | Status |
| --- | --- |
| Japan export sources | Goo-net Exchange, SBT Japan and Car From Japan verified on live pages. jdmbuysell and BE FORWARD not added (see `recipes/SOURCES.md`) |
| Exchange rates | Frankfurter (free, no key), fetched once a day, rate and date stored on every car; approximate fallback if offline |
| Landed cost within £50 of a hand calculation on 3 sample cars | Done, tested (spec section 8 formula; every rate is a setting) |
| Overseas cars outside £10-20k landed never alert | Done, tested |
| Price drops on imports | Compared in the seller's currency, so exchange-rate moves never look like a price cut |
| UK JDM importer recipes | JDM Imports UK only so far; the spec asks the maintainer to approve a list |

**Public repository needed for updates.** Running installs cannot log in to GitHub (the installer never carries a key), so scraper fixes and app updates only reach them if `Tedkzx/car` is public. The repo holds no secrets. Until then, the Sources page says "not reachable" and everything else works.

Bug fixed in this milestone: the plate reader took the engine code "SR20DET" for a UK plate, which could merge different cars. Engine codes are now excluded, and plate merges also need matching years.

### Not in milestone 1 or 2 (by design)

Autotrader and the other milestone 2 sites, image-hash and probable-duplicate merging, recipe hot-update and auto-update, daily scheduled self-tests, digest and quiet hours, full scoring and price history views, imports and landed cost, Facebook, forums, auctions, the MOT API.
