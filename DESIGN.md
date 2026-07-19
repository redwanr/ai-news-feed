# GenAI Daily Digest — Design Doc (1 page, for sign-off)

**Date:** 2026-07-18 · **Owner:** redwan · **Status:** awaiting OK

## Decisions already confirmed by owner
- **Schedule:** daily at **20:00 America/Los_Angeles** via **launchd** on this MacBook (Mac is typically open evenings; launchd fires a missed run on wake).
- **Hosting:** **public GitHub repo + GitHub Pages** (site served from `/docs` on `main`).
- Digest covers the prior ~26 h (2 h overlap buffer; a persistent seen-store prevents repeats).

## Architecture (one Python 3.11 CLI, no server, no database)

```
config/roster.yaml ─┐
config/sources.yaml ┴→ fetch (per-source adapters, each allowed to fail)
                       → normalize + dedupe/cluster (URL canon + title similarity)
                       → seen-store filter (state/seen.json)
                       → LLM score (batched, cascade below) → keep score ≥ 6, top 25
                       → render docs/index.html + docs/digest.md + docs/feed.xml
                       → git commit + push  → GitHub Pages URL (iPhone + MacBook)
```

Run = `python -m digest.run`, invoked by `ops/com.redwan.genai-digest.plist` → `scripts/run_daily.sh`. Structured JSONL logs, per-run summary (counts in/kept/cost), gap register for failed sources. Idempotent: re-running a day overwrites that day's output.

## Sources (all free)
| Route | What | Notes |
|---|---|---|
| RSS/Atom | personal blogs, Substacks, lab blogs (DeepMind, OpenAI, …), curator newsletters (Import AI, The Batch, Zvi, Interconnects) | primary written-word route |
| Bluesky public API | roster handles, no auth needed | primary "what did X just say" |
| arXiv API | cs.AI/cs.LG/cs.CL by roster authors + topic keywords | 1 req/3 s rate limit respected |
| YouTube channel RSS | talks/interviews per channel | free per-channel feed |
| HN Algolia + Google News RSS | discovery, filtered hard by roster names + keywords | supplements only |
| X/Twitter | **best-effort only** (optional Nitter-RSS adapter, allowed to fail, deferred to v1.1) | Bluesky + newsletters carry the signal |

Roster + all URLs live in editable YAML; uncertain feed URLs are marked `[VERIFY]` and a `check_sources` tool validates every configured feed and reports dead ones — nothing is silently guessed.

## LLM: cascade, cheapest-capable first (verified on this machine 2026-07-18)
1. **`claude -p --model haiku`** on your Claude subscription — `--output-format json --json-schema` tested working (claude v2.1.215; parse `structured_output` from result JSON). `ANTHROPIC_API_KEY` is explicitly stripped from the env so subscription auth is used. Items scored in **batches** (~23k token fixed overhead per call → 1–3 calls/run, never per-item). **Marginal cost: $0.**
2. **Gemini Flash free tier** (`GEMINI_API_KEY` already in env) — $0.
3. **Claude Haiku via API** — worst case (both above dead all month): ~50k in / 10k out per day ≈ **$2–3/month**. 
Hard guardrail: monthly ledger (`state/ledger.jsonl`); paid path refuses to run past **$5/month cap**. Every run logs which model handled it + estimated cost.

**Expected cost: $0/month.** Hosting $0, scheduler $0.

## Tradeoffs / risks
- **Subscription automation is billing-policy-sensitive** (your own caveat): fallbacks stay live; a run never silently spends — paid path is logged and capped.
- **Mac asleep/off at 8pm** → digest arrives when lid opens (acceptable per launchd choice).
- **Feeds rot** → gap register + `check_sources`; failures degrade coverage, never crash the run.
- **Public repo** → roster names + public handles visible (accepted).
- **X coverage gap** → accepted; best-effort adapter is a deferred optional task.

## Implementation plan
Handover-grade `SPEC.md` (in this repo) splits the build into 8 tasks with disjoint file zones, per-task tests, and a solved-landmines list, sized for cheap coding agents (Haiku) with orchestrator diff-review before every merge.
