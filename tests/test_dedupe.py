"""Tests for digest.dedupe."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digest.dedupe import cluster, drop_seen, prune_seen, record_seen
from digest.models import Item


def make_item(
    id_="id1",
    title="Some Title",
    url="https://example.com/a",
    published=None,
    source_key="test_source",
    source_type="rss",
    person=None,
    category="discovery",
    author=None,
    text="",
    fetched_at=None,
):
    now = datetime.now(timezone.utc)
    return Item(
        id=id_,
        source_key=source_key,
        source_type=source_type,
        person=person,
        category=category,
        title=title,
        url=url,
        author=author,
        published=published or now,
        text=text,
        fetched_at=fetched_at or now,
    )


class TestCluster:
    """Tests for cluster()."""

    def test_near_duplicate_titles_cluster(self):
        """Titles with high similarity ratio land in the same cluster."""
        t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
        t1 = datetime(2026, 7, 2, tzinfo=timezone.utc)
        a = make_item(id_="a", title="OpenAI releases new frontier model", url="https://x.com/a", published=t1)
        b = make_item(id_="b", title="OpenAI releases new frontier model!", url="https://y.com/b", published=t0)

        clusters = cluster([a, b])

        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_distinct_titles_dont_cluster(self):
        """Sufficiently different titles land in separate clusters."""
        a = make_item(id_="a", title="OpenAI releases new frontier model", url="https://x.com/a")
        b = make_item(id_="b", title="Tyler Cowen on marginal tax rates", url="https://y.com/b")

        clusters = cluster([a, b])

        assert len(clusters) == 2
        assert [item.id for c in clusters for item in c] == ["a", "b"]

    def test_same_id_clusters_even_with_different_title(self):
        """Same id always clusters together, regardless of title similarity."""
        t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
        t1 = datetime(2026, 7, 2, tzinfo=timezone.utc)
        a = make_item(id_="dup", title="Completely different headline one", published=t1)
        b = make_item(id_="dup", title="Something else entirely, unrelated", published=t0)

        clusters = cluster([a, b])

        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_representative_is_earliest_published(self):
        """Cluster index 0 (the representative) is the earliest-published item."""
        early = datetime(2026, 7, 1, tzinfo=timezone.utc)
        late = datetime(2026, 7, 3, tzinfo=timezone.utc)
        a = make_item(id_="a", title="Anthropic ships Claude Opus update", url="https://x.com/a", published=late)
        b = make_item(id_="b", title="Anthropic ships Claude Opus update", url="https://y.com/b", published=early)

        clusters = cluster([a, b])

        assert len(clusters) == 1
        representative = clusters[0][0]
        also_links = clusters[0][1:]
        assert representative.id == "b"
        assert representative.published == early
        assert [i.id for i in also_links] == ["a"]

    def test_single_item_forms_its_own_cluster(self):
        a = make_item(id_="solo", title="A lone item")
        clusters = cluster([a])
        assert clusters == [[a]]

    def test_empty_input(self):
        assert cluster([]) == []


class TestSeenStore:
    """Tests for drop_seen / record_seen / prune_seen."""

    def test_seen_round_trip(self, tmp_path):
        """drop -> record -> drop again yields empty on the second pass."""
        seen_path = tmp_path / "seen.json"
        a = make_item(id_="a", url="https://x.com/a")
        b = make_item(id_="b", url="https://x.com/b")

        first_pass = drop_seen([a, b], seen_path)
        assert {i.id for i in first_pass} == {"a", "b"}

        record_seen(first_pass, seen_path)

        second_pass = drop_seen([a, b], seen_path)
        assert second_pass == []

    def test_unseen_items_pass_missing_file(self, tmp_path):
        """Missing seen.json means everything is unseen."""
        seen_path = tmp_path / "does_not_exist.json"
        a = make_item(id_="a")
        result = drop_seen([a], seen_path)
        assert result == [a]

    def test_record_seen_writes_first_seen_date(self, tmp_path):
        seen_path = tmp_path / "seen.json"
        a = make_item(id_="a")
        record_seen([a], seen_path)

        with seen_path.open() as f:
            data = json.load(f)

        assert "a" in data
        # ISO date, parseable.
        datetime.fromisoformat(data["a"])

    def test_corrupt_seen_json_tolerated(self, tmp_path):
        """A corrupt seen.json is treated as empty, not fatal."""
        seen_path = tmp_path / "seen.json"
        seen_path.write_text("{not valid json::")

        a = make_item(id_="a")
        result = drop_seen([a], seen_path)
        assert result == [a]

        # record_seen must also tolerate corruption and produce a fresh, valid file.
        record_seen([a], seen_path)
        with seen_path.open() as f:
            data = json.load(f)
        assert "a" in data

    def test_corrupt_seen_json_not_a_dict(self, tmp_path):
        """A JSON file whose top level isn't an object is also tolerated."""
        seen_path = tmp_path / "seen.json"
        seen_path.write_text("[1, 2, 3]")

        a = make_item(id_="a")
        result = drop_seen([a], seen_path)
        assert result == [a]

    def test_prune_removes_only_old_entries(self, tmp_path):
        seen_path = tmp_path / "seen.json"
        old_date = (datetime.now(timezone.utc).date() - timedelta(days=100)).isoformat()
        recent_date = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        seen_path.write_text(json.dumps({"old": old_date, "recent": recent_date}))

        prune_seen(seen_path, days=45)

        with seen_path.open() as f:
            data = json.load(f)

        assert "recent" in data
        assert "old" not in data

    def test_prune_tolerates_corrupt_entries(self, tmp_path):
        seen_path = tmp_path / "seen.json"
        recent_date = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        seen_path.write_text(json.dumps({"good": recent_date, "bad": "not-a-date"}))

        prune_seen(seen_path, days=45)

        with seen_path.open() as f:
            data = json.load(f)

        assert "good" in data
        assert "bad" not in data

    def test_prune_missing_file_does_not_raise(self, tmp_path):
        seen_path = tmp_path / "does_not_exist.json"
        prune_seen(seen_path, days=45)
        # Should have created an (empty) valid file, not raised.
        assert seen_path.exists()
        with seen_path.open() as f:
            data = json.load(f)
        assert data == {}
