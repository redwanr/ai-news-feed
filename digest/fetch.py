"""Fetch adapters for GenAI Daily Digest.

One function per source type (rss, arxiv, bluesky, hn, gnews, youtube), a
top-level `gather()` that walks roster + sources config and collects items
(recording per-source failures as Gap records instead of raising), and a
`--check` CLI that hits every configured source live and reports OK/FAIL.
"""

import argparse
import calendar
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

import feedparser
import requests

from digest.models import Item, canonical_url, item_id, load_roster, load_sources

logger = logging.getLogger(__name__)

ConfigEntry = dict[str, Any]
Fetch = Callable[[str], str]


@dataclass
class Gap:
    """Records a per-source failure so the run can continue."""
    source_key: str
    error: str


def default_fetch(url: str) -> str:
    """Default fetch implementation: requests.get with 15s timeout and a UA header."""
    resp = requests.get(
        url, timeout=15, headers={"User-Agent": "genai-digest/1.0"}
    )
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _matches(text: str, roster_names: list[str], keywords: list[str]) -> bool:
    """Case-insensitive substring match against roster names or keywords."""
    lowered = text.lower()
    for name in roster_names or []:
        if name and name.lower() in lowered:
            return True
    for kw in keywords or []:
        if kw and kw.lower() in lowered:
            return True
    return False


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "x"


def _entry_datetime(entry: Any) -> datetime | None:
    """feedparser date rule (landmine #9): published_parsed or updated_parsed;
    missing -> None (caller skips + logs debug)."""
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)


def _entry_to_item(entry: Any, source_type: str, config_entry: ConfigEntry, fetched_at: datetime) -> Item | None:
    dt = _entry_datetime(entry)
    if dt is None:
        logger.debug("skipping entry with no date: %s", entry.get("link") or entry.get("title"))
        return None
    link = entry.get("link", "")
    if not link:
        logger.debug("skipping entry with no link: %s", entry.get("title"))
        return None
    title = entry.get("title", "") or ""
    summary = entry.get("summary", "") or entry.get("description", "") or ""
    author = entry.get("author") or None
    return Item(
        id=item_id(link),
        source_key=config_entry["source_key"],
        source_type=source_type,
        person=config_entry.get("person"),
        category=config_entry.get("category"),
        title=title,
        url=canonical_url(link),
        author=author,
        published=dt,
        text=summary[:2000],
        fetched_at=fetched_at,
    )


def _parse_feed(text: str, source_type: str, config_entry: ConfigEntry, since: datetime) -> list[Item]:
    fetched_at = datetime.now(timezone.utc)
    items: list[Item] = []
    for entry in feedparser.parse(text).entries:
        item = _entry_to_item(entry, source_type, config_entry, fetched_at)
        if item is None or item.published < since:
            continue
        items.append(item)
    return items


# --------------------------------------------------------------------------
# Per-source-type adapters
# --------------------------------------------------------------------------

def fetch_rss(config_entry: ConfigEntry, since: datetime, fetch: Fetch = default_fetch) -> list[Item]:
    url = config_entry["url"]
    text = fetch(url)
    return _parse_feed(text, "rss", config_entry, since)


def fetch_youtube(config_entry: ConfigEntry, since: datetime, fetch: Fetch = default_fetch) -> list[Item]:
    """YouTube channel feed = RSS under the hood (landmine #12)."""
    channel_id = config_entry["channel_id"]
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={quote(channel_id)}"
    text = fetch(url)
    return _parse_feed(text, "youtube", config_entry, since)


def fetch_arxiv(config_entry: ConfigEntry, since: datetime, fetch: Fetch = default_fetch) -> list[Item]:
    """arXiv API (landmine #6): Atom response, parsed with feedparser."""
    query = config_entry["query"]
    max_results = config_entry.get("max_results", 20)
    url = (
        "http://export.arxiv.org/api/query?"
        f"search_query={quote(query)}&start=0&max_results={max_results}"
    )
    text = fetch(url)
    return _parse_feed(text, "arxiv", config_entry, since)


def fetch_gnews(config_entry: ConfigEntry, since: datetime, fetch: Fetch = default_fetch) -> list[Item]:
    """Google News RSS (landmine #10), filtered to roster names / keywords."""
    query = config_entry["query"]
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    text = fetch(url)
    roster_names = config_entry.get("roster_names", [])
    keywords = config_entry.get("keywords", [])
    items = []
    for item in _parse_feed(text, "gnews", config_entry, since):
        if _matches(f"{item.title} {item.text}", roster_names, keywords):
            items.append(item)
    return items


def _hn_datetime(hit: dict) -> datetime | None:
    ts = hit.get("created_at_i")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    created_at = hit.get("created_at")
    if created_at:
        try:
            return datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def fetch_hn(config_entry: ConfigEntry, since: datetime, fetch: Fetch = default_fetch) -> list[Item]:
    """HN Algolia search (landmine #8), filtered to roster names / keywords."""
    query = config_entry["query"]
    since_ts = int(since.timestamp())
    numeric_filter = quote(f"created_at_i>{since_ts}", safe="")
    url = (
        "https://hn.algolia.com/api/v1/search_by_date?"
        f"query={quote(query)}&tags=story&numericFilters={numeric_filter}"
    )
    text = fetch(url)
    fetched_at = datetime.now(timezone.utc)
    data = json.loads(text)
    roster_names = config_entry.get("roster_names", [])
    keywords = config_entry.get("keywords", [])
    items: list[Item] = []
    for hit in data.get("hits", []):
        dt = _hn_datetime(hit)
        if dt is None:
            logger.debug("skipping hn hit with no date: %s", hit.get("objectID"))
            continue
        if dt < since:
            continue
        title = hit.get("title") or hit.get("story_title") or ""
        body = hit.get("story_text") or hit.get("comment_text") or ""
        if not _matches(f"{title} {body}", roster_names, keywords):
            continue
        link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        items.append(Item(
            id=item_id(link),
            source_key=config_entry["source_key"],
            source_type="hn",
            person=config_entry.get("person"),
            category=config_entry.get("category", "discovery"),
            title=title,
            url=canonical_url(link),
            author=hit.get("author"),
            published=dt,
            text=body[:2000],
            fetched_at=fetched_at,
        ))
    return items


def fetch_bluesky(config_entry: ConfigEntry, since: datetime, fetch: Fetch = default_fetch) -> list[Item]:
    """Bluesky author feed, no auth needed (landmine #7)."""
    handle = config_entry["handle"]
    url = (
        "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?"
        f"actor={quote(handle)}&filter=posts_no_replies&limit=50"
    )
    text = fetch(url)
    fetched_at = datetime.now(timezone.utc)
    data = json.loads(text)
    items: list[Item] = []
    for entry in data.get("feed", []):
        post = entry.get("post", {})
        record = post.get("record", {})
        body = record.get("text", "") or ""
        created_at = record.get("createdAt")
        if not created_at:
            logger.debug("skipping bluesky post with no createdAt: %s", post.get("uri"))
            continue
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            logger.debug("skipping bluesky post with unparseable createdAt: %s", created_at)
            continue
        if dt < since:
            continue
        uri = post.get("uri", "")
        rkey = uri.rsplit("/", 1)[-1] if uri else ""
        link = f"https://bsky.app/profile/{handle}/post/{rkey}"
        author = (post.get("author") or {}).get("handle") or handle
        items.append(Item(
            id=item_id(link),
            source_key=config_entry["source_key"],
            source_type="bluesky",
            person=config_entry.get("person"),
            category=config_entry.get("category"),
            title=body[:120],
            url=canonical_url(link),
            author=author,
            published=dt,
            text=body[:2000],
            fetched_at=fetched_at,
        ))
    return items


FETCH_FUNCS: dict[str, Callable[[ConfigEntry, datetime, Fetch], list[Item]]] = {
    "rss": fetch_rss,
    "arxiv": fetch_arxiv,
    "bluesky": fetch_bluesky,
    "hn": fetch_hn,
    "gnews": fetch_gnews,
    "youtube": fetch_youtube,
}


# --------------------------------------------------------------------------
# Source enumeration (shared by gather() and check_sources())
# --------------------------------------------------------------------------

def _all_roster_names(roster: dict) -> list[str]:
    names = []
    for group in roster.values():
        for entry in group.values():
            name = entry.get("name")
            if name:
                names.append(name)
    return names


def _build_sources(roster: dict, settings: dict, discovery: dict) -> list[tuple[str, str, ConfigEntry]]:
    """Returns (source_key, source_type, config_entry) triples for every
    configured source: per-person roster sources, then discovery sources."""
    keywords = settings.get("keywords", [])
    roster_names = _all_roster_names(roster)
    sources: list[tuple[str, str, ConfigEntry]] = []

    for group, persons in roster.items():
        for person_key, entry in persons.items():
            if "blog_rss" in entry:
                sources.append((f"{person_key}_blog", "rss", {
                    "url": entry["blog_rss"], "person": person_key, "category": group,
                }))
            if "bluesky" in entry:
                sources.append((f"{person_key}_bluesky", "bluesky", {
                    "handle": entry["bluesky"], "person": person_key, "category": group,
                }))
            if "arxiv_query" in entry:
                sources.append((f"{person_key}_arxiv", "arxiv", {
                    "query": entry["arxiv_query"], "person": person_key, "category": group,
                }))
            if "youtube_channel_id" in entry:
                sources.append((f"{person_key}_youtube", "youtube", {
                    "channel_id": entry["youtube_channel_id"], "person": person_key, "category": group,
                }))
            if entry.get("gnews"):
                sources.append((f"{person_key}_gnews", "gnews", {
                    "query": f'"{entry["name"]}"', "person": person_key, "category": group,
                    "roster_names": roster_names, "keywords": keywords,
                }))
            # x_handle: informational only in v1 (spec §4.2) -- no adapter.

    if discovery.get("hn_enabled"):
        for kw in keywords:
            sources.append((f"hn_{_slug(kw)}", "hn", {
                "query": kw, "person": None, "category": "discovery",
                "roster_names": roster_names, "keywords": keywords,
            }))

    for q in discovery.get("gnews_queries", []):
        sources.append((f"gnews_{_slug(q)}", "gnews", {
            "query": q, "person": None, "category": "discovery",
            "roster_names": roster_names, "keywords": keywords,
        }))

    for key, entry in discovery.get("lab_blogs", {}).items():
        sources.append((key, "rss", {"url": entry["url"], "person": None, "category": "discovery"}))

    for key, entry in discovery.get("newsletters", {}).items():
        sources.append((key, "rss", {"url": entry["url"], "person": None, "category": "discovery"}))

    return sources


# --------------------------------------------------------------------------
# gather() and check_sources()
# --------------------------------------------------------------------------

def gather(
    roster: dict,
    sources: tuple[dict, dict],
    since: datetime,
    fetch: Fetch = default_fetch,
) -> tuple[list[Item], list[Gap]]:
    """Walk every configured source, collect items published since `since`,
    and record per-source failures as Gap entries instead of raising."""
    settings, discovery = sources
    configs = _build_sources(roster, settings, discovery)
    items: list[Item] = []
    gaps: list[Gap] = []
    last_arxiv_call: float | None = None

    for source_key, source_type, config_entry in configs:
        entry = {**config_entry, "source_key": source_key}
        try:
            if source_type == "arxiv":
                # landmine #6: max 1 request per 3 seconds to arXiv API.
                if last_arxiv_call is not None:
                    elapsed = time.monotonic() - last_arxiv_call
                    if elapsed < 3:
                        time.sleep(3 - elapsed)
                last_arxiv_call = time.monotonic()
            fn = FETCH_FUNCS[source_type]
            items.extend(fn(entry, since, fetch))
        except Exception as e:  # noqa: BLE001 - per-source isolation is the point
            logger.warning("source %s failed: %s", source_key, e)
            gaps.append(Gap(source_key, str(e)))

    return items, gaps


def check_sources(
    roster: dict,
    sources: tuple[dict, dict],
    fetch: Fetch = default_fetch,
) -> list[tuple[str, bool, str]]:
    """Hit every configured source live (or via injected fetch) and report
    OK/FAIL per source_key. Report tool, not a gate -- never raises."""
    settings, discovery = sources
    configs = _build_sources(roster, settings, discovery)
    since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    results: list[tuple[str, bool, str]] = []

    for source_key, source_type, config_entry in configs:
        entry = {**config_entry, "source_key": source_key}
        try:
            FETCH_FUNCS[source_type](entry, since, fetch)
            results.append((source_key, True, "OK"))
        except Exception as e:  # noqa: BLE001 - report every source independently
            results.append((source_key, False, str(e)))

    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m digest.fetch")
    parser.add_argument("--check", action="store_true", help="hit every configured source live and report OK/FAIL")
    parser.add_argument("--roster", default="config/roster.yaml")
    parser.add_argument("--sources", default="config/sources.yaml")
    args = parser.parse_args(argv)

    if args.check:
        roster = load_roster(args.roster)
        sources = load_sources(args.sources)
        for source_key, ok, detail in check_sources(roster, sources):
            status = "OK" if ok else "FAIL"
            print(f"{status} {source_key} {detail}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
