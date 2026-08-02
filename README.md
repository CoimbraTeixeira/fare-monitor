# fare-monitor

Watches airline deal sites for **error fares** (mistake fares) and posts matches to Slack.

Error fares are pricing mistakes — a business-class ticket listed at economy prices, a
missing fuel surcharge, a currency conversion gone wrong. They get pulled within hours,
so the only thing that matters is finding out fast. This script checks a handful of deal
sources on a cron schedule and alerts only when something is actually labelled as an
error fare.

## How it works

```
RSS feeds ─┐
           ├─→ clean HTML ─→ match patterns ─→ drop duplicates ─→ Slack
HTML pages ─┘                                  (seen.json)
```

Each run fetches every source, keeps posts published within `LOOKBACK_HOURS` (24h),
and matches their title, summary and tags against a small set of error-fare patterns
(`error fare`, `mistake fare`, `may not be honoured`, `book before it's fixed`, …).

Anything matched is posted to Slack and recorded in `seen.json`, so a deal is never
alerted twice even though the lookback window overlaps between runs.

## Sources

| Source | Type | Notes |
|---|---|---|
| [Secret Flying](https://www.secretflying.com/) | HTML | The only source that labels error fares explicitly in the title |
| [The Flight Deal](https://www.theflightdeal.com/) | RSS | US-focused; labels mistake fares in the title |
| [Fly4Free](https://www.fly4free.com/) | RSS | European; heavily tagged |
| [Travelfree](https://travelfree.info/) | RSS | European |
| [Fare Deal Alert](https://faredealalert.com/) | RSS | Low volume, pure airfare |

Secret Flying is read from HTML because Cloudflare returns 403 for its feed and category
pages. Its `robots.txt` permits crawling at `Crawl-delay: 1`; one request per six-hour run
is well inside that.

Sites deliberately **not** included, and why:

- **The Points Guy, One Mile at a Time, View from the Wing, Thrifty Traveler** — publish
  commentary *about* error fares ("anatomy of a mistake fare"). Those match the patterns
  but are never bookable.
- **Airfare Spot** — abandoned; newest post is from September 2023.
- **r/flightdeals** — Atom, which `fetch_items` does not parse. Reddit also rate-limits.

## Setup

```bash
pip install -r requirements.txt

mkdir -p ~/.config/fare-monitor
echo '{"webhook_url": "https://hooks.slack.com/services/..."}' > ~/.config/fare-monitor/config.json
chmod 600 ~/.config/fare-monitor/config.json
```

Then run it every six hours:

```cron
0 */6 * * * /usr/bin/python3 /path/to/fare_monitor.py >> /path/to/monitor.log 2>&1
```

The lookback window is wider than the cron interval on purpose: if a run fails, the next
one still catches what it missed, and the seen-file prevents the overlap from
double-alerting.

## Usage

```bash
python3 fare_monitor.py              # normal run, posts to Slack
python3 fare_monitor.py --dry-run    # show what would be sent, post nothing
python3 fare_monitor.py --verbose    # every item, with window / match / already-sent flags
```

`--verbose` is the one to reach for when the log says nothing was found and you want to
know whether that is correct:

```
-- Secret Flying: fetched 11 item(s) --
  [old] [MATCH] [new ] 2026-07-27 15:01 UTC  ⚠️ ERROR FARE ⚠️ Business Class Stockholm → Seoul
  [IN]  [  no ] [new ] 2026-08-02 14:24 UTC  Non-stop Fort Lauderdale → San Juan, $178
```

## State and failure handling

`~/.config/fare-monitor/seen.json` records the RSS `guid` of every alerted post, pruned
after 30 days. It is written **only after Slack accepts the message** — if the webhook
fails the run exits non-zero and leaves state untouched, so the next run retries rather
than silently dropping the deal.

Feed fetches retry three times with backoff. A source that fails is logged to stderr and
skipped; the others still run.

## Tests

```bash
python3 -m unittest test_fare_monitor
```

34 tests, no network, no dependencies beyond the standard library. Error fares are rare,
so a correct run and a broken run both print `No qualifying error fares found` — these
cover the paths that only execute on the day it matters, including detection against real
historical error-fare titles.

## Known limitations

- **The HTML source is fragile by nature.** A Secret Flying redesign breaks extraction.
  Titles come from anchor text but matching also runs against the URL slug, so partial
  changes degrade rather than fail outright, and extracting zero posts logs a warning
  rather than passing as a quiet day.
- **Detection is only as good as the wording.** A genuine error fare posted without any
  of the recognised phrasing is missed. The patterns are deliberately strict: a false
  alert every few days would get the channel muted, which costs more than a rare miss.
- **Feed coverage bounds the lookback.** Sources that publish frequently expose only
  their most recent items, so an outage longer than a feed's span loses whatever fell off
  the end.
