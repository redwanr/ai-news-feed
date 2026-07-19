"""Tests for digest.render.

Scored items and gaps are NOT imported from digest.score / digest.fetch
(T-04 / T-02 own those shapes and aren't necessarily merged yet). Instead we
build lightweight local stand-ins that expose the attributes render_all
reads defensively: .url, .title, .summary, .why_matters, .person, .category,
.source_key, .published, .also_links, .score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import pytest

from digest.render import render_all

PT = ZoneInfo("America/Los_Angeles")


@dataclass
class FakeAlsoLink:
    title: str
    url: str


@dataclass
class FakeScoredItem:
    url: str
    title: str
    person: str | None
    category: str | None
    source_key: str
    published: datetime
    summary: str = "A thing happened."
    why_matters: str = "It matters because reasons."
    score: int = 8
    noteworthy: bool = True
    also_links: list = field(default_factory=list)


@dataclass
class FakeGap:
    source_key: str
    error: str


def _dt(hour: int, day: int = 18) -> datetime:
    return datetime(2026, 7, day, hour, 0, tzinfo=timezone.utc)


def make_item(**kwargs) -> FakeScoredItem:
    defaults = dict(
        url="https://example.com/a",
        title="Example Title",
        person="demis_hassabis",
        category="lab_leaders",
        source_key="demis_blog",
        published=_dt(18),
    )
    defaults.update(kwargs)
    return FakeScoredItem(**defaults)


SETTINGS = {"site_title": "GenAI Signal", "site_url": "https://example.github.io/ai-news-feed"}
RUN_META = {
    "date": "2026-07-18",
    "fetched": 42,
    "after_seen": 30,
    "clusters": 20,
    "scored_kept": 3,
    "engine": "claude_p",
    "est_cost_usd": 0.0,
}


def test_hrefs_match_item_url(tmp_path: Path):
    items = [
        make_item(url="https://a.example.com/one", title="One"),
        make_item(url="https://b.example.com/two", title="Two", category="researchers"),
    ]
    render_all(items, [], RUN_META, SETTINGS, out_dir=tmp_path)

    html_text = (tmp_path / "index.html").read_text()
    hrefs = re.findall(r'<h3><a href="([^"]+)">', html_text)
    assert set(hrefs) == {it.url for it in items}

    md_text = (tmp_path / "digest.md").read_text()
    for it in items:
        assert f"]({it.url})" in md_text


def test_section_order_and_omission(tmp_path: Path):
    items = [
        make_item(category="discovery", title="Discovery Item", source_key="hn"),
        make_item(category="lab_leaders", title="Lab Item"),
        make_item(category="thinkers", title="Thinker Item", person="zvi_mowshowitz"),
    ]
    render_all(items, [], RUN_META, SETTINGS, out_dir=tmp_path)
    html_text = (tmp_path / "index.html").read_text()

    # Present sections appear in fixed order: Lab Leaders, Thinkers, Discovery
    pos_lab = html_text.index("Lab Leaders")
    pos_thinkers = html_text.index("Thinkers")
    pos_discovery = html_text.index("Discovery")
    assert pos_lab < pos_thinkers < pos_discovery

    # Omitted sections never appear
    for absent in ["Researchers", "Economists", "Policymakers"]:
        assert absent not in html_text


def test_feed_well_formed_one_entry_right_date(tmp_path: Path):
    items = [make_item(published=_dt(10)), make_item(published=_dt(20), url="https://example.com/b")]
    render_all(items, [], RUN_META, SETTINGS, out_dir=tmp_path)

    feed_text = (tmp_path / "feed.xml").read_text()
    parsed = feedparser.parse(feed_text)
    assert not parsed.bozo, f"feed not well-formed: {parsed.get('bozo_exception')}"
    assert len(parsed.entries) == 1
    entry = parsed.entries[0]
    assert entry.id.endswith("#2026-07-18")


def test_feed_multiple_days_get_multiple_entries(tmp_path: Path):
    items = [
        make_item(published=_dt(10, day=18), url="https://example.com/a"),
        make_item(published=_dt(10, day=19), url="https://example.com/b"),
    ]
    render_all(items, [], RUN_META, SETTINGS, out_dir=tmp_path)
    feed_text = (tmp_path / "feed.xml").read_text()
    parsed = feedparser.parse(feed_text)
    assert not parsed.bozo
    assert len(parsed.entries) == 2
    ids = {e.id for e in parsed.entries}
    assert any(i.endswith("#2026-07-18") for i in ids)
    assert any(i.endswith("#2026-07-19") for i in ids)


def test_dark_mode_media_query_present(tmp_path: Path):
    render_all([make_item()], [], RUN_META, SETTINGS, out_dir=tmp_path)
    html_text = (tmp_path / "index.html").read_text()
    assert "prefers-color-scheme: dark" in html_text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html_text


def test_gap_register_in_footer(tmp_path: Path):
    gaps = [FakeGap(source_key="deepmind_blog", error="timeout after 15s")]
    render_all([make_item()], gaps, RUN_META, SETTINGS, out_dir=tmp_path)

    html_text = (tmp_path / "index.html").read_text()
    assert "deepmind_blog: timeout after 15s" in html_text

    md_text = (tmp_path / "digest.md").read_text()
    assert "deepmind_blog: timeout after 15s" in md_text


def test_empty_digest_renders_valid_nothing_noteworthy_page(tmp_path: Path):
    render_all([], [], RUN_META, SETTINGS, out_dir=tmp_path)

    html_text = (tmp_path / "index.html").read_text()
    assert "Nothing noteworthy" in html_text
    assert "0 item" in html_text
    # still a valid, complete page
    assert "<!doctype html>" in html_text.lower()
    assert "</html>" in html_text

    md_text = (tmp_path / "digest.md").read_text()
    assert "Nothing noteworthy" in md_text

    feed_text = (tmp_path / "feed.xml").read_text()
    parsed = feedparser.parse(feed_text)
    assert not parsed.bozo
    assert len(parsed.entries) == 0


def test_also_links_rendered(tmp_path: Path):
    item = make_item(
        also_links=[FakeAlsoLink(title="Dupe report", url="https://dup.example.com/x")]
    )
    render_all([item], [], RUN_META, SETTINGS, out_dir=tmp_path)
    html_text = (tmp_path / "index.html").read_text()
    assert "https://dup.example.com/x" in html_text
    assert "also:" in html_text


def test_byline_and_why_matters_present(tmp_path: Path):
    item = make_item(person="dario_amodei", why_matters="Frontier lab strategy shift.")
    render_all([item], [], RUN_META, SETTINGS, out_dir=tmp_path)
    html_text = (tmp_path / "index.html").read_text()
    assert "Dario Amodei" in html_text
    assert "PT" in html_text
    assert "Frontier lab strategy shift." in html_text
    assert "<em>" in html_text


def test_does_not_write_to_real_docs(tmp_path: Path):
    # Sanity: caller-provided out_dir is honored, nothing leaks to repo docs/.
    render_all([make_item()], [], RUN_META, SETTINGS, out_dir=tmp_path)
    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "digest.md").exists()
    assert (tmp_path / "feed.xml").exists()
