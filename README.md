# GenAI Daily Digest

A personal, no-server news digest. Once a day it pulls posts/papers/talks from
a curated roster of GenAI people and labs (RSS, Bluesky, arXiv, YouTube, HN,
Google News), dedupes them against everything seen before, scores them with
an LLM cascade, and publishes a static site (`docs/`) via GitHub Pages —
readable from a MacBook or an iPhone with no app and no backend.

See `DESIGN.md` for the architecture rationale and `SPEC.md` for the full
implementation spec.

## One-time setup

1. **Create the virtualenv and install the package:**
   ```
   /opt/homebrew/bin/python3.11 -m venv .venv
   .venv/bin/pip install -e .
   ```
   (Add `pytest` too if you want to run the test suite: `.venv/bin/pip install pytest`.)

2. **Enable GitHub Pages.** In the repo's GitHub settings → Pages, set the
   source to the `main` branch, `/docs` folder. `docs/.nojekyll` is already
   committed so Pages serves the site as-is. Once enabled, copy the Pages URL
   into `config/sources.yaml` → `settings.site_url` (it starts as
   `[VERIFY after Pages setup]`).

3. **Install the daily launchd job:**
   ```
   bash scripts/install_launchd.sh
   ```
   This copies `ops/com.redwan.genai-digest.plist` into
   `~/Library/LaunchAgents/`, runs `launchctl bootstrap`, and prints a
   verification line (`launchctl print gui/$UID/com.redwan.genai-digest`).
   The job fires daily at **20:00 America/Los_Angeles**.

   To uninstall:
   ```
   launchctl bootout gui/$(id -u)/com.redwan.genai-digest
   rm ~/Library/LaunchAgents/com.redwan.genai-digest.plist
   ```

## Editing the roster

`config/roster.yaml` is the single source of truth for WHO is tracked
(lab leaders, researchers, economists, policymakers, ...); `config/sources.yaml`
holds non-person sources (lab blogs, newsletters, discovery keywords) and
global settings (window, score threshold, cost cap, LLM cascade config).

Add or edit an entry directly in the YAML — each person/source can carry any
combination of `blog_rss`, `x_handle`, `bluesky`, `arxiv_query`,
`youtube_channel_id`, `gnews`. Feed URLs you (or an agent) aren't fully sure
of are marked `# [VERIFY]` — they're best-known guesses, not confirmed
working feeds.

Before trusting a new or edited config, validate every configured source
live:
```
.venv/bin/python -m digest.fetch --check
```
This hits every source and prints `OK <source_key>` or `FAIL <source_key> <error>`
per line — nothing is silently assumed to work. Fix or remove any `FAIL`
entries (and clear the corresponding `[VERIFY]` marker once confirmed) before
the next scheduled run.

## Running manually

Run the full pipeline once, right now:
```
.venv/bin/python -m digest.run
```

Preview without touching the seen-store or publishing (renders to
`state/preview/` instead of `docs/`, does not mark items as seen):
```
.venv/bin/python -m digest.run --dry-run
```

Re-run a specific day:
```
.venv/bin/python -m digest.run --date 2026-07-18
```

The launchd job itself just runs `scripts/run_daily.sh`, which does the
pipeline run above and then, if anything changed, commits and pushes
`docs/` + `state/ledger.jsonl`. You can run that same script by hand:
```
bash scripts/run_daily.sh
```

## Monthly cost

**Expected: $0/month.** The LLM cascade tries the Claude subscription
(`claude -p --model haiku`, no API billing) first, then a free Gemini Flash
tier, and only falls back to the metered Claude API if both of those are
unavailable for the day. That paid fallback is estimated at $2-3/month in the
worst case and is hard-capped: `state/ledger.jsonl` tracks spend, and the
paid path refuses to run once the month crosses **$5**. Every run logs which
engine handled scoring and its estimated cost in `state/run_summary.json`.
Hosting (GitHub Pages) and the scheduler (launchd) are both free.

## Troubleshooting

- **A run seems to have been skipped.** `StartCalendarInterval` fires a
  missed run when the Mac *wakes from sleep* past its scheduled time, but
  **not** across a reboot or if the Mac was off at 20:00 and stayed off.
  Check `state/logs/launchd.log` (stdout+stderr appended per run) and
  `state/logs/launchd.err.log` for the most recent timestamped run.
- **A source silently disappeared from the digest.** Check the `gaps` list
  in `state/run_summary.json` (also logged in `state/logs/run-YYYYMMDD.jsonl`)
  — every failed source is recorded there with the run continuing anyway,
  rather than crashing. Cross-check with `python -m digest.fetch --check`.
- **Where logs live:**
  - `state/logs/launchd.log` / `state/logs/launchd.err.log` — output of the
    daily launchd-triggered run (from `scripts/run_daily.sh`).
  - `state/logs/run-YYYYMMDD.jsonl` — structured per-run JSON-lines log from
    the pipeline itself.
  - `state/run_summary.json` — latest run's summary (counts, engine used,
    estimated cost, gaps, duration).
  - `state/ledger.jsonl` — monthly spend ledger for the paid LLM fallback
    (the one `state/` file that IS committed to git).
- **Nothing got pushed after a run.** `scripts/run_daily.sh` only commits
  when `docs/` or `state/ledger.jsonl` actually changed; an empty digest day
  (nothing new, or nothing above the score threshold) is expected to produce
  no commit.
