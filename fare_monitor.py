#!/usr/bin/env python3
"""Check error/mistake-fare RSS feeds and post qualifying deals to Slack."""

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

import requests

CONFIG_DIR = Path.home() / ".config" / "fare-monitor"
CONFIG_PATH = CONFIG_DIR / "config.json"
SEEN_PATH = CONFIG_DIR / "seen.json"

# Generous window: duplicate alerts are prevented by the seen-file, so a wide
# lookback lets a run recover items that a previous failed run missed.
LOOKBACK_HOURS = 24.0
SEEN_RETENTION_DAYS = 30

FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 3

# Deal-posting feeds only. General points/miles blogs (TPG, One Mile at a
# Time, Thrifty Traveler) are deliberately excluded: they publish commentary
# *about* error fares ("anatomy of a mistake fare"), which matches the
# patterns but is never a bookable deal.
FEEDS = {
    "The Flight Deal": "https://www.theflightdeal.com/feed/",
    "Fly4Free": "https://www.fly4free.com/feed/",
    "Fare Deal Alert": "https://faredealalert.com/feed/",
}

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


def fetch_items(site: str, feed_url: str) -> list[dict]:
    last_exc: Exception | None = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            resp = requests.get(feed_url, timeout=15,
                                headers={"User-Agent": "fare-monitor/1.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            break
        except (requests.RequestException, ET.ParseError) as exc:
            last_exc = exc
            if attempt < FETCH_ATTEMPTS:
                time.sleep(FETCH_BACKOFF_SECONDS * attempt)
    else:
        raise last_exc  # type: ignore[misc]

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


def escape_slack(text: str) -> str:
    """Slack requires these three characters escaped in message text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_slack_message(webhook_url: str, qualifying: list[dict]) -> None:
    lines = [":airplane: *Error Fare Alert*", ""]
    for item in qualifying:
        lines.append(f"*{escape_slack(item['site'])}*")
        lines.append(escape_slack(item["title"]))
        lines.append(item["link"])
        lines.append(f"Published: {item['pub_date'].strftime('%Y-%m-%d %H:%M UTC')}")
        lines.append("")
    payload = {"text": "\n".join(lines).strip()}
    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true",
                        help="Print every fetched item (title, pubDate, in-window?, matched?) for debugging")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be sent without posting to Slack or recording state")
    args = parser.parse_args()

    webhook_url = load_webhook_url()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    seen = load_seen()

    qualifying = []
    for site, feed_url in FEEDS.items():
        try:
            items = fetch_items(site, feed_url)
        except (requests.RequestException, ET.ParseError) as exc:
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
        send_slack_message(webhook_url, qualifying)
    except requests.RequestException as exc:
        # Leave the seen-file untouched so the next run retries these items.
        sys.exit(f"[{now.isoformat()}] Failed to post to Slack: {exc}")

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
