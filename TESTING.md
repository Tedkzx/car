# Testing milestone 1 on Windows

Goal (spec section 12): on a clean Windows 11 machine, a non-developer installs it, answers three questions, and gets an alert on his phone within 10 minutes with no other steps.

## 1. Get the installer

- **Latest build:** on GitHub, open **Actions**, then the newest green **Build Windows installer** run. Download **SX200-Tracker-Setup** under Artifacts (a zip holding the .exe).
- **A release:** push a tag (`git tag v0.1.0 && git push origin v0.1.0`). The installer appears under **Releases**.

## 2. Use a clean machine

Best: a spare Windows 11 PC or laptop, signed in as a **standard (non-admin) user**, with Defender on.
Quick alternative on Windows 11 Pro: **Windows Sandbox** (Start, type "Windows Features", tick Windows Sandbox, restart). It is a throwaway clean Windows. It cannot test start-at-login, so do step 6 on a real PC.

## 3. Install and set up (time it: target under 5 minutes)

| Step | What should happen |
| --- | --- |
| Run `SX200-Tracker-Setup.exe` | A blue SmartScreen warning may appear: **More info**, then **Run anyway**. No admin password is asked for |
| Finish the installer | The browser opens on "Welcome. Three quick questions." |
| Enter postcode and budget, install **ntfy** on the phone, scan the QR code, press **Finish and start** | Within seconds: a phone alert "SX200 Tracker is set up" and a Windows notification |
| Wait | A progress bar checks each site, then "First check finished" and **Open the dashboard** |

## 4. Check the dashboard

- **Sources** tab: PistonHeads and Gumtree should say **Working**. eBay and Car & Classic may say **Slow** or **Being set up**. That is expected; see step 5.
- **Needs a look** tab: cars whose ads don't say enough (for example an S15 with no trim) appear here, with reasons.
- On a car: **Shortlist**, **Ignore**, notes, **Open on PistonHeads**, and **Check MOT** when the ad shows a plate. Press **Check MOT** once and confirm the gov.uk page opens for that plate.
- **Settings**: **Send a test alert** should reach the phone.

A real "New" alert needs a matching car (factory SR20DET 200SX/Silvia, in budget, no red flags) to be listed. There may be none for days. The test alert proves delivery; leave it running to catch a real one.

## 5. Check eBay and Car & Classic from the home connection

These two blocked the cloud build machine. Open **Command Prompt** and run:

```
"%LOCALAPPDATA%\Programs\SX200 Tracker\SX200Tracker-cli.exe" --capture ebay
"%LOCALAPPDATA%\Programs\SX200 Tracker\SX200Tracker-cli.exe" --capture carandclassic
```

Each one either says "Saved ... The recipe found N listings" (send that file to the maintainer) or "The site is blocking automatic checks... Nothing saved". Note which.

## 6. It looks after itself

| Test | Expected |
| --- | --- |
| Restart the PC and sign in | The car icon appears next to the clock with no action. The dashboard's "Last checked" updates |
| Leave it 30 minutes | "Last checked" stays under about 25 minutes old |
| Put the PC to sleep for an hour, wake it | A new check runs within a minute or two |
| Right-click the tray icon | Open, Check now, Pause / Resume, Quit all work |
| Start it again while running (Start menu) | No second copy; the dashboard opens |

## 7. Uninstall

Windows Settings, Apps, **SX200 Tracker**, Uninstall. It asks whether to keep saved cars and settings. Both answers should work. The start-at-login entry is removed.

## Reporting a problem

Sources tab, **Save a problem report**: it saves a zip of the logs (no personal data). Send it with a note of which step failed.

Logs: `%APPDATA%\SX200Tracker\logs\`. Self-test of the scrapers:
`"%LOCALAPPDATA%\Programs\SX200 Tracker\SX200Tracker-cli.exe" --selftest`
