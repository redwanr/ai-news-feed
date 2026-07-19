# Build me a daily GenAI "signal" digest

## What I want
A personal, once-a-day digest of genuinely noteworthy Generative-AI developments from a hand-defined set of high-signal people — reputable AI researchers, frontier-lab leaders, economists studying AI's impact, policymakers, and influential thinkers, performance and cost related work. I want to open one page each morning on my **iPhone and MacBook** and know I haven't missed anything important these people said or published in the last 24 hours trending up rapidly (e.g., a Demis Hassabis post on how we should prepare for what's coming, Datio amodei's essays, dean ball's Kimi K3 impact, notable economic works such by stanford digital economy lab... just to give you some ideas).

This is a solo personal tool. Optimize for signal and low cost, not scale.

## Hard constraints (do not violate)
- **Cost: $0 preferred, $5/month absolute max.** No paid data APIs. Specifically, do NOT use the paid X/Twitter API.
- **Cadence: once daily** — a single scheduled run each morning. No real-time infra.
- **Delivery: one mobile-first web page** readable on iPhone Safari and MacBook, plus a plain-markdown and an RSS/Atom version of the same digest.
- **Runs fully unattended** on a free scheduler.

## Design first, build second
Before writing code, produce a **1-page design doc**: proposed architecture, the exact source list, which LLM you'll use and its real cost, and any tradeoffs/risks. Ask me anything ambiguous, wait for my OK, THEN scaffold and implement. Put the repo under `~/ClaudeProjects`.

## Sources — free / cheap only
Build a config-driven acquisition layer. Prefer free feeds and open APIs:
- **RSS/Atom**: personal blogs, Substacks, and lab blogs (Google DeepMind, OpenAI, Anthropic, Google AI, Meta AI, etc.), plus curator newsletters (e.g., Import AI, The Batch) that already surface noteworthy posts and tweets.
- **arXiv API** (free): `cs.AI` / `cs.LG` / `cs.CL`, filtered by my roster authors and by topic.
- **Bluesky API** (free/open): many AI researchers post here now — treat this as the primary route for "what did person X just say."
- **YouTube channel RSS** (free, per channel) for talks/interviews.
- **Hacker News (Algolia API) + Google News RSS** (free) as discovery supplements, filtered hard by roster + keywords.
- **X/Twitter**: no paid API. Attempt free/best-effort routes only, and treat X as optional and allowed-to-fail — rely on Bluesky + newsletters to catch most of the same signal. Mark X coverage as best-effort in the design doc.

## The roster (who counts)
Create an editable **YAML** file that is the single source of truth for "who I care about," grouped into: researchers, lab leaders, economists, policymakers, thinkers. Each entry lists the person and their known handles/feeds (blog RSS, Bluesky handle, arXiv author id, YouTube channel, X handle where known). Seed it with a solid starter set across all five categories (include Demis Hassabis and similarly prominent figures), but make add/remove trivial. Keep all URLs and handles in config, never hardcoded in logic.

## Filtering + ranking (the LLM step)
Each run: gather last-24h candidates → dedupe and cluster near-duplicates → score each item with a cheap LLM against a noteworthiness rubric → keep the top items → write a 1–2 sentence summary and a one-line "why it matters" for each.

Rubric for "noteworthy": is it from or clearly about someone on the roster, AND is it a substantive development (new capability/model, a safety or policy stance, serious economic analysis, a notable prediction or call-to-action) rather than routine chatter, memes, or promo?

LLM choice — a cascade, cheapest-capable path first:
1. **Primary: `claude -p` (Claude Code headless / print mode)**, running on my Claude subscription — I already have the Claude Code terminal app installed. Use `--output-format json` (and `--json-schema` if available) for the relevance-scoring step so I get structured, parseable output.
   - **Auth gotcha for unattended runs:** in `-p` mode, if `ANTHROPIC_API_KEY` is set it is used *in preference to* the subscription and bills per token — so do NOT set it. For scheduled/headless auth, generate a subscription OAuth token with `claude setup-token` and pass it as `CLAUDE_CODE_OAUTH_TOKEN`. When the job runs locally on my Mac while I'm logged in, `claude -p` just uses my existing session.
   - **Caveat:** Anthropic's coverage of automated headless runs under a subscription is billing-sensitive and may change — so keep the fallbacks below live, and never let a run silently spend money.
2. **Fallback: Google Gemini Flash free tier** (AI Studio API). If my Gemini Flash delegation router already exists in `~/bin`, reuse it.
3. **Fallback: Claude Haiku via the Anthropic API** — pennies/month at this volume.

Add a hard monthly spend guardrail (default: refuse any paid path past a set cap), and log which model handled each run plus its estimated cost.

## Output + hosting + schedule (all free)
- Render one **self-contained, mobile-first HTML page** (readable, minimal JS), grouped by category, each item linking to its **original source** — provenance matters, always link out, never show an item without its source.
- Also write `digest.md` and an RSS/Atom feed.
- **Host on GitHub Pages** (or Cloudflare/Netlify free tier) so it's reachable from iPhone + MacBook at a stable URL.
- **Schedule once each morning** in my timezone (`America/Los_Angeles`). Pick one in the design doc:
  - **Local (simplest for `claude -p` auth):** a `launchd`/cron job on my MacBook that runs while I'm logged in, uses my existing Claude Code session, then pushes the built page to GitHub Pages.
  - **GitHub Actions cron (fully hands-off):** needs my `CLAUDE_CODE_OAUTH_TOKEN` in repo secrets (from `claude setup-token`) for the `claude -p` step, and must NOT set `ANTHROPIC_API_KEY`.

## Engineering quality
- Config-driven (roster + sources in editable files); idempotent daily run.
- A persistent "seen" store so items never repeat across days.
- Graceful degradation: if any source is down, log it to a failure/gap register and continue — never crash the run.
- Structured logging and a short run summary (counts in / kept / estimated cost).
- Python 3.11, clean modules, README with setup + how to edit the roster.
- Keep it simple and cheap to maintain — personal tool, not a product.

## Deliverables
1. The 1-page design doc + cost estimate (first, for my sign-off).
2. Repo scaffold under `~/ClaudeProjects`.
3. Working end-to-end daily run producing the hosted HTML page (+ `digest.md` + RSS).
4. README covering setup, editing the roster, and the monthly cost picture.
