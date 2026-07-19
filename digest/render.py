"""Renderers for the GenAI Daily Digest: HTML, Markdown, and Atom feed.

`render_all` is intentionally decoupled from `digest.score` / `digest.fetch`:
it never imports `ScoredItem` or `Gap` and instead reads whatever attributes
(or dict keys) it needs defensively via `_get`. This keeps T-05 independent
of the exact shapes T-02/T-04 land with.

No JS, no external CSS/fonts/CDNs, no template engine — just f-strings and
`string.Template`-style formatting over stdlib.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from html import escape as h_escape
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as x_escape
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")

# Fixed section order per SPEC §T-05. Keys match Item.category / roster group names.
SECTION_ORDER: list[tuple[str, str]] = [
    ("lab_leaders", "Lab Leaders"),
    ("researchers", "Researchers"),
    ("economists", "Economists"),
    ("policymakers", "Policymakers"),
    ("thinkers", "Thinkers"),
    ("discovery", "Discovery"),
]
_KNOWN_CATEGORIES = {key for key, _ in SECTION_ORDER}

# Order in which run_meta counters are shown in the footer, if present.
_RUN_META_KEYS = [
    "fetched",
    "unverified_skipped",
    "after_seen",
    "clusters",
    "scored_kept",
    "engine",
    "est_cost_usd",
    "duration_s",
]


# --------------------------------------------------------------------------
# Defensive accessors
# --------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read an attribute (or dict key) from a scored-item / gap-like object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_pt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PT)


def _fmt_time_pt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    local = _to_pt(dt)
    hm = local.strftime("%I:%M %p").lstrip("0")
    return f"{hm} PT"


def _fmt_day(dt: datetime) -> str:
    return _to_pt(dt).strftime("%Y-%m-%d")


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _display_person(person: str | None, source_key: str | None) -> str:
    if person:
        return person.replace("_", " ").replace("-", " ").title()
    return source_key or "unknown"


def _resolve_digest_date(run_meta: dict[str, Any]) -> str:
    """Return a 'YYYY-MM-DD' label for the header, from run_meta['date'] if given."""
    raw = run_meta.get("date") if run_meta else None
    if isinstance(raw, datetime):
        return _fmt_day(raw)
    if isinstance(raw, date):
        return raw.isoformat()
    if isinstance(raw, str) and raw:
        return raw
    return datetime.now(PT).strftime("%Y-%m-%d")


def _section_for(item: Any) -> str:
    category = _get(item, "category")
    if category in _KNOWN_CATEGORIES:
        return category
    return "discovery"


def _group_by_section(scored: list[Any]) -> list[tuple[str, str, list[Any]]]:
    """Return (key, label, items) for non-empty sections, in fixed order."""
    buckets: dict[str, list[Any]] = defaultdict(list)
    for item in scored:
        buckets[_section_for(item)].append(item)

    def _sort_key(item: Any) -> tuple[int, str]:
        score = _get(item, "score")
        published = _get(item, "published")
        # Higher score first, then more recent first.
        score_key = -(score if isinstance(score, (int, float)) else 0)
        pub_key = published.isoformat() if isinstance(published, datetime) else ""
        return (score_key, pub_key)

    result = []
    for key, label in SECTION_ORDER:
        items = buckets.get(key, [])
        if not items:
            continue
        items = sorted(items, key=_sort_key)
        result.append((key, label, items))
    return result


def _normalize_gaps(gaps: list[Any] | None) -> list[tuple[str, str]]:
    out = []
    for g in gaps or []:
        source_key = _get(g, "source_key", "?")
        error = _get(g, "error", "?")
        out.append((str(source_key), str(error)))
    return out


def _also_links(item: Any) -> list[tuple[str, str]]:
    """Return (title, url) pairs for cluster members, excluding the item itself."""
    links = _get(item, "also_links", None) or []
    out = []
    for link in links:
        title = _get(link, "title", "") or ""
        url = _get(link, "url", "") or ""
        if url:
            out.append((str(title), str(url)))
    return out


# --------------------------------------------------------------------------
# Per-item rendering (HTML fragment shared by index.html and feed.xml content)
# --------------------------------------------------------------------------


def _render_item_html(item: Any) -> str:
    title = str(_get(item, "title", "") or "(untitled)")
    url = str(_get(item, "url", "") or "")
    person = _get(item, "person")
    source_key = _get(item, "source_key")
    published = _get(item, "published")
    summary = str(_get(item, "summary", "") or "")
    why_matters = str(_get(item, "why_matters", "") or "")

    byline = f"{_display_person(person, source_key)} · {_fmt_time_pt(published)}"
    also = _also_links(item)

    parts = [
        "<article>",
        f'  <h3><a href="{h_escape(url, quote=True)}">{h_escape(title)}</a></h3>',
        f'  <p class="byline">{h_escape(byline)}</p>',
    ]
    if summary:
        parts.append(f'  <p class="summary">{h_escape(summary)}</p>')
    if why_matters:
        parts.append(f'  <p class="why"><em>Why it matters: {h_escape(why_matters)}</em></p>')
    if also:
        links_html = ", ".join(
            f'<a href="{h_escape(u, quote=True)}">{h_escape(t or u)}</a>' for t, u in also
        )
        parts.append(f'  <p class="also">also: {links_html}</p>')
    parts.append("</article>")
    return "\n".join(parts)


def _render_item_md(item: Any) -> str:
    title = str(_get(item, "title", "") or "(untitled)")
    url = str(_get(item, "url", "") or "")
    person = _get(item, "person")
    source_key = _get(item, "source_key")
    published = _get(item, "published")
    summary = str(_get(item, "summary", "") or "")
    why_matters = str(_get(item, "why_matters", "") or "")

    def _md_escape(s: str) -> str:
        return s.replace("[", "\\[").replace("]", "\\]")

    byline = f"{_display_person(person, source_key)} · {_fmt_time_pt(published)}"
    also = _also_links(item)

    lines = [f"### [{_md_escape(title)}]({url})", "", f"*{byline}*", ""]
    if summary:
        lines.append(summary)
        lines.append("")
    if why_matters:
        lines.append(f"*Why it matters: {why_matters}*")
        lines.append("")
    if also:
        links_md = ", ".join(f"[{_md_escape(t or u)}]({u})" for t, u in also)
        lines.append(f"also: {links_md}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

_CSS = """\
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 1rem;
  max-width: 40rem;
  margin-left: auto;
  margin-right: auto;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 18px;
  line-height: 1.5;
  background: #ffffff;
  color: #1a1a1a;
}
header { margin-bottom: 1.5rem; }
header h1 { font-size: 1.4rem; margin: 0 0 0.25rem 0; }
header p { margin: 0; color: #555; }
section { margin-bottom: 2rem; }
section h2 {
  font-size: 1.1rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-bottom: 1px solid #ddd;
  padding-bottom: 0.3rem;
}
article { margin-bottom: 1.5rem; }
article h3 { margin: 0 0 0.2rem 0; font-size: 1.05rem; }
article h3 a { color: #0645ad; text-decoration: none; }
article h3 a:hover { text-decoration: underline; }
p.byline { margin: 0 0 0.4rem 0; font-size: 0.85rem; color: #666; }
p.summary { margin: 0 0 0.4rem 0; }
p.why { margin: 0 0 0.4rem 0; color: #444; }
p.also { margin: 0; font-size: 0.85rem; }
p.also a { color: #0645ad; }
.empty-notice { color: #555; font-style: italic; }
footer {
  margin-top: 2rem;
  padding-top: 1rem;
  border-top: 1px solid #ddd;
  font-size: 0.8rem;
  color: #666;
}
footer ul { padding-left: 1.2rem; }
footer a { color: #0645ad; }

@media (prefers-color-scheme: dark) {
  body { background: #121212; color: #e6e6e6; }
  header p { color: #aaa; }
  section h2 { border-bottom-color: #333; }
  article h3 a { color: #8ab4f8; }
  p.byline { color: #999; }
  p.why { color: #ccc; }
  p.also a { color: #8ab4f8; }
  footer { border-top-color: #333; color: #999; }
  footer a { color: #8ab4f8; }
}
"""


def _render_footer_html(gap_list: list[tuple[str, str]], run_meta: dict[str, Any]) -> str:
    counts = [f"{k}: {run_meta[k]}" for k in _RUN_META_KEYS if k in (run_meta or {})]
    parts = ["<footer>"]
    if counts:
        parts.append(f"  <p>{h_escape(' · '.join(counts))}</p>")
    if gap_list:
        parts.append("  <p>Gaps:</p>")
        parts.append("  <ul>")
        for source_key, error in gap_list:
            parts.append(f"    <li>{h_escape(source_key)}: {h_escape(error)}</li>")
        parts.append("  </ul>")
    parts.append('  <p><a href="feed.xml">Atom feed</a> · <a href="digest.md">Markdown</a></p>')
    parts.append("</footer>")
    return "\n".join(parts)


def _render_html(
    site_title: str,
    digest_date: str,
    scored: list[Any],
    sections: list[tuple[str, str, list[Any]]],
    gap_list: list[tuple[str, str]],
    run_meta: dict[str, Any],
) -> str:
    count = len(scored)
    header = f"""<header>
  <h1>{h_escape(site_title)}</h1>
  <p>{h_escape(digest_date)} (PT) &middot; {count} item{"" if count == 1 else "s"}</p>
</header>"""

    if sections:
        body_parts = []
        for _key, label, items in sections:
            items_html = "\n".join(_render_item_html(it) for it in items)
            body_parts.append(f"<section>\n  <h2>{h_escape(label)}</h2>\n{items_html}\n</section>")
        main = "\n".join(body_parts)
    else:
        main = '<p class="empty-notice">Nothing noteworthy today.</p>'

    footer = _render_footer_html(gap_list, run_meta)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h_escape(site_title)} — {h_escape(digest_date)}</title>
<style>
{_CSS}</style>
</head>
<body>
{header}
<main>
{main}
</main>
{footer}
</body>
</html>
"""


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def _render_footer_md(gap_list: list[tuple[str, str]], run_meta: dict[str, Any]) -> str:
    counts = [f"{k}: {run_meta[k]}" for k in _RUN_META_KEYS if k in (run_meta or {})]
    lines = ["---", ""]
    if counts:
        lines.append(" · ".join(counts))
        lines.append("")
    if gap_list:
        lines.append("Gaps:")
        lines.append("")
        for source_key, error in gap_list:
            lines.append(f"- {source_key}: {error}")
        lines.append("")
    lines.append("[Atom feed](feed.xml) · [Markdown](digest.md)")
    return "\n".join(lines)


def _render_markdown(
    site_title: str,
    digest_date: str,
    scored: list[Any],
    sections: list[tuple[str, str, list[Any]]],
    gap_list: list[tuple[str, str]],
    run_meta: dict[str, Any],
) -> str:
    count = len(scored)
    lines = [f"# {site_title}", "", f"{digest_date} (PT) · {count} item{'' if count == 1 else 's'}", ""]

    if sections:
        for _key, label, items in sections:
            lines.append(f"## {label}")
            lines.append("")
            for it in items:
                lines.append(_render_item_md(it))
    else:
        lines.append("_Nothing noteworthy today._")
        lines.append("")

    lines.append(_render_footer_md(gap_list, run_meta))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Atom feed
# --------------------------------------------------------------------------


def _attr_escape(s: str) -> str:
    return x_escape(s, {'"': "&quot;"})


def _render_feed(site_title: str, site_url: str, scored: list[Any]) -> str:
    by_day: dict[str, list[Any]] = defaultdict(list)
    for item in scored:
        published = _get(item, "published")
        day = _fmt_day(published) if isinstance(published, datetime) else "unknown"
        by_day[day].append(item)

    now_iso = _iso_utc(datetime.now(timezone.utc))
    entries = []
    for day in sorted(by_day):
        items = by_day[day]
        pub_dates = [_get(it, "published") for it in items if isinstance(_get(it, "published"), datetime)]
        updated = _iso_utc(max(pub_dates)) if pub_dates else now_iso
        inner_html = "\n".join(_render_item_html(it) for it in items)
        entry_id = f"{site_url}#{day}"
        entries.append(
            "  <entry>\n"
            f"    <id>{x_escape(entry_id)}</id>\n"
            f"    <title>{x_escape(site_title)} — {x_escape(day)}</title>\n"
            f'    <link href="{_attr_escape(site_url)}"/>\n'
            f"    <updated>{updated}</updated>\n"
            f'    <content type="html">{x_escape(inner_html)}</content>\n'
            "  </entry>"
        )

    body = "\n".join(entries)
    header = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <title>{x_escape(site_title)}</title>\n"
        f"  <id>{x_escape(site_url)}</id>\n"
        f'  <link href="{_attr_escape(site_url)}"/>\n'
        f"  <updated>{now_iso}</updated>\n"
    )
    return header + (f"{body}\n" if body else "") + "</feed>\n"


# --------------------------------------------------------------------------
# Public entrypoint
# --------------------------------------------------------------------------


def render_all(
    scored: list[Any],
    gaps: list[Any],
    run_meta: dict[str, Any],
    settings: dict[str, Any],
    out_dir: str | Path = "docs",
) -> None:
    """Render index.html, digest.md, and feed.xml into out_dir (fully overwritten)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    settings = settings or {}
    run_meta = run_meta or {}
    site_title = str(settings.get("site_title", "GenAI Signal"))
    site_url = str(settings.get("site_url", "") or "")

    digest_date = _resolve_digest_date(run_meta)
    sections = _group_by_section(scored)
    gap_list = _normalize_gaps(gaps)

    html_text = _render_html(site_title, digest_date, scored, sections, gap_list, run_meta)
    md_text = _render_markdown(site_title, digest_date, scored, sections, gap_list, run_meta)
    feed_text = _render_feed(site_title, site_url, scored)

    (out / "index.html").write_text(html_text, encoding="utf-8")
    (out / "digest.md").write_text(md_text, encoding="utf-8")
    (out / "feed.xml").write_text(feed_text, encoding="utf-8")
