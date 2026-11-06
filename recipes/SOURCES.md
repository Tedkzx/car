# Sources

One line per source: status, last checked, method. Add a line whenever a search finds a new place where 200SX or Silvia cars are listed. After changing any recipe or fixture, run `python tools/make_manifest.py` so running installs pick up the change.

"Verified" means a real page was fetched and saved, and the recipe's self-test passes on it. "Unverified" sources blocked the cloud build machine. They run on the owner's PC through Edge and stop at any check page. Run `SX200Tracker-cli.exe --capture NAME` there to save a real page.

| Source | Status | Last checked | Method | Notes |
| --- | --- | --- | --- | --- |
| PistonHeads | Verified | 2026-11-06 | HTTP, embedded JSON | `/buy/nissan/200sx-silvia` lists both models. Seller name and phone are in the page; never read |
| Gumtree | Verified | 2026-11-06 | HTTP, CSS | Little stock. Plain HTTP works from the owner's home connection too |
| Exchange & Mart | Verified | 2026-11-06 | HTTP, CSS | `/nissan/silvia` and `/nissan/200` (also lists NV200 vans, dropped by the matcher) |
| JDM Imports UK | Verified | 2026-11-06 | HTTP, CSS | Homepage lists the whole stock with a status label (Coming Soon, Reserved, Sold). Had 3 in-scope S14s on the day |
| 350Z-UK forum | Verified | 2026-11-06 | RSS | "For Sale" section feed; mostly parts. Posts older than 120 days are ignored |
| Goo-net Exchange (Japan) | Verified | 2026-11-06 | HTTP, CSS, form paging | About 235 Silvias plus 180SXs. Titles carry the trim (Spec-R, K's, Q's), so the engine can be confirmed. Yen FOB prices, landed cost applied |
| SBT Japan | Verified | 2026-11-06 | HTTP, CSS | 50 Silvias a page. Chassis code but rarely the trim, so most go to "Needs a look". USD FOB |
| Car From Japan | Verified | 2026-11-06 | HTTP, CSS | 25 a page. "Grade R" means Spec-R. USD FOB |
| Cazoo | Working on the owner's PC | 2026-11-06 | Edge, auto | Motors.co.uk now redirects here. 24 listings on the first run from home (all Nissans; the matcher filters) |
| eBay UK | Blocked | 2026-11-06 | Edge | Blocks plain requests and the tracker's Edge, from the cloud and from home |
| Car & Classic | Blocked | 2026-11-06 | Edge | Same as eBay |
| Autotrader | Blocked | 2026-11-06 | Edge, auto | Blocked from home too |
| CarGurus UK | Blocked | 2026-11-06 | Edge, auto | Blocked from home too |
| Preloved | Loads, finds nothing | 2026-11-06 | Edge, auto | Needs a captured page to write selectors |
| theparking.eu | Unverified | 2026-11-06 | Edge, auto | Cloudflare on the cloud machine. UK-located cars only |
| ClassicCarsForSale | Gone | 2026-11-06 | | Now redirects to Iconic Auctioneers (auctions are milestone 6) |
| Driftworks forum | Not added | 2026-11-06 | | Cloudflare on the cloud machine; its for-sale feed address is not known yet |
| jdmbuysell.com | Not added | 2026-11-06 | | Cloudflare check on the cloud machine |
| BE FORWARD | Not added | 2026-11-06 | | Its Silvia page returned "not found"; may have no stock |
| Collecting Cars | Not added | 2026-11-06 | | Cloudflare check on the cloud machine (auctions are milestone 6) |
| Classic Car Weekly marketplace | Not added | 2026-11-06 | | Web address not found |

"auto" reads the listing data most car sites embed (JSON-LD or the page's own JSON) without site-specific selectors. If it finds nothing on the owner's PC, the captured page shows what selectors to write.
