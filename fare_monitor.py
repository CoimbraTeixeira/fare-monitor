#!/usr/bin/env python3
"""Check error/mistake-fare RSS feeds and post qualifying deals to Discord."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

import defusedxml.ElementTree as ET
import requests
from defusedxml.common import DefusedXmlException

CONFIG_DIR = Path.home() / ".config" / "fare-monitor"
CONFIG_PATH = CONFIG_DIR / "config.json"
SEEN_PATH = CONFIG_DIR / "seen.json"

# Generous window: duplicate alerts are prevented by the seen-file, so a wide
# lookback lets a run recover items that a previous failed run missed.
LOOKBACK_HOURS = 24.0
SEEN_RETENTION_DAYS = 30

FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 3

# Discord rejects a message body over 2000 characters outright (HTTP 400) rather than
# truncating it, so this is a hard delivery constraint rather than a style preference.
DISCORD_MESSAGE_LIMIT = 2000

# Webhooks allow roughly 5 requests per 2 seconds; a multi-chunk alert can reach that.
RATE_LIMIT_RETRIES = 3
MAX_RETRY_SLEEP = 30.0

# Deal-posting feeds only. General points/miles blogs (TPG, One Mile at a
# Time, Thrifty Traveler) are deliberately excluded: they publish commentary
# *about* error fares ("anatomy of a mistake fare"), which matches the
# patterns but is never a bookable deal.
FEEDS = {
    "The Flight Deal": "https://www.theflightdeal.com/feed/",
    "Fly4Free": "https://www.fly4free.com/feed/",
    "Fare Deal Alert": "https://faredealalert.com/feed/",
    "Travelfree": "https://travelfree.info/feed/",
}

# Sites with no usable feed, read from HTML instead. Secret Flying is the only
# source here that labels error fares explicitly ("ERROR FARE" in the title),
# but Cloudflare 403s its /feed/ and category pages; the homepage is served
# normally, and their robots.txt allows crawling at Crawl-delay: 1. One request
# every 6h is well inside that.
PAGES = {
    "Secret Flying": "https://www.secretflying.com/",
}

ARTICLE_RE = re.compile(r"<article.*?</article>", re.DOTALL)
POST_LINK_RE = re.compile(r'href="(https://www\.secretflying\.com/posts/[^"]+)"')
DATETIME_RE = re.compile(r'datetime="([^"]+)"')
ANCHOR_RE = re.compile(
    r'<a[^>]*href="https://www\.secretflying\.com/posts/[^"]*"[^>]*>(.*?)</a>', re.DOTALL
)
DATE_LABEL_RE = re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")

ERROR_FARE_PATTERNS = [
    r"error fare", r"mistake fare", r"pricing error", r"pricing glitch",
    r"glitch fare", r"fare glitch", r"may not be honou?red",
    r"book (it |this )?(now |asap )?before it('s| is) (fixed|pulled|gone)",
    r"book before it disappears",
]
ERROR_FARE_RE = re.compile("|".join(ERROR_FARE_PATTERNS), re.IGNORECASE)

TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def load_webhook_url() -> str:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing config file: {CONFIG_PATH}")
    config = json.loads(CONFIG_PATH.read_text())
    url = config.get("webhook_url")
    if not url:
        sys.exit(f"No 'webhook_url' key in {CONFIG_PATH}")
    if "hooks.slack.com" in url:
        # Posting a Discord payload at a Slack webhook fails as an opaque 400. Naming
        # the leftover config is the difference between a one-line fix and an evening.
        sys.exit(
            f"{CONFIG_PATH} still holds a Slack webhook. This monitor posts to Discord "
            f"now -- replace it with a Discord webhook URL "
            f"(Server Settings -> Integrations -> Webhooks)."
        )
    return url


def load_seen() -> dict[str, str]:
    """Map of item id -> ISO timestamp of when we alerted on it."""
    if not SEEN_PATH.exists():
        return {}
    try:
        data = json.loads(SEEN_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Ignoring unreadable seen-file {SEEN_PATH}: {exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def save_seen(seen: dict[str, str], now: datetime) -> None:
    """Write the seen-file atomically, pruning entries past the retention window."""
    horizon = now - timedelta(days=SEEN_RETENTION_DAYS)
    pruned = {}
    for item_id, stamp in seen.items():
        try:
            if datetime.fromisoformat(stamp) >= horizon:
                pruned[item_id] = stamp
        except ValueError:
            continue  # drop malformed entries

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pruned, indent=2, sort_keys=True))
    os.replace(tmp, SEEN_PATH)


def clean_text(raw: str) -> str:
    """Strip HTML markup and entities so patterns match the prose, not the tags."""
    return WHITESPACE_RE.sub(" ", unescape(TAG_RE.sub(" ", raw))).strip()


def http_get(url: str) -> requests.Response:
    """GET with retries. Raises the last error if every attempt fails."""
    last_exc: Exception | None = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": "fare-monitor/1.0"})
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < FETCH_ATTEMPTS:
                time.sleep(FETCH_BACKOFF_SECONDS * attempt)
    raise last_exc  # type: ignore[misc]


def parse_page_items(site: str, html: str) -> list[dict]:
    """Extract posts from a Secret Flying-style listing page.

    The title is taken from the post's anchor text, but matching also runs
    against the URL slug ("...posts/error-fare-business-class-..."), which
    survives markup changes that would break title extraction.
    """
    items, seen_links = [], set()
    for block in ARTICLE_RE.findall(html):
        link_match = POST_LINK_RE.search(block)
        date_match = DATETIME_RE.search(block)
        if not (link_match and date_match):
            continue
        link = link_match.group(1)
        if link in seen_links:
            continue
        seen_links.add(link)

        try:
            pub_date = datetime.fromisoformat(date_match.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
        if pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=timezone.utc)

        slug_words = link.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        candidates = [clean_text(a) for a in ANCHOR_RE.findall(block)]
        candidates = [c for c in candidates if len(c) > 12 and not DATE_LABEL_RE.match(c)]
        title = max(candidates, key=len) if candidates else slug_words

        items.append({
            "site": site,
            "title": title,
            "link": link,
            "pub_date": pub_date,
            "description": slug_words,
            "categories": [],
            "id": link,
        })
    return items


def fetch_page_items(site: str, page_url: str) -> list[dict]:
    items = parse_page_items(site, http_get(page_url).text)
    if not items:
        # Silent breakage would look identical to a quiet day in the log.
        print(f"Warning: extracted 0 posts from {page_url} -- page layout may have changed",
              file=sys.stderr)
    return items


def fetch_items(site: str, feed_url: str) -> list[dict]:
    # defusedxml rather than the stdlib parser: feed bodies are third-party input and
    # xml.etree is vulnerable to entity-expansion bombs, where a few KB of XML expands
    # to gigabytes and takes the run down with it.
    root = ET.fromstring(http_get(feed_url).content)
    items = []
    for item in root.findall("./channel/item"):
        title = clean_text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        description = clean_text(item.findtext("description") or "")
        categories = [clean_text(c.text or "") for c in item.findall("category")]
        try:
            pub_date = parsedate_to_datetime(pub_date_raw)
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        items.append({
            "site": site,
            "title": title,
            "link": link,
            "pub_date": pub_date,
            "description": description,
            "categories": categories,
            "id": (item.findtext("guid") or link or title).strip(),
        })
    return items


def is_error_fare(item: dict) -> bool:
    text = " ".join([item["title"], item["description"], *item["categories"]])
    return bool(ERROR_FARE_RE.search(text))


def escape_markdown(text: str) -> str:
    """Neutralise Discord markdown in third-party text.

    Deal titles are written by the feed authors and routinely contain asterisks and
    underscores, which would otherwise bold or italicise the rest of the alert. Note
    this escapes markdown rather than HTML entities -- Discord renders `&amp;`
    literally, so the Slack-era entity escaping would have shown as visible noise."""
    for char in ("\\", "*", "_", "~", "`", "|"):
        text = text.replace(char, "\\" + char)
    return text


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split an over-long message into Discord-sized pieces on line boundaries.

    A busy day can qualify enough deals to clear the limit, and Discord rejects an
    over-long body outright rather than truncating it. Lines are kept whole so a deal
    is never cut in half; an individual line longer than the limit is hard-split as a
    last resort."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0

    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, length = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if current and length + 1 + len(line) > limit:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + (1 if length else 0)

    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def post_chunk(webhook_url: str, content: str) -> None:
    """POST one chunk, honouring Discord's stated wait on a rate-limit response."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        resp = requests.post(webhook_url, json={"content": content}, timeout=15)
        if resp.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            resp.raise_for_status()  # Discord returns 204 No Content on success
            return
        try:
            wait = float(resp.json().get("retry_after", 1.0))
        except (ValueError, AttributeError, requests.exceptions.JSONDecodeError):
            wait = float(resp.headers.get("Retry-After", 1.0) or 1.0)
        # Cap it: a malformed retry_after should not park a cron run for hours.
        time.sleep(min(max(wait, 0.0), MAX_RETRY_SLEEP))


def send_discord_message(webhook_url: str, qualifying: list[dict]) -> None:
    lines = ["✈️ **Error Fare Alert**", ""]
    for item in qualifying:
        lines.append(f"**{escape_markdown(item['site'])}**")
        lines.append(escape_markdown(item["title"]))
        # The link is deliberately unescaped: backslashes in a URL would break it, and
        # Discord auto-links a bare URL anyway.
        lines.append(item["link"])
        lines.append(f"Published: {item['pub_date'].strftime('%Y-%m-%d %H:%M UTC')}")
        lines.append("")
    for chunk in split_message("\n".join(lines).strip()):
        post_chunk(webhook_url, chunk)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true",
                        help="Print every fetched item (title, pubDate, in-window?, matched?) for debugging")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be sent without posting to Discord or recording state")
    args = parser.parse_args()

    webhook_url = load_webhook_url()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    seen = load_seen()

    sources = [(site, url, fetch_items) for site, url in FEEDS.items()]
    sources += [(site, url, fetch_page_items) for site, url in PAGES.items()]

    qualifying = []
    for site, feed_url, fetch in sources:
        try:
            items = fetch(site, feed_url)
        except (requests.RequestException, ET.ParseError, DefusedXmlException) as exc:
            print(f"[{now.isoformat()}] Failed to fetch {site}: {exc}", file=sys.stderr)
            continue
        if args.verbose:
            print(f"-- {site}: fetched {len(items)} item(s) --")
        for i in items:
            in_window = i["pub_date"] >= cutoff
            matched = is_error_fare(i)
            already_sent = i["id"] in seen
            if args.verbose:
                print(f"  [{'IN' if in_window else 'old'}] "
                      f"[{'MATCH' if matched else '  no  '}] "
                      f"[{'sent' if already_sent else 'new '}] "
                      f"{i['pub_date'].strftime('%Y-%m-%d %H:%M UTC')}  {i['title']}")
            if in_window and matched and not already_sent:
                qualifying.append(i)

    if not qualifying:
        print(f"[{now.isoformat()}] No qualifying error fares found.")
        return

    qualifying.sort(key=lambda i: i["pub_date"], reverse=True)

    if args.dry_run:
        print(f"[{now.isoformat()}] DRY RUN: would alert on {len(qualifying)} item(s):")
        for i in qualifying:
            print(f"  {i['site']}: {i['title']}  {i['link']}")
        return

    try:
        send_discord_message(webhook_url, qualifying)
    except requests.RequestException as exc:
        # Leave the seen-file untouched so the next run retries these items.
        sys.exit(f"[{now.isoformat()}] Failed to post to Discord: {exc}")

    for i in qualifying:
        seen[i["id"]] = now.isoformat()
    try:
        save_seen(seen, now)
    except OSError as exc:
        print(f"[{now.isoformat()}] Alert sent but could not write {SEEN_PATH}: {exc}",
              file=sys.stderr)

    print(f"[{now.isoformat()}] Sent alert for {len(qualifying)} item(s).")


if __name__ == "__main__":
    main()
