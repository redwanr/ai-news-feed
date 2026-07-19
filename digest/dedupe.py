"""Dedupe, cluster, and seen-store for GenAI Daily Digest.

Cluster representative convention
----------------------------------
`cluster()` returns `list[list[Item]]`. Since `Item` itself is not modified
(no `also_links` field is added to the dataclass), the "representative vs.
also_links" relationship documented in SPEC.md T-03 is expressed purely by
**list position**: within each returned cluster list, items are sorted by
`published` ascending, so `cluster_list[0]` is always the representative
(earliest published) and `cluster_list[1:]` are the "also_links" (other
items judged to be the same story). This keeps the signature exactly as
specified (`cluster(items) -> list[list[Item]]`) with no extra wrapper type.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from digest.models import Item

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.85


def _load_seen(seen_path: str | Path) -> dict[str, str]:
    """Load state/seen.json mapping item id -> first-seen ISO date.

    Missing or corrupt file (bad JSON, or JSON that isn't an object) starts
    from an empty store; a warning is logged in that case.
    """
    path = Path(seen_path)
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("seen store at %s is corrupt or unreadable (%s); starting empty", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("seen store at %s is not a JSON object; starting empty", path)
        return {}
    return data


def _save_seen(seen_path: str | Path, seen: dict[str, str]) -> None:
    path = Path(seen_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(seen, f, indent=2, sort_keys=True)


def drop_seen(items: list[Item], seen_path: str | Path) -> list[Item]:
    """Return only items whose id is not already present in the seen store."""
    seen = _load_seen(seen_path)
    return [item for item in items if item.id not in seen]


def record_seen(items: list[Item], seen_path: str | Path) -> None:
    """Add items to the seen store, keyed by id -> first-seen ISO date.

    Items whose id is already recorded keep their original first-seen date.
    """
    seen = _load_seen(seen_path)
    today = datetime.now(timezone.utc).date().isoformat()
    for item in items:
        if item.id not in seen:
            seen[item.id] = today
    _save_seen(seen_path, seen)


def prune_seen(seen_path: str | Path, days: int = 45) -> None:
    """Drop seen-store entries whose first-seen date is older than `days`."""
    seen = _load_seen(seen_path)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    pruned = {}
    for item_id, first_seen in seen.items():
        try:
            first_seen_date = datetime.fromisoformat(first_seen).date()
        except (ValueError, TypeError):
            # Corrupt individual entry: drop it rather than crash the run.
            logger.warning("dropping unparseable seen entry %r=%r", item_id, first_seen)
            continue
        if first_seen_date >= cutoff:
            pruned[item_id] = first_seen
    _save_seen(seen_path, pruned)


def _titles_match(a: Item, b: Item) -> bool:
    ratio = SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio()
    return ratio > SIMILARITY_THRESHOLD


def cluster(items: list[Item]) -> list[list[Item]]:
    """Group items into clusters of near-duplicates.

    Two items land in the same cluster if they share the same `id`, or if
    their titles (lowercased) have a `difflib.SequenceMatcher` ratio > 0.85
    against any existing member of the cluster. Greedy single pass over
    `items` in input order.

    Each returned cluster is sorted by `published` ascending, so index 0 is
    the representative (earliest published item) and the remainder are the
    "also_links" for that story (see module docstring).
    """
    clusters: list[list[Item]] = []

    for item in items:
        target = None
        for c in clusters:
            for member in c:
                if member.id == item.id or _titles_match(member, item):
                    target = c
                    break
            if target is not None:
                break
        if target is not None:
            target.append(item)
        else:
            clusters.append([item])

    for c in clusters:
        c.sort(key=lambda it: it.published)

    return clusters
