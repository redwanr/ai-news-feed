"""Data models for GenAI Daily Digest."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import yaml


@dataclass
class Item:
    """Represents a single news/content item."""
    id: str                      # sha1 hex of canonical_url
    source_key: str              # key of the feed/handle in config
    source_type: str             # "rss" | "arxiv" | "bluesky" | "hn" | "gnews" | "youtube"
    person: str | None           # roster person key if this source belongs to a person
    category: str | None         # roster group of that person, else "discovery"
    title: str
    url: str                      # canonical URL of the ORIGINAL source
    author: str | None
    published: datetime           # aware UTC
    text: str                     # body/summary excerpt, hard-truncated to 2000 chars
    fetched_at: datetime          # aware UTC


def canonical_url(url: str) -> str:
    """
    Canonicalize URL: lowercase scheme+host, strip fragment, strip utm/ref/tracking params,
    strip trailing slash.
    """
    parsed = urlparse(url)

    # Lowercase scheme and host
    scheme = parsed.scheme.lower()
    netloc = parsed.hostname.lower() if parsed.hostname else ""

    # Parse query string
    params = parse_qs(parsed.query, keep_blank_values=True)

    # Filter out utm_* (prefix match) and exact tracking param names
    filtered_params = {}
    exact_tracking_keys = {"ref", "source", "fbclid", "gclid"}
    for key, values in params.items():
        key_lower = key.lower()
        # Skip if matches utm_* prefix or exact tracking keys
        if key_lower.startswith("utm_") or key_lower in exact_tracking_keys:
            continue
        filtered_params[key] = values

    # Rebuild query string using urlencode to preserve percent-encoding
    query = urlencode(filtered_params, doseq=True)

    # Strip trailing slash from path
    path = parsed.path.rstrip("/") or "/"

    # Rebuild URL without fragment
    result = urlunparse((scheme, netloc, path, parsed.params, query, ""))

    return result


def item_id(url: str) -> str:
    """Compute item id as sha1 hex of canonical URL."""
    canonical = canonical_url(url)
    return hashlib.sha1(canonical.encode()).hexdigest()


class RosterEntry(TypedDict, total=False):
    """Single roster entry with optional fields."""
    name: str
    gnews: bool
    x_handle: str
    bluesky: str
    blog_rss: str
    arxiv_query: str
    youtube_channel_id: str


class Settings(TypedDict, total=False):
    """Global settings from sources.yaml."""
    timezone: str
    window_hours: int
    score_threshold: int
    keep_top: int
    monthly_cap_usd: float
    batch_size: int
    site_title: str
    site_url: str
    llm: dict[str, Any]


def load_roster(path: str | Path) -> dict[str, dict[str, RosterEntry]]:
    """
    Load and validate roster.yaml.
    Returns dict of groups -> person_key -> RosterEntry.
    Removes fields whose values contain '[VERIFY]' substring; keeps person if 'name' remains valid.
    Raises ValueError if unknown group, missing 'name' field, or 'name' contains '[VERIFY]'.
    """
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f) or {}

    valid_groups = {"lab_leaders", "researchers", "economists", "policymakers", "thinkers"}
    roster = {}

    for group_name, group_data in raw.items():
        if group_name not in valid_groups:
            raise ValueError(f"Unknown roster group: {group_name}")

        roster[group_name] = {}
        for person_key, person_data in (group_data or {}).items():
            if not isinstance(person_data, dict):
                continue

            # Remove fields whose values contain [VERIFY]
            cleaned_entry = {}
            for key, value in person_data.items():
                if isinstance(value, str) and "[VERIFY" in value:
                    # Skip this field
                    continue
                cleaned_entry[key] = value

            # Validate required 'name' field exists and is valid
            if "name" not in cleaned_entry:
                raise ValueError(f"Missing or invalid 'name' field in {group_name}.{person_key}")

            roster[group_name][person_key] = cleaned_entry

    return roster


def load_sources(path: str | Path) -> tuple[Settings, dict[str, Any]]:
    """
    Load and validate sources.yaml.
    Returns (settings dict, discovery dict).
    Skips values containing '[VERIFY]' substring.
    """
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f) or {}

    settings = raw.get("settings", {})
    keywords = raw.get("keywords", [])
    discovery = raw.get("discovery", {})

    # Clean out [VERIFY] values from settings
    cleaned_settings = {}
    for key, value in settings.items():
        if isinstance(value, str) and "[VERIFY" in value:
            # Skip this entire key-value pair
            continue
        if isinstance(value, dict):
            # Recursively clean nested dicts (like llm config)
            cleaned_value = {}
            for k, v in value.items():
                if not (isinstance(v, str) and "[VERIFY" in v):
                    cleaned_value[k] = v
            if cleaned_value:  # Only add if not empty
                cleaned_settings[key] = cleaned_value
        else:
            cleaned_settings[key] = value

    # Add keywords to settings
    cleaned_settings["keywords"] = keywords

    # Clean discovery dict recursively
    cleaned_discovery = {}
    for category, items in discovery.items():
        if isinstance(items, dict):
            cleaned_items = {}
            for key, value in items.items():
                if isinstance(value, dict):
                    # For nested dicts (like lab_blogs), check if any critical field is [VERIFY]
                    # If 'url' is [VERIFY], skip the entire entry (it's unusable)
                    if isinstance(value.get("url"), str) and "[VERIFY" in value.get("url", ""):
                        continue
                    # Otherwise, clean each entry by removing [VERIFY] values
                    cleaned_entry = {}
                    for k, v in value.items():
                        if not (isinstance(v, str) and "[VERIFY" in v):
                            cleaned_entry[k] = v
                    if cleaned_entry:  # Only add if not empty after cleaning
                        cleaned_items[key] = cleaned_entry
                elif isinstance(value, str) and "[VERIFY" in value:
                    # Skip entries that are pure [VERIFY] strings
                    continue
                else:
                    cleaned_items[key] = value
            if cleaned_items:
                cleaned_discovery[category] = cleaned_items
        else:
            cleaned_discovery[category] = items

    return cleaned_settings, cleaned_discovery
