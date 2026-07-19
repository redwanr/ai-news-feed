"""End-to-end tests for digest.run -- the pipeline runner.

No real network or LLM is ever invoked: `fetch` and `engines` are injected
fakes over fixtures. All reads/writes go under `tmp_path`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from digest.score import EngineResult
from digest.run import count_unverified, run

UTC = timezone.utc
NOW = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)  # window_hours=26 -> since = 2026-07-17 08:00 UTC


# --------------------------------------------------------------------------
# Config fixtures
# --------------------------------------------------------------------------


def _write_sources_yaml(path: Path) -> None:
    path.write_text(
        dedent(
            """\
            settings:
              timezone: America/Los_Angeles
              window_hours: 26
              score_threshold: 6
              keep_top: 25
              monthly_cap_usd: 5.0
              batch_size: 40
              site_title: "Test Digest"
              site_url: "https://example.com/digest"
              llm:
                primary: claude_p
                claude_model: haiku
                fallback1: gemini
                gemini_model: gemini-3.1-flash-lite-preview
                fallback2: anthropic_api
                anthropic_model: claude-haiku-4-5-20251001
            keywords: []
            discovery:
              hn_enabled: false
              gnews_queries: []
              lab_blogs: {}
              newsletters: {}
            """
        )
    )


def _write_roster_yaml(path: Path) -> None:
    # `unverified_person`'s blog_rss is a [VERIFY] placeholder: load_roster
    # strips it (never fetched), and it should be counted in
    # unverified_skipped.
    path.write_text(
        dedent(
            """\
            lab_leaders:
              test_person: { name: "Test Person", blog_rss: "https://example.com/feed" }
              unverified_person: { name: "Unverified Person", blog_rss: "[VERIFY]" }
            """
        )
    )


def _rss(items: list[tuple[str, str, str]]) -> str:
    """Build a minimal RSS 2.0 document. items = (title, link, rfc822_pubdate)."""
    entries = "\n".join(
        f"""  <item>
    <title>{title}</title>
    <link>{link}</link>
    <pubDate>{pubdate}</pubDate>
    <description>Body text for {title}.</description>
  </item>"""
        for title, link, pubdate in items
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Sample Blog</title>
  <link>https://example.com</link>
{entries}
</channel>
</rss>
"""


ONE_ITEM_RSS = _rss(
    [("Recent Post About Frontier Models", "https://example.com/posts/recent", "Fri, 17 Jul 2026 10:00:00 GMT")]
)


def make_fetch(url_to_text: dict[str, str]):
    def _fetch(url: str) -> str:
        if url in url_to_text:
            return url_to_text[url]
        raise RuntimeError(f"unexpected fetch url in test: {url}")

    return _fetch


def make_scoring_engine(score: int = 9):
    """An injected claude_p-style engine: scores every item id found in the
    prompt (via the `id=<sha1hex>` marker digest.score.items_block emits)."""

    def _engine(prompt: str, schema: dict) -> EngineResult:
        ids = re.findall(r"id=([0-9a-f]{40})", prompt)
        return EngineResult(
            success=True,
            structured_output={
                "items": [
                    {"id": i, "score": score, "summary": "Auto summary.", "why_matters": "Because test."}
                    for i in ids
                ]
            },
        )

    return _engine


def _setup_config(tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    roster_path = config_dir / "roster.yaml"
    sources_path = config_dir / "sources.yaml"
    _write_roster_yaml(roster_path)
    _write_sources_yaml(sources_path)
    return roster_path, sources_path


# --------------------------------------------------------------------------
# End-to-end: happy path
# --------------------------------------------------------------------------


def test_end_to_end_renders_docs_and_summary(tmp_path):
    roster_path, sources_path = _setup_config(tmp_path)
    state_dir = tmp_path / "state"
    docs_dir = tmp_path / "docs"

    summary = run(
        roster_path=roster_path,
        sources_path=sources_path,
        state_dir=state_dir,
        docs_dir=docs_dir,
        fetch=make_fetch({"https://example.com/feed": ONE_ITEM_RSS}),
        engines={"claude_p": make_scoring_engine(9)},
        now=NOW,
    )

    # docs files exist
    assert (docs_dir / "index.html").exists()
    assert (docs_dir / "digest.md").exists()
    assert (docs_dir / "feed.xml").exists()

    html = (docs_dir / "index.html").read_text()
    assert "Recent Post About Frontier Models" in html
    assert 'href="https://example.com/posts/recent"' in html

    # summary counts
    assert summary["fetched"] == 1
    assert summary["unverified_skipped"] == 1  # unverified_person.blog_rss
    assert summary["after_seen"] == 1
    assert summary["clusters"] == 1
    assert summary["scored_kept"] == 1
    assert summary["engine"] == "claude_p"
    assert summary["est_cost_usd"] == 0.0
    assert summary["gaps"] == []
    assert summary["date"] == "2026-07-18"
    assert summary["duration_s"] >= 0

    # state/run_summary.json matches the returned summary
    written = json.loads((state_dir / "run_summary.json").read_text())
    assert written == summary

    # seen store recorded the item (non-dry-run)
    seen = json.loads((state_dir / "seen.json").read_text())
    assert len(seen) == 1

    # JSON-lines log written, one object per line, run_summary event present
    log_path = state_dir / "logs" / "run-20260718.jsonl"
    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert lines, "expected at least one JSON-lines log record"
    for rec in lines:
        assert "ts" in rec and "level" in rec and "event" in rec
    summary_events = [rec for rec in lines if rec["event"] == "run_summary"]
    assert len(summary_events) == 1
    assert summary_events[0]["fetched"] == 1
    assert summary_events[0]["scored_kept"] == 1


# --------------------------------------------------------------------------
# Idempotency: second run same day keeps nothing new
# --------------------------------------------------------------------------


def test_second_run_same_day_is_idempotent(tmp_path):
    roster_path, sources_path = _setup_config(tmp_path)
    state_dir = tmp_path / "state"
    docs_dir = tmp_path / "docs"
    fetch = make_fetch({"https://example.com/feed": ONE_ITEM_RSS})

    first = run(
        roster_path=roster_path,
        sources_path=sources_path,
        state_dir=state_dir,
        docs_dir=docs_dir,
        fetch=fetch,
        engines={"claude_p": make_scoring_engine(9)},
        now=NOW,
    )
    assert first["after_seen"] == 1
    assert first["scored_kept"] == 1

    second = run(
        roster_path=roster_path,
        sources_path=sources_path,
        state_dir=state_dir,
        docs_dir=docs_dir,
        fetch=fetch,
        engines={"claude_p": make_scoring_engine(9)},
        now=NOW,
    )

    # Same item fetched again (fetch itself is not seen-aware) but dropped by
    # the seen store before clustering/scoring -- nothing re-kept.
    assert second["fetched"] == 1
    assert second["after_seen"] == 0
    assert second["clusters"] == 0
    assert second["scored_kept"] == 0

    # seen store still has exactly the one entry (not duplicated)
    seen = json.loads((state_dir / "seen.json").read_text())
    assert len(seen) == 1

    # Empty-digest guard: the second (empty) run must NOT blank the already-
    # published, same-day digest -- docs still shows the first run's item.
    assert second["skipped_empty_republish"] is True
    html = (docs_dir / "index.html").read_text()
    assert "Recent Post About Frontier Models" in html
    assert "Nothing noteworthy today" not in html


def test_quiet_first_run_of_day_still_renders_empty_page(tmp_path):
    """The guard must only protect a SAME-DAY digest. With no prior published
    page, an empty run renders the valid 'nothing noteworthy' page."""
    roster_path, sources_path = _setup_config(tmp_path)
    state_dir = tmp_path / "state"
    docs_dir = tmp_path / "docs"

    # Fetch returns an RSS doc with no items -> nothing scored.
    empty_rss = _rss([])
    summary = run(
        roster_path=roster_path,
        sources_path=sources_path,
        state_dir=state_dir,
        docs_dir=docs_dir,
        fetch=make_fetch({"https://example.com/feed": empty_rss}),
        engines={"claude_p": make_scoring_engine(9)},
        now=NOW,
    )

    assert summary["scored_kept"] == 0
    assert summary["skipped_empty_republish"] is False
    html = (docs_dir / "index.html").read_text()
    assert "Nothing noteworthy today" in html


def test_guard_does_not_trip_when_published_digest_is_a_prior_day(tmp_path):
    """A non-empty digest from YESTERDAY must not freeze today's empty run --
    today should render its own (empty) page since the dates differ."""
    from datetime import timedelta

    roster_path, sources_path = _setup_config(tmp_path)
    state_dir = tmp_path / "state"
    docs_dir = tmp_path / "docs"

    # Day 1: a populated digest gets published.
    run(
        roster_path=roster_path,
        sources_path=sources_path,
        state_dir=state_dir,
        docs_dir=docs_dir,
        fetch=make_fetch({"https://example.com/feed": ONE_ITEM_RSS}),
        engines={"claude_p": make_scoring_engine(9)},
        now=NOW,
    )

    # Day 2: nothing new (all seen); different date -> guard must NOT trip.
    next_day = NOW + timedelta(days=1)
    second = run(
        roster_path=roster_path,
        sources_path=sources_path,
        state_dir=state_dir,
        docs_dir=docs_dir,
        fetch=make_fetch({"https://example.com/feed": ONE_ITEM_RSS}),
        engines={"claude_p": make_scoring_engine(9)},
        now=next_day,
    )
    assert second["scored_kept"] == 0
    assert second["skipped_empty_republish"] is False
    html = (docs_dir / "index.html").read_text()
    assert "Nothing noteworthy today" in html


# --------------------------------------------------------------------------
# Dry run: no seen-store writes, renders to state/preview
# --------------------------------------------------------------------------


def test_dry_run_leaves_seen_store_untouched_and_writes_preview(tmp_path):
    roster_path, sources_path = _setup_config(tmp_path)
    state_dir = tmp_path / "state"
    docs_dir = tmp_path / "docs"

    summary = run(
        roster_path=roster_path,
        sources_path=sources_path,
        state_dir=state_dir,
        docs_dir=docs_dir,
        dry_run=True,
        fetch=make_fetch({"https://example.com/feed": ONE_ITEM_RSS}),
        engines={"claude_p": make_scoring_engine(9)},
        now=NOW,
    )

    assert summary["scored_kept"] == 1

    # no seen-store writes at all
    assert not (state_dir / "seen.json").exists()

    # rendered to state/preview, not docs_dir
    assert (state_dir / "preview" / "index.html").exists()
    assert (state_dir / "preview" / "digest.md").exists()
    assert (state_dir / "preview" / "feed.xml").exists()
    assert not (docs_dir / "index.html").exists()

    # A follow-up real (non-dry) run on the same day still sees the item as
    # unseen, proving dry-run truly skipped record_seen.
    second = run(
        roster_path=roster_path,
        sources_path=sources_path,
        state_dir=state_dir,
        docs_dir=docs_dir,
        fetch=make_fetch({"https://example.com/feed": ONE_ITEM_RSS}),
        engines={"claude_p": make_scoring_engine(9)},
        now=NOW,
    )
    assert second["after_seen"] == 1
    assert second["scored_kept"] == 1


# --------------------------------------------------------------------------
# also_links bridging: cluster's non-representative members show up as
# "also:" links on the rendered representative item.
# --------------------------------------------------------------------------


def test_also_links_bridged_from_cluster_to_rendered_output(tmp_path):
    roster_path, sources_path = _setup_config(tmp_path)
    state_dir = tmp_path / "state"
    docs_dir = tmp_path / "docs"

    two_item_rss = _rss(
        [
            ("OpenAI Ships New Model Today", "https://example.com/posts/first", "Fri, 17 Jul 2026 09:00:00 GMT"),
            ("OpenAI Ships New Model Today!", "https://example.com/posts/second", "Fri, 17 Jul 2026 11:00:00 GMT"),
        ]
    )

    summary = run(
        roster_path=roster_path,
        sources_path=sources_path,
        state_dir=state_dir,
        docs_dir=docs_dir,
        fetch=make_fetch({"https://example.com/feed": two_item_rss}),
        engines={"claude_p": make_scoring_engine(9)},
        now=NOW,
    )

    # Both items collapse into a single cluster (near-duplicate titles).
    assert summary["fetched"] == 2
    assert summary["after_seen"] == 2
    assert summary["clusters"] == 1
    assert summary["scored_kept"] == 1

    html = (docs_dir / "index.html").read_text()
    # Representative (earliest, 09:00) is the main item.
    assert "OpenAI Ships New Model Today" in html
    assert 'href="https://example.com/posts/first"' in html
    # Also-link (the 11:00 duplicate) appears as an "also:" link.
    assert "also:" in html
    assert 'href="https://example.com/posts/second"' in html


# --------------------------------------------------------------------------
# unverified_skipped derivation (unit-level)
# --------------------------------------------------------------------------


def test_count_unverified_counts_verify_fields(tmp_path):
    roster_path = tmp_path / "roster.yaml"
    sources_path = tmp_path / "sources.yaml"
    roster_path.write_text(
        dedent(
            """\
            lab_leaders:
              a: { name: "A", blog_rss: "https://a.example/feed" }
              b: { name: "B", blog_rss: "[VERIFY]", bluesky: "[VERIFY: handle]" }
            researchers:
              c: { name: "C", arxiv_query: 'au:"C"' }
            """
        )
    )
    sources_path.write_text(
        dedent(
            """\
            settings: {}
            discovery:
              lab_blogs:
                x: { type: rss, url: "https://x.example/feed" }
                y: { type: rss, url: "[VERIFY]" }
            """
        )
    )
    # b.blog_rss + b.bluesky + lab_blogs.y = 3
    assert count_unverified(roster_path, sources_path) == 3


def test_count_unverified_missing_files_is_zero(tmp_path):
    assert count_unverified(tmp_path / "no-roster.yaml", tmp_path / "no-sources.yaml") == 0


# --------------------------------------------------------------------------
# Graceful degradation: per-source fetch errors surface as gaps, run exits
# (returns) normally rather than raising.
# --------------------------------------------------------------------------


def test_source_fetch_failure_becomes_a_gap_not_a_crash(tmp_path):
    roster_path, sources_path = _setup_config(tmp_path)
    state_dir = tmp_path / "state"
    docs_dir = tmp_path / "docs"

    def failing_fetch(url: str) -> str:
        raise RuntimeError("connection refused")

    summary = run(
        roster_path=roster_path,
        sources_path=sources_path,
        state_dir=state_dir,
        docs_dir=docs_dir,
        fetch=failing_fetch,
        engines={"claude_p": make_scoring_engine(9)},
        now=NOW,
    )

    assert summary["fetched"] == 0
    assert len(summary["gaps"]) == 1
    assert summary["gaps"][0]["source_key"] == "test_person_blog"
    assert "connection refused" in summary["gaps"][0]["error"]

    # gap register appears in the rendered footer too
    html = (docs_dir / "index.html").read_text()
    assert "test_person_blog" in html
