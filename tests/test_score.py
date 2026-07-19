"""Tests for digest.score — LLM scoring cascade + spend guardrail.

No test spawns `claude` or hits HTTP: all engines are injected callables.
No test sets or reads ANTHROPIC_API_KEY (the real one).
"""

import json
import os
from datetime import datetime, timezone

import pytest

from digest.models import Item
from digest.score import (
    EngineResult,
    RunCost,
    ScoredItem,
    apply_scores,
    build_prompt,
    current_month_spend,
    score_items,
)

ROSTER = {
    "lab_leaders": {
        "dario_amodei": {"name": "Dario Amodei"},
        "demis_hassabis": {"name": "Demis Hassabis"},
    },
    "researchers": {
        "andrej_karpathy": {"name": "Andrej Karpathy"},
    },
}

SETTINGS = {
    "batch_size": 40,
    "score_threshold": 6,
    "keep_top": 25,
    "monthly_cap_usd": 5.0,
    "llm": {
        "primary": "claude_p",
        "claude_model": "haiku",
        "fallback1": "gemini",
        "gemini_model": "gemini-3.1-flash-lite-preview",
        "fallback2": "anthropic_api",
        "anthropic_model": "claude-haiku-4-5-20251001",
    },
}


def make_item(
    id_: str,
    title: str = "Some title",
    person: str | None = "dario_amodei",
    text: str = "Body text " * 10,
    published: datetime | None = None,
) -> Item:
    return Item(
        id=id_,
        source_key="dario_amodei_blog",
        source_type="rss",
        person=person,
        category="lab_leaders" if person else "discovery",
        title=title,
        url=f"https://example.com/{id_}",
        author="Dario Amodei",
        published=published or datetime(2026, 7, 18, tzinfo=timezone.utc),
        text=text,
        fetched_at=datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc),
    )


def clusters_of(*items: Item) -> list[list[Item]]:
    return [[item] for item in items]


def ok_engine(response_items):
    """Build an injected engine callable that always succeeds with the given items."""

    def _engine(prompt, schema):
        return EngineResult(success=True, structured_output={"items": response_items})

    return _engine


def failing_engine(error="boom"):
    def _engine(prompt, schema):
        return EngineResult(success=False, error=error)

    return _engine


def raising_engine(error="kaboom"):
    def _engine(prompt, schema):
        raise RuntimeError(error)

    return _engine


# --------------------------------------------------------------------------
# Prompt builder
# --------------------------------------------------------------------------


def test_build_prompt_includes_roster_names_and_item_ids():
    items = [make_item("item-aaa", title="Title A"), make_item("item-bbb", title="Title B")]
    prompt = build_prompt(items, ROSTER)

    assert "Dario Amodei" in prompt
    assert "Demis Hassabis" in prompt
    assert "Andrej Karpathy" in prompt
    assert "id=item-aaa" in prompt
    assert "id=item-bbb" in prompt
    assert "Title A" in prompt
    assert "Title B" in prompt
    # Fixed rubric text present verbatim
    assert "Score every item 0-10" in prompt
    assert "why it matters" in prompt


def test_build_prompt_omits_empty_roster_groups():
    prompt = build_prompt([make_item("x")], {"lab_leaders": {}, "researchers": {}})
    assert "Lab Leaders:" not in prompt
    assert "Researchers:" not in prompt


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------


def test_apply_scores_maps_by_id():
    reps = [make_item("id-1", title="One"), make_item("id-2", title="Two")]
    batch_results = [
        {
            "items": [
                {"id": "id-1", "score": 8, "summary": "s1", "why_matters": "w1"},
                {"id": "id-2", "score": 3, "summary": "s2", "why_matters": "w2"},
            ]
        }
    ]
    scored = apply_scores(reps, batch_results)
    by_id = {s.id: s for s in scored}
    assert by_id["id-1"].score == 8
    assert by_id["id-1"].summary == "s1"
    assert by_id["id-1"].why_matters == "w1"
    assert by_id["id-2"].score == 3


def test_apply_scores_tolerates_missing_ids():
    reps = [make_item("id-1"), make_item("id-2"), make_item("id-3")]
    batch_results = [
        {"items": [{"id": "id-1", "score": 9, "summary": "s", "why_matters": "w"}]}
    ]
    scored = apply_scores(reps, batch_results)
    by_id = {s.id: s for s in scored}
    assert by_id["id-1"].score == 9
    assert by_id["id-2"].score is None
    assert by_id["id-2"].summary == ""
    assert by_id["id-3"].score is None


def test_apply_scores_tolerates_unknown_extra_ids_in_response():
    reps = [make_item("id-1")]
    batch_results = [
        {
            "items": [
                {"id": "id-1", "score": 7, "summary": "s", "why_matters": "w"},
                {"id": "id-unknown", "score": 10, "summary": "x", "why_matters": "y"},
            ]
        }
    ]
    scored = apply_scores(reps, batch_results)
    assert len(scored) == 1
    assert scored[0].id == "id-1"
    assert scored[0].score == 7


# --------------------------------------------------------------------------
# Cascade behavior
# --------------------------------------------------------------------------


def test_cascade_uses_primary_when_it_succeeds(tmp_path):
    items = [make_item("id-1", title="One")]
    response = [{"id": "id-1", "score": 8, "summary": "s", "why_matters": "w"}]
    engines = {
        "claude_p": ok_engine(response),
        "gemini": failing_engine(),
        "anthropic_api": failing_engine(),
    }
    scored, run_cost = score_items(
        clusters_of(*items),
        SETTINGS,
        ROSTER,
        engines=engines,
        ledger_path=tmp_path / "ledger.jsonl",
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    assert run_cost.engine == "claude_p"
    assert run_cost.est_cost_usd == 0.0
    assert len(scored) == 1
    assert scored[0].score == 8


def test_cascade_falls_through_to_gemini_when_claude_p_fails(tmp_path):
    items = [make_item("id-1")]
    response = [{"id": "id-1", "score": 7, "summary": "s", "why_matters": "w"}]
    engines = {
        "claude_p": failing_engine(),
        "gemini": ok_engine(response),
        "anthropic_api": failing_engine(),
    }
    scored, run_cost = score_items(
        clusters_of(*items),
        SETTINGS,
        ROSTER,
        engines=engines,
        ledger_path=tmp_path / "ledger.jsonl",
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    assert run_cost.engine == "gemini"
    assert run_cost.est_cost_usd == 0.0
    assert scored[0].score == 7


def test_cascade_falls_through_to_anthropic_api_when_others_fail(tmp_path):
    items = [make_item("id-1")]
    response = [{"id": "id-1", "score": 9, "summary": "s", "why_matters": "w"}]

    def anthropic_engine(prompt, schema):
        return EngineResult(
            success=True,
            structured_output={"items": response},
            usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        )

    engines = {
        "claude_p": failing_engine(),
        "gemini": raising_engine(),  # exceptions must also fall through
        "anthropic_api": anthropic_engine,
    }
    scored, run_cost = score_items(
        clusters_of(*items),
        SETTINGS,
        ROSTER,
        engines=engines,
        ledger_path=tmp_path / "ledger.jsonl",
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    assert run_cost.engine == "anthropic_api"
    # $1/M input + $5/M output, both at 1M tokens => $1 + $5 = $6
    assert run_cost.est_cost_usd == pytest.approx(6.0)
    assert scored[0].score == 9


def test_engine_retried_once_before_falling_through(tmp_path):
    calls = {"claude_p": 0}

    def flaky_then_ok(prompt, schema):
        calls["claude_p"] += 1
        if calls["claude_p"] < 2:
            return EngineResult(success=False, error="transient")
        return EngineResult(
            success=True,
            structured_output={"items": [{"id": "id-1", "score": 8, "summary": "s", "why_matters": "w"}]},
        )

    items = [make_item("id-1")]
    engines = {"claude_p": flaky_then_ok, "gemini": failing_engine(), "anthropic_api": failing_engine()}
    scored, run_cost = score_items(
        clusters_of(*items),
        SETTINGS,
        ROSTER,
        engines=engines,
        ledger_path=tmp_path / "ledger.jsonl",
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    assert calls["claude_p"] == 2  # one retry, then success
    assert run_cost.engine == "claude_p"
    assert scored[0].score == 8


# --------------------------------------------------------------------------
# Spend guardrail / cap
# --------------------------------------------------------------------------


def test_cap_reached_blocks_anthropic_api(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    # Fixture ledger already at (>=) the $5.0 cap for the current month.
    with ledger_path.open("w") as f:
        f.write(json.dumps({"date": "2026-07-01", "engine": "anthropic_api", "items": 10, "est_cost_usd": 5.0}) + "\n")

    items = [make_item("id-1")]
    engines = {
        "claude_p": failing_engine(),
        "gemini": failing_engine(),
        "anthropic_api": ok_engine([{"id": "id-1", "score": 9, "summary": "s", "why_matters": "w"}]),
    }
    scored, run_cost = score_items(
        clusters_of(*items),
        SETTINGS,
        ROSTER,
        engines=engines,
        ledger_path=ledger_path,
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    # anthropic_api must never be called: cascade exhausted -> degraded.
    assert run_cost.degraded is True
    assert run_cost.cap_reached is True
    assert run_cost.engine == "degraded"


def test_current_month_spend_sums_only_current_month(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    with ledger_path.open("w") as f:
        f.write(json.dumps({"date": "2026-07-01", "engine": "anthropic_api", "items": 1, "est_cost_usd": 2.0}) + "\n")
        f.write(json.dumps({"date": "2026-07-15", "engine": "anthropic_api", "items": 1, "est_cost_usd": 1.5}) + "\n")
        f.write(json.dumps({"date": "2026-06-30", "engine": "anthropic_api", "items": 1, "est_cost_usd": 100.0}) + "\n")

    total = current_month_spend(ledger_path, datetime(2026, 7, 18, tzinfo=timezone.utc))
    assert total == pytest.approx(3.5)


def test_current_month_spend_missing_file_is_zero(tmp_path):
    total = current_month_spend(tmp_path / "does-not-exist.jsonl", datetime(2026, 7, 18, tzinfo=timezone.utc))
    assert total == 0.0


# --------------------------------------------------------------------------
# Degraded mode
# --------------------------------------------------------------------------


def test_degraded_mode_keeps_only_roster_authored_items(tmp_path):
    roster_item = make_item("id-1", person="dario_amodei")
    discovery_item = make_item("id-2", person=None, title="Discovery thing")

    engines = {
        "claude_p": failing_engine(),
        "gemini": failing_engine(),
        "anthropic_api": failing_engine(),
    }
    scored, run_cost = score_items(
        clusters_of(roster_item, discovery_item),
        SETTINGS,
        ROSTER,
        engines=engines,
        ledger_path=tmp_path / "ledger.jsonl",
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    assert run_cost.degraded is True
    assert run_cost.engine == "degraded"
    assert run_cost.est_cost_usd == 0.0
    ids = {s.id for s in scored}
    assert "id-1" in ids
    assert "id-2" not in ids
    for s in scored:
        assert s.score is None
        assert s.noteworthy is False


# --------------------------------------------------------------------------
# Ledger append
# --------------------------------------------------------------------------


def test_ledger_line_appended_after_scoring(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    items = [make_item("id-1")]
    response = [{"id": "id-1", "score": 8, "summary": "s", "why_matters": "w"}]
    engines = {"claude_p": ok_engine(response), "gemini": failing_engine(), "anthropic_api": failing_engine()}

    score_items(
        clusters_of(*items),
        SETTINGS,
        ROSTER,
        engines=engines,
        ledger_path=ledger_path,
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )

    lines = ledger_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["date"] == "2026-07-18"
    assert entry["engine"] == "claude_p"
    assert entry["items"] == 1
    assert entry["est_cost_usd"] == 0.0


def test_ledger_line_appended_in_degraded_mode(tmp_path):
    ledger_path = tmp_path / "ledger.jsonl"
    items = [make_item("id-1")]
    engines = {"claude_p": failing_engine(), "gemini": failing_engine(), "anthropic_api": failing_engine()}

    score_items(
        clusters_of(*items),
        SETTINGS,
        ROSTER,
        engines=engines,
        ledger_path=ledger_path,
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )

    lines = ledger_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["engine"] == "degraded"
    assert entry["est_cost_usd"] == 0.0


# --------------------------------------------------------------------------
# Post-filter: threshold + keep_top
# --------------------------------------------------------------------------


def test_post_filter_applies_threshold_and_keep_top(tmp_path):
    items = [make_item(f"id-{i}", title=f"Title {i}") for i in range(5)]
    scores = [9, 5, 7, 6, 10]  # threshold=6 -> keep ids 0,2,3,4 (4 items)
    response = [
        {"id": it.id, "score": sc, "summary": "s", "why_matters": "w"}
        for it, sc in zip(items, scores)
    ]
    engines = {"claude_p": ok_engine(response), "gemini": failing_engine(), "anthropic_api": failing_engine()}

    settings = dict(SETTINGS)
    settings["keep_top"] = 2

    scored, run_cost = score_items(
        clusters_of(*items),
        settings,
        ROSTER,
        engines=engines,
        ledger_path=tmp_path / "ledger.jsonl",
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )

    assert len(scored) == 2  # keep_top cap
    assert [s.score for s in scored] == [10, 9]  # sorted desc, top 2 kept


def test_batching_respects_batch_size(tmp_path):
    """batch_size=1 with 3 clusters should trigger 3 engine calls, not 1."""
    items = [make_item(f"id-{i}") for i in range(3)]
    call_count = {"n": 0}

    def counting_engine(prompt, schema):
        call_count["n"] += 1
        # Figure out which single id is in this batch from the prompt.
        matched = [it.id for it in items if f"id={it.id}" in prompt]
        assert len(matched) == 1
        return EngineResult(
            success=True,
            structured_output={
                "items": [{"id": matched[0], "score": 8, "summary": "s", "why_matters": "w"}]
            },
        )

    settings = dict(SETTINGS)
    settings["batch_size"] = 1
    engines = {"claude_p": counting_engine, "gemini": failing_engine(), "anthropic_api": failing_engine()}

    scored, run_cost = score_items(
        clusters_of(*items),
        settings,
        ROSTER,
        engines=engines,
        ledger_path=tmp_path / "ledger.jsonl",
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    assert call_count["n"] == 3
    assert len(scored) == 3


# --------------------------------------------------------------------------
# Safety: never touch the real ANTHROPIC_API_KEY
# --------------------------------------------------------------------------


def test_anthropic_api_key_never_set_or_read_by_tests():
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_empty_clusters_returns_empty_without_calling_any_engine(tmp_path):
    def boom(prompt, schema):
        raise AssertionError("engine should not be called for empty clusters")

    engines = {"claude_p": boom, "gemini": boom, "anthropic_api": boom}
    scored, run_cost = score_items(
        [],
        SETTINGS,
        ROSTER,
        engines=engines,
        ledger_path=tmp_path / "ledger.jsonl",
        now=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    assert scored == []
    assert run_cost.degraded is True
