"""Tests for digest.fetch. No real network -- all adapters are fed fixtures
via an injected `fetch: Callable[[str], str]`."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from digest.fetch import (
    Gap,
    fetch_arxiv,
    fetch_bluesky,
    fetch_gnews,
    fetch_hn,
    fetch_rss,
    fetch_youtube,
    gather,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


def _fetch_from(text: str):
    """Returns a fetch() stub that ignores the URL and returns fixed text."""
    return lambda url: text


UTC = timezone.utc


class TestFetchRss:
    def test_parses_recent_item_fields(self):
        since = datetime(2026, 7, 1, tzinfo=UTC)
        config = {"source_key": "test_blog", "person": "jane", "category": "thinkers", "url": "https://example.com/feed"}
        items = fetch_rss(config, since, fetch=_fetch_from(_read("sample_rss.xml")))

        assert len(items) == 1
        item = items[0]
        assert item.title == "Recent Post About Frontier Models"
        # utm_source stripped by canonical_url
        assert item.url == "https://example.com/posts/recent"
        assert item.published == datetime(2026, 7, 17, 10, 0, 0, tzinfo=UTC)
        assert item.published.tzinfo is not None
        assert item.source_key == "test_blog"
        assert item.source_type == "rss"
        assert item.person == "jane"
        assert item.category == "thinkers"
        assert "frontier model" in item.text.lower()

    def test_since_filter_excludes_old_item(self):
        since = datetime(2026, 7, 1, tzinfo=UTC)
        config = {"source_key": "test_blog", "url": "https://example.com/feed"}
        items = fetch_rss(config, since, fetch=_fetch_from(_read("sample_rss.xml")))
        titles = [i.title for i in items]
        assert "Old Post" not in titles

    def test_missing_date_skipped(self):
        # Regardless of since, entries with no pubDate/updated must be skipped.
        since = datetime(1970, 1, 1, tzinfo=UTC)
        config = {"source_key": "test_blog", "url": "https://example.com/feed"}
        items = fetch_rss(config, since, fetch=_fetch_from(_read("sample_rss.xml")))
        titles = [i.title for i in items]
        assert "No Date Post" not in titles
        # the other two (recent + old) should both be present with since=epoch
        assert len(items) == 2


class TestFetchArxiv:
    def test_parses_and_filters(self):
        since = datetime(2026, 1, 1, tzinfo=UTC)
        config = {"source_key": "neel_arxiv", "person": "neel_nanda", "category": "researchers", "query": 'au:"Neel Nanda"'}
        items = fetch_arxiv(config, since, fetch=_fetch_from(_read("sample_arxiv.atom")))

        assert len(items) == 1
        item = items[0]
        assert item.title == "Scaling Laws for Something Surprising"
        assert item.url == "http://arxiv.org/abs/2607.12345v1"
        assert item.published == datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)
        assert item.source_type == "arxiv"
        assert item.person == "neel_nanda"

    def test_since_excludes_old_paper(self):
        since = datetime(2026, 1, 1, tzinfo=UTC)
        config = {"source_key": "neel_arxiv", "query": 'au:"Neel Nanda"'}
        items = fetch_arxiv(config, since, fetch=_fetch_from(_read("sample_arxiv.atom")))
        assert all(i.title != "An Old Paper" for i in items)


class TestFetchYoutube:
    def test_parses_channel_feed(self):
        since = datetime(2026, 7, 1, tzinfo=UTC)
        config = {"source_key": "karpathy_youtube", "person": "andrej_karpathy", "category": "researchers", "channel_id": "UCabc"}
        items = fetch_youtube(config, since, fetch=_fetch_from(_read("sample_youtube.xml")))

        assert len(items) == 1
        item = items[0]
        assert item.source_type == "youtube"
        assert item.url == "https://www.youtube.com/watch?v=abc123"
        assert item.published == datetime(2026, 7, 15, 18, 0, 0, tzinfo=UTC)

    def test_since_excludes_old_video(self):
        since = datetime(2026, 7, 1, tzinfo=UTC)
        config = {"source_key": "karpathy_youtube", "channel_id": "UCabc"}
        items = fetch_youtube(config, since, fetch=_fetch_from(_read("sample_youtube.xml")))
        assert all("old video" not in i.title.lower() for i in items)


class TestFetchBluesky:
    def test_title_and_text_truncation(self):
        since = datetime(2026, 7, 1, tzinfo=UTC)
        config = {"source_key": "jane_bluesky", "person": "jane", "category": "thinkers", "handle": "example.bsky.social"}
        items = fetch_bluesky(config, since, fetch=_fetch_from(_read("sample_bluesky.json")))

        assert len(items) == 1
        item = items[0]
        assert len(item.title) == 120
        assert item.text.startswith(item.title)
        assert item.title == item.text[:120]
        assert item.url == "https://bsky.app/profile/example.bsky.social/post/3kexample1"
        assert item.source_type == "bluesky"
        assert item.published == datetime(2026, 7, 17, 9, 30, 0, tzinfo=UTC)

    def test_since_filter_excludes_old_post(self):
        since = datetime(2026, 7, 1, tzinfo=UTC)
        config = {"source_key": "jane_bluesky", "handle": "example.bsky.social"}
        items = fetch_bluesky(config, since, fetch=_fetch_from(_read("sample_bluesky.json")))
        assert len(items) == 1  # the older post is filtered out


class TestFetchHn:
    def test_keyword_filter_and_since(self):
        since = datetime(2026, 6, 1, tzinfo=UTC)
        config = {
            "source_key": "hn_frontier_model",
            "query": "frontier model",
            "roster_names": [],
            "keywords": ["frontier model", "AI policy"],
        }
        items = fetch_hn(config, since, fetch=_fetch_from(_read("sample_hn.json")))

        titles = [i.title for i in items]
        # matches keyword "frontier model"
        assert "New frontier model announced by a major lab" in titles
        # no keyword/roster match -> excluded even though recent
        assert "Unrelated cooking recipe post" not in titles
        # matches keyword but too old (before since) -> excluded
        assert "Old AI policy discussion" not in titles
        # no created_at at all -> excluded
        assert "Post with no date field" not in titles
        assert len(items) == 1
        assert items[0].source_type == "hn"
        assert items[0].category == "discovery"

    def test_roster_name_match(self):
        since = datetime(1970, 1, 1, tzinfo=UTC)
        config = {
            "source_key": "hn_test",
            "query": "anything",
            "roster_names": ["a major lab"],
            "keywords": [],
        }
        items = fetch_hn(config, since, fetch=_fetch_from(_read("sample_hn.json")))
        titles = [i.title for i in items]
        assert "New frontier model announced by a major lab" in titles
        assert "Unrelated cooking recipe post" not in titles


class TestFetchGnews:
    def test_roster_name_filter(self):
        since = datetime(2026, 7, 1, tzinfo=UTC)
        config = {
            "source_key": "gnews_anthropic",
            "query": '"Anthropic"',
            "roster_names": ["Anthropic"],
            "keywords": [],
        }
        items = fetch_gnews(config, since, fetch=_fetch_from(_read("sample_gnews.xml")))

        assert len(items) == 1
        assert items[0].title == "Anthropic ships new frontier model update"
        assert items[0].source_type == "gnews"

    def test_non_matching_item_excluded(self):
        since = datetime(2026, 7, 1, tzinfo=UTC)
        config = {
            "source_key": "gnews_anthropic",
            "query": '"Anthropic"',
            "roster_names": ["Anthropic"],
            "keywords": [],
        }
        items = fetch_gnews(config, since, fetch=_fetch_from(_read("sample_gnews.xml")))
        titles = [i.title for i in items]
        assert "Local weather report for the weekend" not in titles


class TestGather:
    def test_gather_records_gap_and_continues(self):
        roster = {
            "thinkers": {
                "jane": {"name": "Jane Doe", "blog_rss": "https://good.example.com/feed"},
                "bob": {"name": "Bob Roe", "blog_rss": "https://bad.example.com/feed"},
            }
        }
        settings = {"keywords": []}
        discovery = {}
        good_text = _read("sample_rss.xml")

        def flaky_fetch(url: str) -> str:
            if "bad.example.com" in url:
                raise RuntimeError("connection refused")
            return good_text

        since = datetime(2026, 7, 1, tzinfo=UTC)
        items, gaps = gather(roster, (settings, discovery), since, fetch=flaky_fetch)

        assert len(items) == 1  # from the good source only
        assert len(gaps) == 1
        assert isinstance(gaps[0], Gap)
        assert gaps[0].source_key == "bob_blog"
        assert "connection refused" in gaps[0].error

    def test_gather_walks_all_source_types(self):
        roster = {
            "researchers": {
                "neel": {
                    "name": "Neel Nanda",
                    "arxiv_query": 'au:"Neel Nanda"',
                    "bluesky": "example.bsky.social",
                },
            }
        }
        settings = {"keywords": ["frontier model"]}
        discovery = {"hn_enabled": True}

        fixtures = {
            "export.arxiv.org": _read("sample_arxiv.atom"),
            "public.api.bsky.app": _read("sample_bluesky.json"),
            "hn.algolia.com": _read("sample_hn.json"),
        }

        def fetch(url: str) -> str:
            for host, text in fixtures.items():
                if host in url:
                    return text
            raise RuntimeError(f"unexpected url {url}")

        since = datetime(2026, 6, 1, tzinfo=UTC)
        items, gaps = gather(roster, (settings, discovery), since, fetch=fetch)

        assert gaps == []
        source_types = {i.source_type for i in items}
        assert "arxiv" in source_types
        assert "bluesky" in source_types
        assert "hn" in source_types

    def test_gather_applies_since_filter_end_to_end(self):
        roster = {"thinkers": {"jane": {"name": "Jane Doe", "blog_rss": "https://good.example.com/feed"}}}
        settings = {"keywords": []}
        discovery = {}
        since = datetime(2026, 7, 1, tzinfo=UTC)
        items, gaps = gather(roster, (settings, discovery), since, fetch=_fetch_from(_read("sample_rss.xml")))
        assert gaps == []
        assert all(i.published >= since for i in items)
