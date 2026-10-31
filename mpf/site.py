"""Builds the static website from every scored item, grouped into tiers.

Output (folder site/):
  index.html   the project list (works without JavaScript; JavaScript adds live
               countdowns, the "closing soon" list, tier toggles, filters and search)
  about.html   what the tool does, tiers, sources, limits
  items.json   the published records

What gets published:
  - every scored item from the live sources (the DFO test set never appears),
  - except rows you set to "no" in review/review.csv (hidden everywhere),
  - except items whose page did not load or shows a different project (bad link),
  - with duplicates merged (the preferred source is kept, see config duplicates.prefer),
  - and only the Strong match, Possible and Weak tiers. "Probably not relevant" items stay
    in the local review file only and never reach the site or its data files.
Content rules: short exact quotes only, every item links to its official notice,
missing facts show "unknown", nothing says mitigation "will be required".
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from html import escape
from pathlib import Path

from mpf.comments import NOT_PUBLISHED, comment_info
from mpf.history import CHANGE_LABELS
from mpf.news import GDELT_URL
from mpf.coverage import STATUS_LABELS, load_coverage, source_names
from mpf.models import UNKNOWN
from mpf.review import auto_summary, decision, own_summary
from mpf.tiers import TIER_LABELS, TIER_ORDER, find_duplicates, tier_for

# Tiers that appear on the public site. "notrel" stays in the local review file only.
PUBLISHED_TIERS = ("strong", "possible", "weak")

SOURCE_NAMES = {
    "iaac": "Canadian Impact Assessment Registry",
    "nl_ea": "Newfoundland and Labrador environmental assessment",
    "drop": "Added by hand from a public notice",
}

# Contact lines are not evidence, and they often name individual officials. Never quote them.
_CONTACT = re.compile(r"submitted to|send (your )?comments|comments? (should|must|may) be|contact|e-?mail|@|"
                      r"\bphone\b|\btel\b|\bfax\b|P\.?O\.? Box|courriel|téléphone|adress(e|ed) to", re.IGNORECASE)


# Abbreviations that end with a full stop but do not end a sentence ("St. John's", "approx. 20 m").
_ABBREV = re.compile(r"(?:\b(?:St|Ste|Mr|Mrs|Ms|Dr|No|Nos|approx|Approx|e\.g|i\.e|Inc|Ltd|Co|Corp|Fig|Figs|vs|ca)|\b[A-Z])$")
_BOUNDARY = re.compile(r"[.!?][\"”»)]?(?=\s+[\"“«(A-ZÀ-Ý0-9])|\n+")
# Page headers and report codes that PDF extraction glues onto sentences ("Page 6 ... JBC-23-C-192").
_PDF_JUNK = re.compile(r"\bPage \d+\b|\b[A-Z]{2,}-\d{2,}(-[A-Z0-9]+)+\b|\b\d+(\.\d+){2,}\s+[A-Z]")
_GOOD_START = re.compile(r"^[\"“«(A-ZÀ-Ý0-9]")
_GOOD_END = re.compile(r"[.!?][\"”»)]?$")


def split_sentences(text: str) -> list[str]:
    """Whole sentences, each an exact piece of the text."""
    out, start = [], 0
    for m in _BOUNDARY.finditer(text):
        if m.group(0).startswith("\n"):
            end, nxt = m.start(), m.end()
        else:
            if _ABBREV.search(text[start:m.start()]):
                continue
            end, nxt = m.end(), m.end()
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        start = nxt
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _mostly_acronyms(s: str) -> bool:
    """Abbreviation lists ("NLDECCC ... NL ESA NL ...") are not readable evidence."""
    words = re.findall(r"[A-Za-z]{2,}", s)
    caps = sum(1 for w in words if w.isupper())
    return caps >= 4 and caps / max(1, len(words)) > 0.15


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def _word_hits(sentence_lower: str, words: list[str]) -> int:
    return sum(1 for w in words if re.search(r"\b" + w, sentence_lower))


# Fallbacks when settings.yaml has no lists (the same lists, English and French).
DEFAULT_PROJECT_WORDS = ['project', 'proposed', 'proposes', 'proponent', 'will', 'would', 'works?', 'construction', 'construct', 'planned', 'includes?', 'install', 'build', 'undertaking', 'development', 'projet', 'proposé', 'promoteur', 'travaux', 'sera', 'seront', 'prévoit', 'aménagement', 'projekt', 'anlæg', 'etablering', 'vil blive', 'udvidelse', 'prosjekt', 'tiltak', 'vil bli', 'utbygging', 'aanleg', 'wordt', 'uitbreiding', 'proyecto', 'obras', 'construcción', 'ampliación', 'consiste', 'se pretende', 'parc', 'éoliennes', 'éolien']
DEFAULT_HABITAT_WORDS = ['fish habitat', 'offset', 'compensat', 'eelgrass', 'seagrass', 'shellfish', 'intertidal', 'salt marsh', 'new habitat', 'habitat creation', 'habitat enhancement', 'species at risk', 'habitat du poisson', 'compensation', 'zostère', 'mollusques', 'zone intertidale', 'herbier', 'ålegræs', 'ålegras', 'zeegras', 'posidonia', 'praderas', 'muslingebanke', 'mosselbank', 'bancos de moluscos', 'vadehav', 'wadden', 'zona intermareal', 'marisma', 'kompenserende', 'natuurcompensatie', 'medidas compensatorias', 'stenrev', 'arrecife artificial', 'Natura 2000', 'biodiversité', 'habitats marins']


def pick_quotes(full_text: str, reasons: list[dict], site_conf: dict, title: str = "",
                patterns: list[str] | None = None) -> list[str]:
    """Up to max_quotes evidence quotes. Each is a complete sentence copied exactly from the notice,
    about the project itself, and relevant to the score. Sentences with habitat words come first."""
    max_quotes = site_conf.get("max_quotes", 3)
    max_chars = site_conf.get("max_quote_chars", 300)
    project_words = site_conf.get("quote_project_words", DEFAULT_PROJECT_WORDS)
    habitat_words = site_conf.get("quote_habitat_words", DEFAULT_HABITAT_WORDS)
    keywords = {r["matched"].lower() for r in reasons if r.get("matched") and r.get("points", 0) > 0}
    t = _norm(title)
    scored = []
    for pos, s in enumerate(split_sentences(full_text or "")):
        if not (40 <= len(s) <= max_chars) or not _GOOD_START.search(s) or not _GOOD_END.search(s):
            continue                                  # not a complete sentence, or too long to quote whole
        if "..." in s or _CONTACT.search(s) or _mostly_acronyms(s) or _PDF_JUNK.search(s) or _norm(s) == t:
            continue
        low = s.lower()
        project = _word_hits(low, project_words)
        if not project:
            continue                                  # background, not about the project itself
        habitat = _word_hits(low, habitat_words)
        relevant = sum(1 for k in keywords if k in low)
        if not relevant and patterns:   # also accept any scoring-rule pattern (types, habitat, marine setting)
            relevant = 1 if any(re.search(pat, s, re.IGNORECASE) for pat in patterns) else 0
        if not habitat and not relevant:
            continue                                  # about the project, but not about why it scored
        scored.append((habitat * 3 + relevant + project * 0.5, -pos, s))
    out = []
    for _, _, s in sorted(scored, reverse=True):
        if any(_norm(s) in _norm(o) or _norm(o) in _norm(s) for o in out):
            continue
        out.append(s)
        if len(out) >= max_quotes:
            break
    return out


def stage_kind(row: dict, today: date) -> str:
    stage = (row.get("stage") or "").lower()
    deadline = row.get("comment_deadline")
    if _extra(row).get("watch") or row.get("tier") == "watch":
        return "watch"
    if stage.startswith("decision made") or stage.startswith("authorized"):
        return "decided"
    if stage.startswith("withdrawn"):
        return "withdrawn"
    if deadline and deadline != UNKNOWN:
        return "open" if date.fromisoformat(deadline) >= today else "closed"
    if stage in ("under assessment", "registered") or re.search(
            r"in progress|consultation|public information|application", stage):
        return "assessment"
    return "unknown"


STAGE_LABELS = {"open": "Comment period open", "closed": "Comment period closed", "decided": "Decision made",
                "assessment": "Under assessment", "withdrawn": "Withdrawn or rejected", "unknown": "Stage unknown",
                "watch": "Project watch"}


def source_conf(key: str | None) -> dict:
    """The source's block in config/coverage.yaml (quotes allowed, attribution, name)."""
    try:
        return load_coverage().get("sources", {}).get(key or "", {}) or {}
    except (OSError, ValueError):
        return {}


def enabled_sources(settings: dict) -> set | None:
    """Sources switched on in settings.yaml, or None when the settings list no sources (tests)."""
    srcs = settings.get("sources")
    if not srcs:
        return None
    return {name for name, conf in srcs.items() if (conf or {}).get("enabled")} | {"drop"}


def source_display_name(key: str | None) -> str:
    """Name from config/coverage.yaml, so every new source shows its real name."""
    try:
        name = load_coverage().get("sources", {}).get(key or "", {}).get("name")
    except (OSError, ValueError):
        name = None
    return name or SOURCE_NAMES.get(key or "", "Public source")


def _val(v) -> str:
    return v if v not in (None, "") else UNKNOWN


def _extra(row: dict) -> dict:
    e = row.get("extra")
    if isinstance(e, dict):
        return e
    try:
        return json.loads(e or "{}")
    except ValueError:
        return {}


def link_ok(row: dict) -> bool:
    """False only when ingest found the page did not show this project. Unknown counts as fine."""
    return _extra(row).get("link_ok", True) is not False


def quote_patterns(rules: dict) -> list[str]:
    """Every pattern in the scoring rules, used to judge whether a sentence is relevant."""
    pats = list(rules.get("marine_context", []))
    for group in ("project_types", "habitat_clues"):
        for rule in rules.get(group, {}).values():
            pats.extend(rule["patterns"])
    return pats


def english_summary(row: dict, review_row: dict | None, watch: bool = False) -> tuple[str, str]:
    """Return (summary, note). English items: the tool's factual summary. Other languages, with
    no AI translation step: an English summary built only from the keywords and sizes found in
    the notice, clearly labelled; if too little was found, the plain label "Not translated".
    A summary you write in the review file, or an LLM summary with checked quotes, wins."""
    mine = own_summary(review_row)
    if mine:
        return mine, "reviewed"
    if row.get("summary_en"):
        return row["summary_en"], "llm"
    noun = "page" if watch else "notice"
    language = row.get("language") or UNKNOWN
    if language in ("en", UNKNOWN):
        return auto_summary(row, noun), "auto"
    facts = json.loads(row.get("facts") or "{}")
    if facts.get("types") or facts.get("sizes"):
        return auto_summary(row, noun), "keywords"
    return "", "not_translated"


def page_slug(item_id: str) -> str:
    """Stable file name for a project's own page: "iaac:90821" -> "iaac-90821"."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", item_id)


def make_record(row: dict, review_row: dict, settings: dict, today: date,
                patterns: list[str] | None = None) -> dict:
    site_conf = settings.get("site", {})
    reasons = json.loads(row.get("reasons") or "[]")
    kind = stage_kind(row, today)
    tier, tier_reason = tier_for(row, settings.get("tiers", {}))
    watch = bool(_extra(row).get("watch"))
    if watch:
        tier, tier_reason = "watch", "project page, not a notice"
    reviewed = decision(review_row) == "yes"
    summary, summary_note = english_summary(row, review_row, watch)
    return {
        "id": row["key"],
        "title": _val(row.get("title")),
        "url": row.get("url") or "",
        "source": row.get("source"),
        "source_name": source_display_name(row.get("source")),
        "proponent": _val(row.get("proponent")),
        "country": row.get("country") if row.get("country") not in (None, "", UNKNOWN) else UNKNOWN,
        "province": _val(row.get("province")),
        "language": _val(row.get("language")),
        "water_body": _val(row.get("water_body")),
        "types": json.loads(row.get("project_types") or "[]"),
        "stage": _val(row.get("stage")),
        "stage_kind": kind,
        "comment_deadline": row.get("comment_deadline") or None,
        "decision_date": row.get("decision_date") or None,
        "score": row.get("score") or 0,
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "tier_reason": tier_reason,
        "reviewed": reviewed,
        "summary": summary,
        "summary_note": summary_note,
        "quotes": (pick_quotes(row.get("full_text") or "", reasons, site_conf, title=row.get("title") or "",
                               patterns=patterns) if source_conf(row.get("source")).get("quotes", True) else []),
        "attribution": source_conf(row.get("source")).get("attribution", ""),
        "agency": source_conf(row.get("source")).get("agency") or source_display_name(row.get("source")),
        # For a hearing portal, also name the authority that issued the notice.
        "issued_by": row.get("proponent") if row.get("source") == "dk_hoering" and row.get("proponent") not in (None, "", UNKNOWN) else "",
        "too_late": kind in ("decided", "closed"),
        "also_listed": [],
        "page": f"p/{page_slug(row['key'])}.html",
        "comments": comment_info(row, _extra(row), today),
    }


def _sort_key(rec: dict):
    return (TIER_ORDER.index(rec["tier"]), rec["stage_kind"] != "open", -rec["score"], rec["title"])


def build_public_records(conn: sqlite3.Connection, review_rows: dict, settings: dict, today: date,
                         previous: dict | None = None) -> tuple[list[dict], dict]:
    """All records for the public site, plus counts of what was left out and why."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM items WHERE is_test_set=0 AND score IS NOT NULL")]
    enabled = enabled_sources(settings)
    if enabled is not None:      # items from a source that is switched off never reach the site
        rows = [r for r in rows if r["source"] in enabled]
    skipped = {"hidden_by_you": 0, "bad_link": 0, "merged_duplicates": 0, "not_relevant": 0}
    kept = []
    for r in rows:
        if decision(review_rows.get(r["key"])) == "no":
            skipped["hidden_by_you"] += 1
        elif not link_ok(r):
            skipped["bad_link"] += 1
        else:
            kept.append(r)
    losers = find_duplicates(kept, settings.get("duplicates", {}))
    skipped["merged_duplicates"] = len(losers)
    by_key = {r["key"]: r for r in kept}
    from mpf.config import load_scoring
    patterns = quote_patterns(settings.get("scoring") or load_scoring())
    records = {}
    for r in kept:
        if r["key"] not in losers:
            records[r["key"]] = make_record(r, review_rows.get(r["key"], {}), settings, today, patterns)
    # "Probably not relevant" is never published. Duplicates of those are dropped too.
    for key in [k for k, rec in records.items() if rec["tier"] == "notrel"]:
        del records[key]
        skipped["not_relevant"] += 1
    for loser, winner in losers.items():
        if winner not in records:
            continue
        other = by_key[loser]
        records[winner]["also_listed"].append({
            "source_name": source_display_name(other["source"]), "url": other["url"],
            "comment_deadline": other.get("comment_deadline")})
        # If you reviewed only the duplicate, the merged entry still counts as reviewed.
        if decision(review_rows.get(loser)) == "yes":
            records[winner]["reviewed"] = True
    # Items you reviewed that dropped out of this run's data keep their last published record.
    for key, rec in (previous or {}).items():
        if key not in records and key not in losers and decision(review_rows.get(key)) == "yes" \
                and rec.get("tier") in PUBLISHED_TIERS:   # older or unpublished records are not reused
            rec = dict(rec)
            rec.setdefault("page", f"p/{page_slug(key)}.html")
            rec.setdefault("comments", comment_info(rec, {}, today))
            rec["stage_kind"] = stage_kind(rec, today) if rec.get("stage_kind") not in ("decided", "withdrawn") \
                else rec["stage_kind"]
            records[key] = rec
    return sorted(records.values(), key=_sort_key), skipped


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
/* Neutral page; colour only for tiers and badges. */
:root{--bg:#f5f6f7;--card:#ffffff;--text:#1b1f23;--muted:#57606a;--faint:#6e7781;--line:#d8dde2;--line-soft:#e8ebee;
--focus:#1b1f23;--strong:#17603f;--strong-bg:#e1f1e8;--possible:#7a4f00;--possible-bg:#faefd6;--weak:#48525c;
--weak-bg:#eaedf0;--open:#1d4f91;--open-bg:#e3ecf8;--late:#8f3311;--late-bg:#fbe8df;--chg:#5a3e8f;--chg-bg:#eee9f7;
color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1a1d21;--text:#e6e8ea;--muted:#a3abb4;--faint:#8b949e;
--line:#30363d;--line-soft:#262b31;--focus:#e6e8ea;--strong:#7ad3a4;--strong-bg:#173527;--possible:#efc26a;
--possible-bg:#3a2d10;--weak:#c3cad2;--weak-bg:#2a3038;--open:#9cc2f5;--open-bg:#1b2c45;--late:#f3a483;
--late-bg:#3d2116;--chg:#c8b6f2;--chg-bg:#2c2443}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15.5px/1.55 system-ui,-apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;-webkit-text-size-adjust:100%}
a{color:inherit;text-decoration-color:var(--faint);text-underline-offset:2px}
a:hover{text-decoration-color:currentColor}
a:focus-visible,button:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible{
outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
time,.countdown,.badge,.num,dl.facts dd{font-variant-numeric:tabular-nums}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}
h1,h2,h3,h4{line-height:1.25}

/* Header: name, one line, date */
header.site{background:var(--card);border-bottom:1px solid var(--line);padding:22px 0 18px}
header.site .wrap{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:10px 24px}
header.site h1{margin:0;font-size:1.75rem;letter-spacing:-.01em}
header.site p{margin:4px 0 0;color:var(--muted);max-width:60ch}
header.site p.updated{color:var(--text);font-size:.9rem;margin-top:8px}
.crumb{margin:0;font-weight:700;font-size:1.3rem;color:var(--text)}
header.site .crumb a{color:var(--text)}
.crumb a{text-decoration:none}
nav.top{font-size:.95rem;display:flex;gap:18px}
nav.top a{text-decoration:none;color:var(--muted);padding:4px 0;border-bottom:2px solid transparent}
nav.top a:hover{color:var(--text);border-bottom-color:var(--line)}

/* Panels */
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin:0}
.panel h2{margin:0 0 8px;font-size:1rem}
.panel ul{margin:6px 0;padding-left:18px}
.panel p{margin:6px 0}
.overview{display:grid;gap:16px;margin:20px 0 16px}
.overview .side{display:grid;gap:16px;grid-template-columns:minmax(0,1fr) minmax(0,1.7fr);align-items:start}
.overview .side>.explain{grid-column:2;grid-row:1 / span 3}
.overview .side>:not(.explain){grid-column:1}
.summary-line{margin:0;font-weight:600;color:var(--muted);font-size:.92rem}
.soon-note{margin:0 0 10px}
section.explain{margin:0}
section.explain ul{list-style:none;padding:0;display:grid;gap:4px}
section.explain li{padding-left:20px;position:relative;font-size:.93rem}
section.explain li::before{content:"";position:absolute;left:2px;top:.5em;width:10px;height:10px;border-radius:3px;background:var(--weak)}
section.explain li.key-strong::before{background:var(--strong)}
section.explain li.key-possible::before{background:var(--possible)}
section.explain p{color:var(--muted);font-size:.93rem;max-width:90ch}
details.coverage summary,details.news summary,details.more summary{cursor:pointer;font-weight:600}
details.coverage[open] summary{margin-bottom:8px}
table.cov{width:100%;border-collapse:collapse;font-size:.9rem;margin-top:8px}
table.cov th,table.cov td{text-align:left;padding:6px 8px;border-top:1px solid var(--line-soft);vertical-align:top}
table.cov th{color:var(--muted);font-weight:600}
table.cov tr.region td:first-child{padding-left:22px;color:var(--muted)}
.cov-wrap{overflow-x:auto}
.pill{font-size:.75rem;font-weight:600;padding:1px 8px;border-radius:999px;white-space:nowrap;border:1px solid var(--line);color:var(--muted);vertical-align:middle}
.pill.covered{color:var(--strong);border-color:currentColor}
.since p{font-size:.93rem}

/* Closing soon: aligned columns */
section.soon h2{font-size:1rem}
section.soon ul{list-style:none;padding:0;margin:4px 0 0}
section.soon li{display:grid;grid-template-columns:6.2em 6.6em minmax(0,1fr) auto;gap:4px 12px;align-items:baseline;
padding:7px 0;border-top:1px solid var(--line-soft);font-size:.93rem}
section.soon li:first-child{border-top:0}
section.soon .left{color:var(--muted);white-space:nowrap}
section.soon .left.urgent{color:var(--late);font-weight:600}
section.soon .meta{display:flex;gap:8px;align-items:baseline;white-space:nowrap;color:var(--muted)}
section.soon #soon-none{color:var(--muted)}

/* Tools: views, filters */
details.tools{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:0;margin:0 0 8px}
details.tools>summary{cursor:pointer;font-weight:600;padding:12px 18px;list-style:none}
details.tools>summary::-webkit-details-marker{display:none}
details.tools>summary::after{content:"+";float:right;color:var(--muted);font-weight:400}
details.tools[open]>summary::after{content:"\\2212"}
details.tools .tools-body{padding:0 18px 14px}
.views{display:flex;flex-wrap:wrap;gap:0;margin:0 0 12px;border:1px solid var(--line);border-radius:8px;width:max-content;max-width:100%;overflow:hidden}
.views label{position:relative;padding:6px 14px;font-size:.9rem;font-weight:600;color:var(--muted);cursor:pointer;border-left:1px solid var(--line)}
.views label:first-child{border-left:0}
.views input{position:absolute;opacity:0;width:1px;height:1px}
.views label:has(input:checked){background:var(--text);color:var(--card)}
.views label:has(input:focus-visible){outline:2px solid var(--focus);outline-offset:-3px}
.filters{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px 14px;align-items:end;margin:0}
.filters label{display:flex;flex-direction:column;gap:3px;font-size:.8rem;font-weight:600;color:var(--muted)}
.filters select,.filters input[type=search]{font:inherit;font-size:.92rem;color:var(--text);background:var(--card);
padding:7px 9px;border:1px solid var(--line);border-radius:7px;width:100%;min-width:0}
.filters .check,.toggles label{flex-direction:row;align-items:center;gap:8px;font-size:.9rem;color:var(--text);font-weight:500}
.toggles{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;margin-top:12px;padding-top:12px;border-top:1px solid var(--line-soft);font-size:.9rem}
.toggles label{display:flex}
.toggles button,.watch-btn{font:inherit;font-size:.85rem;padding:4px 11px;border-radius:7px;border:1px solid var(--line);
background:var(--card);color:var(--text);cursor:pointer}
.toggles button:hover,.watch-btn:hover{border-color:var(--faint)}
.watch-btn[aria-pressed="true"]{background:var(--text);color:var(--card);border-color:var(--text)}

/* Sections and cards */
section.country>h2,section.group>h2{margin:30px 0 4px;font-size:1.3rem;display:flex;align-items:center;gap:10px}
section.country>p.country-note{color:var(--muted);margin:0 0 8px;font-size:.93rem;max-width:90ch}
section.tier h3{margin:18px 0 2px;font-size:1rem;display:flex;align-items:center;gap:8px}
section.tier h3::before{content:"";width:10px;height:10px;border-radius:3px;background:var(--weak)}
section.tier[data-tier=strong] h3::before{background:var(--strong)}
section.tier[data-tier=possible] h3::before{background:var(--possible)}
section.tier p.tier-desc{color:var(--muted);margin:0 0 10px;font-size:.88rem}
.count{color:var(--muted);font-weight:500}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,500px),1fr));gap:14px;margin-top:8px}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--weak);border-radius:10px;
padding:16px 18px;display:flex;flex-direction:column;min-width:0}
.card[data-tier=strong]{border-left-color:var(--strong)}
.card[data-tier=possible]{border-left-color:var(--possible)}
.card h4{margin:0 0 8px;font-size:1.05rem}
.card h4 a{text-decoration:none}
.card h4 a:hover{text-decoration:underline}
.badges{display:flex;flex-wrap:wrap;gap:5px;margin:0 0 12px}
.badge{font-size:.76rem;font-weight:600;padding:2px 8px;border-radius:6px;white-space:nowrap;line-height:1.5;border:1px solid transparent}
.t-strong{color:var(--strong);background:var(--strong-bg)}
.t-possible{color:var(--possible);background:var(--possible-bg)}
.t-weak{color:var(--weak);background:var(--weak-bg)}
.t-watch{color:var(--muted);border-color:var(--line)}
.score{color:var(--text);border-color:var(--line)}
.late{color:var(--late);background:var(--late-bg)}
.st-open{color:var(--open);background:var(--open-bg)}
.st-other{color:var(--muted);border-color:var(--line)}
.rev{color:var(--text);border-color:var(--text)}
.rev::before{content:"\\2713\\00a0"}
.auto{color:var(--muted);border:1px dashed var(--line)}
.lang{color:var(--muted);border-color:var(--line)}
.badge.chg{color:var(--chg);background:var(--chg-bg)}
dl.facts{display:grid;grid-template-columns:9.5em minmax(0,1fr);gap:3px 14px;margin:0 0 12px;font-size:.92rem}
dl.facts dt{color:var(--muted)}
dl.facts dd{margin:0;min-width:0;overflow-wrap:anywhere}
.countdown{font-weight:600}
.card p.summary{margin:0 0 10px;max-width:75ch}
.note{color:var(--muted);font-size:.88rem;margin:6px 0}
.watch-row{margin:0 0 12px}
ul.quotes{margin:0 0 12px;padding:0;list-style:none}
ul.quotes li{border-left:2px solid var(--line);padding:2px 0 2px 12px;margin:6px 0;color:var(--muted);font-size:.92rem;max-width:75ch}
.card-foot{margin-top:auto;padding-top:10px;border-top:1px solid var(--line-soft);font-size:.88rem}
p.source{margin:4px 0;color:var(--muted)}
p.source a{color:var(--text)}
p.attr{margin:6px 0 0;color:var(--faint);font-size:.82rem}
.empty{padding:24px;text-align:center;color:var(--muted);background:var(--card);border:1px dashed var(--line);border-radius:10px}
footer{border-top:1px solid var(--line);padding:20px 0 40px;color:var(--muted);font-size:.88rem;margin-top:36px}
footer p{max-width:90ch}
main.about{padding-bottom:20px}
main.about h2{margin-top:30px;font-size:1.15rem}
main.about p,main.about li{max-width:75ch}
[hidden]{display:none!important}
/* Project page */
article.project{margin:20px 0 0;max-width:820px;border-left-width:4px}
article.project h1{margin:2px 0 8px;font-size:1.55rem}
article.project h2{font-size:1.02rem;margin:24px 0 8px;padding-top:14px;border-top:1px solid var(--line-soft)}
ol.timeline{list-style:none;padding:0;margin:6px 0;border-left:2px solid var(--line)}
ol.timeline li{padding:3px 0 3px 14px;font-size:.93rem}
ol.timeline time{font-weight:600;margin-right:10px}
ol.timeline li.official time{color:var(--open)}
ul.news{padding-left:18px;margin:6px 0}
ul.news li{margin:4px 0}
.gone{background:var(--late-bg);color:var(--late);padding:10px 14px;border-radius:10px;margin:16px 0 0;max-width:820px}
@media (max-width:900px){.overview .side{grid-template-columns:1fr}.overview .side>*{grid-column:1!important;grid-row:auto!important}.filters{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:600px){.wrap{padding:0 14px}header.site h1{font-size:1.45rem}
.filters{grid-template-columns:1fr}dl.facts{grid-template-columns:1fr;gap:0}dl.facts dd{margin-bottom:6px}
section.soon li{grid-template-columns:6.2em minmax(0,1fr)}section.soon li a{grid-column:1/-1}
section.soon .meta{grid-column:1/-1}.card{padding:14px}.views{width:100%}.views label{flex:1 1 auto;text-align:center}}
"""

# Shared by the list and the project pages: countdowns and the watch list (stored only in this browser).
JS_COMMON = r"""
var MPF = (function(){
  var today = new Date(); today.setHours(0,0,0,0);
  function daysLeft(iso){ if(!iso) return null; var p = iso.split('-');
    var d = new Date(+p[0], +p[1]-1, +p[2]); return Math.round((d - today)/86400000); }
  var STAGE = {open:'Comment period open', closed:'Comment period closed', decided:'Decision made',
               assessment:'Under assessment', withdrawn:'Withdrawn or rejected', unknown:'Stage unknown',
               watch:'Project watch'};
  function countdown(el){
    var dl = el.getAttribute('data-deadline'), n = daysLeft(dl), kind = el.getAttribute('data-stage');
    if (kind !== 'decided' && kind !== 'withdrawn' && kind !== 'watch' && n !== null) { kind = n >= 0 ? 'open' : 'closed'; }
    el.setAttribute('data-stage-now', kind); el._days = n;
    var cd = el.querySelector('.countdown');
    if (cd) {
      if (kind === 'watch') { /* project watch: keep "not applicable" */ }
      else if (kind === 'decided') cd.textContent = (dl ? dl + ', ' : '') + 'decision made';
      else if (n === null) cd.textContent = 'Not published';
      else if (n > 1) cd.textContent = dl + ' (' + n + ' days left)';
      else if (n === 1) cd.textContent = dl + ' (1 day left)';
      else if (n === 0) cd.textContent = dl + ' (closes today)';
      else cd.textContent = dl + ' (closed)';
    }
    var late = el.querySelector('.late'); if (late) late.hidden = !(kind === 'decided' || kind === 'closed');
    var st = el.querySelector('.stage-badge');
    if (st) { st.textContent = STAGE[kind]; st.className = 'badge stage-badge ' + (kind === 'open' ? 'st-open' : 'st-other'); }
  }
  // The watch list lives only in this browser (localStorage). No account, nothing is sent anywhere.
  var KEY = 'watchlist-v1';
  function getWatch(){ try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { return []; } }
  function setWatch(ids){ try { localStorage.setItem(KEY, JSON.stringify(ids)); return true; } catch (e) { return false; } }
  function storageOk(){ try { localStorage.setItem(KEY + '-test', '1'); localStorage.removeItem(KEY + '-test'); return true; }
                        catch (e) { return false; } }
  function paintButtons(){
    var w = getWatch();
    Array.prototype.forEach.call(document.querySelectorAll('.watch-btn'), function(b){
      var on = w.indexOf(b.getAttribute('data-id')) >= 0;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.textContent = on ? 'On your watch list (remove)' : 'Add to watch list';
    });
  }
  function initWatch(onChange){
    if (!storageOk()) return;      // private windows or blocked storage: buttons stay hidden
    Array.prototype.forEach.call(document.querySelectorAll('.watch-btn'), function(b){
      b.hidden = false;
      b.addEventListener('click', function(){
        var w = getWatch(), id = b.getAttribute('data-id'), i = w.indexOf(id);
        if (i >= 0) w.splice(i, 1); else w.push(id);
        setWatch(w); paintButtons(); if (onChange) onChange();
      });
    });
    paintButtons();
  }
  return {daysLeft: daysLeft, countdown: countdown, getWatch: getWatch, initWatch: initWatch, STAGE: STAGE};
})();
"""

JS = r"""
(function(){
  var cards = Array.prototype.slice.call(document.querySelectorAll('#server-list .card'));
  var SOON = parseInt(document.body.getAttribute('data-soon') || '30', 10);
  cards.forEach(MPF.countdown);
  var $ = function(id){ return document.getElementById(id); };
  var fc = $('f-country'), fr = $('f-region'), frl = $('f-region-label'), frt = $('f-region-text'), ft = $('f-type'),
      fs = $('f-stage'), fo = $('f-open'), fq = $('f-search'), fw = $('t-weak'), fsort = $('f-sort'), fgroup = $('f-group'),
      line = $('summary-line'), empty = $('no-match'), ul = $('soon-list'), none = $('soon-none'),
      view = $('view'), server = $('server-list'), holder = document.createElement('div');
  holder.hidden = true; document.body.appendChild(holder);
  Array.prototype.forEach.call(document.querySelectorAll('.js-only'), function(el){ el.hidden = false; });
  var types = {};
  cards.forEach(function(c){ (c.getAttribute('data-types') || '').split('|').forEach(function(t){ if (t) types[t] = 1; }); });
  Object.keys(types).sort().forEach(function(v){ var o = document.createElement('option'); o.value = v; o.textContent = v; ft.appendChild(o); });
  // The province or state filter only appears for countries that have regions (Canada, United States).
  function fillRegions(){
    var opt = fc.options[fc.selectedIndex], label = opt ? opt.getAttribute('data-region-label') : '';
    while (fr.options.length > 1) fr.remove(1);
    fr.value = '';
    if (!fc.value || !label) { frl.hidden = true; return; }
    frt.textContent = label; frl.hidden = false;
    var regs = {};
    cards.forEach(function(c){ if (c.getAttribute('data-country') === fc.value) regs[c.getAttribute('data-region')] = 1; });
    Object.keys(regs).sort().forEach(function(v){ var o = document.createElement('option'); o.value = v; o.textContent = v; fr.appendChild(o); });
  }
  function param(name){ return (location.search.match(new RegExp('[?&]' + name + '=([^&]*)')) || [])[1] || ''; }
  if (param('show').indexOf('weak') >= 0) fw.checked = true;
  if (/^[A-Za-z]{2}$/.test(param('country'))) fc.value = param('country').toUpperCase();
  var views = document.querySelectorAll('input[name="view"]');
  function currentView(){ for (var i = 0; i < views.length; i++) if (views[i].checked) return views[i].value; return 'all'; }
  ['all', 'soon', 'changed', 'watch'].forEach(function(v){ if (param('view') === v) $('v-' + v).checked = true; });
  if (param('sort') && fsort.querySelector('option[value="' + param('sort') + '"]')) fsort.value = param('sort');
  if (param('group') && fgroup.querySelector('option[value="' + param('group') + '"]')) fgroup.value = param('group');
  fillRegions();
  var TIERS = ['strong', 'possible', 'weak', 'watch'];
  var TIER_NAMES = JSON.parse($('tier-names').textContent);
  var STAGE_ORDER = ['open', 'assessment', 'closed', 'decided', 'withdrawn', 'watch', 'unknown'];
  var MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  function placeOk(c){ return (!fc.value || c.getAttribute('data-country') === fc.value) &&
                              (!fr.value || c.getAttribute('data-region') === fr.value); }
  function visible(c, v, watch){
    var k = c.getAttribute('data-stage-now'), t = c.getAttribute('data-tier'), q = (fq.value || '').toLowerCase().trim();
    // Closing soon, changed and watch list views show every published tier.
    var tierOk = v !== 'all' || t === 'strong' || t === 'possible' || t === 'watch' || (t === 'weak' && fw.checked);
    var viewOk = v === 'all' ||
      (v === 'soon' && t !== 'watch' && k === 'open' && c._days !== null && c._days <= SOON) ||
      (v === 'changed' && !!c.getAttribute('data-changes')) ||
      (v === 'watch' && watch.indexOf(c.getAttribute('data-id')) >= 0);
    return tierOk && viewOk && placeOk(c) &&
      (!ft.value || ('|' + c.getAttribute('data-types') + '|').indexOf('|' + ft.value + '|') >= 0) &&
      (!fs.value || k === fs.value) && (!fo.checked || k === 'open') &&
      (!q || c.getAttribute('data-search').indexOf(q) >= 0);
  }
  function cmp(a, b){
    var s = fsort.value, sa = +a.getAttribute('data-score'), sb = +b.getAttribute('data-score');
    if (s === 'deadline') { var da = a._days === null ? 1e9 : (a._days < 0 ? 1e8 - a._days : a._days),
                                db = b._days === null ? 1e9 : (b._days < 0 ? 1e8 - b._days : b._days);
                            return da - db || sb - sa; }
    if (s === 'score') return sb - sa;
    if (s === 'newest') return (b.getAttribute('data-first') || '').localeCompare(a.getAttribute('data-first') || '') || sb - sa;
    if (s === 'title') return a.getAttribute('data-title').localeCompare(b.getAttribute('data-title'));
    var ao = a.getAttribute('data-stage-now') === 'open' ? 0 : 1, bo = b.getAttribute('data-stage-now') === 'open' ? 0 : 1;
    return TIERS.indexOf(a.getAttribute('data-tier')) - TIERS.indexOf(b.getAttribute('data-tier')) || ao - bo || sb - sa;
  }
  function groupOf(c){
    var g = fgroup.value;
    if (g === 'country') return {key: ('000' + c.getAttribute('data-country-order')).slice(-3), name: c.getAttribute('data-country-name')};
    if (g === 'type') { var t = (c.getAttribute('data-types') || '').split('|')[0];
                        return {key: t ? '0' + t : '1', name: t || 'Project type unknown'}; }
    if (g === 'stage') { var k = c.getAttribute('data-stage-now'); return {key: String(STAGE_ORDER.indexOf(k)), name: MPF.STAGE[k]}; }
    if (g === 'month') { var d = c.getAttribute('data-deadline');
                         if (!d || c.getAttribute('data-stage') === 'watch') return {key: '9999', name: 'No comment deadline published'};
                         return {key: d.slice(0, 7), name: 'Comments close in ' + MONTHS[+d.slice(5, 7) - 1] + ' ' + d.slice(0, 4)}; }
    return {key: '', name: ''};
  }
  function section(cls, title, n){
    var s = document.createElement('section'); s.className = cls;
    if (title) { var h = document.createElement(cls === 'group' ? 'h2' : 'h3'); h.textContent = title + ' ';
      var span = document.createElement('span'); span.className = 'count'; span.textContent = '(' + n + ')'; h.appendChild(span); s.appendChild(h); }
    var d = document.createElement('div'); d.className = 'cards'; s.appendChild(d); return s;
  }
  var shownOrder = [];
  function apply(){
    var v = currentView(), watch = MPF.getWatch();
    var shown = cards.filter(function(c){ return visible(c, v, watch); }).sort(cmp);
    cards.forEach(function(c){ holder.appendChild(c); });
    while (view.firstChild) view.removeChild(view.firstChild);
    var groups = [], byKey = {};
    shown.forEach(function(c){ var g = groupOf(c);
      if (!byKey[g.key]) { byKey[g.key] = {key: g.key, name: g.name, cards: []}; groups.push(byKey[g.key]); }
      byKey[g.key].cards.push(c); });
    groups.sort(function(a, b){ return a.key < b.key ? -1 : a.key > b.key ? 1 : 0; });
    shownOrder = [];
    groups.forEach(function(g){
      var gs = section('group', g.name, g.cards.length);
      // Grouped by country with the default order: keep the tier headings inside each country.
      if (fgroup.value === 'country' && fsort.value === 'default') {
        gs.removeChild(gs.lastChild);
        TIERS.forEach(function(t){
          var cs = g.cards.filter(function(c){ return c.getAttribute('data-tier') === t; });
          if (!cs.length) return;
          var ts = section('tier', TIER_NAMES[t], cs.length); ts.setAttribute('data-tier', t);
          cs.forEach(function(c){ ts.lastChild.appendChild(c); shownOrder.push(c); }); gs.appendChild(ts);
        });
      } else { g.cards.forEach(function(c){ gs.lastChild.appendChild(c); shownOrder.push(c); }); }
      view.appendChild(gs);
    });
    server.hidden = true; view.hidden = false;
    empty.hidden = shown.length > 0;
    empty.textContent = v === 'watch' && !watch.length ? 'Your watch list is empty. Use "Add to watch list" on any project.'
      : 'No projects match these filters.';
    // Closing soon list at the top: every published tier in the chosen country or region.
    while (ul.firstChild) ul.removeChild(ul.firstChild);
    var soon = cards.filter(function(c){ return placeOk(c) && c.getAttribute('data-tier') !== 'watch' &&
      c.getAttribute('data-stage-now') === 'open' && c._days !== null && c._days <= SOON; })
      .sort(function(a,b){ return a._days - b._days; });
    // One row per deadline, in aligned columns: date, days left, project, tier and country.
    function el(tag, cls, text){ var e = document.createElement(tag); if (cls) e.className = cls;
                                 if (text !== undefined) e.textContent = text; return e; }
    soon.forEach(function(c){
      var li = el('li'), a = el('a', '', c.querySelector('h4').textContent), tier = c.querySelector('.tier-badge');
      a.href = c.querySelector('h4 a').getAttribute('href');
      var t = el('time', 'd', c.getAttribute('data-deadline')); t.setAttribute('datetime', c.getAttribute('data-deadline'));
      li.appendChild(t);
      li.appendChild(el('span', 'left' + (c._days <= 7 ? ' urgent' : ''),
        c._days === 0 ? 'closes today' : c._days === 1 ? '1 day left' : c._days + ' days left'));
      li.appendChild(a);
      var meta = el('span', 'meta'); meta.appendChild(el('span', tier.className.replace('tier-badge', '').trim() + ' badge', tier.textContent));
      meta.appendChild(el('span', '', c.getAttribute('data-country-name'))); li.appendChild(meta);
      ul.appendChild(li);
    });
    if (none) none.hidden = soon.length > 0;
    line.textContent = soon.length + (soon.length === 1 ? ' comment deadline' : ' comment deadlines') +
      ' in the next ' + SOON + ' days.';
  }
  // CSV of the projects shown right now, in the order shown.
  var DATA = JSON.parse($('csv-data').textContent);
  function csvCell(v){ v = v === null || v === undefined ? '' : String(v);
    return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }
  $('csv-btn').addEventListener('click', function(){
    var rows = [DATA.columns.map(csvCell).join(',')];
    shownOrder.forEach(function(c){ var r = DATA.rows[c.getAttribute('data-id')]; if (r) rows.push(r.map(csvCell).join(',')); });
    var blob = new Blob(['﻿' + rows.join('\r\n') + '\r\n'], {type: 'text/csv;charset=utf-8'});
    var a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'projects.csv';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(a.href); }, 1000);
  });
  // A link ending in #coverage opens the Coverage panel.
  var cov = $('coverage');
  if (cov && location.hash === '#coverage') { cov.open = true; }
  fc.addEventListener('change', function(){ fillRegions(); apply(); });
  [fr, ft, fs, fo, fw, fsort, fgroup].forEach(function(el){ el.addEventListener('change', apply); });
  Array.prototype.forEach.call(views, function(el){ el.addEventListener('change', apply); });
  fq.addEventListener('input', apply);
  MPF.initWatch(function(){ if (currentView() === 'watch') apply(); });
  // On phones the filters and views start folded away.
  var tools = $('tools');
  if (tools && window.matchMedia && window.matchMedia('(max-width: 600px)').matches) tools.open = false;
  apply();
})();
"""

JS_PAGE = r"""
(function(){
  var el = document.querySelector('.project'); if (el) MPF.countdown(el);
  MPF.initWatch();
})();
"""


def _head(title: str, description: str) -> str:
    return (f'<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<meta name="robots" content="noindex, nofollow">\n<title>{escape(title)}</title>\n'
            f'<meta name="description" content="{escape(description)}">\n<style>{CSS}</style>')


def _header(title: str, desc: str, updated: date) -> str:
    return (f'<header class="site"><div class="wrap">\n<div class="masthead"><h1>{escape(title)}</h1>\n<p>{escape(desc)}</p>\n'
            f'<p class="updated">Last updated: <time>{updated.strftime("%B %-d, %Y")}</time></p></div>\n'
            f'<nav class="top"><a href="index.html">Projects</a><a href="about.html">About</a></nav>\n</div></header>')


def _footer(site: dict) -> str:
    email = escape(site.get("contact_email", ""))
    return (
        '<footer><div class="wrap">'
        f"<p>{escape(site.get('title', 'MitiMark'))} is a prototype. Flags show projects that may need marine habitat work. They do not state that "
        "mitigation is required. Always check the official notice. Compiled from public sources.</p>"
        f'<p>Contact: {escape(site.get("contact_name", ""))}, <a href="mailto:{email}">{email}</a></p>'
        "</div></footer>")


def _pill(status: str) -> str:
    return f'<span class="pill {status}">{STATUS_LABELS.get(status, status)}</span>'


def coverage_rows(coverage: dict, counts: dict | None = None) -> str:
    """Table rows: one per country, then one per region. Partial places name their sources.
    counts (items published per country) adds the country's empty_note when it has none."""
    rows = []
    for c in coverage.get("countries", []):
        detail = c.get("note") or ""
        if c["status"] != "coming_soon":
            detail = (detail + " " if detail else "") + "Sources: " + source_names(coverage, c.get("sources", [])) + "."
            if counts is not None and not counts.get(c["code"]) and c.get("empty_note"):
                detail += " " + c["empty_note"]
        rows.append(f'<tr><td><strong>{escape(c["name"])}</strong></td><td>{_pill(c["status"])}</td>'
                    f'<td>{escape(detail) if detail else "No source yet."}</td></tr>')
        for r in c.get("regions", []):
            d = r.get("note") or ""
            if r["status"] != "coming_soon":
                d = "Sources: " + source_names(coverage, r.get("sources", [])) + "." + (" " + d if d else "")
            rows.append(f'<tr class="region"><td>{escape(r["name"])}</td><td>{_pill(r["status"])}</td>'
                        f'<td>{escape(d) if d else "No source yet."}</td></tr>')
    return "\n".join(rows)


def country_counts(records: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in records:
        out[r.get("country") or UNKNOWN] = out.get(r.get("country") or UNKNOWN, 0) + 1
    return out


def balance_note(records: list[dict]) -> str:
    """Say plainly how much of the site is Canada."""
    notices = [r for r in records if r.get("tier") != "watch"]
    if not notices:
        return ""
    ca = sum(1 for r in notices if r.get("country") == "CA")
    others = sorted({r.get("country") for r in notices if r.get("country") != "CA"})
    if ca * 2 < len(notices):
        return ""
    return (f"For now the site is mostly Canada: {ca} of the {len(notices)} listed notices come from Canadian sources. "
            f"Other countries have one source each or none yet, so their lists are short or empty.")


def coverage_counts(coverage: dict) -> dict:
    counts = {"covered": 0, "partial": 0, "coming_soon": 0}
    for c in coverage.get("countries", []):
        counts[c["status"]] += 1
    return counts


def less_tested_note(coverage: dict) -> str:
    langs = coverage.get("less_tested_languages", [])
    if not langs:
        return ""
    names = {"fr": "French", "no": "Norwegian", "nb": "Norwegian", "da": "Danish", "nl": "Dutch", "es": "Spanish",
             "de": "German", "pt": "Portuguese", "it": "Italian", "sv": "Swedish", "fi": "Finnish", "pl": "Polish",
             "el": "Greek", "en": "English"}
    listed = ", ".join(names.get(x, x) for x in langs)
    return (f"Less tested languages: {listed}. The keyword rules for these languages have fewer tests, so "
            f"listings in them are lower confidence and are marked on each card.")


def _place(rec: dict, country_name: str) -> str:
    return ", ".join(x for x in (rec["province"] if rec["province"] != UNKNOWN else "", country_name) if x) or UNKNOWN


def _deadline_text(rec: dict) -> tuple[str | None, str]:
    """(deadline for the countdown, text shown before JavaScript updates it)."""
    deadline = rec.get("comment_deadline")
    text = deadline if deadline else NOT_PUBLISHED
    if rec["stage_kind"] == "watch":
        return None, "not applicable (project watch, not a notice)"
    if rec["stage_kind"] == "decided":
        text = (deadline + ", " if deadline else "") + "decision made"
    elif rec["stage_kind"] == "closed" and deadline:
        text = deadline + " (closed)"
    return deadline, text


def _badges(rec: dict, less_tested: list, changes: bool = True) -> str:
    review_badge = ('<span class="badge rev">Reviewed by a person</span>' if rec["reviewed"]
                    else '<span class="badge auto">Automatic, not yet reviewed</span>')
    lang_badge = ""
    if rec.get("language") in less_tested:
        lang_badge = f'<span class="badge lang">Less tested language ({escape(rec["language"])})</span>'
    chg = ""
    if changes:
        chg = "".join(f'<span class="badge chg">{escape(CHANGE_LABELS[c])}</span>' for c in rec.get("changes") or [])
    return (f'<div class="badges"><span class="badge tier-badge t-{rec["tier"]}">{escape(rec["tier_label"])}</span>\n'
            f'<span class="badge score" title="{escape(rec["tier_reason"])}">Score {int(rec["score"])}/100</span>\n'
            f'<span class="badge stage-badge {"st-open" if rec["stage_kind"] == "open" else "st-other"}">'
            f'{STAGE_LABELS[rec["stage_kind"]]}</span>\n'
            f'<span class="badge late"{"" if rec["too_late"] else " hidden"}>Likely too late</span>'
            f'{review_badge}{lang_badge}{chg}</div>')


def _comment_count_text(rec: dict, prefix: str = "") -> str:
    """'463 (as of 2026-10-12), view on the registry' or 'Not published'. Never comment text or names."""
    c = rec.get("comments") or {}
    if c.get("count") is None:
        return NOT_PUBLISHED
    link = (f', <a href="{escape(c["comments_url"])}" rel="noopener">official comments page</a>'
            if c.get("comments_url") else "")
    as_of = f' (as of {escape(c["count_as_of"])})' if c.get("count_as_of") else ""
    return f'{int(c["count"])}{as_of}{link}'


def _opens_text(rec: dict) -> str:
    c = rec.get("comments") or {}
    if not c.get("opens"):
        return NOT_PUBLISHED
    return escape(c["opens"]) + (" (date of the notice)" if c.get("opens_how") == "notice date" else "")


def _source_lines(rec: dict, link: str) -> str:
    also = ""
    if rec.get("also_listed"):
        parts = []
        for a in rec["also_listed"]:
            d = f', comment deadline {escape(a["comment_deadline"])}' if a.get("comment_deadline") else ""
            parts.append(f'<a href="{escape(a["url"])}" rel="noopener">{escape(a["source_name"])}</a>{d}')
        also = f'<p class="source">Also listed: {"; ".join(parts)}</p>\n'
    official = "Official page" if rec["stage_kind"] == "watch" else "Official notice"
    attribution = (" " + escape(rec["attribution"])) if rec.get("attribution") else ""
    return (f'<p class="source"><a href="{link}" rel="noopener">{official}</a> ({escape(rec["source_name"])}){attribution}</p>\n'
            f'{also}'
            f'<p class="attr">Source: {escape(rec.get("agency") or rec["source_name"])}'
            f'{(". Hearing by " + escape(rec["issued_by"])) if rec.get("issued_by") else ""}'
            f'{(". " + escape(rec["attribution"])) if rec.get("attribution") else ""}.</p>')


def _watch_button(rec: dict) -> str:
    return (f'<button type="button" class="watch-btn" data-id="{escape(rec["id"])}" aria-pressed="false" hidden>'
            f'Add to watch list</button>')


def _card(rec: dict, country_name: str, less_tested: list, country_order: int = 0) -> str:
    types = rec["types"] or []
    location = f'{_place(rec, country_name)}. Water body: {rec["water_body"]}'
    search = " ".join([rec["title"], rec["proponent"], rec["province"], country_name, rec["water_body"],
                       rec["summary"], " ".join(types)]).lower()
    deadline, deadline_text = _deadline_text(rec)
    quotes = "".join(f"<li>&ldquo;{escape(q)}&rdquo;</li>" for q in rec["quotes"])
    anchor = "p-" + page_slug(rec["id"])
    link = escape(rec["url"]) if rec["url"] else "#"
    page = escape(rec.get("page") or "")
    comments = ""
    if rec["stage_kind"] != "watch":
        comments = (f'<dt>Comments open</dt><dd>{_opens_text(rec)}</dd>\n'
                    f'<dt>Comments close</dt><dd><span class="countdown">{escape(deadline_text)}</span></dd>\n'
                    f'<dt>Comments received</dt><dd>{_comment_count_text(rec)}</dd>')
    else:
        comments = f'<dt>Comment deadline</dt><dd><span class="countdown">{escape(deadline_text)}</span></dd>'
    title_link = page or link
    return f"""<article class="card" id="{anchor}" data-id="{escape(rec['id'])}" data-tier="{rec['tier']}" data-country="{escape(rec.get('country') or UNKNOWN)}"
 data-country-name="{escape(country_name)}" data-country-order="{country_order}" data-region="{escape(rec['province'])}"
 data-types="{escape('|'.join(types))}" data-stage="{rec['stage_kind']}" data-deadline="{escape(deadline or '')}"
 data-score="{int(rec['score'])}" data-first="{escape(rec.get('first_listed') or '')}"
 data-changes="{escape('|'.join(rec.get('changes') or []))}" data-title="{escape(rec['title'].lower())}" data-search="{escape(search)}">
<h4><a href="{title_link}">{escape(rec['title'])}</a></h4>
{_badges(rec, less_tested)}
<dl class="facts">
<dt>Proponent</dt><dd>{escape(rec['proponent'])}</dd>
<dt>Location</dt><dd>{escape(location)}</dd>
<dt>Project type</dt><dd>{escape(', '.join(types) if types else UNKNOWN)}</dd>
<dt>Stage</dt><dd>{escape(rec['stage'])}</dd>
{comments}
</dl>
{_summary_html(rec)}
{f'<ul class="quotes">{quotes}</ul>' if quotes else ''}
<div class="card-foot">
<p class="source">{f'<a href="{page}">Project page and timeline</a> &middot; ' if page else ''}{_watch_button(rec)}</p>
{_source_lines(rec, link)}
</div>
</article>"""


LANG_NAMES = {"fr": "French", "da": "Danish", "es": "Spanish", "no": "Norwegian", "nl": "Dutch", "en": "English"}


def _summary_html(rec: dict) -> str:
    note = rec.get("summary_note", "auto")
    lang = LANG_NAMES.get(rec.get("language"), rec.get("language") or "unknown")
    if note == "not_translated":
        return (f'<p class="summary"><span class="badge lang">Not translated</span> This {lang} notice has no '
                f'English summary. See the original-language quotes and the official notice.</p>')
    label = ""
    if note == "keywords":
        label = (f' <span class="note">(English summary built from keywords found in the {lang} text. '
                 f'Not a translation.)</span>')
    return f'<p class="summary">{escape(rec["summary"])}{label}</p>'


def tier_descriptions(tiers: dict) -> dict:
    s, p, w = tiers.get("strong_min", 60), tiers.get("possible_min", 35), tiers.get("weak_min", 20)
    return {
        "strong": f"Score {s} or more. Several signals of habitat work, such as offsetting, new habitat, "
                  f"or large in-water works.",
        "possible": f"Score {p} to {s - 1}. Some signals. Worth a look.",
        "weak": f"Score {w} to {p - 1}. Few signals. Often routine maintenance.",
        "watch": "Project pages, not notices. There are no comment deadlines here, so no countdown is shown "
                 "and these projects never appear in the closing-soon list.",
    }


def _countries_in_order(records: list[dict], coverage: dict) -> list[dict]:
    """Countries that have published items, in config order. Unknown countries go last."""
    have = {r.get("country") or UNKNOWN for r in records}
    ordered = [c for c in coverage.get("countries", []) if c["code"] in have]
    known = {c["code"] for c in ordered}
    for code in sorted(have - known):
        ordered.append({"code": code, "name": "Other" if code == UNKNOWN else code, "status": "partial",
                        "sources": [], "note": ""})
    return ordered


def render_index(records: list[dict], site: dict, tiers: dict, updated: date, coverage: dict | None = None,
                 history_dates: list[str] | None = None, news_html: str = "") -> str:
    coverage = coverage if coverage is not None else load_coverage()
    title = site.get("title", "MitiMark")
    desc = site.get("description", "")
    soon_days = int(site.get("closing_soon_days", 30))
    td = tier_descriptions(tiers)
    less_tested = coverage.get("less_tested_languages", [])
    n_soon = sum(1 for r in records if r["stage_kind"] == "open" and r.get("comment_deadline")
                 and 0 <= (date.fromisoformat(r["comment_deadline"]) - updated).days <= soon_days)
    countries = _countries_in_order(records, coverage)
    names = {c["code"]: c["name"] for c in coverage.get("countries", [])}
    names.update({c["code"]: c["name"] for c in countries})

    country_sections = []
    for ci, c in enumerate(countries):
        recs_c = [r for r in records if (r.get("country") or UNKNOWN) == c["code"]]
        tier_parts = []
        for t in PUBLISHED_TIERS + ("watch",):
            recs = [r for r in recs_c if r["tier"] == t]
            if not recs:
                continue
            tier_parts.append(
                f'<section class="tier" data-country="{escape(c["code"])}" data-tier="{t}">\n'
                f'<h3>{TIER_LABELS[t]} <span class="count">({len(recs)})</span></h3>\n'
                f'<p class="tier-desc">{escape(td[t])}</p>\n<div class="cards">\n'
                + "\n".join(_card(r, names.get(c["code"], c["code"]), less_tested, ci) for r in recs)
                + "\n</div>\n</section>")
        note = f'<p class="country-note">{escape(c.get("note", ""))}</p>' if c.get("note") else ""
        country_sections.append(
            f'<section class="country" data-country="{escape(c["code"])}" aria-label="{escape(c["name"])}">\n'
            f'<h2>{escape(c["name"])} {_pill(c["status"])}</h2>\n{note}\n' + "\n".join(tier_parts) + "\n</section>")
    body = "\n".join(country_sections) if country_sections else '<p class="empty">No projects yet.</p>'
    since = since_panel(records, history_dates or [])
    csv_data = json.dumps({"columns": CSV_COLUMNS, "rows": {r["id"]: csv_row(r, names, site) for r in records}},
                          ensure_ascii=False).replace("</", "<\\/")
    tier_names = json.dumps({t: TIER_LABELS[t] for t in PUBLISHED_TIERS + ("watch",)})

    opts = ['<option value="">All countries</option>']
    with_data = {c["code"] for c in countries}
    for c in coverage.get("countries", []):
        if c["code"] in with_data:
            opts.append(f'<option value="{escape(c["code"])}" data-region-label="{escape(c.get("region_label", ""))}">'
                        f'{escape(c["name"])}</option>')
    for c in coverage.get("countries", []):
        if c["code"] not in with_data:
            opts.append(f'<option value="{escape(c["code"])}" disabled>{escape(c["name"])} (coming soon)</option>')
    cc = coverage_counts(coverage)
    lt = less_tested_note(coverage)
    bn = balance_note(records)
    counts = country_counts(records)
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(title, desc)}
</head>
<body data-soon="{soon_days}">
{_header(title, desc, updated)}
<main class="wrap">


<div class="overview">
<section class="panel soon" aria-labelledby="soon-h">
<h2 id="soon-h">Comment deadlines closing soon (next {soon_days} days)</h2>
<p class="summary-line" id="summary-line">{n_soon} comment deadlines in the next {soon_days} days.</p>
<p class="note soon-note">Includes Weak matches.</p>
<ul id="soon-list"></ul>
<p id="soon-none">No comment deadlines in the next {soon_days} days.</p>
<noscript><p>Turn on JavaScript for live countdowns, filters and search. Without it, all three tiers are listed below.</p></noscript>
</section>
<div class="side">
{since}
<details class="panel coverage" id="coverage">
<summary>Coverage: {cc['covered']} covered, {cc['partial']} partial, {cc['coming_soon']} coming soon{' (mostly Canada for now)' if bn else ''}</summary>
{f'<p>{escape(bn)}</p>' if bn else ''}
<div class="cov-wrap"><table class="cov"><thead><tr><th>Place</th><th>Status</th><th>Details</th></tr></thead>
<tbody>
{coverage_rows(coverage, counts)}
</tbody></table></div>
{f'<p>{escape(lt)}</p>' if lt else ''}
</details>
<section class="panel explain" aria-labelledby="explain-h">
<h2 id="explain-h">How to read this list</h2>
<p>Every notice is scored automatically from the words and sizes in it, then placed in a tier:</p>
<ul>
<li class="key-strong"><strong>{TIER_LABELS['strong']}:</strong> {escape(td['strong'])}</li>
<li class="key-possible"><strong>{TIER_LABELS['possible']}:</strong> {escape(td['possible'])}</li>
<li class="key-weak"><strong>{TIER_LABELS['weak']}:</strong> {escape(td['weak'])} Hidden unless you tick "Show weaker matches".</li>
</ul>
<p>Notices scoring under {int(tiers.get('weak_min', 20))}, quarries and pits, and withdrawn projects are not listed.</p>
<p>A flag means a notice may involve marine habitat work. It is not a statement that mitigation or
compensation is required. Items marked "Reviewed by a person" have been checked; the rest are automatic.
Always read the official notice.</p>
</section>
</div>
</div>

<details class="tools" id="tools" open>
<summary>Filters and views</summary>
<div class="tools-body">
<div class="views js-only" role="radiogroup" aria-label="Which projects" hidden>
<label><input type="radio" name="view" id="v-all" value="all" checked> All projects</label>
<label><input type="radio" name="view" id="v-soon" value="soon"> Closing soon</label>
<label><input type="radio" name="view" id="v-changed" value="changed"> Changed since last update</label>
<label><input type="radio" name="view" id="v-watch" value="watch"> My watch list</label>
</div>
<form class="filters" onsubmit="return false" aria-label="Filter projects">
<label>Search<input type="search" id="f-search" placeholder="Name, place, proponent"></label>
<label>Country<select id="f-country">{''.join(opts)}</select></label>
<label id="f-region-label" hidden><span id="f-region-text">Province or state</span><select id="f-region"><option value="">All</option></select></label>
<label>Project type<select id="f-type"><option value="">All types</option></select></label>
<label>Stage<select id="f-stage"><option value="">All stages</option>
<option value="open">Comment period open</option><option value="closed">Comment period closed</option>
<option value="assessment">Under assessment</option><option value="decided">Decision made</option>
<option value="withdrawn">Withdrawn or rejected</option><option value="unknown">Stage unknown</option></select></label>
<label class="js-only" hidden>Sort by<select id="f-sort"><option value="default">Tier, open comment periods first</option>
<option value="deadline">Comment deadline, soonest first</option><option value="score">Score, highest first</option>
<option value="newest">Newest listed first</option><option value="title">Name, A to Z</option></select></label>
<label class="js-only" hidden>Group by<select id="f-group"><option value="country">Country</option>
<option value="type">Project type</option><option value="stage">Stage</option>
<option value="month">Deadline month</option><option value="none">No grouping</option></select></label>
<label class="check"><input type="checkbox" id="f-open"> Comment period open only</label>
</form>
<div class="toggles">
<label><input type="checkbox" id="t-weak"> Show weaker matches</label>
<button type="button" id="csv-btn" class="js-only" hidden>Download shown projects (CSV)</button>
<a href="projects.csv">All listed projects (CSV)</a>
</div>
</div>
</details>
<p class="empty" id="no-match" hidden>No projects match these filters.</p>
{news_html}
<div id="view" hidden></div>
<div id="server-list">
{body}
</div>
</main>
{_footer(site)}
<script type="application/json" id="csv-data">{csv_data}</script>
<script type="application/json" id="tier-names">{tier_names}</script>
<script>{JS_COMMON}{JS}</script>
</body>
</html>
"""


CSV_COLUMNS = ["id", "title", "country", "region", "proponent", "project_types", "stage", "tier", "score",
               "reviewed_by_a_person", "comments_open", "comments_close", "comments_received", "comments_received_as_of",
               "changes_since_last_update", "first_listed", "project_page", "official_notice", "source"]


def csv_row(rec: dict, names: dict, site_conf: dict) -> list:
    """One CSV row: facts only (no quotes, no summaries). Missing comment data is "Not published"."""
    c = rec.get("comments") or {}
    base = site_conf.get("url", "")
    return [rec["id"], rec["title"], names.get(rec.get("country"), rec.get("country")), rec["province"],
            rec["proponent"], "; ".join(rec["types"] or []), STAGE_LABELS.get(rec["stage_kind"], rec["stage"]),
            rec["tier_label"], int(rec["score"]), "yes" if rec["reviewed"] else "no",
            c.get("opens") or NOT_PUBLISHED, c.get("closes") or NOT_PUBLISHED,
            c["count"] if c.get("count") is not None else NOT_PUBLISHED, c.get("count_as_of") or "",
            "; ".join(CHANGE_LABELS[x] for x in rec.get("changes") or []), rec.get("first_listed") or "",
            base + rec["page"] if rec.get("page") else "", rec["url"],
            rec.get("agency") or rec["source_name"]]


def write_csv(path: Path, records: list[dict], names: dict, site_conf: dict) -> None:
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in records:
            w.writerow(csv_row(r, names, site_conf))


def since_panel(records: list[dict], dates: list[str]) -> str:
    """Counts of what changed since the previous update, with links to the "changed" view."""
    if len(dates) < 2:
        return ""
    counts = {k: sum(1 for r in records if k in (r.get("changes") or [])) for k in CHANGE_LABELS}
    parts = [f"{counts[k]} {CHANGE_LABELS[k].lower() if i else CHANGE_LABELS[k].split(' since')[0].lower()}"
             for i, k in enumerate(CHANGE_LABELS)]
    return (f'<section class="panel since" aria-labelledby="since-h"><h2 id="since-h">Since the last update '
            f'({escape(dates[-2])})</h2>\n<p>{escape(", ".join(parts))}. '
            f'<a href="?view=changed&amp;show=weak">Show the changed projects</a>.</p></section>')


TIMELINE_TYPES = {"new": "Listed", "deadline": "Deadline", "decided": "Decision", "stage": "Stage", "tier": "Tier"}


def _timeline_html(rec: dict) -> str:
    evs = rec.get("timeline") or []
    items = []
    if rec.get("first_listed") and not any(e["type"] == "new" for e in evs):
        items.append(f'<li><time>{escape(rec["first_listed"])}</time> Listed on this site (first snapshot)</li>')
    for e in evs:
        text = "First listed on this site" if e["type"] == "new" else e["text"]
        items.append(f'<li><time>{escape(e["date"])}</time> {escape(text)}</li>')
    c = rec.get("comments") or {}
    if c.get("opens"):
        items.append(f'<li class="official"><time>{escape(c["opens"])}</time> Comment period opens (official notice)</li>')
    if c.get("closes"):
        items.append(f'<li class="official"><time>{escape(c["closes"])}</time> Comment period closes (official notice)</li>')
    if rec.get("decision_date"):
        items.append(f'<li class="official"><time>{escape(rec["decision_date"])}</time> Decision date (official notice)</li>')
    items.sort(key=lambda li: re.search(r"<time>([^<]+)", li).group(1))
    if not items:
        return "<p>No changes recorded yet.</p>"
    return '<ol class="timeline">' + "".join(items) + "</ol>"


def _news_source(n: dict) -> str:
    via = (f' (via <a href="{GDELT_URL}" rel="noopener">{escape(n["via"])}</a>)' if n.get("via") else "")
    return f"Source: {escape(n['source_name'])}{via}"


def _news_li(n: dict) -> str:
    return (f'<li><a href="{escape(n["url"])}" rel="noopener nofollow">{escape(n["title"])}</a> '
            f'<span class="note">{escape(n.get("date") or "date unknown")}. {_news_source(n)}.</span></li>')


def news_section(items: list[dict], shown: int) -> str:
    """Collapsed list of unmatched news items. Matched items appear on their project's page instead."""
    loose = [n for n in items if not n.get("matches")]
    if not loose:
        return ""
    first, rest = loose[:shown], loose[shown:]
    more = (f'<details class="more"><summary>Show all {len(loose)}</summary><ul class="news">'
            + "".join(_news_li(n) for n in rest) + "</ul></details>") if rest else ""
    return (f'<details class="panel news" id="news"><summary>News signal, unverified ({len(loose)})</summary>\n'
            f'<p class="note">Headlines from news feeds that mention marine construction. They are not official '
            f'notices, have not been checked, and are never ranked as Strong match or Possible. Only the headline, '
            f'source, date and link are shown. A headline that matches an official notice appears on that '
            f'project\'s page as "Also in the news".</p>\n<ul class="news">'
            + "".join(_news_li(n) for n in first) + f"</ul>\n{more}</details>")


def attach_news(records: list[dict], items: list[dict]) -> None:
    by_id = {r["id"]: r for r in records}
    for r in records:
        r["in_the_news"] = []
    for n in items:
        for rid in n.get("matches") or []:
            if rid in by_id:
                by_id[rid]["in_the_news"].append({k: n.get(k) for k in ("title", "url", "source_name", "date", "via")})


def _in_the_news_html(rec: dict) -> str:
    news = rec.get("in_the_news") or []
    if not news:
        return ""
    return ('<h2>Also in the news</h2>\n<p class="note">News headlines that name this project. Unverified; the '
            'official notice is the record.</p>\n<ul class="news">' + "".join(_news_li(n) for n in news) + "</ul>")


def render_project_page(rec: dict, site: dict, updated: date, country_name: str, less_tested: list) -> str:
    title = site.get("title", "MitiMark")
    types = rec["types"] or []
    deadline, deadline_text = _deadline_text(rec)
    link = escape(rec["url"]) if rec["url"] else "#"
    quotes = "".join(f"<li>&ldquo;{escape(q)}&rdquo;</li>" for q in rec["quotes"])
    c = rec.get("comments") or {}
    if rec["stage_kind"] == "watch":
        comments = "<p>Not applicable: this is a project page, not a notice.</p>"
    else:
        note = f'<p class="note">{escape(c["count_note"])}</p>' if c.get("count_note") else ""
        comments = f"""<dl class="facts">
<dt>Opens</dt><dd>{_opens_text(rec)}</dd>
<dt>Closes</dt><dd><span class="countdown">{escape(deadline_text)}</span></dd>
<dt>Comments received</dt><dd>{_comment_count_text(rec)}</dd>
</dl>
{note}
<p class="note">Only dates and counts the official registry publishes are shown. Comment text and the names of
people who commented are not copied here. To comment, use the official notice.</p>"""
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(rec['title'] + ' - ' + title, rec.get('summary') or site.get('description', ''))}
</head>
<body data-soon="{int(site.get('closing_soon_days', 30))}">
<header class="site"><div class="wrap">
<div class="masthead"><p class="crumb"><a href="../index.html">{escape(title)}</a></p>
<p class="updated">Last updated: <time>{updated.strftime("%B %-d, %Y")}</time></p></div>
<nav class="top"><a href="../index.html">Projects</a><a href="../about.html">About</a></nav>
</div></header>
<main class="wrap">
<article class="project card" data-id="{escape(rec['id'])}" data-stage="{rec['stage_kind']}" data-deadline="{escape(deadline or '')}">
<h1>{escape(rec['title'])}</h1>
{_badges(rec, less_tested)}
<p class="watch-row">{_watch_button(rec)}</p>
<dl class="facts">
<dt>Proponent</dt><dd>{escape(rec['proponent'])}</dd>
<dt>Location</dt><dd>{escape(_place(rec, country_name))}. Water body: {escape(rec['water_body'])}</dd>
<dt>Project type</dt><dd>{escape(', '.join(types) if types else UNKNOWN)}</dd>
<dt>Stage</dt><dd>{escape(rec['stage'])}</dd>
</dl>
{_summary_html(rec)}
{f'<h2>Evidence from the notice</h2><ul class="quotes">{quotes}</ul>' if quotes else ''}
<h2>Public comments</h2>
{comments}
{_in_the_news_html(rec)}
<h2>Timeline</h2>
{_timeline_html(rec)}
<p class="note">Dates marked "official notice" come from the registry. Other entries are what this site saw
at each update.</p>
<h2>Source</h2>
{_source_lines(rec, link)}
</article>
</main>
{_footer(site)}
<script>{JS_COMMON}{JS_PAGE}</script>
</body>
</html>
"""


def render_about(site: dict, tiers: dict, updated: date, coverage: dict | None = None,
                 records: list[dict] | None = None) -> str:
    coverage = coverage if coverage is not None else load_coverage()
    records = records or []
    bn = balance_note(records)
    title = "About: " + site.get("title", "MitiMark")
    td = tier_descriptions(tiers)
    sources = "".join(f'<li><a href="{escape(s["url"])}" rel="noopener">{escape(s["name"])}</a></li>'
                      for s in coverage.get("sources", {}).values())
    freq = escape(site.get("update_frequency", "every 2 weeks"))
    tier_list = "".join(f"<li><strong>{TIER_LABELS[t]}:</strong> {escape(td[t])}</li>" for t in PUBLISHED_TIERS)
    lt = less_tested_note(coverage)
    from mpf.comments import COUNT_STATUS
    comment_sources = "".join(f'<li><strong>{escape(s.get("name", key))}:</strong> {escape(COUNT_STATUS[key].replace(" This project page shows none.", ""))}</li>'
                              for key, s in coverage.get("sources", {}).items() if key in COUNT_STATUS)
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(title, site.get('description', ''))}
</head>
<body>
{_header("About " + site.get("title", "MitiMark"), site.get('description', ''), updated)}
<main class="wrap about">
<h2>What it does</h2>
<p>The tool reads public environmental assessment and permit notices for marine construction projects,
such as ports, wharves, breakwaters, dredging and shoreline works. It flags projects that may need marine
habitat work, for example avoidance and reduction measures, habitat enhancement, fish habitat offsetting, or
nature-inclusive design. Each listing links to the official notice.</p>

<h2>What the score and tiers mean</h2>
<p>Each notice gets a score from 0 to 100 from simple, visible rules: the type of marine work, words about
fish habitat, offsetting, eelgrass, shellfish or species at risk, and the size of the work. The score places
each notice in a tier:</p>
<ul>{tier_list}</ul>
<p>Notices scoring under {int(tiers.get('weak_min', 20))}, quarries and pits, and withdrawn projects are not
listed at all. Only Strong and Possible matches are shown at first; Weak matches can be switched on with
the "Show weaker matches" toggle. A high score means the notice is worth a closer look. It does not mean
that mitigation or compensation is required. Only the regulator decides that.</p>
<p>The short quotes on each listing are complete sentences copied exactly from the notice, chosen because
they describe the project and its habitat signals, so you can judge for yourself.</p>

<h2>Automatic listings and review</h2>
<p>Every scored notice is published automatically, marked "Automatic, not yet reviewed". When a person has
checked a listing against the official notice, it is marked "Reviewed by a person". Listings found to be
wrong or irrelevant on review are removed. Summaries are kept short and factual, and facts that a notice
does not give are shown as "unknown".</p>
<p>When the same project appears in two registries, it is listed once, with a link to the other listing.
Listings whose official page cannot be found are left out.</p>

<h2>Notices in other languages</h2>
<p>Quotes always stay in the notice's original language. There is no automatic translation. For notices
not in English, the English summary is built only from the keywords and sizes the tool found in the text,
and is labelled "built from keywords, not a translation". When too little was found, the listing says
"Not translated" and shows the original-language quotes instead. The keyword rules for French, Danish,
Spanish, Norwegian and Dutch have fewer tests than the English rules, so those listings are lower confidence
and carry a "Less tested language" badge.</p>

<h2>Public comments</h2>
<p>Each project shows when its public comment period opens and closes and how many days are left, but
only when the official registry publishes those dates. When an opening date is taken from the date of the
notice that invited comments, it is labelled "date of the notice". Where a registry publishes the number
of comments received, the number is shown with the date it was read and a link to the registry's own
comments page. Comment text, the names of people who commented and other personal details are never copied
to this site. Anything a source does not publish is shown as "Not published".</p>
<ul>{comment_sources}</ul>

<h2>Changes between updates and project pages</h2>
<p>Every project has its own page with a stable address. At each update the site keeps a short snapshot
(deadline, stage and tier) and marks projects that are new, whose comment deadline changed, or that are now
decided. Each project page shows a timeline of those changes next to the official comment dates.</p>

<h2>Your watch list</h2>
<p>"Add to watch list" saves a project in your own browser only. There are no accounts, and nothing about
your watch list is sent anywhere. Clearing your browser data clears the list.</p>

<h2>Project watch</h2>
<p>Some sources list projects rather than notices. When such a source is used, its items appear in a
separate "Project watch (not a notice feed)" section, with no comment deadline, no countdown, and never in
the closing-soon list. No project watch source is in use at the moment.</p>

<h2>Coverage</h2>
{f'<p><strong>{escape(bn)}</strong></p>' if bn else ''}
<p>"Covered" means a source for that place is read automatically. "Partial" means only some sources are
read, and they are named. "Coming soon" means no source is read yet, and no listings are shown for it.</p>
<div class="cov-wrap"><table class="cov"><thead><tr><th>Place</th><th>Status</th><th>Details</th></tr></thead>
<tbody>
{coverage_rows(coverage, country_counts(records))}
</tbody></table></div>
{f'<p>{escape(lt)}</p>' if lt else ''}

<h2>Public sources used</h2>
<ul>{sources}</ul>

<h2>How often it updates</h2>
<p>The list is refreshed {freq}, and new notices appear on their own. Comment countdowns are calculated
in your browser, so they stay current between updates.</p>

<h2>Known limits</h2>
<ul>
<li>Public notices often appear after a project's design is done, so some listings will be too late to
influence the design.</li>
<li>Automatic listings have not been checked by a person and can be wrong.</li>
<li>Some size readings can be wrong, for example when a notice also mentions a road or pipeline length.</li>
<li>Many countries and regions are not covered yet; see the coverage table above.</li>
<li>Notices are quoted in their original language. Keyword rules in languages other than English have
fewer tests, so those listings are lower confidence.</li>
</ul>
</main>
{_footer(site)}
</body>
</html>
"""


def write_site(out_dir: Path, records: list[dict], settings: dict, updated: date,
               coverage: dict | None = None, news_items: list[dict] | None = None) -> None:
    """news_items: only passed when news.show_on_site is on (see mpf/news.py)."""
    from mpf import history as hist
    coverage = coverage if coverage is not None else load_coverage()
    site_conf, tiers = settings.get("site", {}), settings.get("tiers", {})
    out_dir.mkdir(parents=True, exist_ok=True)
    # Snapshot history: compare with the previous update, then add the timeline to each record.
    h = hist.record_snapshot(hist.load(out_dir / "history.json"), records, updated.isoformat())
    hist.annotate(records, h)
    names = {c["code"]: c["name"] for c in coverage.get("countries", [])}
    less_tested = coverage.get("less_tested_languages", [])
    news_html = ""
    if news_items is not None:
        attach_news(records, news_items)
        news_html = news_section(news_items, int((settings.get("news") or {}).get("default_shown", 15)))
    (out_dir / "index.html").write_text(render_index(records, site_conf, tiers, updated, coverage, h["snapshots"],
                                                     news_html), encoding="utf-8")
    (out_dir / "about.html").write_text(render_about(site_conf, tiers, updated, coverage, records), encoding="utf-8")
    # One page per project, at a stable address (p/<source>-<id>.html). Pages of projects no longer
    # listed are removed, so the folder only holds what the list shows.
    pages = out_dir / "p"
    pages.mkdir(exist_ok=True)
    keep = set()
    for r in records:
        path = out_dir / r["page"]
        keep.add(path.name)
        path.write_text(render_project_page(r, site_conf, updated, names.get(r.get("country"), r.get("country") or UNKNOWN),
                                            less_tested), encoding="utf-8")
    for old in pages.glob("*.html"):
        if old.name not in keep:
            mark_unlisted(old, updated)
    write_csv(out_dir / "projects.csv", records, names, site_conf)
    (out_dir / "items.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    hist.save(out_dir / "history.json", h)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")   # tells GitHub Pages to serve files as-is


GONE_MARK = '<p class="gone">'


def mark_unlisted(path: Path, updated: date) -> None:
    """Keep a project page's address working after it leaves the list, with a clear note on top."""
    html = path.read_text(encoding="utf-8")
    if GONE_MARK in html:
        return
    note = (f'{GONE_MARK}This project is no longer listed on this site (as of {updated.isoformat()}). The details '
            f'below are from the last time it was listed. Check the official notice for the current status.</p>')
    path.write_text(html.replace('<main class="wrap">', '<main class="wrap">\n' + note, 1), encoding="utf-8")


def load_previous(out_dir: Path) -> dict:
    path = out_dir / "items.json"
    if not path.exists():
        return {}
    try:
        return {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
    except (ValueError, KeyError):
        return {}
