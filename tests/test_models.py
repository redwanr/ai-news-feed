"""Tests for digest.models."""

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from digest.models import Item, canonical_url, item_id, load_roster, load_sources


class TestCanonicalUrl:
    """Test canonical URL transformation."""

    def test_lowercase_scheme_and_host(self):
        """Lowercase scheme and host."""
        url = "HTTPS://Example.COM/path"
        result = canonical_url(url)
        assert result.startswith("https://example.com")

    def test_strip_utm_params(self):
        """Strip utm_* query parameters."""
        url = "https://example.com/page?utm_source=foo&utm_campaign=bar&other=value"
        result = canonical_url(url)
        assert "utm_" not in result
        assert "other=value" in result

    def test_strip_tracking_params(self):
        """Strip ref, source, fbclid, gclid parameters."""
        url = "https://example.com/page?ref=old&source=twitter&fbclid=123&gclid=456&keep=yes"
        result = canonical_url(url)
        assert "ref=" not in result
        assert "source=" not in result
        assert "fbclid=" not in result
        assert "gclid=" not in result
        assert "keep=yes" in result

    def test_strip_fragment(self):
        """Strip URL fragment."""
        url = "https://example.com/page?q=v#section"
        result = canonical_url(url)
        assert "#" not in result
        assert "q=v" in result

    def test_strip_trailing_slash(self):
        """Strip trailing slash from path."""
        url1 = "https://example.com/page/"
        url2 = "https://example.com/page"
        assert canonical_url(url1) == canonical_url(url2)

    def test_preserve_root_path(self):
        """Preserve / for root path."""
        url = "https://example.com/"
        result = canonical_url(url)
        assert result == "https://example.com/"

    def test_combined_transformations(self):
        """Multiple transformations together."""
        url = "HTTPS://Example.COM/path/?utm_source=x&ref=y&keep=z#frag"
        result = canonical_url(url)
        assert result == "https://example.com/path?keep=z"


class TestItemId:
    """Test item ID generation."""

    def test_same_canonical_url_same_id(self):
        """Same canonical URL produces same ID."""
        url1 = "https://example.com/page"
        url2 = "https://example.com/page/?utm_source=x#frag"
        assert item_id(url1) == item_id(url2)

    def test_different_canonical_url_different_id(self):
        """Different canonical URLs produce different IDs."""
        url1 = "https://example.com/page1"
        url2 = "https://example.com/page2"
        assert item_id(url1) != item_id(url2)

    def test_id_is_sha1_hex(self):
        """ID is valid SHA1 hex digest."""
        url = "https://example.com/test"
        id_val = item_id(url)
        # SHA1 hex digest is 40 chars
        assert len(id_val) == 40
        assert all(c in "0123456789abcdef" for c in id_val)
        # Verify against manual hash
        expected = hashlib.sha1(canonical_url(url).encode()).hexdigest()
        assert id_val == expected


class TestRosterLoader:
    """Test roster.yaml loading."""

    def test_load_valid_roster(self):
        """Load valid roster with multiple groups."""
        yaml_content = """\
lab_leaders:
  alice: { name: "Alice", gnews: true }
researchers:
  bob: { name: "Bob", arxiv_query: "au:Bob" }
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            roster = load_roster(f.name)

        assert "lab_leaders" in roster
        assert "researchers" in roster
        assert roster["lab_leaders"]["alice"]["name"] == "Alice"
        assert roster["researchers"]["bob"]["name"] == "Bob"
        Path(f.name).unlink()

    def test_skip_verify_entries(self):
        """Skip entries containing [VERIFY]."""
        yaml_content = """\
lab_leaders:
  alice: { name: "Alice", bluesky: "[VERIFY]" }
  bob: { name: "Bob" }
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            roster = load_roster(f.name)

        assert "alice" not in roster["lab_leaders"]
        assert "bob" in roster["lab_leaders"]
        Path(f.name).unlink()

    def test_reject_unknown_group(self):
        """Reject unknown group names."""
        yaml_content = """\
unknown_group:
  alice: { name: "Alice" }
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            with pytest.raises(ValueError, match="Unknown roster group"):
                load_roster(f.name)
            Path(f.name).unlink()

    def test_missing_name_field(self):
        """Reject entries without 'name' field."""
        yaml_content = """\
lab_leaders:
  alice: { gnews: true }
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            with pytest.raises(ValueError, match="Missing 'name' field"):
                load_roster(f.name)
            Path(f.name).unlink()


class TestSourcesLoader:
    """Test sources.yaml loading."""

    def test_load_valid_sources(self):
        """Load valid sources configuration."""
        yaml_content = """\
settings:
  timezone: America/Los_Angeles
  window_hours: 26
  score_threshold: 6
keywords:
  - frontier model
discovery:
  hn_enabled: true
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            settings, discovery = load_sources(f.name)

        assert settings["timezone"] == "America/Los_Angeles"
        assert settings["window_hours"] == 26
        assert settings["score_threshold"] == 6
        assert settings["keywords"] == ["frontier model"]
        assert discovery["hn_enabled"] is True
        Path(f.name).unlink()

    def test_skip_verify_in_settings(self):
        """Skip [VERIFY] values in settings."""
        yaml_content = """\
settings:
  timezone: America/Los_Angeles
  site_url: "[VERIFY after Pages setup]"
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            settings, _ = load_sources(f.name)

        assert "site_url" not in settings or settings.get("site_url") != "[VERIFY after Pages setup]"
        assert settings["timezone"] == "America/Los_Angeles"
        Path(f.name).unlink()

    def test_skip_verify_in_nested_config(self):
        """Skip [VERIFY] in nested discovery configs."""
        yaml_content = """\
discovery:
  lab_blogs:
    blog1: { type: rss, url: "https://example.com/feed" }
    blog2: { type: rss, url: "[VERIFY]" }
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            _, discovery = load_sources(f.name)

        assert "blog1" in discovery["lab_blogs"]
        assert "blog2" not in discovery["lab_blogs"]
        Path(f.name).unlink()


class TestItemDataclass:
    """Test Item dataclass."""

    def test_create_item(self):
        """Create a valid Item."""
        now = datetime.now(timezone.utc)
        item = Item(
            id="abc123",
            source_key="test_source",
            source_type="rss",
            person=None,
            category="discovery",
            title="Test Title",
            url="https://example.com/article",
            author="Author Name",
            published=now,
            text="Sample text content",
            fetched_at=now,
        )
        assert item.id == "abc123"
        assert item.source_type == "rss"
        assert item.title == "Test Title"
