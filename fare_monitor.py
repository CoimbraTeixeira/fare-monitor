#!/usr/bin/env python3
"""Check error/mistake-fare RSS feeds and post qualifying deals to Slack."""

import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

CONFIG_PATH = Path.home() / ".config" / "fare-monitor" / "config.json"
LOOKBACK_HOURS = 6.5  # 6h cron cadence + 30min overlap buffer

FEEDS = {
    "The Flight Deal": "https://www.theflightdeal.com/feed/",
    "Fly4Free": "https://www.fly4free.com/feed/",
}

ERROR_FARE_PATTERNS = [
    r"error fare", r"mistake fare", r"pricing error", r"pricing glitch",
    r"glitch fare", r"fare glitch", r"may not be honou?red",
    r"book (it |this )?(now |asap )?before it('s| is) (fixed|pulled|gone)",
    r"book before it disappears",
]
ERROR_FARE_RE = re.compile("|".join(ERROR_FARE_PATTERNS), re.IGNORECASE)


def load_webhook_url() -> str:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing config file: {CONFIG_PATH}")
    config = json.loads(CONFIG_PATH.read_text())
    url = config.get("webhook_url")
    if not url:
        sys.exit(f"No 'webhook_url' key in {CONFIG_PATH}")
    return url


def fetch_items(site: str, feed_url: str) -> list[dict]:
    resp = requests.get(feed_url, timeout=15, headers={"User-Agent": "fare-monitor/1.0"})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        description = (item.findtext("description") or "").strip()
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
        })
    return items


def is_error_fare(item: dict) -> bool:
    text = f"{item['title']} {item['description']}"
    return bool(ERROR_FARE_RE.search(text))


def send_slack_message(webhook_url: str, qualifying: list[dict]) -> None:
    lines = [":airplane: *Error Fare Alert*", ""]
    for item in qualifying:
        lines.append(f"*{item['site']}*")
        lines.append(item["title"])
        lines.append(item["link"])
        lines.append(f"Published: {item['pub_date'].strftime('%Y-%m-%d %H:%M UTC')}")
        lines.append("")
    payload = {"text": "\n".join(lines).strip()}
    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()


def main() -> None:
    webhook_url = load_webhook_url()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)

    qualifying = []
    for site, feed_url in FEEDS.items():
        try:
            items = fetch_items(site, feed_url)
        except (requests.RequestException, ET.ParseError) as exc:
            print(f"[{now.isoformat()}] Failed to fetch {site}: {exc}", file=sys.stderr)
            continue
        recent = [i for i in items if i["pub_date"] >= cutoff]
        qualifying.extend(i for i in recent if is_error_fare(i))

    if qualifying:
        qualifying.sort(key=lambda i: i["pub_date"], reverse=True)
        send_slack_message(webhook_url, qualifying)
        print(f"[{now.isoformat()}] Sent alert for {len(qualifying)} item(s).")
    else:
        print(f"[{now.isoformat()}] No qualifying error fares found.")


if __name__ == "__main__":
    main()
