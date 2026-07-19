"""LLM scoring cascade + spend guardrail for GenAI Daily Digest.

score_items() batches cluster representatives, scores them through a
claude_p -> gemini -> anthropic_api cascade (each engine gets one retry
before falling through to the next), enforces a monthly spend cap before
ever using the paid anthropic_api engine, appends a ledger line, and
degrades gracefully (roster-authored items kept unscored) if every engine
fails.

Engines are injected as callables so tests never spawn `claude` or hit
the network. See EngineFn / EngineResult below.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from digest.models import Item

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class ScoredItem(Item):
    """An Item plus scoring output. score is None in degraded mode."""

    score: int | None = None
    summary: str = ""
    why_matters: str = ""
    noteworthy: bool = False


@dataclass
class RunCost:
    """Summary of what the scoring step spent/used, for the run ledger."""

    engine: str  # "claude_p" | "gemini" | "anthropic_api" | "degraded"
    items: int
    est_cost_usd: float
    degraded: bool = False
    cap_reached: bool = False


@dataclass
class EngineResult:
    """Result of a single engine call for one batch."""

    success: bool
    structured_output: dict[str, Any] | None = None
    usage: dict[str, int] | None = None  # {"input_tokens": .., "output_tokens": ..}
    error: str | None = None


# (prompt, json_schema) -> EngineResult
EngineFn = Callable[[str, dict[str, Any]], EngineResult]

ROSTER_GROUP_TITLES: dict[str, str] = {
    "lab_leaders": "Lab Leaders",
    "researchers": "Researchers",
    "economists": "Economists",
    "policymakers": "Policymakers",
    "thinkers": "Thinkers",
}

JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "summary": {"type": "string"},
                    "why_matters": {"type": "string"},
                },
                "required": ["id", "score", "summary", "why_matters"],
            },
        }
    },
    "required": ["items"],
}

PROMPT_TEMPLATE = """You are filtering a personal daily GenAI digest. Roster of people I follow:
{roster_names}
Rubric — an item is noteworthy (score >= 6) only if BOTH hold:
(a) it is from or clearly about a roster person, or a frontier-lab/major-policy
    development of the kind they would weigh in on;
(b) it is substantive: new capability or model, safety or policy stance,
    serious economic analysis, notable prediction or call-to-action,
    or significant performance/cost result — NOT routine chatter, memes,
    hiring, or promo.
Score every item 0-10. For each, write a 1-2 sentence factual summary and a
one-line "why it matters". Items:
{items_block}"""


# --------------------------------------------------------------------------
# Prompt building
# --------------------------------------------------------------------------


def roster_names_block(roster: dict[str, dict[str, Any]]) -> str:
    """Render roster names grouped by section, e.g. 'Lab Leaders: A, B'."""
    lines = []
    for group_key, title in ROSTER_GROUP_TITLES.items():
        entries = roster.get(group_key) or {}
        names = [e["name"] for e in entries.values() if e.get("name")]
        if names:
            lines.append(f"{title}: {', '.join(names)}")
    return "\n".join(lines)


def items_block(batch: list[Item]) -> str:
    """Render the numbered item list: id, source, person?, title, first 500 chars."""
    lines = []
    for i, item in enumerate(batch, start=1):
        person = item.person or "-"
        text = (item.text or "")[:500]
        lines.append(
            f'{i}. id={item.id} source={item.source_type} person={person} '
            f'title="{item.title}"\n   text: {text}'
        )
    return "\n".join(lines)


def build_prompt(batch: list[Item], roster: dict[str, dict[str, Any]]) -> str:
    """Build the scoring prompt for one batch of cluster representatives."""
    return PROMPT_TEMPLATE.format(
        roster_names=roster_names_block(roster),
        items_block=items_block(batch),
    )


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------


def apply_scores(
    reps: list[Item], batch_results: list[dict[str, Any]]
) -> list[ScoredItem]:
    """Map {"items": [...]} structured outputs back onto representative items by id.

    Tolerates missing ids (item not present in any batch result -> score=None)
    and extra/unknown ids in the response (ignored).
    """
    score_map: dict[str, dict[str, Any]] = {}
    for result in batch_results:
        for entry in result.get("items", []):
            entry_id = entry.get("id")
            if entry_id:
                score_map[entry_id] = entry

    scored: list[ScoredItem] = []
    for rep in reps:
        entry = score_map.get(rep.id)
        if entry is not None:
            try:
                score: int | None = int(entry.get("score"))
            except (TypeError, ValueError):
                score = None
            summary = str(entry.get("summary", ""))
            why_matters = str(entry.get("why_matters", ""))
        else:
            score = None
            summary = ""
            why_matters = ""
        scored.append(
            ScoredItem(
                **vars(rep),
                score=score,
                summary=summary,
                why_matters=why_matters,
                noteworthy=False,
            )
        )
    return scored


def degraded_scored_items(reps: list[Item]) -> list[ScoredItem]:
    """Degraded-mode fallback: keep roster-authored items only, unscored."""
    out = [
        ScoredItem(**vars(rep), score=None, summary="", why_matters="", noteworthy=False)
        for rep in reps
        if rep.person
    ]
    out.sort(key=lambda s: s.published, reverse=True)
    return out


# --------------------------------------------------------------------------
# Ledger (spend guardrail)
# --------------------------------------------------------------------------


def current_month_spend(ledger_path: Path, now: datetime) -> float:
    """Sum est_cost_usd of ledger lines whose date falls in now's year-month."""
    if not ledger_path.exists():
        return 0.0
    prefix = now.strftime("%Y-%m")
    total = 0.0
    try:
        with ledger_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                date = obj.get("date", "")
                if isinstance(date, str) and date.startswith(prefix):
                    try:
                        total += float(obj.get("est_cost_usd", 0.0))
                    except (TypeError, ValueError):
                        pass
    except OSError as e:
        logger.warning("could not read ledger %s: %s", ledger_path, e)
        return 0.0
    return total


def append_ledger_line(
    ledger_path: Path, now: datetime, engine: str, items: int, est_cost_usd: float
) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "date": now.strftime("%Y-%m-%d"),
        "engine": engine,
        "items": items,
        "est_cost_usd": round(est_cost_usd, 6),
    }
    with ledger_path.open("a") as f:
        f.write(json.dumps(line) + "\n")


# --------------------------------------------------------------------------
# Cascade execution
# --------------------------------------------------------------------------


def _call_with_retry(engine_fn: EngineFn, prompt: str, schema: dict[str, Any]) -> EngineResult:
    """One retry per landmine #1-3: try, retry once, then give up."""
    result: EngineResult | None = None
    for _attempt in range(2):
        try:
            result = engine_fn(prompt, schema)
        except Exception as e:  # noqa: BLE001 - any engine failure falls through
            result = EngineResult(success=False, error=str(e))
        if result.success:
            return result
    assert result is not None
    return result


def _run_engine_for_all_batches(
    engine_fn: EngineFn,
    batches: list[list[Item]],
    roster: dict[str, dict[str, Any]],
    schema: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]], dict[str, int]]:
    """Run one engine across every batch. All-or-nothing: if any batch fails
    (after its retry), the whole engine is considered failed for this run."""
    results: list[dict[str, Any]] = []
    usage_totals = {"input_tokens": 0, "output_tokens": 0}
    for batch in batches:
        prompt = build_prompt(batch, roster)
        result = _call_with_retry(engine_fn, prompt, schema)
        if not result.success or result.structured_output is None:
            return False, [], usage_totals
        results.append(result.structured_output)
        if result.usage:
            usage_totals["input_tokens"] += result.usage.get("input_tokens", 0)
            usage_totals["output_tokens"] += result.usage.get("output_tokens", 0)
    return True, results, usage_totals


def _estimate_anthropic_cost(usage_totals: dict[str, int]) -> float:
    """$1/M input tokens, $5/M output tokens."""
    input_cost = usage_totals.get("input_tokens", 0) / 1_000_000 * 1.0
    output_cost = usage_totals.get("output_tokens", 0) / 1_000_000 * 5.0
    return input_cost + output_cost


# --------------------------------------------------------------------------
# Default (real) engines — never invoked from tests
# --------------------------------------------------------------------------


def _call_claude_p(prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
    """Landmine #1/#2: env copy with ANTHROPIC_API_KEY removed; prompt on stdin."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        proc = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                model,
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(schema),
            ],
            input=prompt,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    except Exception as e:  # noqa: BLE001
        return EngineResult(success=False, error=str(e))
    if proc.returncode != 0:
        return EngineResult(success=False, error=f"exit {proc.returncode}: {proc.stderr[:500]}")
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return EngineResult(success=False, error=f"bad json: {e}")
    if obj.get("is_error") is not False or "structured_output" not in obj:
        return EngineResult(success=False, error="unexpected claude -p response shape")
    return EngineResult(success=True, structured_output=obj["structured_output"])


def _call_gemini(prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
    """Landmine #14."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return EngineResult(success=False, error="GEMINI_API_KEY not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    full_prompt = prompt + "\n\nRespond ONLY with JSON matching this schema:\n" + json.dumps(schema)
    body = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    try:
        resp = requests.post(url, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        structured = json.loads(text)
    except Exception as e:  # noqa: BLE001
        return EngineResult(success=False, error=str(e))
    return EngineResult(success=True, structured_output=structured)


def _call_anthropic_api(prompt: str, schema: dict[str, Any], model: str) -> EngineResult:
    """Fallback2. Uses ANTHROPIC_API_KEY_FALLBACK — never the real ANTHROPIC_API_KEY."""
    api_key = os.environ.get("ANTHROPIC_API_KEY_FALLBACK")
    if not api_key:
        return EngineResult(success=False, error="ANTHROPIC_API_KEY_FALLBACK not set")
    full_prompt = prompt + "\n\nRespond ONLY with JSON matching this schema:\n" + json.dumps(schema)
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": full_prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
        structured = json.loads(text)
        usage = data.get("usage", {})
    except Exception as e:  # noqa: BLE001
        return EngineResult(success=False, error=str(e))
    return EngineResult(
        success=True,
        structured_output=structured,
        usage={
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    )


def default_engines(settings: dict[str, Any]) -> dict[str, EngineFn]:
    """Build the real engine callables (spawns `claude`, hits real HTTP). Never
    used in tests — tests always pass their own `engines` dict."""
    llm = settings.get("llm", {}) or {}
    claude_model = llm.get("claude_model", "haiku")
    gemini_model = llm.get("gemini_model", "gemini-3.1-flash-lite-preview")
    anthropic_model = llm.get("anthropic_model", "claude-haiku-4-5-20251001")
    return {
        "claude_p": lambda prompt, schema: _call_claude_p(prompt, schema, claude_model),
        "gemini": lambda prompt, schema: _call_gemini(prompt, schema, gemini_model),
        "anthropic_api": lambda prompt, schema: _call_anthropic_api(prompt, schema, anthropic_model),
    }


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------


def score_items(
    clusters: list[list[Item]],
    settings: dict[str, Any],
    roster: dict[str, dict[str, Any]],
    engines: dict[str, EngineFn] | None = None,
    ledger_path: str | Path = "state/ledger.jsonl",
    now: datetime | None = None,
) -> tuple[list[ScoredItem], RunCost]:
    """Score cluster representatives through the claude_p -> gemini ->
    anthropic_api cascade, apply the spend guardrail, and post-filter.
    """
    now = now or datetime.now(timezone.utc)
    ledger_path = Path(ledger_path)

    llm_cfg = settings.get("llm", {}) or {}
    cascade = [
        llm_cfg.get("primary", "claude_p"),
        llm_cfg.get("fallback1", "gemini"),
        llm_cfg.get("fallback2", "anthropic_api"),
    ]
    batch_size = settings.get("batch_size", 40)
    score_threshold = settings.get("score_threshold", 6)
    keep_top = settings.get("keep_top", 25)
    monthly_cap = settings.get("monthly_cap_usd", 5.0)

    if engines is None:
        engines = default_engines(settings)

    reps = [min(cluster, key=lambda it: it.published) for cluster in clusters]
    batches = [reps[i : i + batch_size] for i in range(0, len(reps), batch_size)]

    used_engine: str | None = None
    batch_results: list[dict[str, Any]] = []
    usage_totals = {"input_tokens": 0, "output_tokens": 0}
    cap_reached = False

    if batches:
        for engine_name in cascade:
            if engine_name == "anthropic_api":
                spent = current_month_spend(ledger_path, now)
                if spent >= monthly_cap:
                    cap_reached = True
                    logger.info(
                        json.dumps(
                            {"event": "cap_reached", "spent": spent, "cap": monthly_cap}
                        )
                    )
                    continue

            engine_fn = engines.get(engine_name)
            if engine_fn is None:
                logger.warning("no engine registered for %s", engine_name)
                continue

            success, results, usage = _run_engine_for_all_batches(
                engine_fn, batches, roster, JSON_SCHEMA
            )
            if success:
                used_engine = engine_name
                batch_results = results
                usage_totals = usage
                break
            logger.warning("engine %s failed for this run, falling through", engine_name)

    if used_engine is None:
        scored = degraded_scored_items(reps)
        append_ledger_line(ledger_path, now, "degraded", 0, 0.0)
        run_cost = RunCost(
            engine="degraded", items=0, est_cost_usd=0.0, degraded=True, cap_reached=cap_reached
        )
        return scored, run_cost

    scored_all = apply_scores(reps, batch_results)
    for s in scored_all:
        s.noteworthy = s.score is not None and s.score >= score_threshold

    if used_engine == "anthropic_api":
        est_cost = _estimate_anthropic_cost(usage_totals)
    else:
        est_cost = 0.0

    append_ledger_line(ledger_path, now, used_engine, len(reps), est_cost)

    kept = [s for s in scored_all if s.noteworthy]
    kept.sort(key=lambda s: s.score, reverse=True)
    kept = kept[:keep_top]

    run_cost = RunCost(
        engine=used_engine,
        items=len(reps),
        est_cost_usd=est_cost,
        degraded=False,
        cap_reached=cap_reached,
    )
    return kept, run_cost
