# SPEC — GenAI Daily Digest v1

Implementation spec for coding agents. Each agent implements EXACTLY ONE task
(T-01 … T-08). This spec wins over any prompt summary you were given.
Companion context: `DESIGN.md` (architecture rationale; read-only for you).

---

## 0. Ground rules (read before your task section)

- **Language/runtime:** Python 3.11. Interpreter on this machine: `/opt/homebrew/bin/python3.11`. Virtualenv at `.venv/` (created in T-01). All commands below assume `.venv/bin/python` / `.venv/bin/pytest`.
- **Allowed third-party deps (complete list):** `feedparser`, `PyYAML`, `requests`, `pytest` (dev). Do NOT add any other dependency. Prefer stdlib.
- **Style:** plain functions + `dataclasses`. No classes for adapters, no async, no ORM, no Jinja (use `string.Template` or f-strings), no click/argparse frameworks beyond stdlib `argparse`. Type hints on public functions. Keep modules small.
- **Timestamps:** UTC internally (`datetime` with `timezone.utc`), convert to `America/Los_Angeles` (`zoneinfo`) only at render time.
- **No network in tests.** Adapters take the raw fetched payload (or an injected `fetch: Callable[[str], str]`) so tests feed fixtures from `tests/fixtures/`. Never call the real internet or a real LLM from pytest.
- **Graceful degradation:** any single source or LLM failure is caught, recorded in the gap register, and the run continues. The run process exits 0 unless the final render/publish itself fails.
- **Logging:** stdlib `logging`, JSON-lines handler writing `state/logs/run-YYYYMMDD.jsonl` (one JSON object per line: `ts`, `level`, `event`, plus free fields).
- **Secrets:** never commit secrets. Never set `ANTHROPIC_API_KEY` anywhere (code, docs, plist, CI). `GEMINI_API_KEY` is read from the environment only.
- **File scope:** touch ONLY the files listed in your task's *Files* section. Do not "improve" other modules.
- **Git:** work on branch `task/T-0X-<slug>`; commit; open PR with `gh pr create`; **DO NOT MERGE**. Work only inside your assigned worktree — never `cd` into the main checkout.
- **Definition of done (every task):** all listed tests pass via `.venv/bin/pytest -q`; no test in the repo broken; report per §7.
- If anything is ambiguous or you fail the same step twice: **STOP and report**, do not improvise.

---

## 1. Solved landmines — do NOT re-derive these

1. **`claude -p` auth:** if `ANTHROPIC_API_KEY` is in the env it silently bills per-token instead of using the subscription. Always invoke with an env copy that has the key **removed** (`env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}`).
2. **Verified `claude -p` invocation** (this machine, claude v2.1.215, 2026-07-18):
   ```
   claude -p --model haiku --output-format json --json-schema '<inline JSON schema string>'
   ```
   with the prompt on **stdin**. Stdout is one JSON object; the parsed structured answer is at key `"structured_output"`. Success check: exit code 0 AND `obj["is_error"] == False` AND `"structured_output" in obj`. Also present: `obj["total_cost_usd"]` (informational under subscription — log it, treat as $0 spend).
3. **Batch LLM calls.** Each `claude -p` call carries ~23k tokens of fixed system overhead. Score items in batches of ≤ 40 per call (1–3 calls per run). Never one call per item.
4. **macOS/BSD shell:** no `date +%s%N`, no `sed -i` without a backup suffix, no GNU-only flags. launchd jobs get a minimal PATH — the plist must set `PATH` explicitly including `/opt/homebrew/bin`. Test any shell script once on this machine.
5. **launchd:** install with `launchctl bootstrap gui/$(id -u) <plist path>`; uninstall with `launchctl bootout gui/$(id -u)/<label>`. `StartCalendarInterval` fires a missed job on wake from sleep (not across reboots). Use `StandardOutPath`/`StandardErrorPath` for logs.
6. **arXiv API:** `http://export.arxiv.org/api/query?search_query=...&start=0&max_results=...` — response is Atom; parse with `feedparser`. Respect **max 1 request per 3 seconds** (sleep between calls).
7. **Bluesky, no auth needed:** `GET https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor=<handle>&filter=posts_no_replies&limit=50` returns JSON. Post URL = `https://bsky.app/profile/<handle>/post/<rkey>` where rkey is the last path segment of the post's `uri` (`at://did/app.bsky.feed.post/<rkey>`).
8. **HN Algolia, no key:** `GET https://hn.algolia.com/api/v1/search_by_date?query=<quoted words>&tags=story&numericFilters=created_at_i><unix_ts>`.
9. **feedparser dates:** use `entry.get("published_parsed") or entry.get("updated_parsed")`; either may be missing → skip the entry (log at debug). Convert `struct_time` → aware UTC datetime via `calendar.timegm`.
10. **Google News RSS:** `https://news.google.com/rss/search?q=<urlencoded query>&hl=en-US&gl=US&ceid=US:en`.
11. **Substack feed pattern:** `<publication domain>/feed` (works on custom domains too).
12. **YouTube channel RSS:** `https://www.youtube.com/feeds/videos.xml?channel_id=<UC...>`.
13. **GitHub Pages** serves `/docs` on `main`. Keep `docs/.nojekyll` present.
14. **Gemini fallback:** `POST https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent?key=$GEMINI_API_KEY`, body `{"contents":[{"parts":[{"text": prompt}]}], "generationConfig": {"response_mime_type": "application/json"}}`; model id from config (default `gemini-3.1-flash-lite-preview`). `[VERIFY]` field names against current docs at implementation time if the call fails — do not guess variants beyond one retry.
15. **Roster URLs marked `[VERIFY]`** are best-known guesses. Do not replace them with your own guesses — `check_sources` (T-02) is the mechanism that finds dead ones; the orchestrator fixes them.

---

## 2. Repo layout (final state after all tasks)

```
ai-news-feed/
  DESIGN.md  SPEC.md  README.md
  pyproject.toml            # name=digest, deps, pytest config
  config/
    roster.yaml             # WHO (single source of truth)
    sources.yaml            # non-person sources + global settings
  digest/
    __init__.py
    models.py               # Item dataclass, id rule, config loaders
    fetch.py                # all source adapters + check_sources CLI
    dedupe.py               # canonicalize, cluster, seen-store
    score.py                # LLM cascade + spend guardrail + ledger
    render.py               # HTML + markdown + Atom
    run.py                  # pipeline entrypoint (python -m digest.run)
  docs/                     # published site (GitHub Pages root)
    .nojekyll  index.html  digest.md  feed.xml
  state/                    # gitignored EXCEPT ledger.jsonl
    seen.json  ledger.jsonl  logs/
  ops/com.redwan.genai-digest.plist
  scripts/run_daily.sh  scripts/install_launchd.sh
  tests/
    fixtures/               # recorded payloads
    test_models.py test_fetch.py test_dedupe.py test_score.py test_render.py test_run.py
```

---

## 3. Data model (T-01 defines; everyone imports from `digest.models`)

```python
@dataclass
class Item:
    id: str                # sha1 hex of canonical_url (see rule below)
    source_key: str        # key of the feed/handle in config
    source_type: str       # "rss" | "arxiv" | "bluesky" | "hn" | "gnews" | "youtube"
    person: str | None     # roster person key if this source belongs to a person
    category: str | None   # roster group of that person, else "discovery"
    title: str
    url: str               # canonical URL of the ORIGINAL source
    author: str | None
    published: datetime    # aware UTC
    text: str              # body/summary excerpt, hard-truncated to 2000 chars
    fetched_at: datetime   # aware UTC
```

**Canonical URL rule** (`models.canonical_url(url) -> str`): lowercase scheme+host, strip fragment, strip query params whose names start with `utm_` or equal `ref`/`source`/`fbclid`/`gclid`, strip trailing `/`. `Item.id = hashlib.sha1(canonical_url.encode()).hexdigest()`.

**Scored item** (added by score step): `score: int (0-10)`, `summary: str` (1–2 sentences), `why_matters: str` (one line), `noteworthy: bool`.

---

## 4. Config files (T-01 creates verbatim, then owner-editable)

### 4.1 `config/sources.yaml`

```yaml
settings:
  timezone: America/Los_Angeles
  window_hours: 26
  score_threshold: 6
  keep_top: 25
  monthly_cap_usd: 5.0
  batch_size: 40
  site_title: "GenAI Signal"
  site_url: "[VERIFY after Pages setup]"
  llm:
    primary: claude_p          # claude -p, subscription
    claude_model: haiku
    fallback1: gemini
    gemini_model: gemini-3.1-flash-lite-preview
    fallback2: anthropic_api
    anthropic_model: claude-haiku-4-5-20251001

keywords:   # discovery filters (HN/Google News must match roster name OR one of these)
  - frontier model
  - AI safety
  - AI policy
  - AI economics
  - inference cost
  - compute scaling
  - AGI

discovery:
  hn_enabled: true
  gnews_queries:
    - '"Anthropic"'
    - '"Google DeepMind"'
  lab_blogs:
    deepmind_blog: { type: rss, url: "https://deepmind.google/blog/rss.xml" }   # [VERIFY]
    openai_news:   { type: rss, url: "https://openai.com/news/rss.xml" }        # [VERIFY]
    google_ai:     { type: rss, url: "https://blog.google/technology/ai/rss/" } # [VERIFY]
  newsletters:
    import_ai:     { type: rss, url: "https://importai.substack.com/feed" }
    the_batch:     { type: rss, url: "https://www.deeplearning.ai/the-batch/feed/" } # [VERIFY]
    interconnects: { type: rss, url: "https://www.interconnects.ai/feed" }
    epoch_ai:      { type: rss, url: "https://epoch.ai/feed.xml" }              # [VERIFY]
```

### 4.2 `config/roster.yaml` — seed content (verbatim; owner edits later)

Schema: top-level groups `researchers | lab_leaders | economists | policymakers | thinkers`; each entry keyed by slug with `name` (required) and any of: `blog_rss`, `bluesky`, `arxiv_query`, `youtube_channel_id`, `x_handle` (informational only in v1), `gnews` (bool — add a Google News query for the name).

```yaml
lab_leaders:
  demis_hassabis: { name: "Demis Hassabis", gnews: true, x_handle: "demishassabis",
                    bluesky: "[VERIFY]" }
  dario_amodei:   { name: "Dario Amodei", gnews: true,
                    blog_rss: "https://darioamodei.com/feed.xml" }   # [VERIFY]
  sam_altman:     { name: "Sam Altman", gnews: true, x_handle: "sama",
                    blog_rss: "https://blog.samaltman.com/posts.atom" } # [VERIFY]
  yann_lecun:     { name: "Yann LeCun", gnews: true, bluesky: "yann-lecun.bsky.social" } # [VERIFY]
researchers:
  andrej_karpathy: { name: "Andrej Karpathy", gnews: true, x_handle: "karpathy",
                     blog_rss: "https://karpathy.bearblog.dev/feed/",   # [VERIFY]
                     youtube_channel_id: "[VERIFY]" }
  ilya_sutskever:  { name: "Ilya Sutskever", gnews: true,
                     arxiv_query: 'au:"Ilya Sutskever"' }
  francois_chollet:{ name: "François Chollet", gnews: true,
                     bluesky: "fchollet.bsky.social" }  # [VERIFY]
  neel_nanda:      { name: "Neel Nanda",
                     bluesky: "[VERIFY]", arxiv_query: 'au:"Neel Nanda"' }
  jan_leike:       { name: "Jan Leike", bluesky: "janleike.bsky.social", # [VERIFY]
                     blog_rss: "https://aligned.substack.com/feed" }
economists:
  erik_brynjolfsson: { name: "Erik Brynjolfsson", gnews: true,
                       blog_rss: "[VERIFY: digitaleconomy.stanford.edu feed]" }
  daron_acemoglu:    { name: "Daron Acemoglu", gnews: true }
  tyler_cowen:       { name: "Tyler Cowen",
                       blog_rss: "https://marginalrevolution.com/feed" }
  anton_korinek:     { name: "Anton Korinek", gnews: true,
                       arxiv_query: 'au:"Anton Korinek"' }
policymakers:
  dean_ball:      { name: "Dean Ball",
                    blog_rss: "https://www.hyperdimensional.co/feed" }  # [VERIFY]
  jack_clark:     { name: "Jack Clark",
                    blog_rss: "https://importai.substack.com/feed",
                    bluesky: "jackclark.bsky.social" }  # [VERIFY]
  miles_brundage: { name: "Miles Brundage",
                    blog_rss: "https://milesbrundage.substack.com/feed", # [VERIFY]
                    bluesky: "[VERIFY]" }
  helen_toner:    { name: "Helen Toner", gnews: true,
                    blog_rss: "https://helentoner.substack.com/feed" }   # [VERIFY]
thinkers:
  zvi_mowshowitz: { name: "Zvi Mowshowitz",
                    blog_rss: "https://thezvi.substack.com/feed" }
  ethan_mollick:  { name: "Ethan Mollick",
                    blog_rss: "https://www.oneusefulthing.org/feed",
                    bluesky: "emollick.bsky.social" }  # [VERIFY]
  simon_willison: { name: "Simon Willison",
                    blog_rss: "https://simonwillison.net/atom/everything/" }
  nathan_lambert: { name: "Nathan Lambert",
                    blog_rss: "https://www.interconnects.ai/feed",
                    bluesky: "natolambert.bsky.social" }  # [VERIFY]
  gwern:          { name: "Gwern Branwen",
                    blog_rss: "https://gwern.net/feed.xml" }  # [VERIFY]
```

Loader requirements (`models.load_roster`, `models.load_sources`): validate group names and required `name` field; a value containing the substring `[VERIFY]` is treated as **absent** (source skipped, counted in the run summary as `unverified_skipped`), never fetched.

---

## 5. Tasks

Dependency order: **T-01 first**; then T-02, T-03, T-04, T-05 in parallel (disjoint files); then T-06; then T-07. T-08 optional/deferred.

---

### T-01 — Scaffold, models, config

**Files:** `pyproject.toml`, `.gitignore`, `config/roster.yaml`, `config/sources.yaml`, `digest/__init__.py`, `digest/models.py`, `tests/test_models.py`, empty `state/.gitkeep`, `docs/.nojekyll`.
**Behavior:** repo scaffold per §2; `Item` + `canonical_url` + `item_id` per §3; YAML loaders per §4 returning typed dicts; `.gitignore` covers `.venv/`, `state/*` except `state/ledger.jsonl`, `__pycache__`. `pyproject.toml`: project `digest`, deps `feedparser, PyYAML, requests`, dev dep `pytest`.
**Tests (`test_models.py`):** canonical_url strips utm/ref/fragment/trailing slash and lowercases host (≥4 cases); same canonical → same id, different → different; roster loader skips `[VERIFY]` values and rejects an unknown group name; sources loader returns settings with expected defaults.
**Do NOT:** implement any fetching, scoring, or rendering; add extra config keys.

---

### T-02 — Fetch adapters + `check_sources`

**Files:** `digest/fetch.py`, `tests/test_fetch.py`, `tests/fixtures/*` (create fixtures you need: sample RSS xml, arXiv atom, bluesky JSON, HN JSON, gnews RSS).
**Behavior:** one function per source type, all with signature `(config_entry, since: datetime, fetch: Callable[[str], str]) -> list[Item]`: `fetch_rss`, `fetch_arxiv`, `fetch_bluesky`, `fetch_hn`, `fetch_gnews`, `fetch_youtube` (YouTube = RSS under the hood). A top-level `gather(roster, sources, since, fetch=default_fetch) -> tuple[list[Item], list[Gap]]` walks all configured sources, applies landmines #6–#12, filters to `published >= since`, catches per-source exceptions into `Gap(source_key, error)` records. Default `fetch` = `requests.get` with 15 s timeout and UA header `genai-digest/1.0`. HN/gnews items only kept if title/text matches a roster name or a `keywords` entry (case-insensitive substring). Bluesky post text → `title` = first 120 chars, `text` = full.
CLI: `python -m digest.fetch --check` hits every configured URL/handle live, prints `OK/FAIL <source_key> <detail>` lines and exits 0 (report tool, not a gate).
**Tests (`test_fetch.py`):** each adapter parses its fixture into correct `Item` fields (spot-check url/title/published/UTC-ness); missing dates skipped; `since` filter works; `gather` records a Gap and continues when injected fetch raises for one source; roster/keyword filter for HN.
**Do NOT:** call the real network in tests; implement retries beyond one; touch dedupe/score/render.

---

### T-03 — Dedupe, cluster, seen-store

**Files:** `digest/dedupe.py`, `tests/test_dedupe.py`.
**Behavior:**
- `drop_seen(items, seen_path) -> list[Item]`: `state/seen.json` maps item id → first-seen ISO date; unseen items pass; `record_seen(items, seen_path)` adds them; `prune_seen(seen_path, days=45)` drops old entries. Missing/corrupt file → start empty (log warning).
- `cluster(items) -> list[list[Item]]`: same id → same cluster; else same-cluster if `difflib.SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio() > 0.85`. Greedy single pass is fine. Cluster representative = earliest `published`; keep others as `also_links`.
**Tests:** near-duplicate titles cluster, distinct don't; representative is earliest; seen round-trip (drop → record → drop again = empty); prune removes only old; corrupt seen.json tolerated.
**Do NOT:** use embeddings/LLM for similarity; add a database.

---

### T-04 — LLM scoring cascade + spend guardrail

**Files:** `digest/score.py`, `tests/test_score.py`.
**Behavior:** `score_items(clusters, settings, roster) -> tuple[list[ScoredItem], RunCost]`.
- Build batches (≤ `batch_size` representatives). Prompt template (verbatim, fill the `{}` slots):

  ```
  You are filtering a personal daily GenAI digest. Roster of people I follow:
  {names by group}.
  Rubric — an item is noteworthy (score >= 6) only if BOTH hold:
  (a) it is from or clearly about a roster person, or a frontier-lab/major-policy
      development of the kind they would weigh in on;
  (b) it is substantive: new capability or model, safety or policy stance,
      serious economic analysis, notable prediction or call-to-action,
      or significant performance/cost result — NOT routine chatter, memes,
      hiring, or promo.
  Score every item 0-10. For each, write a 1-2 sentence factual summary and a
  one-line "why it matters". Items:
  {numbered items: id, source, person?, title, first 500 chars of text}
  ```
- JSON schema for structured output: `{"items": [{"id": str, "score": int, "summary": str, "why_matters": str}]}` (all required).
- Cascade per landmines #1–#3, #14: `claude_p` → `gemini` → `anthropic_api` (plain `requests` POST to `https://api.anthropic.com/v1/messages`, header `x-api-key` — only if `ANTHROPIC_API_KEY_FALLBACK` env var is set; note the distinct name, so the real var never exists). Each engine: one retry, then next engine. All engines fail → mark run degraded: keep clusters whose person is set (roster-authored), score = None, no summaries.
- Guardrail: before using `anthropic_api`, sum current-month `est_cost_usd` from `state/ledger.jsonl`; if ≥ `monthly_cap_usd`, skip it (log `cap_reached`). After scoring, append ledger line `{date, engine, items, est_cost_usd}` (claude_p/gemini → 0.0; anthropic_api → estimate from usage fields at $1/M input, $5/M output).
- Post-filter: keep `score >= score_threshold`, sort desc, cap `keep_top`.
**Tests:** prompt builder includes roster names + item ids; response parsing maps scores back to clusters by id and tolerates missing ids; cascade falls through on failure (mock engines as injected callables); cap check blocks paid engine (fixture ledger at $5); degraded mode keeps roster-authored items; ledger line appended. Engines must be injectable — tests never spawn `claude` or hit HTTP.
**Do NOT:** call any real LLM in tests; set/read `ANTHROPIC_API_KEY`; score items one call per item.

---

### T-05 — Renderers

**Files:** `digest/render.py`, `tests/test_render.py`.
**Behavior:** `render_all(scored, gaps, run_meta, settings, out_dir="docs")` writes:
- `docs/index.html` — self-contained, inline CSS, **zero JS**, mobile-first (`meta viewport`, single column, 16px+ base font, `prefers-color-scheme` dark mode). Header: site title + digest date (PT) + item count. Sections in fixed order: Lab Leaders, Researchers, Economists, Policymakers, Thinkers, Discovery — omit empty sections. Each item: title as link to original `url` (always; never render an item without its link), byline `person/source · time PT`, summary para, *why it matters* line in italic, `also:` links for cluster members. Footer: run counts, gap list (source_key: error), link to `feed.xml` and `digest.md`.
- `docs/digest.md` — same content in markdown.
- `docs/feed.xml` — Atom, hand-built with `xml.sax.saxutils.escape`: feed id/link = `site_url`, one entry **per digest day** (`id = site_url + "#" + YYYY-MM-DD`), entry content = the day's item list as escaped HTML.
All three fully overwritten each run (idempotent).
**Tests:** every rendered item's `href` equals its `Item.url`; section order and omission; feed.xml parses with `feedparser` (well-formed, 1 entry, right date); dark-mode media query present; gap register appears in footer; empty-digest day renders a valid "nothing noteworthy" page.
**Do NOT:** add JS, external CSS/fonts/CDNs, or an HTML template engine.

---

### T-06 — Pipeline runner

**Files:** `digest/run.py`, `tests/test_run.py`.
**Behavior:** `python -m digest.run [--date YYYY-MM-DD] [--dry-run]`. Steps: load config → `since = now - window_hours` → `fetch.gather` → `dedupe.drop_seen` → `cluster` → `score_items` → `render_all` → `record_seen` + `prune_seen` → write `state/run_summary.json` `{date, fetched, unverified_skipped, after_seen, clusters, scored_kept, engine, est_cost_usd, gaps: [...], duration_s}` and log the same object. `--dry-run`: no seen-store writes, render to `state/preview/`. Wire the JSONL logging setup here (ground rules). Any single stage's per-source errors accumulate as gaps; only unrecoverable pipeline errors exit non-zero.
**Tests:** end-to-end with injected fake fetch + fake LLM engine over fixtures → docs files exist, summary counts correct; second run same day = same output, nothing re-kept (idempotent + seen); dry-run leaves seen.json untouched.
**Do NOT:** invoke git, launchd, or real network/LLM.

---

### T-07 — Publish script, launchd, README

**Files:** `scripts/run_daily.sh`, `scripts/install_launchd.sh`, `ops/com.redwan.genai-digest.plist`, `README.md`.
**Behavior:**
- `run_daily.sh` (zsh/bash, BSD-safe, `set -euo pipefail`): cd to repo (absolute path), `.venv/bin/python -m digest.run`, then `git add docs state/ledger.jsonl && git commit -m "digest: $(date +%F)" && git push` — skip commit cleanly if no changes. All output appended to `state/logs/launchd.log`.
- Plist: label `com.redwan.genai-digest`, `ProgramArguments` → the script, `StartCalendarInterval` Hour 20 Minute 0, explicit `PATH` env including `/opt/homebrew/bin`, `WorkingDirectory` repo path, `StandardOutPath`/`StandardErrorPath` under `state/logs/`.
- `install_launchd.sh`: copies/loads plist per landmine #5, prints verification command (`launchctl print gui/$UID/com.redwan.genai-digest | head`).
- `README.md`: what it is, one-time setup (venv, `pip install -e .`, GitHub Pages `/docs` setting, install_launchd), how to edit the roster (with the `[VERIFY]`/check_sources workflow), how to run manually, monthly cost picture ($0 expected, cap $5), troubleshooting (missed runs fire on wake; gap register; where logs live).
**Tests:** none automated beyond `bash -n` both scripts and `plutil -lint` the plist (run these, paste output in report).
**Do NOT:** run `launchctl bootstrap` yourself; push to any remote; put `ANTHROPIC_API_KEY` anywhere.

---

### T-08 (OPTIONAL, deferred) — X best-effort adapter

Nitter-instance RSS per `x_handle` with 10 s timeout, wired as one more adapter in `fetch.py`, failures → gap register, instance URL in `sources.yaml`. Do not build unless explicitly dispatched.

---

## 6. Orchestrator-owned steps (agents: skip)

GitHub repo creation + Pages enablement; filling `site_url`; resolving `[VERIFY]` entries after running `--check`; PR review + merge; first live run; `claude setup-token` never needed for local launchd (uses logged-in session).

## 7. Agent report format

Branch, PR URL, test command + pass counts per suite, fixtures added, any deviation from this spec and why (deviations require quoting the spec line you deviated from).
