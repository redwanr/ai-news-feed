"""Pipeline runner for the GenAI Daily Digest.

`python -m digest.run [--date YYYY-MM-DD] [--dry-run]`

Wires together: load config -> fetch.gather -> dedupe.drop_seen -> cluster ->
score_items -> render_all -> record_seen + prune_seen -> write
state/run_summary.json (and log the same object as JSON-lines).

`run()` accepts injected `fetch` and `engines` so tests never touch the real
network or a real LLM; `main()` wires the real defaults for the CLI.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import sys
import time
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from digest.dedupe import cluster, drop_seen, prune_seen, record_seen
from digest.fetch import Fetch, default_fetch, gather
from digest.models import load_roster, load_sources
from digest.render import render_all
from digest.score import EngineFn, score_items

logger = logging.getLogger(__name__)

# Roster fields that name an actual fetchable source (x_handle is
# informational-only per SPEC §4.2 -- no adapter fetches it, so a [VERIFY]
# x_handle is not counted as a "skipped source").
_FETCHABLE_ROSTER_FIELDS = ("blog_rss", "bluesky", "arxiv_query", "youtube_channel_id")


# --------------------------------------------------------------------------
# JSON-lines logging (ground rules §0)
# --------------------------------------------------------------------------


class JsonLinesHandler(logging.Handler):
    """Writes one JSON object per line: ts, level, event, plus free fields."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "level": record.levelname,
                "event": record.getMessage(),
            }
            fields = record.__dict__.get("fields")
            if isinstance(fields, dict):
                payload.update(fields)
            with self.path.open("a") as f:
                f.write(json.dumps(payload, default=str) + "\n")
        except Exception:  # noqa: BLE001 - logging must never crash the run
            self.handleError(record)


def _setup_logging(logs_dir: Path, run_date: date_cls) -> tuple[JsonLinesHandler, int]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"run-{run_date.strftime('%Y%m%d')}.jsonl"
    handler = JsonLinesHandler(log_path)
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return handler, previous_level


def _teardown_logging(handler: JsonLinesHandler, previous_level: int) -> None:
    root = logging.getLogger()
    root.removeHandler(handler)
    root.setLevel(previous_level)
    handler.close()


# --------------------------------------------------------------------------
# unverified_skipped: derived straight from the raw YAML, mirroring the
# [VERIFY]-skip rule models.load_roster / models.load_sources already apply.
# --------------------------------------------------------------------------


def count_unverified(roster_path: str | Path, sources_path: str | Path) -> int:
    """Count roster/discovery source fields skipped because their value
    contained '[VERIFY]' (SPEC §4.2: "counted in the run summary as
    unverified_skipped"). Read from the raw YAML so this works regardless of
    what the loaders keep, without changing models.py."""
    count = 0

    roster_path = Path(roster_path)
    if roster_path.exists():
        with roster_path.open() as f:
            raw_roster = yaml.safe_load(f) or {}
        for group in raw_roster.values():
            for entry in (group or {}).values():
                if not isinstance(entry, dict):
                    continue
                for field in _FETCHABLE_ROSTER_FIELDS:
                    value = entry.get(field)
                    if isinstance(value, str) and "[VERIFY" in value:
                        count += 1

    sources_path = Path(sources_path)
    if sources_path.exists():
        with sources_path.open() as f:
            raw_sources = yaml.safe_load(f) or {}
        discovery = raw_sources.get("discovery", {}) or {}
        for _category, items in discovery.items():
            if not isinstance(items, dict):
                continue
            for _key, value in items.items():
                if isinstance(value, dict):
                    url = value.get("url")
                    if isinstance(url, str) and "[VERIFY" in url:
                        count += 1
                elif isinstance(value, str) and "[VERIFY" in value:
                    count += 1

    return count


def _resolve_now(date: str | None, now: datetime | None) -> datetime:
    if now is not None:
        return now
    if date:
        d = datetime.strptime(date, "%Y-%m-%d").date()
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _published_digest_state(index_path: Path) -> tuple[bool, str | None]:
    """Inspect an already-rendered index.html for the empty-digest guard.

    Returns (has_items, digest_date) — whether the published page currently
    shows any items, and the PT date in its header (or None if unreadable /
    not present). Used to avoid overwriting a good same-day digest with an
    empty one on a re-run."""
    if not index_path.exists():
        return False, None
    try:
        html = index_path.read_text(encoding="utf-8")
    except OSError:
        return False, None
    has_items = "<article>" in html
    match = re.search(r"(\d{4}-\d{2}-\d{2}) \(PT\)", html)
    return has_items, (match.group(1) if match else None)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def run(
    *,
    date: str | None = None,
    dry_run: bool = False,
    fetch: Fetch = default_fetch,
    engines: dict[str, EngineFn] | None = None,
    roster_path: str | Path = "config/roster.yaml",
    sources_path: str | Path = "config/sources.yaml",
    state_dir: str | Path = "state",
    docs_dir: str | Path = "docs",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the full pipeline once. Returns the run summary dict (also written
    to state/run_summary.json and logged as a JSON-lines event).

    `dry_run=True`: no seen-store writes (skip record_seen/prune_seen);
    renders to `state_dir/preview` instead of `docs_dir`.
    """
    start = time.monotonic()
    now = _resolve_now(date, now)
    run_date = now.date()

    roster_path = Path(roster_path)
    sources_path = Path(sources_path)
    state_dir = Path(state_dir)
    logs_dir = state_dir / "logs"

    handler, previous_level = _setup_logging(logs_dir, run_date)
    try:
        roster = load_roster(roster_path)
        settings, discovery = load_sources(sources_path)
        window_hours = settings.get("window_hours", 26)
        since = now - timedelta(hours=window_hours)

        unverified_skipped = count_unverified(roster_path, sources_path)

        items, gaps = gather(roster, (settings, discovery), since, fetch=fetch)
        fetched = len(items)

        seen_path = state_dir / "seen.json"
        kept_items = drop_seen(items, seen_path)
        after_seen = len(kept_items)

        clusters = cluster(kept_items)

        # Bridge the also_links gap: score_items only returns ScoredItems for
        # cluster representatives, dropping [1:] (the also_links). Map each
        # representative's id back to its cluster's non-representative
        # members so render_all can show "also:" links.
        also_links_by_rep_id = {c[0].id: c[1:] for c in clusters if c}

        ledger_path = state_dir / "ledger.jsonl"
        scored, run_cost = score_items(
            clusters,
            settings,
            roster,
            engines=engines,
            ledger_path=ledger_path,
            now=now,
        )
        for scored_item in scored:
            scored_item.also_links = also_links_by_rep_id.get(scored_item.id, [])

        run_meta = {
            "date": run_date.isoformat(),
            "fetched": fetched,
            "unverified_skipped": unverified_skipped,
            "after_seen": after_seen,
            "clusters": len(clusters),
            "scored_kept": len(scored),
            "engine": run_cost.engine,
            "est_cost_usd": run_cost.est_cost_usd,
        }

        out_dir = (state_dir / "preview") if dry_run else Path(docs_dir)

        # Empty-digest guard: a real run that produced nothing must NOT blank an
        # already-published, non-empty digest for the same day (e.g. a manual
        # re-run after items were marked seen). Skip the render so docs/ is left
        # intact and run_daily.sh finds no change to commit. A genuinely quiet
        # first run of a new day still renders its "nothing noteworthy" page,
        # because the guard only trips when the live page is same-dated.
        skipped_empty_republish = False
        if not dry_run and len(scored) == 0:
            prev_has_items, prev_date = _published_digest_state(out_dir / "index.html")
            if prev_has_items and prev_date == run_date.isoformat():
                skipped_empty_republish = True

        if not skipped_empty_republish:
            render_all(scored, gaps, run_meta, settings, out_dir=out_dir)

        if not dry_run:
            record_seen(kept_items, seen_path)
            prune_seen(seen_path)

        duration_s = time.monotonic() - start
        summary = {
            **run_meta,
            "gaps": [dataclasses.asdict(g) for g in gaps],
            "duration_s": duration_s,
            "skipped_empty_republish": skipped_empty_republish,
        }
        if skipped_empty_republish:
            logger.info(
                "skipped_empty_republish",
                extra={"fields": {"date": run_date.isoformat()}},
            )

        logger.info("run_summary", extra={"fields": summary})

        summary_path = state_dir / "run_summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("w") as f:
            json.dump(summary, f, indent=2)

        return summary
    except Exception as exc:  # noqa: BLE001 - only unrecoverable pipeline errors
        logger.error(
            "pipeline_failed",
            extra={"fields": {"date": run_date.isoformat(), "error": str(exc)}},
        )
        raise
    finally:
        _teardown_logging(handler, previous_level)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m digest.run")
    parser.add_argument("--date", help="YYYY-MM-DD; defaults to today (UTC)")
    parser.add_argument("--dry-run", action="store_true", help="render to state/preview, skip seen-store writes")
    args = parser.parse_args(argv)

    try:
        summary = run(date=args.date, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - unrecoverable pipeline error -> exit 1
        print(f"pipeline failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
