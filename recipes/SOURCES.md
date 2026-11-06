# Sources

One line per source: status, last checked, method. Add a line whenever a search finds a new place where 200SX or Silvia cars are listed.

| Source | Status | Last checked | Method | Notes |
| --- | --- | --- | --- | --- |
| PistonHeads | Verified, saved page + self-test | 2026-11-06 | HTTP, embedded Next.js/Apollo JSON | `/buy/nissan/200sx-silvia` lists both models. Keyword search is noisy (Suzuki SX4, Fiat SX), the matcher drops it. Seller name and phone sit in the page; the recipe never reads them |
| Gumtree | Verified, saved page + self-test | 2026-11-06 | HTTP, CSS (`data-q` attributes) | Very little stock on the day (one S12 Silvia, one wanted ad). JSON-LD has names and links only |
| eBay UK | Unverified, no saved page | 2026-11-06 | HTTP, CSS | Refused the cloud build machine (403 error page). Run `--capture ebay` on the owner's PC |
| Car & Classic | Unverified, no saved page | 2026-11-06 | HTTP (JSON-LD, then embedded JSON, then CSS) | Showed a Cloudflare check to the cloud build machine. Never bypassed. Run `--capture carandclassic` on the owner's PC; if it is challenged there too, try `method: browser` |

Milestone 2 adds Autotrader, Motors.co.uk, CarGurus UK, Exchange & Mart, Preloved, ClassicCarsForSale, Classic Car Weekly and theparking.eu.
