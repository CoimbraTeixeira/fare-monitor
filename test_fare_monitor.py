#!/usr/bin/env python3
"""Tests for fare_monitor.

Run with:  python3 -m unittest -v test_fare_monitor

Error fares are rare, so the alerting path would otherwise sit untested
until the day it actually matters. Everything here is offline: feeds and
the Discord webhook are stubbed, and state is written to a temp directory.
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

# A fixed instant, for tests that hand it to the code under test explicitly (the
# seen-file retention ones). Deliberately NOT used to age feed items: main() reads the
# real clock, so pinning item dates here gave the suite a 24-hour shelf life -- every
# window-dependent test began failing once wall-clock time passed NOW + LOOKBACK_HOURS.
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def make_item(title, description="", categories=(), age_hours=1, item_id="id-1",
              link="https://example.com/deal"):
    """Build an item the way fetch_items would, with text already cleaned.

    Ages are relative to the real clock so they stay meaningful against main()'s own
    lookback window whenever the suite happens to run."""
    return {
        "site": "Test Site",
        "title": fm.clean_text(title),
        "link": link,
        "pub_date": datetime.now(timezone.utc) - timedelta(hours=age_hours),
        "description": fm.clean_text(description),
        "categories": [fm.clean_text(c) for c in categories],
        "id": item_id,
    }


def feed_bytes(items_xml):
    return f'<?xml version="1.0"?><rss><channel>{items_xml}</channel></rss>'.encode()


class FakeResponse:
    def __init__(self, content=b"", exc=None, status_code=200, payload=None, headers=None):
        self.content = content
        self._exc = exc
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    @property
    def text(self):
        return self.content.decode() if isinstance(self.content, bytes) else self.content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self._exc:
            raise self._exc
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


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


class MarkdownEscapingTests(unittest.TestCase):
    def test_escapes_discord_markdown(self):
        self.assertEqual(fm.escape_markdown("*deal*"), r"\*deal\*")
        self.assertEqual(fm.escape_markdown("LIS_TYO"), r"LIS\_TYO")
        self.assertEqual(fm.escape_markdown("a|b~c`d"), r"a\|b\~c\`d")

    def test_escapes_backslash_first(self):
        """Escaping the backslash last would re-escape the ones just inserted."""
        self.assertEqual(fm.escape_markdown(r"a\*b"), r"a\\\*b")

    def test_leaves_html_entities_alone(self):
        """Discord renders &amp; literally, so HTML-entity escaping is wrong here."""
        self.assertEqual(fm.escape_markdown("Prague & Warsaw"), "Prague & Warsaw")
        self.assertEqual(fm.escape_markdown("<deal>"), "<deal>")

    def test_payload_uses_content_key_and_escapes_title(self):
        posted = []

        def fake_post(url, json=None, timeout=None):
            posted.append(json)
            return FakeResponse()

        with mock.patch.object(fm.requests, "post", fake_post):
            fm.send_discord_message("https://hook", [make_item("Deal *now* or_never")])
        # Discord's payload key is "content"; any other key is silently ignored.
        self.assertIn("content", posted[0])
        self.assertIn(r"Deal \*now\* or\_never", posted[0]["content"])

    def test_link_is_not_escaped(self):
        """Backslashes in a URL would break it, and Discord auto-links bare URLs."""
        posted = []

        def fake_post(url, json=None, timeout=None):
            posted.append(json)
            return FakeResponse()

        item = make_item("A deal")
        item["link"] = "https://example.com/error_fare_to_tokyo"
        with mock.patch.object(fm.requests, "post", fake_post):
            fm.send_discord_message("https://hook", [item])
        self.assertIn("https://example.com/error_fare_to_tokyo", posted[0]["content"])


class MessageSplittingTests(unittest.TestCase):
    def test_short_message_is_one_chunk(self):
        self.assertEqual(fm.split_message("one\ntwo"), ["one\ntwo"])

    def test_empty_message_still_yields_a_chunk(self):
        self.assertEqual(fm.split_message(""), [""])

    def test_every_chunk_is_within_the_limit(self):
        text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
        chunks = fm.split_message(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), fm.DISCORD_MESSAGE_LIMIT)

    def test_split_preserves_content_and_line_boundaries(self):
        text = "\n".join(f"deal {i}" for i in range(600))
        chunks = fm.split_message(text)
        self.assertEqual("\n".join(chunks), text)  # nothing lost or duplicated
        for chunk in chunks:
            self.assertFalse(chunk.startswith("\n"))

    def test_line_longer_than_limit_is_hard_split(self):
        chunks = fm.split_message("y" * (fm.DISCORD_MESSAGE_LIMIT * 2 + 5))
        self.assertEqual(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), fm.DISCORD_MESSAGE_LIMIT)

    def test_oversized_alert_posts_multiple_times(self):
        posted = []

        def fake_post(url, json=None, timeout=None):
            posted.append(json["content"])
            return FakeResponse()

        items = [make_item("Error fare " + "z" * 120, item_id=f"i{i}") for i in range(40)]
        with mock.patch.object(fm.requests, "post", fake_post):
            fm.send_discord_message("https://hook", items)
        self.assertGreater(len(posted), 1)
        for chunk in posted:
            self.assertLessEqual(len(chunk), fm.DISCORD_MESSAGE_LIMIT)


class RateLimitTests(unittest.TestCase):
    def test_retries_after_429_and_succeeds(self):
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(json)
            if len(calls) == 1:
                return FakeResponse(status_code=429, payload={"retry_after": 0.01})
            return FakeResponse()

        with mock.patch.object(fm.requests, "post", fake_post), \
             mock.patch.object(fm.time, "sleep") as slept:
            fm.post_chunk("https://hook", "hello")
        self.assertEqual(len(calls), 2)
        slept.assert_called_once()
        self.assertAlmostEqual(slept.call_args[0][0], 0.01, places=3)

    def test_retry_wait_is_capped(self):
        """A malformed retry_after must not park a cron run for hours."""
        def fake_post(url, json=None, timeout=None):
            return FakeResponse(status_code=429, payload={"retry_after": 99999})

        with mock.patch.object(fm.requests, "post", fake_post), \
             mock.patch.object(fm.time, "sleep") as slept:
            with self.assertRaises(requests.HTTPError):
                fm.post_chunk("https://hook", "hello")
        for call in slept.call_args_list:
            self.assertLessEqual(call[0][0], fm.MAX_RETRY_SLEEP)

    def test_gives_up_after_repeated_429(self):
        def fake_post(url, json=None, timeout=None):
            return FakeResponse(status_code=429, payload={"retry_after": 0})

        with mock.patch.object(fm.requests, "post", fake_post), \
             mock.patch.object(fm.time, "sleep"):
            with self.assertRaises(requests.HTTPError):
                fm.post_chunk("https://hook", "hello")


class WebhookConfigTests(unittest.TestCase):
    def _load(self, url):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"webhook_url": url}))
            with mock.patch.object(fm, "CONFIG_PATH", path):
                return fm.load_webhook_url()

    def test_non_discord_webhook_is_rejected_by_name(self):
        """Any other provider's URL would fail as an opaque 400 from Discord."""
        with self.assertRaises(SystemExit) as ctx:
            self._load("https://example.com/services/T1/B2/xyz")
        self.assertIn("does not hold a Discord webhook", str(ctx.exception))

    def test_discord_webhook_is_accepted(self):
        url = "https://discord.com/api/webhooks/123/abc"
        self.assertEqual(self._load(url), url)

    def test_legacy_discordapp_domain_is_accepted(self):
        url = "https://discordapp.com/api/webhooks/123/abc"
        self.assertEqual(self._load(url), url)


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
        self.assertEqual(len(self.items), 2)
        for item in self.items:
            with self.subTest(link=item["link"]):
                self.assertIn("/posts/", item["link"])
                self.assertIsNotNone(item["pub_date"].tzinfo)

    def test_category_widget_is_not_taken_as_a_post(self):
        """The fixture's "Trending flight deals" widget links to /posts/category/...;
        that is a listing page, and was once emitted as a deal."""
        self.assertFalse([i for i in self.items if "/posts/category/" in i["link"]])

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

    def test_expired_stamp_is_carried_on_the_item(self):
        """Secret Flying stamps pulled deals rather than removing them."""
        self.assertFalse(any(i["expired"] for i in self.items))
        stamped = self.html.replace(
            '<div class="entry-img">',
            '<div class="entry-img"><img src="/EXPIRED1.gif" class="expiredStamp">', 1)
        items = fm.parse_page_items("Secret Flying", stamped)
        self.assertTrue(items[0]["expired"])
        self.assertEqual(fm.skip_reason(items[0]), "expired")


class SkipReasonTests(unittest.TestCase):
    def test_live_deal_is_not_skipped(self):
        self.assertIsNone(fm.skip_reason(make_item(
            "ERROR FARE: New York to Seychelles $335",
            link="https://travel-dealz.com/deal/newyork-seychelles-fare/")))

    def test_expired_title_is_skipped(self):
        for title in ("**EXPIRED** ERROR FARE: Business to Seoul", "Error fare (expired)"):
            with self.subTest(title=title):
                self.assertEqual(fm.skip_reason(make_item(title)), "expired")

    def test_commentary_sections_are_skipped(self):
        """News about an error fare matches the patterns but is never bookable."""
        for link in ("https://www.fly4free.com/news/airline-honours-error-fare/",
                     "https://travel-dealz.de/blog/anleitung-errorfare-buchen"):
            with self.subTest(link=link):
                self.assertEqual(
                    fm.skip_reason(make_item("Mistake fare honoured", link=link)),
                    "commentary")

    def test_section_match_is_on_the_first_path_segment_only(self):
        """A deal whose slug merely contains 'news' must still alert."""
        item = make_item("Error fare", link="https://example.com/deal/good-news-error-fare/")
        self.assertIsNone(fm.skip_reason(item))


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
    """End-to-end runs of main() with feeds and Discord stubbed out."""

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

    def test_skipped_items_are_not_alerted_or_recorded(self):
        """Expired and commentary items match the patterns; only the live deal posts."""
        items = [
            make_item("**EXPIRED** ERROR FARE: Dublin to USA", item_id="expired"),
            make_item("Airline honours last week's mistake fare", item_id="news",
                      link="https://www.fly4free.com/news/airline-honours-fare/"),
            make_item("ERROR FARE: Oslo to Bangkok", item_id="live"),
        ]
        self.assertIn("Sent alert for 1 item", self.run_main(items))
        self.assertIn("Oslo to Bangkok", self.posts[0]["content"])
        self.assertEqual(list(json.loads(fm.SEEN_PATH.read_text())), ["live"])

    def test_discord_failure_leaves_state_unwritten(self):
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

    def test_prunes_expired_and_malformed_entries_and_normalizes_naive_dates(self):
        fm.save_seen(
            {
                "keep": NOW.isoformat(),
                "naive": "2026-08-02T11:00:00",
                "expired": (NOW - timedelta(days=60)).isoformat(),
                "malformed": "not-a-timestamp",
                "not-string": None,
            },
            NOW,
        )
        stored = json.loads(fm.SEEN_PATH.read_text())
        self.assertEqual(sorted(stored), ["keep", "naive"])
        self.assertEqual(stored["naive"], "2026-08-02T11:00:00+00:00")

    def test_round_trips(self):
        fm.save_seen({"a": NOW.isoformat()}, NOW)
        self.assertEqual(fm.load_seen(), {"a": NOW.isoformat()})


if __name__ == "__main__":
    unittest.main()
