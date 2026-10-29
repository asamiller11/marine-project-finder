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
            rec["stage_kind"] = stage_kind(rec, today) if rec.get("stage_kind") not in ("decided", "withdrawn") \
                else rec["stage_kind"]
            records[key] = rec
    return sorted(records.values(), key=_sort_key), skipped


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f4f8fa;--card:#ffffff;--text:#13293a;--muted:#4f6676;--line:#d3e0e8;--accent:#0b6b85;
--accent-soft:#e2f1f5;--strong:#0f6b4f;--strong-bg:#dcf3ea;--possible:#7a5300;--possible-bg:#fbefd5;
--weak:#4b5a66;--weak-bg:#e8edf1;--late:#9a3412;--late-bg:#fdebe2;--rev:#0f6b4f;--rev-bg:#dcf3ea;
--soon:#5b4a8a;--soon-bg:#ece8f6;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#0c1a22;--card:#12252f;--text:#e2edf3;--muted:#9fb4c2;
--line:#24404e;--accent:#6cc7da;--accent-soft:#163846;--strong:#7fdcb7;--strong-bg:#143a2f;--possible:#f2c46b;
--possible-bg:#3a2e12;--weak:#c2ced6;--weak-bg:#25343d;--late:#f6a982;--late-bg:#3d2116;--rev:#7fdcb7;
--rev-bg:#143a2f;--soon:#c6b8f0;--soon-bg:#2a2440}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,"Helvetica Neue",Arial,sans-serif}
a{color:var(--accent)}
.wrap{max-width:1040px;margin:0 auto;padding:0 16px}
header.site{background:var(--card);border-bottom:1px solid var(--line);padding:20px 0 16px}
header.site h1{margin:0;font-size:1.6rem;line-height:1.25}
header.site p{margin:6px 0 0;color:var(--muted)}
nav.top{margin-top:10px;font-size:.95rem}
nav.top a{margin-right:16px}
.updated{font-size:.9rem}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:16px 0 4px}
.panel h2{margin:0 0 6px;font-size:1.05rem}
.panel ul{margin:6px 0;padding-left:18px}
.panel p{margin:6px 0}
details.coverage summary{cursor:pointer;font-weight:600}
table.cov{width:100%;border-collapse:collapse;font-size:.93rem;margin-top:8px}
table.cov th,table.cov td{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}
table.cov th{color:var(--muted);font-weight:600}
table.cov tr.region td:first-child{padding-left:22px;color:var(--muted)}
.cov-wrap{overflow-x:auto}
.pill{font-size:.78rem;font-weight:600;padding:1px 8px;border-radius:999px;white-space:nowrap}
.pill.covered{color:var(--strong);background:var(--strong-bg)}
.pill.partial{color:var(--possible);background:var(--possible-bg)}
.pill.coming_soon{color:var(--soon);background:var(--soon-bg)}
.summary-line{margin:14px 0 8px;font-weight:600}
section.soon{background:var(--accent-soft);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}
section.soon h2{margin:0 0 6px;font-size:1.05rem}
section.soon ul{margin:0;padding-left:18px}
section.soon li{margin:4px 0}
.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0;align-items:end}
.filters label{display:flex;flex-direction:column;font-size:.85rem;color:var(--muted);gap:4px}
.filters select,.filters input[type=search]{font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--text);min-width:0}
.filters .check{flex-direction:row;align-items:center;gap:8px;font-size:.95rem;color:var(--text);padding-bottom:8px}
.toggles{display:flex;flex-wrap:wrap;gap:8px 20px;margin:4px 0 12px}
.toggles label{display:flex;align-items:center;gap:8px}
section.country>h2{font-size:1.35rem;margin:26px 0 2px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
section.country>p.country-note{margin:0 0 4px;color:var(--muted);font-size:.95rem}
section.tier h3{font-size:1.12rem;margin:18px 0 2px}
section.tier p.tier-desc{margin:0 0 10px;color:var(--muted);font-size:.95rem}
.cards{display:grid;gap:14px;margin:8px 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h4{margin:0 0 4px;font-size:1.1rem;line-height:1.35}
.card h4 a{text-decoration:none}
.card h4 a:hover{text-decoration:underline}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 10px}
.badge{font-size:.8rem;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap}
.t-strong{color:var(--strong);background:var(--strong-bg)}
.t-possible{color:var(--possible);background:var(--possible-bg)}
.t-weak{color:var(--weak);background:var(--weak-bg)}
.t-watch{color:var(--soon);background:var(--soon-bg)}
.score{color:var(--muted);border:1px solid var(--line)}
.late{color:var(--late);background:var(--late-bg)}
.st-open{color:var(--accent);background:var(--accent-soft)}
.st-other{color:var(--weak);background:var(--weak-bg)}
.rev{color:var(--rev);background:var(--rev-bg)}
.auto{color:var(--muted);border:1px dashed var(--line)}
.lang{color:var(--soon);background:var(--soon-bg)}
dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:3px 12px;margin:0 0 10px;font-size:.95rem}
dl.facts dt{color:var(--muted)}
dl.facts dd{margin:0}
.countdown{font-weight:600}
.card p.summary{margin:8px 0}
.note{color:var(--muted);font-size:.88rem}
ul.quotes{margin:8px 0;padding:0;list-style:none}
ul.quotes li{border-left:3px solid var(--line);padding:2px 0 2px 10px;margin:6px 0;color:var(--muted);font-size:.95rem}
p.source{margin:10px 0 0;font-size:.92rem}
p.attr{margin:4px 0 0;font-size:.85rem;color:var(--muted)}
.empty{padding:24px;text-align:center;color:var(--muted);background:var(--card);border:1px dashed var(--line);border-radius:12px}
footer{border-top:1px solid var(--line);padding:18px 0 40px;color:var(--muted);font-size:.9rem;margin-top:20px}
main.about h2{margin-top:28px;font-size:1.2rem}
main.about{padding-bottom:20px}
[hidden]{display:none!important}
@media (max-width:560px){header.site h1{font-size:1.3rem}dl.facts{grid-template-columns:1fr}dl.facts dt{margin-top:4px}}
"""

JS = r"""
(function(){
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var today = new Date(); today.setHours(0,0,0,0);
  var SOON = parseInt(document.body.getAttribute('data-soon') || '30', 10);
  function daysLeft(iso){ if(!iso) return null; var p = iso.split('-');
    var d = new Date(+p[0], +p[1]-1, +p[2]); return Math.round((d - today)/86400000); }
  var STAGE = {open:'Comment period open', closed:'Comment period closed', decided:'Decision made',
               assessment:'Under assessment', withdrawn:'Withdrawn or rejected', unknown:'Stage unknown',
               watch:'Project watch'};
  cards.forEach(function(c){
    var dl = c.getAttribute('data-deadline'), n = daysLeft(dl), kind = c.getAttribute('data-stage');
    if (kind !== 'decided' && kind !== 'withdrawn' && kind !== 'watch' && n !== null) { kind = n >= 0 ? 'open' : 'closed'; }
    c.setAttribute('data-stage-now', kind); c._days = n;
    var cd = c.querySelector('.countdown');
    if (cd) {
      if (kind === 'watch') { /* project watch: keep "not applicable" */ }
      else if (kind === 'decided') cd.textContent = (dl ? dl + ', ' : '') + 'decision made';
      else if (n === null) cd.textContent = 'unknown';
      else if (n > 1) cd.textContent = dl + ' (' + n + ' days left)';
      else if (n === 1) cd.textContent = dl + ' (1 day left)';
      else if (n === 0) cd.textContent = dl + ' (closes today)';
      else cd.textContent = dl + ' (closed)';
    }
    var late = c.querySelector('.late'); if (late) late.hidden = !(kind === 'decided' || kind === 'closed');
    var st = c.querySelector('.stage-badge');
    if (st) { st.textContent = STAGE[kind]; st.className = 'badge stage-badge ' + (kind === 'open' ? 'st-open' : 'st-other'); }
  });
  // In each tier: open comment periods first, then by score.
  Array.prototype.forEach.call(document.querySelectorAll('section.tier .cards'), function(list){
    var cs = Array.prototype.slice.call(list.querySelectorAll('.card'));
    cs.sort(function(a,b){
      var ao = a.getAttribute('data-stage-now') === 'open' ? 0 : 1, bo = b.getAttribute('data-stage-now') === 'open' ? 0 : 1;
      return ao - bo || (+b.getAttribute('data-score')) - (+a.getAttribute('data-score'));
    });
    cs.forEach(function(c){ list.appendChild(c); });
  });
  var fc = document.getElementById('f-country'), fr = document.getElementById('f-region'), frl = document.getElementById('f-region-label'),
      frt = document.getElementById('f-region-text'), ft = document.getElementById('f-type'), fs = document.getElementById('f-stage'),
      fo = document.getElementById('f-open'), fq = document.getElementById('f-search'), fw = document.getElementById('t-weak'),
      line = document.getElementById('summary-line'), empty = document.getElementById('no-match'),
      ul = document.getElementById('soon-list'), none = document.getElementById('soon-none');
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
  var params = location.search;
  if ((params.match(/[?&]show=([^&]*)/) || [])[1] && params.indexOf('weak') >= 0) fw.checked = true;
  var pc = (params.match(/[?&]country=([A-Za-z]{2})/) || [])[1];
  if (pc) { fc.value = pc.toUpperCase(); }
  fillRegions();
  function tierShown(t){ return t === 'strong' || t === 'possible' || t === 'watch' || (t === 'weak' && fw.checked); }
  function placeOk(c){ return (!fc.value || c.getAttribute('data-country') === fc.value) &&
                              (!fr.value || c.getAttribute('data-region') === fr.value); }
  function apply(){
    var q = (fq.value || '').toLowerCase().trim(), shown = 0, counts = {};
    cards.forEach(function(c){
      var k = c.getAttribute('data-stage-now'), t = c.getAttribute('data-tier');
      var ok = tierShown(t) && placeOk(c) &&
               (!ft.value || ('|' + c.getAttribute('data-types') + '|').indexOf('|' + ft.value + '|') >= 0) &&
               (!fs.value || k === fs.value) && (!fo.checked || k === 'open') &&
               (!q || c.getAttribute('data-search').indexOf(q) >= 0);
      c.hidden = !ok;
      if (ok) { shown++; var key = c.getAttribute('data-country') + '|' + t; counts[key] = (counts[key] || 0) + 1;
                counts[c.getAttribute('data-country')] = (counts[c.getAttribute('data-country')] || 0) + 1; }
    });
    Array.prototype.forEach.call(document.querySelectorAll('section.tier'), function(s){
      var key = s.getAttribute('data-country') + '|' + s.getAttribute('data-tier'), n = counts[key] || 0;
      s.hidden = n === 0;
      var cnt = s.querySelector('.count'); if (cnt) cnt.textContent = '(' + n + ')';
    });
    Array.prototype.forEach.call(document.querySelectorAll('section.country'), function(s){
      s.hidden = !(counts[s.getAttribute('data-country')] > 0);
    });
    if (empty) empty.hidden = shown > 0;
    // Closing soon: every published tier (Strong, Possible, Weak) in the chosen country or region.
    while (ul.firstChild) ul.removeChild(ul.firstChild);
    var soon = cards.filter(function(c){ return placeOk(c) && c.getAttribute('data-tier') !== 'watch' &&
      c.getAttribute('data-stage-now') === 'open' && c._days !== null && c._days <= SOON; })
      .sort(function(a,b){ return a._days - b._days; });
    soon.forEach(function(c){
      var li = document.createElement('li'), a = document.createElement('a');
      a.href = '#' + c.id; a.textContent = c.querySelector('h4').textContent;
      // A Weak item is hidden until the toggle is on, so clicking it turns the toggle on.
      if (c.getAttribute('data-tier') === 'weak') a.addEventListener('click', function(){
        if (!fw.checked) { fw.checked = true; apply(); } });
      li.appendChild(a); li.appendChild(document.createTextNode(': ' + c.getAttribute('data-deadline') + ' (' +
        (c._days === 0 ? 'closes today' : c._days === 1 ? '1 day left' : c._days + ' days left') + '), ' +
        c.querySelector('.tier-badge').textContent + ', ' + c.getAttribute('data-country-name')));
      ul.appendChild(li);
    });
    if (none) none.hidden = soon.length > 0;
    line.textContent = shown + (shown === 1 ? ' project' : ' projects') + ' shown. ' + soon.length +
      (soon.length === 1 ? ' comment deadline' : ' comment deadlines') + ' in the next ' + SOON + ' days.';
  }
  // A link ending in #coverage opens the Coverage panel.
  var cov = document.getElementById('coverage');
  if (cov && location.hash === '#coverage') { cov.open = true; }
  fc.addEventListener('change', function(){ fillRegions(); apply(); });
  [fr, ft, fs, fo, fw].forEach(function(el){ el.addEventListener('change', apply); });
  fq.addEventListener('input', apply);
  apply();
})();
"""


def _head(title: str, description: str) -> str:
    return (f'<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<meta name="robots" content="noindex, nofollow">\n<title>{escape(title)}</title>\n'
            f'<meta name="description" content="{escape(description)}">\n<style>{CSS}</style>')


def _header(title: str, desc: str, updated: date) -> str:
    return (f'<header class="site"><div class="wrap">\n<h1>{escape(title)}</h1>\n<p>{escape(desc)}</p>\n'
            f'<p class="updated">Last updated: {updated.strftime("%B %-d, %Y")}</p>\n'
            f'<nav class="top"><a href="index.html">Projects</a><a href="about.html">About</a></nav>\n</div></header>')


def _footer(site: dict) -> str:
    email = escape(site.get("contact_email", ""))
    return (
        '<footer><div class="wrap">'
        "<p>Prototype. Flags show projects that may need marine habitat work. They do not state that "
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


def _card(rec: dict, country_name: str, less_tested: list) -> str:
    types = rec["types"] or []
    place = ", ".join(x for x in (rec["province"] if rec["province"] != UNKNOWN else "", country_name) if x) or UNKNOWN
    location = f'{place}. Water body: {rec["water_body"]}'
    search = " ".join([rec["title"], rec["proponent"], rec["province"], country_name, rec["water_body"],
                       rec["summary"], " ".join(types)]).lower()
    deadline = rec.get("comment_deadline")
    deadline_text = deadline if deadline else UNKNOWN
    if rec["stage_kind"] == "watch":
        deadline, deadline_text = None, "not applicable (project watch, not a notice)"
    elif rec["stage_kind"] == "decided":
        deadline_text = (deadline + ", " if deadline else "") + "decision made"
    elif rec["stage_kind"] == "closed" and deadline:
        deadline_text = deadline + " (closed)"
    quotes = "".join(f"<li>&ldquo;{escape(q)}&rdquo;</li>" for q in rec["quotes"])
    review_badge = ('<span class="badge rev">Reviewed by a person</span>' if rec["reviewed"]
                    else '<span class="badge auto">Automatic, not yet reviewed</span>')
    lang_badge = ""
    if rec.get("language") in less_tested:
        lang_badge = f'<span class="badge lang">Less tested language ({escape(rec["language"])})</span>'
    anchor = "p-" + re.sub(r"[^A-Za-z0-9_-]", "-", rec["id"])
    link = escape(rec["url"]) if rec["url"] else "#"
    also = ""
    if rec.get("also_listed"):
        parts = []
        for a in rec["also_listed"]:
            d = f', comment deadline {escape(a["comment_deadline"])}' if a.get("comment_deadline") else ""
            parts.append(f'<a href="{escape(a["url"])}" rel="noopener">{escape(a["source_name"])}</a>{d}')
        also = f'<p class="source">Also listed: {"; ".join(parts)}</p>'
    return f"""<article class="card" id="{anchor}" data-tier="{rec['tier']}" data-country="{escape(rec.get('country') or UNKNOWN)}"
 data-country-name="{escape(country_name)}" data-region="{escape(rec['province'])}"
 data-types="{escape('|'.join(types))}" data-stage="{rec['stage_kind']}" data-deadline="{escape(deadline or '')}"
 data-score="{int(rec['score'])}" data-search="{escape(search)}">
<h4><a href="{link}" rel="noopener">{escape(rec['title'])}</a></h4>
<div class="badges"><span class="badge tier-badge t-{rec['tier']}">{escape(rec['tier_label'])}</span>
<span class="badge score" title="{escape(rec['tier_reason'])}">Score {int(rec['score'])}/100</span>
<span class="badge stage-badge {'st-open' if rec['stage_kind'] == 'open' else 'st-other'}">{STAGE_LABELS[rec['stage_kind']]}</span>
<span class="badge late"{'' if rec['too_late'] else ' hidden'}>Likely too late</span>{review_badge}{lang_badge}</div>
<dl class="facts">
<dt>Proponent</dt><dd>{escape(rec['proponent'])}</dd>
<dt>Location</dt><dd>{escape(location)}</dd>
<dt>Project type</dt><dd>{escape(', '.join(types) if types else UNKNOWN)}</dd>
<dt>Stage</dt><dd>{escape(rec['stage'])}</dd>
<dt>Comment deadline</dt><dd><span class="countdown">{escape(deadline_text)}</span></dd>
</dl>
{_summary_html(rec)}
{f'<ul class="quotes">{quotes}</ul>' if quotes else ''}
<p class="source"><a href="{link}" rel="noopener">{'Official page' if rec['stage_kind'] == 'watch' else 'Official notice'}</a> ({escape(rec['source_name'])}){(' ' + escape(rec['attribution'])) if rec.get('attribution') else ''}</p>
{also}
<p class="attr">Source: {escape(rec.get('agency') or rec['source_name'])}{('. Hearing by ' + escape(rec['issued_by'])) if rec.get('issued_by') else ''}{('. ' + escape(rec['attribution'])) if rec.get('attribution') else ''}.</p>
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


def render_index(records: list[dict], site: dict, tiers: dict, updated: date, coverage: dict | None = None) -> str:
    coverage = coverage if coverage is not None else load_coverage()
    title = site.get("title", "Marine Project Finder")
    desc = site.get("description", "")
    soon_days = int(site.get("closing_soon_days", 30))
    td = tier_descriptions(tiers)
    less_tested = coverage.get("less_tested_languages", [])
    default = [r for r in records if r["tier"] in ("strong", "possible")]
    n_soon = sum(1 for r in records if r["stage_kind"] == "open" and r.get("comment_deadline")
                 and 0 <= (date.fromisoformat(r["comment_deadline"]) - updated).days <= soon_days)
    countries = _countries_in_order(records, coverage)
    names = {c["code"]: c["name"] for c in coverage.get("countries", [])}
    names.update({c["code"]: c["name"] for c in countries})

    country_sections = []
    for c in countries:
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
                + "\n".join(_card(r, names.get(c["code"], c["code"]), less_tested) for r in recs)
                + "\n</div>\n</section>")
        note = f'<p class="country-note">{escape(c.get("note", ""))}</p>' if c.get("note") else ""
        country_sections.append(
            f'<section class="country" data-country="{escape(c["code"])}" aria-label="{escape(c["name"])}">\n'
            f'<h2>{escape(c["name"])} {_pill(c["status"])}</h2>\n{note}\n' + "\n".join(tier_parts) + "\n</section>")
    body = "\n".join(country_sections) if country_sections else '<p class="empty">No projects yet.</p>'

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
<section class="panel" aria-labelledby="explain-h">
<h2 id="explain-h">How to read this list</h2>
<p>Every notice is scored automatically from the words and sizes in it, then placed in a tier:</p>
<ul>
<li><strong>{TIER_LABELS['strong']}:</strong> {escape(td['strong'])}</li>
<li><strong>{TIER_LABELS['possible']}:</strong> {escape(td['possible'])}</li>
<li><strong>{TIER_LABELS['weak']}:</strong> {escape(td['weak'])} Hidden unless you tick "Show weaker matches".</li>
</ul>
<p>Notices scoring under {int(tiers.get('weak_min', 20))}, quarries and pits, and withdrawn projects are not listed.</p>
<p>A flag means a notice may involve marine habitat work. It is not a statement that mitigation or
compensation is required. Items marked "Reviewed by a person" have been checked; the rest are automatic.
Always read the official notice.</p>
</section>
<details class="panel coverage" id="coverage">
<summary>Coverage: {cc['covered']} covered, {cc['partial']} partial, {cc['coming_soon']} coming soon{' (mostly Canada for now)' if bn else ''}</summary>
{f'<p>{escape(bn)}</p>' if bn else ''}
<div class="cov-wrap"><table class="cov"><thead><tr><th>Place</th><th>Status</th><th>Details</th></tr></thead>
<tbody>
{coverage_rows(coverage, counts)}
</tbody></table></div>
{f'<p>{escape(lt)}</p>' if lt else ''}
</details>
<p class="summary-line" id="summary-line">{len(default)} projects shown. {n_soon} comment deadlines in the next {soon_days} days.</p>
<section class="soon" aria-labelledby="soon-h">
<h2 id="soon-h">Comment deadlines closing soon (next {soon_days} days)</h2>
<ul id="soon-list"></ul>
<p id="soon-none">No comment deadlines in the next {soon_days} days.</p>
<noscript><p>Turn on JavaScript for live countdowns, filters and search. Without it, all three tiers are listed below.</p></noscript>
</section>
<form class="filters" onsubmit="return false" aria-label="Filter projects">
<label>Country<select id="f-country">{''.join(opts)}</select></label>
<label id="f-region-label" hidden><span id="f-region-text">Province or state</span><select id="f-region"><option value="">All</option></select></label>
<label>Project type<select id="f-type"><option value="">All types</option></select></label>
<label>Stage<select id="f-stage"><option value="">All stages</option>
<option value="open">Comment period open</option><option value="closed">Comment period closed</option>
<option value="assessment">Under assessment</option><option value="decided">Decision made</option>
<option value="withdrawn">Withdrawn or rejected</option><option value="unknown">Stage unknown</option></select></label>
<label>Search<input type="search" id="f-search" placeholder="Name, place, proponent"></label>
<label class="check"><input type="checkbox" id="f-open"> Comment period open only</label>
</form>
<div class="toggles">
<label><input type="checkbox" id="t-weak"> Show weaker matches</label>
</div>
<p class="empty" id="no-match" hidden>No projects match these filters.</p>
{body}
</main>
{_footer(site)}
<script>{JS}</script>
</body>
</html>
"""


def render_about(site: dict, tiers: dict, updated: date, coverage: dict | None = None,
                 records: list[dict] | None = None) -> str:
    coverage = coverage if coverage is not None else load_coverage()
    records = records or []
    bn = balance_note(records)
    title = "About: " + site.get("title", "Marine Project Finder")
    td = tier_descriptions(tiers)
    sources = "".join(f'<li><a href="{escape(s["url"])}" rel="noopener">{escape(s["name"])}</a></li>'
                      for s in coverage.get("sources", {}).values())
    freq = escape(site.get("update_frequency", "every 2 weeks"))
    tier_list = "".join(f"<li><strong>{TIER_LABELS[t]}:</strong> {escape(td[t])}</li>" for t in PUBLISHED_TIERS)
    lt = less_tested_note(coverage)
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(title, site.get('description', ''))}
</head>
<body>
{_header("About this prototype", site.get('description', ''), updated)}
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
               coverage: dict | None = None) -> None:
    coverage = coverage if coverage is not None else load_coverage()
    site_conf, tiers = settings.get("site", {}), settings.get("tiers", {})
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_index(records, site_conf, tiers, updated, coverage), encoding="utf-8")
    (out_dir / "about.html").write_text(render_about(site_conf, tiers, updated, coverage, records), encoding="utf-8")
    (out_dir / "items.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")   # tells GitHub Pages to serve files as-is


def load_previous(out_dir: Path) -> dict:
    path = out_dir / "items.json"
    if not path.exists():
        return {}
    try:
        return {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
    except (ValueError, KeyError):
        return {}
