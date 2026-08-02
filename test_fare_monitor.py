#!/usr/bin/env python3
"""Tests for fare_monitor.

Run with:  python3 -m unittest -v test_fare_monitor

Error fares are rare, so the alerting path would otherwise sit untested
until the day it actually matters. Everything here is offline: feeds and
the Slack webhook are stubbed, and state is written to a temp directory.
"""

import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

import fare_monitor as fm

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def make_item(title, description="", categories=(), age_hours=1, item_id="id-1"):
    """Build an item the way fetch_items would, with text already cleaned."""
    return {
        "site": "Test Site",
        "title": fm.clean_text(title),
        "link": "https://example.com/deal",
        "pub_date": NOW - timedelta(hours=age_hours),
        "description": fm.clean_text(description),
        "categories": [fm.clean_text(c) for c in categories],
        "id": item_id,
    }


def feed_bytes(items_xml):
    return f'<?xml version="1.0"?><rss><channel>{items_xml}</channel></rss>'.encode()


class FakeResponse:
    def __init__(self, content=b"", exc=None):
        self.content = content
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc


class MatchingTests(unittest.TestCase):
    def test_matches_plain_phrases(self):
        for text in ["Error fare: LIS-NYC $99", "MISTAKE FARE!!", "a pricing glitch"]:
            with self.subTest(text=text):
                self.assertTrue(fm.is_error_fare(make_item(text)))

    def test_matches_phrase_split_by_html_tag(self):
        """The bug that caused silent misses: description is markup, not prose."""
        item = make_item("Deal", "this is a pricing <strong>error</strong> folks")
        self.assertTrue(fm.is_error_fare(item))

    def test_matches_both_spellings_of_honored(self):
        for text in ["may not be honored", "may not be honoured"]:
            with self.subTest(text=text):
                self.assertTrue(fm.is_error_fare(make_item("Deal", text)))

    def test_matches_urgency_phrasing(self):
        item = make_item("Deal", "book now before it's fixed")
        self.assertTrue(fm.is_error_fare(item))

    def test_matches_via_category_tag(self):
        item = make_item("Deal", "ordinary prose", categories=["Error Fare", "asia"])
        self.assertTrue(fm.is_error_fare(item))

    def test_ignores_ordinary_deals(self):
        """Real headlines from the live feeds must not trigger alerts."""
        for title in [
            "Copa: Chicago - San Jose, Costa Rica. $346. Roundtrip",
            "PRICE DROP: Cheap direct flights from Amsterdam to Sal",
            "Book fast, top-rated 4* hotel deal",
        ]:
            with self.subTest(title=title):
                self.assertFalse(fm.is_error_fare(make_item(title, "A good sale")))

    def test_matches_real_historical_error_fares(self):
        """Titles of actual error-fare posts from The Flight Deal's archive.

        These are the ground truth for the whole script. The site labels such
        posts in the title, which the feed carries in full -- so the truncated
        "Read More" excerpt is not a detection gap, and there is no need to
        fetch article bodies. If a pattern change breaks these, the monitor
        has stopped doing its only job.
        """
        for title in [
            "Mistake Fare Alert: United. Seattle - Beijing, China. $405! Round trip. All in.",
            "Mistake Fare - Delta: Newark - San Pedro, Honduras. As low as $206. Roundtrip.",
            "Mistake Fare Alert! Swiss Air: Los Angeles/San Francisco - Zurich. $568. Roundtrip.",
        ]:
            with self.subTest(title=title):
                self.assertTrue(fm.is_error_fare(make_item(title)))


class CleanTextTests(unittest.TestCase):
    def test_strips_tags(self):
        self.assertEqual(fm.clean_text("<div><b>hi</b> there</div>"), "hi there")

    def test_decodes_entities(self):
        self.assertEqual(fm.clean_text("Rio &#8211; SP &amp; more"), "Rio – SP & more")

    def test_collapses_whitespace(self):
        self.assertEqual(fm.clean_text("a\n\n\tb   c"), "a b c")

    def test_preserves_unicode_and_emoji(self):
        self.assertEqual(fm.clean_text("Prague 🌴 for €594"), "Prague 🌴 for €594")


class SlackEscapingTests(unittest.TestCase):
    def test_escapes_required_characters(self):
        self.assertEqual(fm.escape_slack("Prague & Warsaw"), "Prague &amp; Warsaw")
        self.assertEqual(fm.escape_slack("<deal>"), "&lt;deal&gt;")

    def test_does_not_double_escape(self):
        self.assertEqual(fm.escape_slack("a & b").count("amp"), 1)

    def test_payload_escapes_title(self):
        posted = {}

        def fake_post(url, json=None, timeout=None):
            posted["payload"] = json
            return FakeResponse()

        with mock.patch.object(fm.requests, "post", fake_post):
            fm.send_slack_message("https://hook", [make_item("Prague & Warsaw")])
        self.assertIn("Prague &amp; Warsaw", posted["payload"]["text"])


class FetchItemsTests(unittest.TestCase):
    ITEM = (
        "<item><title>Mistake fare LIS</title><link>https://example.com/1</link>"
        "<pubDate>Sun, 02 Aug 2026 10:00:00 +0000</pubDate>"
        "<description>x</description><guid>g1</guid>"
        "<category>Error Fare</category></item>"
    )
    BAD_DATE = (
        "<item><title>Bad date</title><link>https://example.com/2</link>"
        "<pubDate>not-a-date</pubDate><description>y</description>"
        "<guid>g2</guid></item>"
    )

    def fetch(self, xml):
        with mock.patch.object(
            fm.requests, "get", lambda url, timeout=None, headers=None: FakeResponse(xml)
        ):
            return fm.fetch_items("Test Site", "https://feed")

    def test_parses_guid_and_categories(self):
        items = self.fetch(feed_bytes(self.ITEM))
        self.assertEqual(items[0]["id"], "g1")
        self.assertEqual(items[0]["categories"], ["Error Fare"])

    def test_skips_unparseable_dates(self):
        items = self.fetch(feed_bytes(self.ITEM + self.BAD_DATE))
        self.assertEqual([i["id"] for i in items], ["g1"])

    def test_falls_back_to_link_when_guid_missing(self):
        xml = self.ITEM.replace("<guid>g1</guid>", "")
        self.assertEqual(self.fetch(feed_bytes(xml))[0]["id"], "https://example.com/1")


class PageParsingTests(unittest.TestCase):
    """Secret Flying is read from HTML: its feed and category pages are 403."""

    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).parent / "testdata" / "secretflying_home.html"
        cls.html = fixture.read_text()
        cls.items = fm.parse_page_items("Secret Flying", cls.html)

    def test_extracts_posts_with_dates_and_links(self):
        self.assertGreaterEqual(len(self.items), 5)
        for item in self.items:
            with self.subTest(link=item["link"]):
                self.assertIn("/posts/", item["link"])
                self.assertIsNotNone(item["pub_date"].tzinfo)

    def test_deduplicates_repeated_links(self):
        links = [i["link"] for i in self.items]
        self.assertEqual(len(links), len(set(links)))

    def test_prefers_readable_title_over_slug(self):
        """Titles carry emoji and capitalisation; slugs are hyphenated lowercase."""
        titled = [i for i in self.items if "ERROR FARE" in i["title"]]
        self.assertTrue(titled, "fixture should contain a labelled error fare")
        self.assertNotIn("-", titled[0]["title"].split()[0])

    def test_title_is_never_a_bare_date(self):
        for item in self.items:
            with self.subTest(title=item["title"]):
                self.assertIsNone(fm.DATE_LABEL_RE.match(item["title"]))

    def test_detects_the_labelled_error_fare(self):
        matched = [i for i in self.items if fm.is_error_fare(i)]
        self.assertTrue(matched, "the 'ERROR FARE' post must be detected")

    def test_matches_via_slug_when_title_extraction_fails(self):
        """Markup can change; the URL slug is the durable signal."""
        # Blank out plain-text anchor content (the titles) while leaving nested
        # markup alone, so <time datetime=...> inside the date link survives.
        stripped = re.sub(r"(<a[^>]*>)([^<]{12,})(</a>)", r"\1\3", self.html)
        items = fm.parse_page_items("Secret Flying", stripped)
        self.assertTrue(items, "structure should still parse without title text")
        self.assertTrue([i for i in items if fm.is_error_fare(i)])

    def test_warns_when_extraction_yields_nothing(self):
        """A layout change must not look like a quiet day."""
        err = io.StringIO()
        empty = type("R", (), {"text": "<html><body>no articles</body></html>"})()
        with mock.patch.object(fm, "http_get", lambda url: empty), \
                mock.patch.object(sys, "stderr", err):
            items = fm.fetch_page_items("Secret Flying", "https://example.com/")
        self.assertEqual(items, [])
        self.assertIn("0 posts", err.getvalue())


class RetryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(fm, "FETCH_BACKOFF_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_raises_after_exhausting_attempts(self):
        calls = []

        def always_fail(url, timeout=None, headers=None):
            calls.append(1)
            raise requests.ConnectionError("boom")

        with mock.patch.object(fm.requests, "get", always_fail):
            with self.assertRaises(requests.RequestException):
                fm.fetch_items("Test Site", "https://feed")
        self.assertEqual(len(calls), fm.FETCH_ATTEMPTS)

    def test_recovers_from_transient_failure(self):
        calls = []

        def flaky(url, timeout=None, headers=None):
            calls.append(1)
            if len(calls) == 1:
                raise requests.Timeout("slow")
            return FakeResponse(feed_bytes(FetchItemsTests.ITEM))

        with mock.patch.object(fm.requests, "get", flaky):
            items = fm.fetch_items("Test Site", "https://feed")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 2)


class MainFlowTests(unittest.TestCase):
    """End-to-end runs of main() with feeds and Slack stubbed out."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.posts = []

        for target, value in [
            ("CONFIG_DIR", self.tmp),
            ("SEEN_PATH", self.tmp / "seen.json"),
            ("FEEDS", {"Test Site": "https://feed"}),
            ("PAGES", {}),  # keep main() offline; HTML sources covered separately
            ("load_webhook_url", lambda: "https://hook"),
        ]:
            patcher = mock.patch.object(fm, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        patcher = mock.patch.object(sys, "argv", ["fare_monitor.py"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, items, post=None):
        def default_post(url, json=None, timeout=None):
            self.posts.append(json)
            return FakeResponse()

        buf = io.StringIO()
        with mock.patch.object(fm, "fetch_items", lambda s, u: items), \
                mock.patch.object(fm.requests, "post", post or default_post), \
                redirect_stdout(buf):
            fm.main()
        return buf.getvalue()

    def test_alerts_once_then_suppresses_duplicate(self):
        """A 6.5h lookback on a 6h cron used to re-report the overlap."""
        item = make_item("Mistake fare: LIS-NYC $99", item_id="guid-42")

        self.assertIn("Sent alert for 1 item", self.run_main([item]))
        self.assertIn("No qualifying", self.run_main([item]))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(list(json.loads(fm.SEEN_PATH.read_text())), ["guid-42"])

    def test_ignores_items_outside_lookback_window(self):
        item = make_item("Error fare, but stale", age_hours=100, item_id="old")
        self.assertIn("No qualifying", self.run_main([item]))
        self.assertFalse(fm.SEEN_PATH.exists())

    def test_slack_failure_leaves_state_unwritten(self):
        """State must not record a send that never happened, or we lose the deal."""

        def boom(url, json=None, timeout=None):
            raise requests.ConnectionError("network down")

        with self.assertRaises(SystemExit):
            self.run_main([make_item("Error fare now", item_id="new")], post=boom)
        self.assertFalse(fm.SEEN_PATH.exists())

    def test_dry_run_does_not_post_or_record(self):
        with mock.patch.object(sys, "argv", ["fare_monitor.py", "--dry-run"]):
            out = self.run_main([make_item("Error fare now", item_id="new")])
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.posts, [])
        self.assertFalse(fm.SEEN_PATH.exists())


class SeenFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for target, value in [("CONFIG_DIR", self.tmp), ("SEEN_PATH", self.tmp / "seen.json")]:
            patcher = mock.patch.object(fm, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missing_file_is_empty(self):
        self.assertEqual(fm.load_seen(), {})

    def test_corrupt_file_is_tolerated(self):
        fm.SEEN_PATH.write_text("{ not valid json")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(fm.load_seen(), {})

    def test_prunes_expired_and_malformed_entries(self):
        fm.save_seen(
            {
                "keep": NOW.isoformat(),
                "expired": (NOW - timedelta(days=60)).isoformat(),
                "malformed": "not-a-timestamp",
            },
            NOW,
        )
        self.assertEqual(sorted(json.loads(fm.SEEN_PATH.read_text())), ["keep"])

    def test_round_trips(self):
        fm.save_seen({"a": NOW.isoformat()}, NOW)
        self.assertEqual(fm.load_seen(), {"a": NOW.isoformat()})


if __name__ == "__main__":
    unittest.main()
