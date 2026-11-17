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
from mpf.profiles import (ALL, ALL_LABEL, EMPTY_NOTE, ProfileScorer, is_freshwater, listed_in_profile,
                          profile_results)
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
        "approved_on": approval_date(row, kind, today),
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
        # Several licences for one job (Scotland): every licence type, merged into one entry.
        "licence_types": list(_extra(row).get("licence_types") or []),
    }


def _licences_row(rec: dict) -> str:
    """'Licences' fact row when one job has several licences (Scotland), else nothing."""
    types = rec.get("licence_types") or []
    return f"\n<dt>Licences</dt><dd>{escape('; '.join(types))}</dd>" if len(types) > 1 else ""


def approval_date(row: dict, kind: str, today: date) -> str | None:
    """The date an approved project was decided, when the notice gives one (ISO date), else None."""
    if kind != "decided":
        return None
    made = _extra(row).get("decision_made")
    d = (made.get("date") if isinstance(made, dict) else None) or row.get("decision_date") or ""
    try:
        return d if date.fromisoformat(d[:10]) <= today else None
    except ValueError:
        return None


def approved_label(iso: str | None) -> str:
    """'Approved July 2025' from '2025-07-11'; empty when there is no date."""
    if not iso:
        return ""
    d = date.fromisoformat(iso[:10])
    return f"Approved {d.strftime('%B')} {d.year}"


def _sort_key(rec: dict):
    return (TIER_ORDER.index(rec["tier"]), rec["stage_kind"] != "open", -rec["score"], rec["title"])


def build_public_records(conn: sqlite3.Connection, review_rows: dict, settings: dict, today: date,
                         previous: dict | None = None) -> tuple[list[dict], dict]:
    """All records for the public site, plus counts of what was left out and why."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM items WHERE is_test_set=0 AND score IS NOT NULL")]
    enabled = enabled_sources(settings)
    if enabled is not None:      # items from a source that is switched off never reach the site
        rows = [r for r in rows if r["source"] in enabled]
    # A source can be read and scored but kept off the site (sources.<name>.public: false).
    hidden = {n for n, c in (settings.get("sources") or {}).items() if isinstance(c, dict) and c.get("public") is False}
    rows = [r for r in rows if r["source"] not in hidden]
    skipped = {"hidden_by_you": 0, "bad_link": 0, "merged_duplicates": 0, "not_relevant": 0}
    kept = []
    for r in rows:
        if decision(review_rows.get(r["key"])) == "no":
            skipped["hidden_by_you"] += 1
        elif not link_ok(r):
            skipped["bad_link"] += 1
        else:
            kept.append(r)
    # A notice that a more recently read entry carries among its documents (Scotland: merged into that entry)
    # is not shown on its own.
    carried = {}
    for r in kept:
        for d in _extra(r).get("documents") or []:
            if d.get("url") and d["url"] != r["url"]:
                carried.setdefault(d["url"], []).append(r)
    absorbed = [r for r in kept if any((c["last_seen"] or "") > (r["last_seen"] or "") for c in carried.get(r["url"], []))]
    kept = [r for r in kept if r not in absorbed]
    losers = find_duplicates(kept, settings.get("duplicates", {}))
    skipped["merged_duplicates"] = len(losers) + len(absorbed)
    by_key = {r["key"]: r for r in kept}
    from mpf.config import load_scoring
    scoring = settings.get("scoring") or load_scoring()
    patterns = quote_patterns(scoring)
    scorer = ProfileScorer(scoring) if scoring.get("profiles") else None
    tiers_conf = settings.get("tiers", {})
    records = {}
    for r in kept:
        if r["key"] not in losers:
            rec = make_record(r, review_rows.get(r["key"], {}), settings, today, patterns)
            rec["profiles"] = profile_results(r, rec["tier"], scorer, tiers_conf) if scorer else {}
            rec["freshwater"] = is_freshwater(r, scoring.get("freshwater"))
            records[r["key"]] = rec
    # "Probably not relevant" is never on the general list. Such a project is kept only when a buyer
    # profile lists it at Possible or better; it then appears under that profile only.
    possible_min = int(tiers_conf.get("possible_min", 35))
    for key in [k for k, rec in records.items() if rec["tier"] == "notrel"]:
        if any(listed_in_profile(records[key], p, possible_min) for p in records[key]["profiles"]):
            records[key]["profile_only"] = True
            continue
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
            rec.setdefault("profiles", {})
            rec.setdefault("freshwater", False)
            rec.setdefault("page", f"p/{page_slug(key)}.html")
            rec.setdefault("comments", comment_info(rec, {}, today))
            rec["stage_kind"] = stage_kind(rec, today) if rec.get("stage_kind") not in ("decided", "withdrawn") \
                else rec["stage_kind"]
            records[key] = rec
    return sorted(records.values(), key=_sort_key), skipped


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = r"""
/* MitiMark. Calm neutral surfaces, one ocean accent; colour carries meaning only for tiers and status. */
:root{--bg:#f4f6f8;--surface:#ffffff;--surface-2:#f6f8fa;--ink:#0f1c2b;--ink-2:#2b3846;--muted:#566273;--faint:#76818f;
--line:#e2e6eb;--line-strong:#cdd5de;--brand:#0b6684;--brand-ink:#ffffff;--brand-soft:#e2f0f5;--focus:#0b6684;
--hero-1:#0a2135;--hero-2:#0d3d55;
--strong:#15703f;--strong-bg:#e1f2e8;--possible:#8a5a00;--possible-bg:#fbefd5;--weak:#4f5a66;--weak-bg:#eceff2;
--open:#1d4f91;--open-bg:#e3ecf8;--late:#9a3412;--late-bg:#fcebe2;--chg:#5a3e8f;--chg-bg:#eee9f7;
--shadow:0 1px 2px rgba(15,28,43,.04),0 1px 3px rgba(15,28,43,.05);
--shadow-lg:0 2px 4px rgba(15,28,43,.05),0 10px 24px -8px rgba(15,28,43,.14);
--radius:14px;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#0c1117;--surface:#141b23;--surface-2:#1a222c;--ink:#e7ecf1;--ink-2:#cdd5de;
--muted:#9aa6b3;--faint:#7d8896;--line:#253041;--line-strong:#334055;--brand:#5bbfdf;--brand-ink:#04202b;--brand-soft:#0f2c38;
--focus:#5bbfdf;--hero-1:#06141f;--hero-2:#0a2a3b;
--strong:#7ad3a4;--strong-bg:#163327;--possible:#efc26a;--possible-bg:#3a2d10;--weak:#c3cad2;--weak-bg:#28303a;
--open:#9cc2f5;--open-bg:#1b2c45;--late:#f3a483;--late-bg:#3d2116;--chg:#c8b6f2;--chg-bg:#2c2443;
--shadow:0 1px 2px rgba(0,0,0,.3);--shadow-lg:0 2px 4px rgba(0,0,0,.3),0 12px 28px -10px rgba(0,0,0,.6)}}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:80px}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}*{transition:none!important}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,"Helvetica Neue",Arial,sans-serif;-webkit-text-size-adjust:100%;-webkit-font-smoothing:antialiased}
a{color:var(--brand);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:3px}
a:focus-visible,button:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible{
outline:2px solid var(--focus);outline-offset:2px;border-radius:6px}
time,.countdown,.badge,.num,dl.facts dd,.stats .n{font-variant-numeric:tabular-nums}
.wrap{max-width:1260px;margin:0 auto;padding:0 24px}
h1,h2,h3,h4{line-height:1.25;letter-spacing:-.012em;color:var(--ink)}
[hidden]{display:none!important}
.skip{position:absolute;left:-999px;top:8px;z-index:50;background:var(--surface);padding:8px 12px;border-radius:8px}
.skip:focus{left:12px}

/* Top bar */
.topbar{position:sticky;top:0;z-index:30;background:var(--surface);background:color-mix(in srgb,var(--surface) 86%,transparent);
-webkit-backdrop-filter:saturate(1.5) blur(12px);backdrop-filter:saturate(1.5) blur(12px);border-bottom:1px solid var(--line)}
.topbar .wrap{display:flex;align-items:center;gap:20px;height:60px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:1.06rem;color:var(--ink);letter-spacing:-.01em;flex:none}
.brand:hover{text-decoration:none}
.brand svg{width:30px;height:30px;flex:none}
nav.top{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}
nav.top a{color:var(--muted);font-weight:550;font-size:.9rem;padding:6px 11px;border-radius:8px;white-space:nowrap}
nav.top a:hover{background:var(--surface-2);color:var(--ink);text-decoration:none}
nav.top a[aria-current=page]{color:var(--ink);background:var(--surface-2)}
.topbar .updated{margin:0 0 0 auto;font-size:.8rem;color:var(--muted);white-space:nowrap;display:flex;align-items:center;gap:7px}
.topbar .updated::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--strong)}

/* Hero */
.hero{background:radial-gradient(1200px 400px at 85% -10%,rgba(91,191,223,.22),transparent 60%),
linear-gradient(135deg,var(--hero-1),var(--hero-2));color:#fff}
.hero .wrap{padding-top:44px;padding-bottom:40px}
.eyebrow{margin:0 0 10px;font-size:.76rem;font-weight:650;letter-spacing:.09em;text-transform:uppercase;color:#8fd3ea}
.hero h1{color:#fff;font-size:clamp(1.6rem,3.2vw,2.35rem);margin:0 0 12px;max-width:26ch;letter-spacing:-.02em}
.hero p.lede{color:#c3d4e0;max-width:66ch;margin:0;font-size:1.02rem}
.hero p.meta{color:#9fb6c6;margin:10px 0 0;font-size:.86rem}
.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:28px 0 0;padding:0;list-style:none}
.stats a{display:block;height:100%;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.13);border-radius:12px;
padding:14px 16px;color:#fff;transition:background .15s,border-color .15s}
.stats a:hover{background:rgba(255,255,255,.11);border-color:rgba(255,255,255,.25);text-decoration:none}
.stats .n{display:block;font-size:1.7rem;font-weight:700;line-height:1.2;letter-spacing:-.02em}
.stats .l{display:flex;align-items:center;gap:7px;font-size:.82rem;color:#b9cbd8}
.stats .dot{width:8px;height:8px;border-radius:50%;flex:none}
.stats-for{margin:26px 0 -16px;font-size:.8rem;color:#9fb6c6}
.stats-for strong{color:#fff;font-weight:600}
.dot.strong{background:#7ad3a4}.dot.possible{background:#efc26a}.dot.soon{background:#f3a483}

/* Buyer profile bar */
.profile-bar{background:var(--surface);border-bottom:1px solid var(--line)}
.profile-bar .wrap{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:8px 24px;align-items:center;padding-top:14px;padding-bottom:14px}
.profile-pick{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.profile-pick label{font-weight:650;white-space:nowrap}
.profile-pick select{font:inherit;font-size:.95rem;font-weight:600;color:var(--ink);background:var(--brand-soft);
border:1px solid var(--brand);border-radius:9px;padding:8px 12px;min-width:0;max-width:100%}
.profile-desc{margin:0;color:var(--muted);font-size:.88rem}
.profile-share{display:flex;gap:12px;align-items:center;font-size:.86rem;white-space:nowrap}
.profile-empty{grid-column:1/-1;margin:0;padding:10px 14px;border-radius:10px;background:var(--possible-bg);color:var(--possible);font-weight:550;font-size:.9rem}
.profile-note{grid-column:1/-1;margin:0;color:var(--faint);font-size:.8rem}
.card p.because{margin:8px 0 0;font-size:.86rem;color:var(--ink-2);background:var(--brand-soft);border-radius:8px;padding:7px 10px}
.card p.general-line{margin:6px 0 0;font-size:.8rem;color:var(--muted)}
.t-none{color:var(--muted);border-color:var(--line)}
ul.profile-list{list-style:none;padding:0;margin:0;display:grid;gap:12px}
ul.profile-list li{font-size:.9rem}
ul.profile-list p.because{margin:4px 0 0;font-size:.84rem;color:var(--muted)}
@media (max-width:800px){.profile-bar .wrap{grid-template-columns:1fr}.profile-share{white-space:normal;flex-wrap:wrap}}

/* Page layout: filters beside results */
.layout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:28px;align-items:start;padding-top:28px}
.sidebar{position:sticky;top:80px;max-height:calc(100vh - 96px);overflow:auto;border-radius:var(--radius);scrollbar-width:thin}
.content{min-width:0}

/* Panels */
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px;margin:0;box-shadow:var(--shadow)}
.panel h2{margin:0;font-size:1.02rem}
.panel p{margin:6px 0}
.panel-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:4px 16px}
.stack{display:grid;gap:16px}
.row-2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:16px;align-items:start}
.row-2>details[open]{grid-column:1/-1}
.summary-line{margin:2px 0 0;color:var(--muted);font-size:.9rem;font-weight:500}
.note{color:var(--muted);font-size:.86rem;margin:6px 0}
.soon-note{margin:0;font-size:.8rem}

/* Closing soon */
section.soon ul{list-style:none;padding:0;margin:14px 0 0}
section.soon li{display:grid;grid-template-columns:6.6em 7.6em minmax(0,1fr) auto;gap:4px 14px;align-items:center;
padding:10px 4px;border-top:1px solid var(--line);font-size:.92rem}
section.soon li time{font-weight:600;color:var(--ink-2)}
section.soon .left{justify-self:start;font-size:.76rem;font-weight:650;padding:2px 10px;border-radius:999px;
background:var(--surface-2);color:var(--muted);white-space:nowrap;border:1px solid var(--line)}
section.soon .left.urgent{background:var(--late-bg);color:var(--late);border-color:transparent}
section.soon li a{color:var(--ink);font-weight:550}
section.soon li a:hover{color:var(--brand)}
section.soon .meta{display:flex;gap:8px;align-items:center;white-space:nowrap;color:var(--muted);font-size:.84rem}
section.soon #soon-none{color:var(--muted);margin-top:12px}

/* How to read */
section.explain ul{list-style:none;padding:0;margin:14px 0 10px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
section.explain li{border:1px solid var(--line);border-top:3px solid var(--weak);border-radius:10px;padding:12px 14px;
font-size:.87rem;color:var(--muted);background:var(--surface-2)}
section.explain li strong{display:block;color:var(--ink);margin-bottom:2px}
section.explain li.key-strong{border-top-color:var(--strong)}
section.explain li.key-possible{border-top-color:var(--possible)}
section.explain p{color:var(--muted);font-size:.9rem;max-width:95ch}

/* Collapsible panels */
details.panel>summary{cursor:pointer;font-weight:650;list-style:none;display:flex;align-items:center;gap:10px}
details>summary::-webkit-details-marker{display:none}
/* Open/closed arrow: two small images (down, up). No rotation, so it draws the same in every browser. */
details.panel>summary::after,details.tools>summary::after{content:"";margin-left:auto;width:12px;height:12px;flex:none;
background:var(--faint);-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 4.5l4 4 4-4' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/12px 12px no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 4.5l4 4 4-4' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/12px 12px no-repeat}
details.panel[open]>summary::after,details.tools[open]>summary::after{-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 7.5l4-4 4 4' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 7.5l4-4 4 4' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")}
details.panel[open]>summary{margin-bottom:10px}
details.more summary{cursor:pointer;font-weight:600;color:var(--brand);font-size:.9rem;margin-top:6px}
.since p{font-size:.92rem}
.since h2::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--chg);margin-right:8px;vertical-align:2px}

/* Coverage table */
.cov-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin-top:12px}
table.cov{width:100%;border-collapse:collapse;font-size:.88rem}
table.cov th,table.cov td{text-align:left;padding:10px 12px;border-top:1px solid var(--line);vertical-align:top}
table.cov thead th{border-top:0;background:var(--surface-2);color:var(--muted);font-size:.74rem;font-weight:650;
text-transform:uppercase;letter-spacing:.05em}
table.cov tr.region td:first-child{padding-left:28px;color:var(--muted)}
.pill{display:inline-block;font-size:.72rem;font-weight:650;padding:2px 9px;border-radius:999px;white-space:nowrap;
vertical-align:middle;background:var(--weak-bg);color:var(--weak);letter-spacing:0}
.pill.covered{color:var(--strong);background:var(--strong-bg)}
.pill.partial{color:var(--possible);background:var(--possible-bg)}

/* Filters sidebar */
details.tools{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);margin:0;box-shadow:var(--shadow)}
details.tools>summary{cursor:pointer;font-weight:650;padding:14px 18px;list-style:none;display:flex;align-items:center;gap:8px}
details.tools[open]>summary{border-bottom:1px solid var(--line)}
details.tools .tools-body{padding:16px 18px 18px;display:grid;gap:16px}
.filters{display:grid;gap:14px;margin:0}
.filters label,.toggles>label{display:flex;flex-direction:column;gap:6px;font-size:.74rem;font-weight:650;color:var(--muted);
text-transform:uppercase;letter-spacing:.05em}
.filters select,.filters input[type=search]{font:inherit;font-size:.92rem;letter-spacing:0;text-transform:none;color:var(--ink);
background:var(--surface-2);padding:9px 11px;border:1px solid var(--line);border-radius:9px;width:100%;min-width:0;
transition:border-color .15s,background .15s}
.filters select:hover,.filters input[type=search]:hover{border-color:var(--line-strong)}
.filters select:focus,.filters input[type=search]:focus{background:var(--surface);border-color:var(--brand)}
.filters .check,.toggles .check{flex-direction:row;align-items:center;gap:9px;text-transform:none;letter-spacing:0;
font-size:.9rem;font-weight:500;color:var(--ink);cursor:pointer}
input[type=checkbox]{accent-color:var(--brand);width:16px;height:16px;margin:0;flex:none}
.filters .sep,.toggles{border-top:1px solid var(--line);padding-top:14px}
.toggles{display:grid;gap:10px}
.side-label{font-size:.72rem;font-weight:650;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;margin:0}
.downloads{display:grid;gap:8px;border-top:1px solid var(--line);padding-top:14px}
.downloads a{font-size:.88rem}

/* Buttons */
.btn,.watch-btn,.toggles button{display:inline-flex;align-items:center;justify-content:center;gap:6px;font:inherit;font-size:.86rem;
font-weight:600;padding:7px 13px;border-radius:9px;border:1px solid var(--line-strong);background:var(--surface);color:var(--ink);
cursor:pointer;text-decoration:none;line-height:1.3;transition:background .15s,border-color .15s}
.btn:hover,.watch-btn:hover,.toggles button:hover{border-color:var(--faint);background:var(--surface-2);text-decoration:none}
.btn.primary{background:var(--brand);border-color:var(--brand);color:var(--brand-ink)}
.btn.primary:hover{background:var(--brand);filter:brightness(1.08)}
.btn.block{width:100%}
.btn.quiet{border-color:transparent;background:transparent;color:var(--muted);padding-left:0;padding-right:0}
.btn.quiet:hover{background:transparent;color:var(--ink)}
.watch-btn::before{content:"\2606";font-size:1rem;line-height:1}
.watch-btn[aria-pressed="true"]{background:var(--brand-soft);color:var(--brand);border-color:var(--brand)}
.watch-btn[aria-pressed="true"]::before{content:"\2605"}

/* Results bar and view tabs */
.results-bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin:30px 0 4px}
.results-bar h2{margin:0;font-size:1.25rem}
#result-count{margin:0;color:var(--muted);font-size:.9rem}
.views{display:inline-flex;gap:2px;padding:3px;background:var(--surface-2);border:1px solid var(--line);border-radius:11px;
max-width:100%;overflow-x:auto;scrollbar-width:none}
.views label{position:relative;padding:7px 14px;border-radius:8px;font-size:.87rem;font-weight:600;color:var(--muted);
cursor:pointer;white-space:nowrap;transition:color .15s,background .15s}
.views label:hover{color:var(--ink)}
.views input{position:absolute;opacity:0;width:1px;height:1px}
.views label:has(input:checked){background:var(--surface);color:var(--ink);box-shadow:0 1px 2px rgba(15,28,43,.1),0 0 0 1px var(--line)}
.views label:has(input:focus-visible){outline:2px solid var(--focus);outline-offset:-2px}

/* Groups and tiers */
section.country>h2,section.group>h2{display:flex;align-items:center;flex-wrap:wrap;gap:10px;font-size:1.2rem;
margin:30px 0 6px;padding-bottom:10px;border-bottom:1px solid var(--line)}
section.country>p.country-note{color:var(--muted);margin:0 0 8px;font-size:.9rem;max-width:95ch}
section.tier h3{display:flex;align-items:center;gap:9px;margin:20px 0 2px;font-size:.98rem}
section.tier h3::before{content:"";width:10px;height:10px;border-radius:50%;background:var(--weak);flex:none}
section.tier[data-tier=strong] h3::before{background:var(--strong)}
section.tier[data-tier=possible] h3::before{background:var(--possible)}
section.tier p.tier-desc{color:var(--muted);margin:0 0 4px 19px;font-size:.86rem}
.count{display:inline-block;font-size:.74rem;font-weight:650;color:var(--muted);background:var(--surface-2);
border:1px solid var(--line);padding:0 9px;border-radius:999px;line-height:1.7;letter-spacing:0}

/* Cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,430px),1fr));gap:16px;margin-top:12px}
.card{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:20px 20px 16px;
display:flex;flex-direction:column;min-width:0;box-shadow:var(--shadow);overflow:hidden;transition:box-shadow .18s,border-color .18s}
.card::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;background:var(--weak)}
.card[data-tier=strong]::before{background:var(--strong)}
.card[data-tier=possible]::before{background:var(--possible)}
.card:hover{box-shadow:var(--shadow-lg);border-color:var(--line-strong)}
.card:target{border-color:var(--brand);box-shadow:0 0 0 3px var(--brand-soft)}
.card h4{margin:12px 0 0;font-size:1.06rem;line-height:1.35}
.card h4 a{color:var(--ink)}
.card h4 a:hover{color:var(--brand);text-decoration:none}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:0}
.badge{font-size:.72rem;font-weight:650;padding:2px 9px;border-radius:999px;white-space:nowrap;line-height:1.55;border:1px solid transparent}
.tier-badge::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor;margin-right:6px;vertical-align:1px}
.t-strong{color:var(--strong);background:var(--strong-bg)}
.t-possible{color:var(--possible);background:var(--possible-bg)}
.t-weak{color:var(--weak);background:var(--weak-bg)}
.t-watch{color:var(--muted);border-color:var(--line)}
.score{color:var(--ink-2);border-color:var(--line-strong)}
.late{color:var(--late);background:var(--late-bg)}
.st-open{color:var(--open);background:var(--open-bg)}
.st-other{color:var(--muted);border-color:var(--line)}
.st-lead{color:var(--strong);background:var(--strong-bg)}
ol.shortlist{list-style:none;padding:0;margin:20px 0}
.badge.rank{color:var(--brand-ink);background:var(--brand)}
.shortlist-intro p{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:24px 0 0;color:var(--muted);font-size:.9rem}
.badge.fresh{color:var(--brand);background:var(--brand-soft)}
abbr[title]{text-decoration:underline dotted;text-underline-offset:3px;cursor:help}
.result-actions{margin:2px 0 0;display:flex;flex-wrap:wrap;gap:4px 14px}
button.linklike{font:inherit;font-size:.86rem;font-weight:600;color:var(--brand);background:none;border:0;padding:0;cursor:pointer;
text-decoration:underline;text-underline-offset:3px}
.rev{color:var(--strong);border-color:currentColor}
.rev::before{content:"\2713\00a0"}
.auto{color:var(--muted);border:1px dashed var(--line-strong)}
.lang{color:var(--muted);border-color:var(--line)}
.badge.chg{color:var(--chg);background:var(--chg-bg)}
dl.facts{display:grid;grid-template-columns:8.6em minmax(0,1fr);gap:5px 14px;margin:14px 0;padding:14px 0;font-size:.88rem;
border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
dl.facts dt{color:var(--muted)}
dl.facts dd{margin:0;min-width:0;overflow-wrap:anywhere;color:var(--ink-2)}
.countdown{font-weight:650;color:var(--ink)}
.card p.summary{margin:0 0 12px;max-width:75ch;font-size:.93rem;color:var(--ink-2)}
details.evidence{margin:0 0 14px}
details.evidence>summary{cursor:pointer;font-size:.86rem;font-weight:600;color:var(--brand);list-style:none;display:inline-flex;align-items:center;gap:7px}
details.evidence>summary::before{content:"";width:6px;height:6px;border-right:2px solid currentColor;border-bottom:2px solid currentColor;
transform:rotate(-45deg);transition:transform .15s}
details.evidence[open]>summary::before{transform:rotate(45deg)}
ul.quotes{margin:8px 0 0;padding:0;list-style:none;display:grid;gap:8px}
ul.quotes li{border-left:3px solid var(--line-strong);background:var(--surface-2);border-radius:0 8px 8px 0;padding:9px 12px;
color:var(--ink-2);font-size:.89rem;max-width:75ch}
.card-foot{margin-top:auto;padding-top:14px;border-top:1px solid var(--line)}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 10px}
p.source{margin:3px 0;color:var(--muted);font-size:.85rem}
p.attr{margin:6px 0 0;color:var(--faint);font-size:.78rem}
.empty{padding:32px;text-align:center;color:var(--muted);background:var(--surface);border:1px dashed var(--line-strong);border-radius:var(--radius);margin:16px 0}

/* Project page */
.crumbs{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:24px 0 14px;font-size:.86rem;color:var(--muted)}
.crumbs a{color:var(--muted)}
.crumbs a:hover{color:var(--brand)}
.crumbs span[aria-hidden]{color:var(--faint)}
article.project{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:24px;align-items:start;margin:0 0 8px}
.project-main{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:28px 32px 30px;box-shadow:var(--shadow);
position:relative;overflow:hidden;min-width:0}
.project-main::before{content:"";position:absolute;left:0;right:0;top:0;height:4px;background:var(--weak)}
article.project[data-tier=strong] .project-main::before{background:var(--strong)}
article.project[data-tier=possible] .project-main::before{background:var(--possible)}
.project-main h1{margin:14px 0 6px;font-size:clamp(1.45rem,2.6vw,1.95rem);letter-spacing:-.02em}
.project-main .place{margin:0 0 18px;color:var(--muted)}
.project-main p.summary{font-size:1.02rem;color:var(--ink-2);max-width:72ch;margin:0}
.project-main h2{font-size:1.05rem;margin:28px 0 10px;padding-top:22px;border-top:1px solid var(--line)}
.project-main ul.quotes li{font-size:.93rem}
.project-side{display:grid;gap:16px;position:sticky;top:80px}
.side-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px;box-shadow:var(--shadow)}
.side-card h2{margin:0 0 12px;font-size:.74rem;font-weight:650;color:var(--faint);text-transform:uppercase;letter-spacing:.06em}
.project-side dl.facts{grid-template-columns:1fr;gap:0;margin:0;padding:0;border:0}
.project-side dl.facts dt{font-size:.78rem}
.project-side dl.facts dd{margin:0 0 10px}
.project-side dl.facts dd:last-of-type{margin-bottom:0}
.project-side .actions{display:grid;gap:8px;margin:0}
.scorebox{display:flex;align-items:baseline;gap:6px}
.scorebox .big{font-size:2rem;font-weight:700;letter-spacing:-.02em;line-height:1}
.scorebox .of{color:var(--muted);font-size:.9rem}
.meter{height:6px;border-radius:99px;background:var(--surface-2);border:1px solid var(--line);overflow:hidden;margin:10px 0 8px}
.meter span{display:block;height:100%;width:calc(var(--s) * 1%);background:var(--weak);border-radius:99px}
article.project[data-tier=strong] .meter span{background:var(--strong)}
article.project[data-tier=possible] .meter span{background:var(--possible)}
ol.timeline{list-style:none;padding:0;margin:10px 0}
ol.timeline li{position:relative;padding:0 0 16px 24px;font-size:.92rem;color:var(--ink-2)}
ol.timeline li::before{content:"";position:absolute;left:0;top:.35em;width:11px;height:11px;border-radius:50%;
background:var(--surface);border:2px solid var(--line-strong);z-index:1}
ol.timeline li::after{content:"";position:absolute;left:5.5px;top:1.1em;bottom:-.3em;width:2px;background:var(--line);transform:translateX(-50%)}
ol.timeline li:last-child::after{display:none}
ol.timeline li.official::before{background:var(--open);border-color:var(--open)}
ol.timeline time{display:block;font-weight:650;font-size:.8rem;color:var(--muted)}
ol.timeline li.official time{color:var(--open)}
ul.news{padding-left:18px;margin:8px 0}
ul.news li{margin:6px 0}
.gone{background:var(--late-bg);color:var(--late);padding:12px 16px;border-radius:12px;margin:20px 0 0;font-weight:500}

/* About */
.page-head{background:radial-gradient(1000px 360px at 85% -10%,rgba(91,191,223,.2),transparent 60%),
linear-gradient(135deg,var(--hero-1),var(--hero-2));color:#fff}
.page-head .wrap{padding-top:40px;padding-bottom:36px}
.page-head h1{color:#fff;margin:0 0 10px;font-size:clamp(1.6rem,3vw,2.2rem);letter-spacing:-.02em}
.page-head p{margin:0;color:#c3d4e0;max-width:66ch}
.page-head p.meta{color:#9fb6c6;margin-top:10px;font-size:.86rem}
.about-layout{display:grid;grid-template-columns:220px minmax(0,1fr);gap:36px;align-items:start;padding-top:32px}
nav.toc{position:sticky;top:84px;display:grid;gap:1px}
nav.toc p{margin:0 0 8px}
nav.toc a{display:block;padding:6px 12px;border-left:2px solid var(--line);color:var(--muted);font-size:.88rem}
nav.toc a:hover{color:var(--ink);border-left-color:var(--brand);text-decoration:none}
.prose{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:8px 36px 32px;box-shadow:var(--shadow);min-width:0}
.prose h2{font-size:1.2rem;margin:30px 0 10px;padding-top:24px;border-top:1px solid var(--line)}
.prose h2:first-child{border-top:0;padding-top:20px;margin-top:0}
.prose p,.prose li{max-width:72ch;color:var(--ink-2)}
.prose li{margin:4px 0}

/* Footer */
footer{margin-top:56px;background:var(--surface);border-top:1px solid var(--line);padding:32px 0 44px;color:var(--muted);font-size:.86rem}
.foot-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:24px 48px}
footer p{max-width:80ch;margin:0 0 8px}
footer .brand{margin-bottom:10px;font-size:1rem}
footer nav{display:grid;gap:6px;align-content:start}
footer nav a{color:var(--muted)}
footer nav a:hover{color:var(--brand)}

@media (max-width:1040px){.layout{grid-template-columns:1fr;gap:16px}.sidebar{position:static;max-height:none;overflow:visible}
.filters{grid-template-columns:repeat(2,minmax(0,1fr))}.filters .sep{grid-column:1/-1}
article.project{grid-template-columns:1fr}.project-side{position:static}
.about-layout{grid-template-columns:1fr;gap:20px}nav.toc{position:static;display:flex;flex-wrap:wrap;gap:6px}
nav.toc p{width:100%}nav.toc a{border:1px solid var(--line);border-radius:999px;padding:4px 12px;background:var(--surface)}
.stats{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (min-width:1041px){details.tools>summary{pointer-events:none}details.tools>summary::after{display:none}}
@media (max-width:640px){.wrap{padding:0 16px}body{font-size:14.5px}.topbar .updated{display:none}.topbar .wrap{gap:10px}
.brand span{display:none}nav.top a{padding:6px 7px;font-size:.84rem}.hero .wrap{padding-top:30px;padding-bottom:28px}
.stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:18px}.stats .stat-countries{display:none}
.stats a{padding:9px 12px;border-radius:10px}.stats .n{font-size:1.3rem}.stats .l{font-size:.74rem}.stats-for{margin:18px 0 -10px}
.filters{grid-template-columns:1fr}section.explain ul{grid-template-columns:1fr}
dl.facts{grid-template-columns:1fr;gap:0}dl.facts dd{margin-bottom:8px}
section.soon li{grid-template-columns:auto 1fr}section.soon li a,section.soon .meta{grid-column:1/-1}
.card{padding:18px 16px 14px}.project-main{padding:22px 18px}.prose{padding:4px 18px 24px}
.views{display:grid;grid-template-columns:1fr 1fr;width:100%;overflow:visible}.views label{text-align:center;padding:7px 8px;white-space:normal}
.foot-grid{grid-template-columns:1fr}}
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
  // Cards that only a buyer profile lists sit in #profile-only; they never show under "All habitat work".
  var cards = Array.prototype.slice.call(document.querySelectorAll('#server-list .card, #profile-only .card'));
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
  // Buyer profiles. "all" is the general score. The choice is kept only in this browser (localStorage)
  // and can be shared with ?profile=<key>.
  var pdEl = $('profile-data'), PD = pdEl ? JSON.parse(pdEl.textContent) : {profiles: [], cards: {}},
      fp = $('f-profile'), PSTORE = 'mpf-profile-v1', PKEYS = [], PCOUNT = {}, PDESC = {all: PD.all ? PD.all.short : ''};
  var PAUD = {}, PFRESH = {};
  PD.profiles.forEach(function(m){ PKEYS.push(m.key); PCOUNT[m.key] = m.count; PDESC[m.key] = m.short;
                                   PAUD[m.key] = m.audience; PFRESH[m.key] = m.exclude_freshwater; });
  // Product makers care about the project stage, not the comment deadline: approved projects are leads.
  var PSTAGE = {open: 'Under assessment, comments open', closed: 'Under assessment, comments closed',
                assessment: 'Under assessment', decided: 'Approved, may not be built yet', withdrawn: 'Withdrawn or rejected',
                unknown: 'Stage unknown', watch: 'Project watch'};
  function productView(){ return PAUD[prof()] === 'product_maker'; }
  var tf = $('t-fresh'), tfl = $('t-fresh-label');
  function prof(){ return fp && fp.value ? fp.value : 'all'; }
  function pinfo(c){ var p = prof(); return p === 'all' ? null : ((PD.cards[c.getAttribute('data-id')] || {})[p] || null); }
  function tierOf(c){ if (prof() === 'all') return c.getAttribute('data-gtier'); var i = pinfo(c); return i ? i[0] : 'none'; }
  function scoreOf(c){ var i = pinfo(c); return i ? i[1] : +c.getAttribute('data-score'); }
  function listed(t){ return t === 'strong' || t === 'possible' || t === 'weak' || t === 'watch'; }
  cards.forEach(function(c){ var b = c.querySelector('.tier-badge'), s = c.querySelector('.badge.score');
    c._gb = {text: b.textContent, cls: b.className}; c._gs = s ? s.textContent : ''; });
  // With a profile chosen, the card shows that profile's tier and score, why it matches, and the general score.
  function paint(c){
    var i = pinfo(c), b = c.querySelector('.tier-badge'), s = c.querySelector('.badge.score'),
        bc = c.querySelector('.because'), gl = c.querySelector('.general-line'), gt = c.getAttribute('data-gtier');
    var st = c.querySelector('.stage-badge'), late = c.querySelector('.late'), k = c.getAttribute('data-stage-now');
    if (c._st === undefined) { c._st = st ? {text: st.textContent, cls: st.className} : null; c._late = late ? late.hidden : true; }
    if (productView()) {
      if (st) { st.textContent = PSTAGE[k] || st.textContent; st.className = 'badge stage-badge ' + (k === 'decided' ? 'st-lead' : 'st-other'); }
      if (late) late.hidden = true;                 // "Likely too late" is about commenting, not buying
    } else {
      if (st && c._st) { st.textContent = c._st.text; st.className = c._st.cls; }
      if (late) late.hidden = c._late;
    }
    if (!i) { c.setAttribute('data-tier', gt); b.textContent = c._gb.text; b.className = c._gb.cls;
              if (s) s.textContent = c._gs; if (bc) bc.hidden = true; if (gl) gl.hidden = true; return; }
    c.setAttribute('data-tier', i[0]); b.textContent = TIER_NAMES[i[0]] || i[0]; b.className = 'badge tier-badge t-' + i[0];
    if (s) s.textContent = 'Profile score ' + i[1] + '/100';
    if (bc) { bc.textContent = 'Matches because: ' + i[2]; bc.hidden = false; }
    if (gl) { gl.textContent = 'General score ' + c.getAttribute('data-score') + '/100, ' +
                (gt === 'none' ? 'not on the general list' : (TIER_NAMES[gt] || gt)) + '.'; gl.hidden = false; }
  }
  // Freshwater projects are hidden under profiles that leave them out, unless "Include freshwater" is ticked.
  function freshOk(c, withFresh){
    return !(PFRESH[prof()] && c.getAttribute('data-fresh') && !(withFresh || (tf && tf.checked)));
  }
  // Banner figures: for the chosen profile, every country, ignoring the filters below (except freshwater, which
  // the profile itself leaves out, so the figures agree with the list).
  function paintStats(){
    var mine = cards.filter(function(c){ return listed(tierOf(c)) && freshOk(c); }), countries = {}, sources = {};
    function n(t){ return mine.filter(function(c){ return tierOf(c) === t; }).length; }
    mine.forEach(function(c){ countries[c.getAttribute('data-country')] = 1; if (c.getAttribute('data-source')) sources[c.getAttribute('data-source')] = 1; });
    var nc = Object.keys(countries).length, ns = Object.keys(sources).length;
    var set = function(id, v){ var e = $(id); if (e) e.textContent = v; };
    set('stat-total', mine.length); set('stat-strong', n('strong')); set('stat-possible', n('possible'));
    set('stat-soon', mine.filter(function(c){ return tierOf(c) !== 'watch' && c.getAttribute('data-stage-now') === 'open' &&
      c._days !== null && c._days >= 0 && c._days <= SOON; }).length);
    set('stat-countries', nc);
    set('stat-countries-l', (nc === 1 ? 'Country' : 'Countries') + ' with listings' + (ns ? ', ' + ns + (ns === 1 ? ' source' : ' sources') : ''));
    set('stats-for-name', prof() === 'all' ? (PD.all ? PD.all.label : 'All habitat work')
      : (PD.profiles.filter(function(m){ return m.key === prof(); })[0] || {label: prof()}).label);
  }
  function stored(){ try { return localStorage.getItem(PSTORE) || ''; } catch (e) { return ''; } }
  function store(k){ try { localStorage.setItem(PSTORE, k); } catch (e) {} }
  function shareUrl(){
    var base = location.href.split('#')[0].split('?')[0];
    var qs = location.search.replace(/^\?/, '').split('&').filter(function(x){ return x && x.indexOf('profile=') !== 0; });
    if (prof() !== 'all') qs.push('profile=' + encodeURIComponent(prof()));
    return base + (qs.length ? '?' + qs.join('&') : '');
  }
  function paintBar(){
    var l = $('profile-link'), d = $('profile-desc'), pe = $('profile-empty');
    if (l) l.href = shareUrl();
    if (d) d.textContent = PDESC[prof()] || '';
    if (pe) pe.hidden = prof() === 'all' || PCOUNT[prof()] > 0;
  }
  function syncUrl(){ try { history.replaceState(null, '', shareUrl() + location.hash); } catch (e) {} paintBar(); }
  var STAGE_ORDER = ['open', 'assessment', 'closed', 'decided', 'withdrawn', 'watch', 'unknown'];
  var MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  function placeOk(c){ return (!fc.value || c.getAttribute('data-country') === fc.value) &&
                              (!fr.value || c.getAttribute('data-region') === fr.value); }
  function visible(c, v, watch, withWeak, withFresh){
    var k = c.getAttribute('data-stage-now'), t = tierOf(c), q = (fq.value || '').toLowerCase().trim();
    if (!listed(t)) return false;      // not listed under the chosen profile
    if (!freshOk(c, withFresh)) return false;
    // Closing soon, changed and watch list views show every published tier.
    var tierOk = v !== 'all' || t === 'strong' || t === 'possible' || t === 'watch' || (t === 'weak' && (fw.checked || withWeak));
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
    var s = fsort.value, sa = scoreOf(a), sb = scoreOf(b);
    if (s === 'deadline') { var da = a._days === null ? 1e9 : (a._days < 0 ? 1e8 - a._days : a._days),
                                db = b._days === null ? 1e9 : (b._days < 0 ? 1e8 - b._days : b._days);
                            return da - db || sb - sa; }
    if (s === 'score') return sb - sa;
    if (s === 'newest') return (b.getAttribute('data-first') || '').localeCompare(a.getAttribute('data-first') || '') || sb - sa;
    if (s === 'title') return a.getAttribute('data-title').localeCompare(b.getAttribute('data-title'));
    var first = productView() ? 'decided' : 'open';
    var ao = a.getAttribute('data-stage-now') === first ? 0 : 1, bo = b.getAttribute('data-stage-now') === first ? 0 : 1;
    return TIERS.indexOf(tierOf(a)) - TIERS.indexOf(tierOf(b)) || ao - bo || sb - sa;
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
      var span = document.createElement('span'); span.className = 'count'; span.textContent = String(n); h.appendChild(span); s.appendChild(h); }
    var d = document.createElement('div'); d.className = 'cards'; s.appendChild(d); return s;
  }
  var shownOrder = [];
  function apply(){
    var v = currentView(), watch = MPF.getWatch();
    cards.forEach(paint);
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
          var cs = g.cards.filter(function(c){ return tierOf(c) === t; });
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
      : prof() !== 'all' && !PCOUNT[prof()] ? PD.empty_note
      : (function(){ var o = fc.options[fc.selectedIndex], m = o && /\(only under (.+)\)$/.exec(o.text);
          return m && prof() === 'all' ? 'Projects in this country appear only under ' + m[1] +
            '. Choose it in "I work on" above.' : 'No projects match these filters.'; })();
    // Closing soon list at the top: every published tier in the chosen country or region.
    while (ul.firstChild) ul.removeChild(ul.firstChild);
    var soon = cards.filter(function(c){ var t = tierOf(c); return placeOk(c) && listed(t) && t !== 'watch' && freshOk(c) &&
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
    line.hidden = soon.length === 0;      // "No comment deadlines ..." says it once
    var rc = $('result-count');
    if (rc) {
      var hw = v === 'all' && !fw.checked ? cards.filter(function(c){ return tierOf(c) === 'weak' && visible(c, v, watch, true); }).length : 0;
      var hf = PFRESH[prof()] && !(tf && tf.checked) ? cards.filter(function(c){ return c.getAttribute('data-fresh') && visible(c, v, watch, false, true); }).length : 0;
      rc.textContent = shown.length + (shown.length === 1 ? ' project shown' : ' projects shown') +
        (hw ? '. ' + hw + ' Weak ' + (hw === 1 ? 'match is' : 'matches are') + ' hidden' : '') +
        (hf ? '. ' + hf + ' freshwater ' + (hf === 1 ? 'project is' : 'projects are') + ' hidden' : '') + (hw || hf ? '.' : '');
      var sw = $('show-weak'), sf = $('show-fresh');
      if (sw) sw.hidden = !hw;
      if (sf) sf.hidden = !hf;
    }
    if (tfl) tfl.hidden = !PFRESH[prof()];
    paintStats();
  }
  if (fp) {
    var want = param('profile') || stored();
    if (want === 'all' || PKEYS.indexOf(want) >= 0) fp.value = want;
    if (prof() !== 'all') syncUrl(); else paintBar();
    fp.addEventListener('change', function(){ store(prof()); syncUrl(); apply(); });
    var cp = $('profile-copy');
    if (cp) cp.addEventListener('click', function(){
      var url = shareUrl();
      function done(t){ cp.textContent = t; setTimeout(function(){ cp.textContent = 'Copy link'; }, 2500); }
      if (navigator.clipboard && navigator.clipboard.writeText)
        navigator.clipboard.writeText(url).then(function(){ done('Link copied'); }, function(){ done('Copy the link from the address bar'); });
      else done('Copy the link from the address bar');
    });
  }
  if (tf) tf.addEventListener('change', apply);
  var swb = $('show-weak'), sfb = $('show-fresh');
  if (swb) swb.addEventListener('click', function(){ fw.checked = true; apply(); });
  if (sfb) sfb.addEventListener('click', function(){ if (tf) tf.checked = true; apply(); });
  var reset = $('reset-btn');
  if (reset) reset.addEventListener('click', function(){
    fq.value = ''; fc.value = ''; fillRegions(); ft.value = ''; fs.value = ''; fo.checked = false; fw.checked = false;
    fsort.value = 'default'; fgroup.value = 'country'; $('v-all').checked = true; if (tf) tf.checked = false; apply();
  });
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
  // On phones and tablets the filters sit above the list, so they start folded away.
  var tools = $('tools');
  if (tools && window.matchMedia && window.matchMedia('(max-width: 1040px)').matches) tools.open = false;
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


# A wave over a marker: the site's mark, drawn inline so the page needs no image files.
LOGO = ('<svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#0b6684"/>'
        '<path d="M6 19c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2 2.5 2 5 2" fill="none" stroke="#fff" stroke-width="2.2" '
        'stroke-linecap="round"/><path d="M6 24c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2 2.5 2 5 2" fill="none" '
        'stroke="#8fd3ea" stroke-width="2.2" stroke-linecap="round"/><circle cx="16" cy="10" r="3.2" fill="#8fd3ea"/></svg>')


def _date(d: date) -> str:
    return d.strftime("%B %-d, %Y")


def _topbar(site: dict, updated: date, active: str = "", prefix: str = "") -> str:
    """Sticky bar on every page. prefix is "../" on project pages."""
    title = escape(site.get("title", "MitiMark"))
    links = [("projects", "index.html", "Projects"), ("soon", "index.html#soon", "Closing soon"),
             ("coverage", "index.html#coverage", "Coverage"), ("about", "about.html", "About")]
    current = ' aria-current="page"'
    nav = "".join(f'<a href="{prefix}{href}"{current if key == active else ""}>{label}</a>'
                  for key, href, label in links)
    return (f'<a class="skip" href="#main">Skip to content</a>\n'
            f'<header class="topbar"><div class="wrap">\n<a class="brand" href="{prefix}index.html">{LOGO}<span>{title}</span></a>\n'
            f'<nav class="top" aria-label="Main">{nav}</nav>\n'
            f'<p class="updated">Last successful update: <time>{_date(updated)}</time></p>\n</div></header>')


def _footer(site: dict, prefix: str = "") -> str:
    email = escape(site.get("contact_email", ""))
    title = escape(site.get("title", "MitiMark"))
    return (
        '<footer><div class="wrap foot-grid"><div>'
        f'<a class="brand" href="{prefix}index.html">{LOGO}<span>{title}</span></a>'
        f"<p>{title} is a prototype. Flags show projects that may need marine habitat work. They do not state that "
        "mitigation is required. Always check the official notice. Compiled from public sources.</p>"
        f'<p>Contact: {escape(site.get("contact_name", ""))}, <a href="mailto:{email}">{email}</a></p></div>'
        f'<nav aria-label="Footer"><a href="{prefix}index.html">Projects</a><a href="{prefix}index.html#soon">Closing soon</a>'
        f'<a href="{prefix}shortlist.html">Hard-structure shortlist</a><a href="{prefix}about.html">About and method</a><a href="{prefix}about.html#coverage">Coverage</a>'
        f'<a href="{prefix}projects.csv">All listed projects (CSV)</a></nav>'
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
            f'{review_badge}{lang_badge}{chg}'
            f'{"<span class=" + chr(34) + "badge fresh" + chr(34) + ">Freshwater</span>" if rec.get("freshwater") else ""}</div>')


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


def _card(rec: dict, country_name: str, less_tested: list, country_order: int = 0, site: dict | None = None) -> str:
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
        received = _comment_count_text(rec)
        comments = (f'<dt>Comments open</dt><dd>{_opens_text(rec)}</dd>\n'
                    f'<dt>Comments close</dt><dd><span class="countdown">{escape(deadline_text)}</span></dd>'
                    # The project page always shows the count line; cards only when the registry publishes one.
                    + ('' if received == NOT_PUBLISHED else f'\n<dt>Comments received</dt><dd>{received}</dd>'))
    else:
        comments = f'<dt>Comment deadline</dt><dd><span class="countdown">{escape(deadline_text)}</span></dd>'
    title_link = page or link
    if rec.get("profile_only"):
        # Not on the general list: shown only under a buyer profile, where the script sets its tier.
        rec = {**rec, "tier": "none", "tier_label": NOT_GENERAL}
    return f"""<article class="card" id="{anchor}" data-id="{escape(rec['id'])}" data-tier="{rec['tier']}" data-gtier="{rec['tier']}" data-fresh="{'1' if rec.get('freshwater') else ''}" data-source="{escape(rec.get('source') or '')}" data-country="{escape(rec.get('country') or UNKNOWN)}"
 data-country-name="{escape(country_name)}" data-country-order="{country_order}" data-region="{escape(rec['province'])}"
 data-types="{escape('|'.join(types))}" data-stage="{rec['stage_kind']}" data-deadline="{escape(deadline or '')}"
 data-score="{int(rec['score'])}" data-first="{escape(rec.get('first_listed') or '')}"
 data-changes="{escape('|'.join(rec.get('changes') or []))}" data-title="{escape(rec['title'].lower())}" data-search="{escape(search)}">
{_badges(rec, less_tested)}
<h4><a href="{title_link}">{_plain(escape(rec['title']))}</a></h4>
<p class="because" hidden></p>
<p class="general-line" hidden></p>
<dl class="facts">
<dt>Proponent</dt><dd>{escape(rec['proponent'])}</dd>
<dt>Location</dt><dd>{escape(location)}</dd>
<dt>Project type</dt><dd>{escape(', '.join(types) if types else UNKNOWN)}</dd>{_licences_row(rec)}
<dt>Stage</dt><dd>{_plain(escape(rec['stage']))}</dd>
{comments}
</dl>
{_summary_html(rec)}
{_evidence(rec, quotes)}
<div class="card-foot">
<div class="actions">{f'<a class="btn primary" href="{page}">Project page and timeline</a>' if page else ''}{_watch_button(rec)}{_feedback_link(rec, site)}</div>
{_source_lines(rec, link)}
</div>
</article>"""


# Registry jargon, explained where it appears (titles and stages; quotes are never changed).
JARGON = [
    (r"\bSCH\b", "SCH", "Small Craft Harbour: a fishing or recreational harbour run by Fisheries and Oceans Canada"),
    (r"Intent to Make a Determination", "Intent to Make a Determination",
     "The federal authority plans to decide whether the project is likely to cause significant adverse "
     "environmental effects. The public can comment before it decides."),
]


def _plain(escaped: str) -> str:
    """Wrap known jargon in <abbr> with a plain-language explanation. Input and output are escaped HTML."""
    for pat, word, meaning in JARGON:
        escaped = re.sub(pat, f'<abbr title="{escape(meaning)}">{word}</abbr>', escaped)
    return escaped


def feedback_url(rec: dict, site: dict | None) -> str:
    """The Google Form address with this project's id filled in, or "" while the settings hold the placeholder."""
    site = site or {}
    url, field = site.get("feedback_form_url") or "", site.get("feedback_form_project_field") or ""
    if not url or "YOUR_FORM_ID" in url:
        return ""
    from urllib.parse import quote
    sep = "&" if "?" in url else "?"
    return url + (f"{sep}usp=pp_url&{field}={quote(rec['id'])}" if field and "0000000000" not in field else "")


def _feedback_link(rec: dict, site: dict | None) -> str:
    url = feedback_url(rec, site)
    return (f'<a class="btn" href="{escape(url)}" target="_blank" rel="noopener">Feedback on this project</a>'
            if url else "")


NOT_GENERAL = "Not on the general list"


def listing_summary(records: list[dict]) -> str:
    """"31 on the general list, plus 1 shown only under a profile" (the second part only when there are any)."""
    extra = sum(1 for r in records if r.get("profile_only"))
    text = f"{len(records) - extra} on the general list"
    return text + (f", plus {extra} shown only under a profile" if extra else "")


def profile_meta(records: list[dict], scoring: dict | None = None) -> list[dict]:
    """Buyer profiles in config order: key, label, short description, and whether any project matches."""
    from mpf.config import load_scoring
    conf = (scoring or load_scoring()).get("profiles") or {}
    if not any(r.get("profiles") for r in records):
        return []
    out = []
    for key, p in conf.items():
        n = sum(1 for r in records if listed_in_profile(r, key))
        out.append({"key": key, "label": p["label"], "short": p.get("short", ""), "count": n,
                    "audience": p.get("audience", "comment"), "exclude_freshwater": bool(p.get("exclude_freshwater"))})
    return out


def profile_data(records: list[dict], meta: list[dict]) -> dict:
    """For the page script: per project, [tier, score, matches-because] for each profile that lists it."""
    cards = {}
    for r in records:
        d = {m["key"]: [r["profiles"][m["key"]]["tier"], r["profiles"][m["key"]]["score"],
                        r["profiles"][m["key"]]["because"]]
             for m in meta if listed_in_profile(r, m["key"])}
        if d:
            cards[r["id"]] = d
    return {"all": {"key": ALL, "label": ALL_LABEL, "short": "Every kind of marine habitat work. The general score."},
            "profiles": meta, "cards": cards, "empty_note": EMPTY_NOTE}


def _evidence(rec: dict, quotes_html: str) -> str:
    """Quotes fold away on the list to keep cards short; the project page shows them in full."""
    if not quotes_html:
        return ""
    n = len(rec["quotes"])
    return (f'<details class="evidence"><summary>Evidence from the notice ({n} {"quote" if n == 1 else "quotes"})</summary>'
            f'<ul class="quotes">{quotes_html}</ul></details>')


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


def country_option(c: dict, all_records: list[dict], meta: list[dict]) -> str:
    """The country menu entry for a country with nothing on the general list.
    "(coming soon)" only when MitiMark does not read it yet; a country it reads whose projects appear only under a
    buyer profile says which ("United States (only under Hard-structure)") and can be chosen."""
    code, name = escape(c["code"]), escape(c["name"])
    if c.get("status") == "coming_soon":
        return f'<option value="{code}" disabled>{name} (coming soon)</option>'
    labels = {m["key"]: m["label"].replace(" eco-engineering", "") for m in meta or []}
    under = [labels.get(k, k) for k in labels
             if any(r.get("profile_only") and r.get("country") == c["code"] and listed_in_profile(r, k)
                    for r in all_records)]
    if under:
        region = escape(c.get("region_label", ""))
        return (f'<option value="{code}" data-region-label="{region}">{name} (only under '
                f'{escape(" and ".join(under))})</option>')
    return f'<option value="{code}" disabled>{name} (nothing listed now)</option>'


def render_index(records: list[dict], site: dict, tiers: dict, updated: date, coverage: dict | None = None,
                 history_dates: list[str] | None = None, news_html: str = "", scoring: dict | None = None) -> str:
    coverage = coverage if coverage is not None else load_coverage()
    all_records = records
    meta = profile_meta(all_records, scoring)
    # The general list ("All habitat work") never shows projects that only a buyer profile lists.
    records = [r for r in all_records if not r.get("profile_only")]
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

    # One country order for every card (general list and profile-only), from the coverage page, so the page groups
    # a country's cards together whichever list they come from.
    order = {c["code"]: i for i, c in enumerate(coverage.get("countries", []))}
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
                f'<h3>{TIER_LABELS[t]} <span class="count">{len(recs)}</span></h3>\n'
                f'<p class="tier-desc">{escape(td[t])}</p>\n<div class="cards">\n'
                + "\n".join(_card(r, names.get(c["code"], c["code"]), less_tested, order.get(c["code"], 999), site)
                            for r in recs)
                + "\n</div>\n</section>")
        note = f'<p class="country-note">{escape(c.get("note", ""))}</p>' if c.get("note") else ""
        country_sections.append(
            f'<section class="country" data-country="{escape(c["code"])}" aria-label="{escape(c["name"])}">\n'
            f'<h2>{escape(c["name"])} <span class="count">{len(recs_c)}</span> {_pill(c["status"])}</h2>\n{note}\n'
            + "\n".join(tier_parts) + "\n</section>")
    body = "\n".join(country_sections) if country_sections else '<p class="empty">No projects yet.</p>'
    since = since_panel(records, history_dates or [])
    csv_data = json.dumps({"columns": csv_columns(meta), "rows": {r["id"]: csv_row(r, names, site, meta)
                                                                  for r in all_records}},
                          ensure_ascii=False).replace("</", "<\\/")
    extra_cards = "\n".join(_card(r, names.get(r.get("country"), r.get("country") or UNKNOWN), less_tested,
                                  order.get(r.get("country"), 999), site) for r in all_records if r.get("profile_only"))
    prof_json = json.dumps(profile_data(all_records, meta), ensure_ascii=False).replace("</", "<\\/")
    prof_opts = "".join(f'<option value="{escape(m["key"])}">{escape(m["label"])}'
                        f'{"" if m["count"] else " (no current matches)"}</option>' for m in meta)
    profile_bar = f"""<section class="profile-bar js-only" aria-label="Buyer profile" hidden><div class="wrap">
<div class="profile-pick"><label for="f-profile">I work on:</label>
<select id="f-profile"><option value="{ALL}">{ALL_LABEL}</option>{prof_opts}</select></div>
<p id="profile-desc" class="profile-desc"></p>
<div class="profile-share"><a id="profile-link" href="?">Shareable link</a>
<button type="button" class="btn" id="profile-copy">Copy link</button></div>
<p id="profile-empty" class="profile-empty" hidden>{escape(EMPTY_NOTE)}</p>
<p class="profile-note">Profiles are rule-based guesses, not a statement that mitigation is required.
Your choice is remembered only in this browser. <a href="about.html#buyer-profiles">How profiles work</a></p>
</div></section>""" if meta else ""
    tier_names = json.dumps({t: TIER_LABELS[t] for t in PUBLISHED_TIERS + ("watch",)})

    opts = ['<option value="">All countries</option>']
    with_data = {c["code"] for c in countries}
    for c in coverage.get("countries", []):
        if c["code"] in with_data:
            opts.append(f'<option value="{escape(c["code"])}" data-region-label="{escape(c.get("region_label", ""))}">'
                        f'{escape(c["name"])}</option>')
    for c in coverage.get("countries", []):
        if c["code"] not in with_data:
            opts.append(country_option(c, all_records, meta))
    cc = coverage_counts(coverage)
    lt = less_tested_note(coverage)
    bn = balance_note(records)
    counts = country_counts(records)
    n_tier = {t: sum(1 for r in records if r["tier"] == t) for t in PUBLISHED_TIERS}
    n_sources = len({r.get("source") for r in records if r.get("source")})
    freq = escape(site.get("update_frequency", "every 2 weeks"))
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(title, desc)}
</head>
<body data-soon="{soon_days}">
{_topbar(site, updated, "projects")}
<section class="hero"><div class="wrap">
<p class="eyebrow">{escape(title)} &middot; marine habitat early alerts</p>
<h1>{escape(desc)}</h1>
<p class="lede">Public project registries are read automatically and each notice is scored for signs of marine habitat
work. Every flag shows the words that triggered it and, where the source's terms allow, the exact sentences.</p>
<p class="meta">Last successful update: <time>{_date(updated)}</time>. Updated {freq}.</p>
<p class="stats-for" id="stats-for">Figures for: <strong id="stats-for-name">{ALL_LABEL}</strong></p>
<ul class="stats">
<li><a href="?show=weak#results"><span class="n" id="stat-total">{len(records)}</span><span class="l">Projects listed, all tiers</span></a></li>
<li><a href="#results"><span class="n" id="stat-strong">{n_tier['strong']}</span><span class="l"><span class="dot strong"></span>{TIER_LABELS['strong']}</span></a></li>
<li><a href="#results"><span class="n" id="stat-possible">{n_tier['possible']}</span><span class="l"><span class="dot possible"></span>{TIER_LABELS['possible']}</span></a></li>
<li><a href="#soon"><span class="n" id="stat-soon">{n_soon}</span><span class="l"><span class="dot soon"></span>Closing in {soon_days} days</span></a></li>
<li class="stat-countries"><a href="#coverage"><span class="n" id="stat-countries">{len(countries)}</span><span class="l" id="stat-countries-l">Countries with listings{f', {n_sources} sources' if n_sources else ''}</span></a></li>
</ul>
</div></section>
{profile_bar}
<main class="wrap layout" id="main">

<aside class="sidebar" aria-label="Filters">
<details class="tools" id="tools" open>
<summary>Filters and views</summary>
<div class="tools-body">
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
<label class="check sep"><input type="checkbox" id="f-open"> Comment period open only</label>
</form>
<div class="toggles">
<label class="check"><input type="checkbox" id="t-weak"> Show weaker matches</label>
<label class="check" id="t-fresh-label" hidden><input type="checkbox" id="t-fresh"> Include freshwater (lakes and rivers)</label>
<button type="button" id="reset-btn" class="btn quiet js-only" hidden>Reset all filters</button>
</div>
<div class="downloads">
<p class="side-label">Download</p>
<button type="button" id="csv-btn" class="btn block js-only" hidden>Download shown projects (CSV)</button>
<a href="projects.csv">All listed projects (CSV)</a>
</div>
</div>
</details>
</aside>

<div class="content">
<div class="stack">
<section class="panel soon" id="soon" aria-labelledby="soon-h">
<div class="panel-head"><h2 id="soon-h">Comment deadlines closing soon (next {soon_days} days)</h2>
<p class="note soon-note">Includes Weak matches.</p></div>
<p class="summary-line" id="summary-line">{n_soon} comment deadlines in the next {soon_days} days.</p>
<ul id="soon-list"></ul>
<p id="soon-none">No comment deadlines in the next {soon_days} days.</p>
<noscript><p>Turn on JavaScript for live countdowns, filters and search. Without it, all three tiers are listed below.</p></noscript>
</section>

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

<div class="row-2">
{since}
<details class="panel coverage" id="coverage">
<summary>Coverage: {cc['covered']} covered, {cc['partial']} partial, {cc['coming_soon']} coming soon{' (mostly Canada for now)' if bn else ''}</summary>
{f'<p>{escape(bn)}</p>' if bn else ''}
<div class="cov-wrap"><table class="cov"><thead><tr><th>Place</th><th>Status</th><th>Details</th></tr></thead>
<tbody>
{coverage_rows(coverage, counts)}
</tbody></table></div>
{f'<p class="note">{escape(lt)}</p>' if lt else ''}
</details>
</div>
{news_html}
</div>

<div class="results-bar" id="results">
<div><h2>Projects</h2><p id="result-count" aria-live="polite"></p>
<p class="result-actions"><button type="button" class="linklike" id="show-weak" hidden>Show Weak matches</button>
<button type="button" class="linklike" id="show-fresh" hidden>Include freshwater</button></p></div>
<div class="views js-only" role="radiogroup" aria-label="Which projects" hidden>
<label><input type="radio" name="view" id="v-all" value="all" checked> All projects</label>
<label><input type="radio" name="view" id="v-soon" value="soon"> Closing soon</label>
<label><input type="radio" name="view" id="v-changed" value="changed"> Changed since last update</label>
<label><input type="radio" name="view" id="v-watch" value="watch"> My watch list</label>
</div>
</div>
<p class="empty" id="no-match" hidden>No projects match these filters.</p>
<div id="view" hidden></div>
<div id="server-list">
{body}
</div>
<div id="profile-only" hidden>
{extra_cards}
</div>
</div>
</main>
{_footer(site)}
<script type="application/json" id="csv-data">{csv_data}</script>
<script type="application/json" id="tier-names">{tier_names}</script>
<script type="application/json" id="profile-data">{prof_json}</script>
<script>{JS_COMMON}{JS}</script>
</body>
</html>
"""


CSV_COLUMNS = ["id", "title", "country", "region", "proponent", "project_types", "stage", "tier", "score",
               "reviewed_by_a_person", "comments_open", "comments_close", "comments_received", "comments_received_as_of",
               "changes_since_last_update", "first_listed", "project_page", "official_notice", "source"]


def csv_columns(meta: list[dict] | None = None) -> list[str]:
    """The fixed columns, then one per buyer profile (e.g. profile_seagrass)."""
    return CSV_COLUMNS + ["profile_" + m["key"].replace("-", "_") for m in meta or []]


def _profile_cell(rec: dict, key: str) -> str:
    """"Possible (53)" when the profile lists the project, else empty."""
    if not listed_in_profile(rec, key):
        return ""
    p = rec["profiles"][key]
    return f'{p["tier_label"]} ({int(p["score"])})'


def csv_row(rec: dict, names: dict, site_conf: dict, meta: list[dict] | None = None) -> list:
    """One CSV row: facts only (no quotes, no summaries). Missing comment data is "Not published"."""
    c = rec.get("comments") or {}
    base = site_conf.get("url", "")
    return [rec["id"], rec["title"], names.get(rec.get("country"), rec.get("country")), rec["province"],
            rec["proponent"], "; ".join(rec["types"] or []), STAGE_LABELS.get(rec["stage_kind"], rec["stage"]),
            NOT_GENERAL if rec.get("profile_only") else rec["tier_label"], int(rec["score"]),
            "yes" if rec["reviewed"] else "no",
            c.get("opens") or NOT_PUBLISHED, c.get("closes") or NOT_PUBLISHED,
            c["count"] if c.get("count") is not None else NOT_PUBLISHED, c.get("count_as_of") or "",
            "; ".join(CHANGE_LABELS[x] for x in rec.get("changes") or []), rec.get("first_listed") or "",
            base + rec["page"] if rec.get("page") else "", rec["url"],
            rec.get("agency") or rec["source_name"]] + [_profile_cell(rec, m["key"]) for m in meta or []]


def write_csv(path: Path, records: list[dict], names: dict, site_conf: dict, meta: list[dict] | None = None) -> None:
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(csv_columns(meta))
        for r in records:
            w.writerow(csv_row(r, names, site_conf, meta))


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


def decision_word(rec: dict) -> str:
    """'Decided' only when an official decision is posted (the project is decided and its decision date is not
    in the future); otherwise the date is when a decision is due: 'Decision due'."""
    if rec.get("stage_kind") == "decided" and rec.get("approved_on"):
        return "Decided"
    return "Decision due"


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
        items.append(f'<li class="official"><time>{escape(rec["decision_date"])}</time> '
                     f'{decision_word(rec)} (official notice)</li>')
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


def _profiles_card(rec: dict, meta: list[dict] | None) -> str:
    """Every buyer profile that lists this project, with its tier, score and why."""
    if not meta:
        return ""
    items = []
    for m in meta:
        if not listed_in_profile(rec, m["key"]):
            continue
        p = rec["profiles"][m["key"]]
        items.append(f'<li><a href="../index.html?profile={escape(m["key"])}#results">{escape(m["label"])}</a> '
                     f'<span class="badge tier-badge t-{p["tier"]}">{escape(p["tier_label"])}</span> '
                     f'<span class="note">score {int(p["score"])}</span>'
                     f'<p class="because">Matches because: {escape(p["because"])}</p></li>')
    body = (f'<ul class="profile-list">{"".join(items)}</ul>' if items
            else '<p class="note">No buyer profile matches this project.</p>')
    return (f'<section class="side-card">\n<h2 class="side-h">Buyer profiles</h2>\n{body}\n'
            f'<p class="note">Rule-based guesses, not a statement that mitigation is required.</p>\n</section>')


def render_project_page(rec: dict, site: dict, updated: date, country_name: str, less_tested: list,
                        meta: list[dict] | None = None) -> str:
    title = site.get("title", "MitiMark")
    if rec.get("profile_only"):
        rec = {**rec, "tier": "none", "tier_label": NOT_GENERAL}
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
    official = "Official page" if rec["stage_kind"] == "watch" else "Official notice"
    score = int(rec["score"])
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(rec['title'] + ' - ' + title, rec.get('summary') or site.get('description', ''))}
</head>
<body data-soon="{int(site.get('closing_soon_days', 30))}">
{_topbar(site, updated, "", "../")}
<main class="wrap">
<nav class="crumbs" aria-label="Breadcrumb"><a href="../index.html">Projects</a><span aria-hidden="true">/</span>
<a href="../index.html?country={escape(rec.get('country') or '')}#results">{escape(country_name)}</a><span aria-hidden="true">/</span>
<span>{escape(rec['title'])}</span></nav>
<article class="project" id="main" data-id="{escape(rec['id'])}" data-tier="{rec['tier']}" data-stage="{rec['stage_kind']}" data-deadline="{escape(deadline or '')}">
<div class="project-main">
{_badges(rec, less_tested)}
<h1>{_plain(escape(rec['title']))}</h1>
<p class="place">{escape(_place(rec, country_name))} &middot; {escape(rec['proponent'])}</p>
{_summary_html(rec)}
{f'<h2>Evidence from the notice</h2><ul class="quotes">{quotes}</ul>' if quotes else ''}
<h2>Timeline</h2>
{_timeline_html(rec)}
<p class="note">Dates marked "official notice" come from the registry. Other entries are what this site saw
at each update.</p>
{_in_the_news_html(rec)}
<h2>Source</h2>
{_source_lines(rec, link)}
</div>
<aside class="project-side">
<section class="side-card">
<h2 class="side-h">Public comments</h2>
{comments}
</section>
<section class="side-card">
<div class="actions"><a class="btn primary" href="{link}" rel="noopener">{official} &#8599;</a>{_watch_button(rec)}{_feedback_link(rec, site)}</div>
</section>
<section class="side-card">
<h2 class="side-h">Score</h2>
<div class="scorebox"><span class="big">{score}</span><span class="of">/ 100 &middot; {escape(rec['tier_label'])}</span></div>
<div class="meter" style="--s:{score}"><span></span></div>
{'' if rec['tier_reason'].lower().startswith('score') else f'<p class="note">{escape(rec["tier_reason"])}</p>'}
</section>
{_profiles_card(rec, meta)}
<section class="side-card">
<h2 class="side-h">Key facts</h2>
<dl class="facts">
<dt>Proponent</dt><dd>{escape(rec['proponent'])}</dd>
<dt>Location</dt><dd>{escape(_place(rec, country_name))}. Water body: {escape(rec['water_body'])}</dd>
<dt>Project type</dt><dd>{escape(', '.join(types) if types else UNKNOWN)}</dd>{_licences_row(rec)}
<dt>Stage</dt><dd>{_plain(escape(rec['stage']))}</dd>
</dl>
</section>
</aside>
</article>
</main>
{_footer(site, "../")}
<script>{JS_COMMON}{JS_PAGE}</script>
</body>
</html>
"""


def _about_profiles(meta: list[dict] | None, tiers: dict) -> str:
    """About section: what buyer profiles are and what they are not."""
    if not meta:
        return ""
    rows = "".join(f'<li><strong>{escape(m["label"])}:</strong> {escape(m["short"])}'
                   f'{"" if m["count"] else " " + escape(EMPTY_NOTE)}</li>' for m in meta)
    pm = int(tiers.get("possible_min", 35))
    return f"""<h2 id="buyer-profiles">Buyer profiles</h2>
<p>At the top of the project list you can choose the kind of habitat work you do ("I work on: ..."). The list
then uses that profile's tiers and order, and each project says in plain words why it matches. "{ALL_LABEL}" is
the general score described above and is the default.</p>
<ul>{rows}</ul>
<p>Profiles are rule-based guesses. Each one re-weights the same visible rules (keywords, project types and sizes)
toward one kind of work, using the same Strong, Possible and Weak cut-offs. A project is listed for a profile only
if the notice mentions at least one of that profile's core subjects. A project that the general list leaves out
appears under a profile only if it reaches Possible ({pm} or more) there. A flag under any profile means the notice
may be relevant to that kind of work. It is not a statement that mitigation, compensation or any particular
measure is required.</p>
<p>Profiles read notices a little more strictly than the general score: a keyword does not count when the same
clause negates it or makes it conditional ("does not propose changes to the wharf", "if required"), sizes taken
from sentences about past works or distances ("within 250 m of the site") are not counted, and for the
hard-structure profile, small routine repairs (under 100 m long, 2,000 m&sup2; and 5,000 m&sup3;) stay Weak unless the
notice also mentions habitat work. Your profile choice is stored only in your browser. You can share a profile
with a link such as <code>?profile=seagrass</code>.</p>
"""


PRODUCT_STAGES = {"open": "Under assessment, comments open", "closed": "Under assessment, comments closed",
                  "assessment": "Under assessment", "decided": "Approved, may not be built yet", "withdrawn": "Withdrawn or rejected",
                  "unknown": "Stage unknown", "watch": "Project watch"}


def shortlist(records: list[dict], key: str, n: int = 10) -> list[dict]:
    """The buyer-test list for one profile, by the profile's tier (Strong, then Possible, then Weak). Within each tier,
    approved projects come first, then the rest, each group by the profile's score. Freshwater projects and projects
    the profile does not list are left out."""
    from mpf.profiles import listed_in_profile as listed
    pool = [r for r in records if listed(r, key) and not r.get("freshwater")
            and r["profiles"][key]["tier"] in PUBLISHED_TIERS]
    pool.sort(key=lambda r: (PUBLISHED_TIERS.index(r["profiles"][key]["tier"]),
                             0 if r["stage_kind"] == "decided" else 1, -r["profiles"][key]["score"], r["title"]))
    return pool[:n]


def general_feedback_url(site: dict | None) -> str:
    url = (site or {}).get("feedback_form_url") or ""
    return "" if not url or "YOUR_FORM_ID" in url else url


def _shortlist_item(rec: dict, rank: int, key: str, site: dict, country_name: str, less_tested: list) -> str:
    p = rec["profiles"][key]
    types = rec["types"] or []
    deadline, deadline_text = _deadline_text(rec)
    quotes = "".join(f"<li>&ldquo;{escape(q)}&rdquo;</li>" for q in rec["quotes"])
    link = escape(rec["url"]) if rec["url"] else "#"
    page = escape(rec.get("page") or "")
    review_badge = ('<span class="badge rev">Reviewed by a person</span>' if rec["reviewed"]
                    else '<span class="badge auto">Automatic, not yet reviewed</span>')
    lang = (f'<span class="badge lang">Less tested language ({escape(rec["language"])})</span>'
            if rec.get("language") in less_tested else "")
    stage = PRODUCT_STAGES.get(rec["stage_kind"], STAGE_LABELS.get(rec["stage_kind"], ""))
    general = NOT_GENERAL.lower() if rec.get("profile_only") else rec["tier_label"]
    when = rec.get("approved_on")
    approved = (f'<span class="badge approved-on"><time datetime="{escape(when)}">{escape(approved_label(when))}</time></span>'
                if when else "")
    return f"""<li class="card" id="s-{escape(page_slug(rec['id']))}" data-tier="{p['tier']}" data-stage="{rec['stage_kind']}" data-deadline="{escape(deadline or '')}">
<div class="badges"><span class="badge rank">#{rank}</span><span class="badge tier-badge t-{p['tier']}">{escape(p['tier_label'])}</span>
<span class="badge score">Profile score {int(p['score'])}/100</span>
<span class="badge {'st-lead' if rec['stage_kind'] == 'decided' else 'st-other'}">{escape(stage)}</span>{approved}{review_badge}{lang}</div>
<h4><a href="{page or link}">{_plain(escape(rec['title']))}</a></h4>
<p class="because">Matches because: {escape(p['because'])}</p>
<p class="general-line">General score {int(rec['score'])}/100, {escape(general)}.</p>
<dl class="facts">
<dt>Proponent</dt><dd>{escape(rec['proponent'])}</dd>
<dt>Location</dt><dd>{escape(_place(rec, country_name))}. Water body: {escape(rec['water_body'])}</dd>
<dt>Project type</dt><dd>{escape(', '.join(types) if types else UNKNOWN)}</dd>{_licences_row(rec)}
<dt>Stage</dt><dd>{_plain(escape(rec['stage']))}</dd>
<dt>Comments close</dt><dd><span class="countdown">{escape(deadline_text)}</span></dd>
</dl>
{_summary_html(rec)}
{_evidence(rec, quotes)}
<div class="card-foot">
<div class="actions">{f'<a class="btn primary" href="{page}">Project page and timeline</a>' if page else ''}{_feedback_link(rec, site)}</div>
{_source_lines(rec, link)}
</div>
</li>"""


def render_shortlist(records: list[dict], site: dict, updated: date, meta: list[dict], coverage: dict | None = None) -> str:
    """shortlist.html: ten projects for the buyer test, for the profile named in site.shortlist_profile."""
    coverage = coverage if coverage is not None else load_coverage()
    key = site.get("shortlist_profile", "hard-structure")
    m = next((x for x in meta if x["key"] == key), None)
    if not m:
        return ""
    n = int(site.get("shortlist_size", 10))
    picks = shortlist(records, key, n)
    names = {c["code"]: c["name"] for c in coverage.get("countries", [])}
    less_tested = coverage.get("less_tested_languages", [])
    items = "\n".join(_shortlist_item(r, i, key, site, names.get(r.get("country"), r.get("country") or UNKNOWN),
                                      less_tested) for i, r in enumerate(picks, 1))
    fb = general_feedback_url(site)
    ask = (f'<p><a class="btn primary" href="{escape(fb)}" target="_blank" rel="noopener">Give feedback on this list</a> '
           f'Each project also has its own feedback link, with the project filled in.</p>' if fb else "")
    title = site.get("title", "MitiMark")
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(m['label'] + ' shortlist - ' + title, 'Ten projects for ' + m['label'])}
</head>
<body data-soon="{int(site.get('closing_soon_days', 30))}">
{_topbar(site, updated, "")}
<section class="page-head"><div class="wrap">
<p class="eyebrow">Shortlist &middot; {escape(m['label'])}</p>
<h1>{len(picks)} projects to look at</h1>
<p>Projects that may suit eco-engineered marine products, chosen by the rules on the
<a href="about.html#buyer-profiles">About page</a>. Strong matches come first, then Possible, then Weak. Within each
group, approved projects come first: they have passed environmental review but construction has not been reported, so
design and purchasing may still be ahead. Then projects still under assessment. Freshwater projects are left out.</p>
<p class="meta">Updated <time>{_date(updated)}</time>. These are rule-based guesses, not a statement that mitigation
or any product is required. Always read the official notice.</p>
</div></section>
<main class="wrap" id="main">
<div class="shortlist-intro">{ask}</div>
<ol class="cards shortlist">
{items}
</ol>
<p class="note"><a href="index.html?profile={escape(key)}#results">See every {escape(m['label'].lower())} project</a></p>
</main>
{_footer(site)}
<script>{JS_COMMON}
Array.prototype.forEach.call(document.querySelectorAll('.shortlist .card'), MPF.countdown);
</script>
</body>
</html>
"""


def _feeds_section(meta: list[dict] | None) -> str:
    links = ['<li><a href="feeds/all.xml">All projects on the general list</a></li>']
    links += [f'<li><a href="feeds/{escape(m["key"])}.xml">{escape(m["label"])}</a></li>' for m in meta or []]
    return ('<h2 id="feeds">RSS feeds</h2>\n<p>Each feed lists the projects that are new since the last update, '
            'with a link to the official notice and the source. Add a feed address to any feed reader. There is no '
            'sign-up and no email: this site stores nothing about who reads the feeds.</p>\n<ul>' + "".join(links) + "</ul>")


def render_about(site: dict, tiers: dict, updated: date, coverage: dict | None = None,
                 records: list[dict] | None = None, meta: list[dict] | None = None) -> str:
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
    toc = '<a href="#what-it-does">What it does</a><a href="#what-the-score-and-tiers-mean">What the score and tiers mean</a><a href="#automatic-listings-and-review">Automatic listings and review</a><a href="#notices-in-other-languages">Notices in other languages</a><a href="#public-comments">Public comments</a><a href="#changes-between-updates-and-project-pages">Changes between updates and project pages</a>' + ('<a href="#buyer-profiles">Buyer profiles</a>' if meta else '') + '<a href="#stages-and-leads">Project stage and leads</a><a href="#terms-used">Terms used in notices</a><a href="#your-watch-list">Your watch list</a><a href="#project-watch">Project watch</a><a href="#coverage">Coverage</a><a href="#public-sources-used">Public sources used</a><a href="#how-often-it-updates">How often it updates</a><a href="#known-limits">Known limits</a><a href="#who-runs-mitimark">Who runs MitiMark</a>'
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(title, site.get('description', ''))}
</head>
<body>
{_topbar(site, updated, "about")}
<section class="page-head"><div class="wrap">
<p class="eyebrow">About and method</p>
<h1>About {escape(site.get("title", "MitiMark"))}</h1>
<p>{escape(site.get('description', ''))}</p>
<p class="meta">Last successful update: <time>{_date(updated)}</time></p>
</div></section>
<main class="wrap about-layout" id="main">
<nav class="toc" aria-label="On this page"><p class="side-label">On this page</p>
{toc}
</nav>
<div class="prose">
<h2 id="what-it-does">What it does</h2>
<p>The tool reads public environmental assessment and permit notices for marine construction projects,
such as ports, wharves, breakwaters, dredging and shoreline works. It flags projects that may need marine
habitat work, for example avoidance and reduction measures, habitat enhancement, fish habitat offsetting, or
nature-inclusive design. Each listing links to the official notice.</p>

<h2 id="what-the-score-and-tiers-mean">What the score and tiers mean</h2>
<p>Each notice gets a score from 0 to 100 from simple, visible rules: the type of marine work, words about
fish habitat, offsetting, eelgrass, shellfish or species at risk, and the size of the work. The score places
each notice in a tier:</p>
<ul>{tier_list}</ul>
<p>Notices scoring under {int(tiers.get('weak_min', 20))}, quarries and pits, and withdrawn projects are not
listed at all. Only Strong and Possible matches are shown at first; Weak matches can be switched on with
the "Show weaker matches" toggle. A high score means the notice is worth a closer look. It does not mean
that mitigation or compensation is required. Only the regulator decides that.</p>
<p>Where a source's terms allow it, each listing shows short quotes: complete sentences copied exactly from the
notice, chosen because they describe the project and its habitat signals, so you can judge for yourself. Where
they do not (at the moment, Scotland's marine licensing site), the listing shows short facts, the words that
matched and a link to the official notice instead. US Federal Register notices also show no quotes: they are
mostly about marine mammals and noise, so their cards show only what the notice says about the construction,
and a link.</p>

<h2 id="automatic-listings-and-review">Automatic listings and review</h2>
<p>Every scored notice is published automatically, marked "Automatic, not yet reviewed". When a person has
checked a listing against the official notice, it is marked "Reviewed by a person". Listings found to be
wrong or irrelevant on review are removed. Summaries are kept short and factual, and facts that a notice
does not give are shown as "unknown".</p>
<p>When the same project appears in two registries, it is listed once, with a link to the other listing.
Listings whose official page cannot be found are left out.</p>

<h2 id="notices-in-other-languages">Notices in other languages</h2>
<p>Quotes always stay in the notice's original language. There is no automatic translation. For notices
not in English, the English summary is built only from the keywords and sizes the tool found in the text,
and is labelled "built from keywords, not a translation". When too little was found, the listing says
"Not translated" and shows the original-language quotes instead. The keyword rules for French, Danish,
Spanish, Norwegian and Dutch have fewer tests than the English rules, so those listings are lower confidence
and carry a "Less tested language" badge.</p>

<h2 id="public-comments">Public comments</h2>
<p>Each project shows when its public comment period opens and closes and how many days are left, but
only when the official registry publishes those dates. When an opening date is taken from the date of the
notice that invited comments, it is labelled "date of the notice". Where a registry publishes the number
of comments received, the number is shown with the date it was read and a link to the registry's own
comments page. Comment text, the names of people who commented and other personal details are never copied
to this site. Anything a source does not publish is shown as "Not published".</p>
<ul>{comment_sources}</ul>

<h2 id="changes-between-updates-and-project-pages">Changes between updates and project pages</h2>
<p>Every project has its own page with a stable address. At each update the site keeps a short snapshot
(deadline, stage and tier) and marks projects that are new, whose comment deadline changed, or that are now
decided. Each project page shows a timeline of those changes next to the official comment dates.</p>

{_about_profiles(meta, tiers)}
<h2 id="stages-and-leads">Project stage and leads</h2>
<p>For comment periods, a project whose comment period has closed or that has been decided is marked "Likely too
late". For product makers (the hard-structure profile and its shortlist) that label is replaced by the project's
stage: under assessment (comments open or closed) or approved. An approved project has passed environmental review;
it is treated as a lead because design and purchasing may still be ahead. The site cannot yet tell whether
construction has started or a tender has been issued, so treat "Approved, may not be built yet" as a prompt to check.</p>
<p>Projects on lakes or rivers carry a "Freshwater" label. They are hidden from the hard-structure profile unless you
tick "Include freshwater". Hydroelectric stations and dams, and maintenance dredging of an existing berth with no new
structure, are not treated as marine habitat projects.</p>
<h2 id="terms-used">Terms used in notices</h2>
<ul>
<li><strong>SCH (Small Craft Harbour):</strong> a fishing or recreational harbour run by Fisheries and Oceans Canada.</li>
<li><strong>Intent to Make a Determination:</strong> the federal authority plans to decide whether the project is
likely to cause significant adverse environmental effects. The public can comment before it decides.</li>
</ul>
<h2 id="your-watch-list">Your watch list</h2>
<p>"Add to watch list" saves a project in your own browser only. There are no accounts, and nothing about
your watch list is sent anywhere. Clearing your browser data clears the list.</p>

<h2 id="project-watch">Project watch</h2>
<p>Some sources list projects rather than notices. When such a source is used, its items appear in a
separate "Project watch (not a notice feed)" section, with no comment deadline, no countdown, and never in
the closing-soon list. No project watch source is in use at the moment.</p>

<h2 id="coverage">Coverage</h2>
{f'<p><strong>{escape(bn)}</strong></p>' if bn else ''}
<p>"Covered" means a source for that place is read automatically. "Partial" means only some sources are
read, and they are named. "Coming soon" means no source is read yet, and no listings are shown for it.</p>
<div class="cov-wrap"><table class="cov"><thead><tr><th>Place</th><th>Status</th><th>Details</th></tr></thead>
<tbody>
{coverage_rows(coverage, country_counts(records))}
</tbody></table></div>
{f'<p>{escape(lt)}</p>' if lt else ''}

<h2 id="public-sources-used">Public sources used</h2>
<ul>{sources}</ul>

<h2 id="how-often-it-updates">How often it updates</h2>
<p>The list is refreshed {freq}, and new notices appear on their own. Comment countdowns are calculated
in your browser, so they stay current between updates.</p>

{_feeds_section(meta)}
<h2 id="known-limits">Known limits</h2>
<ul>
<li>Public notices often appear after a project's design is done, so some listings will be too late to
influence the design.</li>
<li>Automatic listings have not been checked by a person and can be wrong.</li>
<li>Some size readings can be wrong, for example when a notice also mentions a road or pipeline length.</li>
<li>Many countries and regions are not covered yet; see the coverage table above.</li>
<li>Notices are quoted in their original language. Keyword rules in languages other than English have
fewer tests, so those listings are lower confidence.</li>
</ul>

<h2 id="who-runs-mitimark">Who runs MitiMark</h2>
<p>MitiMark is a prototype run by {escape(site.get("contact_name", ""))}, who also works part-time for ECOncrete, a
maker of eco-engineered marine products. MitiMark uses only public sources: every listing comes from a public registry
or notice, and nothing comes from any company's internal information.</p>
<p>Feedback links on the projects go to a Google Form. This site stores nothing about you.</p>
</div>
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
    scoring = settings.get("scoring")
    meta = profile_meta(records, scoring)
    general = [r for r in records if not r.get("profile_only")]
    (out_dir / "index.html").write_text(render_index(records, site_conf, tiers, updated, coverage, h["snapshots"],
                                                     news_html, scoring), encoding="utf-8")
    (out_dir / "about.html").write_text(render_about(site_conf, tiers, updated, coverage, general, meta),
                                        encoding="utf-8")
    sl = render_shortlist(records, site_conf, updated, meta, coverage)
    if sl:
        (out_dir / "shortlist.html").write_text(sl, encoding="utf-8")
    # One page per project, at a stable address (p/<source>-<id>.html). Pages of projects no longer
    # listed are removed, so the folder only holds what the list shows.
    pages = out_dir / "p"
    pages.mkdir(exist_ok=True)
    keep = set()
    for r in records:
        path = out_dir / r["page"]
        keep.add(path.name)
        path.write_text(render_project_page(r, site_conf, updated, names.get(r.get("country"), r.get("country") or UNKNOWN),
                                            less_tested, meta), encoding="utf-8")
    for old in pages.glob("*.html"):
        if old.name not in keep:
            mark_unlisted(old, updated)
    write_csv(out_dir / "projects.csv", records, names, site_conf, meta)
    write_feeds(out_dir, records, meta, site_conf, updated)
    (out_dir / "items.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    hist.save(out_dir / "history.json", h)
    # Read by the health check (mpf/health.py): the live copy only changes when an update is uploaded.
    (out_dir / "status.json").write_text(json.dumps({"last_successful_update": updated.isoformat(),
                                                    "items": len(records)}, indent=1), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")   # tells GitHub Pages to serve files as-is


# ---------------------------------------------------------------------------
# RSS: one feed per buyer profile (feeds/<profile>.xml), plus feeds/all.xml for the general list.
# Each feed holds the projects that are new since the last update, with the Source line. Nothing is stored
# about readers: no email, no addresses, no tracking.
# ---------------------------------------------------------------------------

def source_line(rec: dict) -> str:
    """The card's Source line, as plain text."""
    out = f"Source: {rec.get('agency') or rec['source_name']}"
    if rec.get("issued_by"):
        out += f". Hearing by {rec['issued_by']}"
    if rec.get("attribution"):
        out += f". {rec['attribution']}"
    return out + "."


def feed_items(records: list[dict], key: str | None, exclude_freshwater: bool = False) -> list[dict]:
    """Projects new since the last update, under one profile (or the general list when key is None)."""
    out = []
    for r in records:
        if "new" not in (r.get("changes") or []):
            continue
        if key is None:
            if r.get("profile_only") or r.get("tier") not in ("strong", "possible", "weak", "watch"):
                continue
        elif not listed_in_profile(r, key):
            continue
        if exclude_freshwater and r.get("freshwater"):
            continue
        out.append(r)
    return sorted(out, key=lambda r: (TIER_ORDER.index(r["tier"]) if r.get("tier") in TIER_ORDER else 9, r["title"]))


def _rfc822(day: str) -> str:
    from email.utils import format_datetime
    from datetime import datetime, timezone
    d = date.fromisoformat(day[:10])
    return format_datetime(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc))


def render_feed(items: list[dict], title: str, description: str, site_url: str, feed_path: str, updated: date) -> str:
    from xml.sax.saxutils import escape as x, quoteattr as qa
    base = site_url.rstrip("/") + "/"
    entries = []
    for r in items:
        place = ", ".join(v for v in (r.get("province"), r.get("country")) if v and v != UNKNOWN)
        tier = r.get("tier_label") or ""
        link = r["url"] if re.match(r"https?://", r.get("url") or "") else ""
        lines = [f"{tier}." if tier else "", f"Place: {place}." if place else "",
                 f"Type: {', '.join(r.get('types') or [])}." if r.get("types") else "",
                 (r.get("summary") or "").strip(), f"Official notice: {link}" if link else "", source_line(r)]
        body = "\n".join(l for l in lines if l)
        entries.append(
            "<item>"
            f"<title>{x(r['title'])}</title>"
            f"<link>{x(base + r['page'])}</link>"
            f'<guid isPermaLink="false">{x(r["id"])}</guid>'
            f"<pubDate>{_rfc822(r.get('first_listed') or updated.isoformat())}</pubDate>"
            f"<description>{x(body)}</description>"
            + (f"<source url={qa(link)}>{x(r.get('agency') or r['source_name'])}</source>" if link else "")
            + "</item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
            f"<channel><title>{x(title)}</title><link>{x(base)}</link>"
            f'<atom:link href={qa(base + feed_path)} rel="self" type="application/rss+xml"/>'
            f"<description>{x(description)}</description><language>en</language>"
            f"<lastBuildDate>{_rfc822(updated.isoformat())}</lastBuildDate>"
            + "".join(entries) + "</channel></rss>\n")


def write_feeds(out_dir: Path, records: list[dict], meta: list[dict], site: dict, updated: date) -> list[str]:
    folder = out_dir / "feeds"
    folder.mkdir(exist_ok=True)
    name = site.get("title", "MitiMark")
    url = site.get("url", "")
    written = []
    feeds = [("all", None, f"{name}: new projects", "New projects on the general list since the last update.", False)]
    for m in meta:
        feeds.append((m["key"], m["key"], f"{name}: new projects, {m['label']}",
                      f"New projects since the last update, for the profile: {m['label']}.", m.get("exclude_freshwater")))
    for slug, key, title, desc, fresh in feeds:
        path = f"feeds/{slug}.xml"
        (out_dir / path).write_text(render_feed(feed_items(records, key, bool(fresh)), title, desc, url, path, updated),
                                    encoding="utf-8")
        written.append(path)
    return written


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
