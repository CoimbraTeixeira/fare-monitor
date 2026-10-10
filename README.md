# fare-monitor

Watches airline deal sites for **error fares** (mistake fares) and posts matches to Discord.

Error fares are pricing mistakes — a business-class ticket listed at economy prices, a
missing fuel surcharge, a currency conversion gone wrong. They get pulled within hours,
so the only thing that matters is finding out fast. This script checks a handful of deal
sources on a cron schedule and alerts only when something is actually labelled as an
error fare.

## How it works

```
RSS feeds ─┐
           ├─→ clean HTML ─→ match patterns ─→ skip expired / commentary ─→ drop duplicates ─→ Discord
HTML pages ─┘                                                               (seen.json)
```

Each run fetches every source, keeps posts published within `LOOKBACK_HOURS` (24h),
and matches their title, summary and tags against a small set of error-fare patterns
(`error fare`, `mistake fare`, `may not be honoured`, `book before it's fixed`, …).

Two kinds of match are dropped before alerting: deals already marked **expired** (Secret
Flying's `EXPIRED` stamp, or "expired" in the title), and **commentary** — items under a
`/news/`, `/blog/`, `/lexikon/`, `/podcast/` or `/magazin/` URL section, which report
*on* error fares and are never bookable. `--verbose` shows the reason next to a skipped
match.

Anything left is posted to Discord and recorded in `seen.json`, so a deal is never
alerted twice even though the lookback window overlaps between runs.

## Sources

| Source | Type | Notes |
|---|---|---|
| [Secret Flying](https://www.secretflying.com/) | HTML | Labels error fares in the title and stamps them when expired |
| [The Flight Deal](https://www.theflightdeal.com/) | RSS | US-focused; labels mistake fares in the title |
| [Fly4Free](https://www.fly4free.com/) | RSS | European; heavily tagged; mixes in `/news/` articles |
| [Travelfree](https://travelfree.info/) | RSS | European |
| [Fare Deal Alert](https://faredealalert.com/) | RSS | Low volume, pure airfare |
| [Travel Dealz](https://travel-dealz.com/) | RSS | European; titles error fares `ERROR FARE:` and tags them `Error Fares` |
| [Travel Dealz DE](https://travel-dealz.de/) | RSS | German edition; uses the English term "Error Fare" |

Travel Dealz publishes an "Error Fares" tag feed, but it is always empty — deals are a
custom WordPress post type that tag feeds do not include — so the main feeds are read,
and their `/news/` and `/blog/` articles are dropped by the commentary filter.

Secret Flying is read from HTML because Cloudflare returns 403 for its feed and category
pages. Its `robots.txt` permits crawling at `Crawl-delay: 1` — one second between
requests — so one request per hourly run is well inside that.

Sites deliberately **not** included, and why:

- **The Points Guy, One Mile at a Time, View from the Wing, Thrifty Traveler** — publish
  commentary *about* error fares ("anatomy of a mistake fare"). Those match the patterns
  but are never bookable.
- **Airfare Spot** — abandoned; newest post is from September 2023.
- **r/flightdeals** — Atom, which `fetch_items` does not parse. Reddit also rate-limits.
- **HolidayPirates, Urlaubspiraten, Wakacyjni Piraci, Fly4free.pl** — feeds work, but
  they are package-holiday and hotel deals that do not label error fares.
- **Loyalty Lobby, Head for Points** — loyalty-programme news and reviews, not deals.
- **Jack's Flight Club, Going, Flynous** — no public feed (404/403).

## Setup

```bash
pip install -r requirements.txt

mkdir -p ~/.config/fare-monitor
echo '{"webhook_url": "https://discord.com/api/webhooks/..."}' > ~/.config/fare-monitor/config.json
chmod 600 ~/.config/fare-monitor/config.json

git config core.hooksPath hooks   # enable the pre-commit secret guard (see below)
```

This repo is public, and the design keeps the webhook out of it entirely. `hooks/pre-commit`
enforces that: it scans staged content for webhook URLs, API tokens and private keys, and
refuses the commit if it finds one. Git does not enable repo-tracked hooks automatically, so
the `core.hooksPath` line above is required once per clone. Bypass a false positive with
`git commit --no-verify`.

Then run it every hour:

```cron
7 * * * * /usr/bin/python3 /path/to/fare_monitor.py >> /path/to/monitor.log 2>&1
```

Hourly, because that is what the thing being watched demands: an error fare is a pricing
mistake, and mistakes get corrected. A six-hour cadence can miss one entirely between
runs — and a deal found five hours late is often a deal already pulled.

The lookback window (24h) is far wider than the interval on purpose: if a run fails, the
next one still catches what it missed. Re-examining the same window hourly is safe
because the seen-file gates posting, not fetching — a deal is alerted once no matter how
many runs observe it.

## Usage

```bash
python3 fare_monitor.py              # normal run, posts to Discord
python3 fare_monitor.py --dry-run    # show what would be sent, post nothing
python3 fare_monitor.py --verbose    # every item, with window / match / already-sent flags and skip reasons
```

`--verbose` is the one to reach for when the log says nothing was found and you want to
know whether that is correct:

```
-- Secret Flying: fetched 6 item(s) --
  [IN] [MATCH] [new ] 2026-10-10 09:12 UTC  ERROR FARE Business Class Stockholm to Seoul
  [old] [MATCH] [sent] 2026-08-21 06:22 UTC  ERROR FARE Non-stop from Dublin to the USA  (skipped: expired)
  [IN] [  no  ] [new ] 2026-10-10 08:40 UTC  Rome, Italy to Riyadh, Saudi Arabia for only €164
```

## State and failure handling

`~/.config/fare-monitor/seen.json` records the RSS `guid` of every alerted post, pruned
after 30 days. It is written **only after Discord accepts the message** — if the webhook
fails the run exits non-zero and leaves state untouched, so the next run retries rather
than silently dropping the deal.

During pruning, timezone-naive timestamps are interpreted as UTC; invalid or non-string
entries are discarded rather than aborting the run.

Feed fetches retry three times with backoff. A source that fails is logged to stderr and
skipped; the others still run.

## Running against fakes

Every outside dependency can be redirected with an environment variable, so the real code
can run end to end against local fake services (the `factory` repo's `fakes/`) with no
real side effects. Each is opt-in: unset, the monitor reads and writes exactly what the
cron job does.

| Variable | Default | Replaces |
|---|---|---|
| `MONITOR_CONFIG` | `~/.config/fare-monitor/config.json` | the webhook config file |
| `FARE_MONITOR_STATE_DIR` | `~/.config/fare-monitor` | the directory holding `seen.json` |
| `FARE_MONITOR_SOURCES` | built-in `FEEDS` and `PAGES` | every source, wholesale |

`FARE_MONITOR_SOURCES` names a JSON file with exactly two keys, each mapping source name
to an http(s) URL:

```json
{"feeds": {"Fly4Free": "http://127.0.0.1:8765/sources/fly4free.xml"},
 "pages": {"Secret Flying": "http://127.0.0.1:8765/sources/secret-flying.html"}}
```

It replaces the built-in lists rather than merging with them, so a sandboxed run cannot
fall through to a live site it forgot to list; a malformed file stops the run with a
message naming it. Recorded Secret Flying pages work unchanged, because the parser keys on
the absolute `https://www.secretflying.com/posts/` links inside the page, not on where the
page was served from.

The webhook check accepts one non-Discord URL shape: a loopback fake, plain `http` to
exactly `127.0.0.1` or `localhost` with a path under `/api/webhooks/`. Any other host is
still rejected.

```bash
MONITOR_CONFIG=/tmp/sandbox/config.json \
FARE_MONITOR_STATE_DIR=/tmp/sandbox/state \
FARE_MONITOR_SOURCES=/tmp/sandbox/sources.json \
python3 fare_monitor.py --verbose
```

## Tests

```bash
python3 -m unittest test_fare_monitor
```

67 tests, no network, no dependencies beyond the standard library. Error fares are rare,
so a correct run and a broken run both print `No qualifying error fares found` — these
cover the paths that only execute on the day it matters, including detection against real
historical error-fare titles.

## CI and merging

`master` is protected: a change lands only through a pull request, and only after the
`check` job in `.github/workflows/ci.yml` passes. The rule applies to admins too, so
nothing — a person or a coding agent — can push straight to `master`. The cron job runs
from the local checkout and is unaffected.

`check` runs three gates; the same commands work locally:

```bash
python3 hooks/pre-commit --all     # secret scan over every tracked file
ruff check .                       # syntax errors + pyflakes; rules in ruff.toml
python3 -m unittest test_fare_monitor
```

The secret scan runs in CI as well as locally because the local hook is opt-in per clone
and `--no-verify` skips it; the CI copy is the one nothing can bypass. The lint rules are
deliberately narrow — undefined names, unused imports, syntax errors — so the gate
catches defects rather than demanding reformatting.

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
