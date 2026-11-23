"""Builds the static website from every scored item, grouped into tiers.

Output (folder site/):
  index.html   the home page; projects.html is the project list (works without JavaScript; JavaScript adds live
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
from mpf.profiles import (ALL, ALL_LABEL, EMPTY_NOTE, PROFILE_ONLY_SOURCES, PROFILE_TIERS, ProfileScorer,
                          is_freshwater, listed_in_profile, profile_results, profile_tier)
from mpf import winnability
from mpf.stakeholders import NOT_NAMED, ROLES, find_stakeholders, summary_line
from mpf import research as rsrch

DEVELOPER_ONLY = "From the developer's website, not in a permit notice yet"
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


STAGE_LABELS = {"open": "Open for public comment", "closed": "Comments closed, decision pending", "decided": "Approved",
                "assessment": "Under review", "withdrawn": "Withdrawn or rejected", "unknown": "Stage not stated",
                "watch": "Project page we follow"}
# The stage a supplier cares about (mpf/winnability.py), in plain words. Used on the front of each card.
WIN_STAGE_TEXT = {"pre_application": "Before the permit application (early)", "open": "Under review, open for public comment",
                  "assessment": "Under review", "closed": "Under review, decision pending", "decided": "Approved",
                  "under_construction": "Under construction", "unknown": "Stage not stated"}


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
                               patterns=patterns) if source_conf(row.get("source")).get("quotes", True)
                    and row.get("source") != "developer" else []),   # developer cards hold our summary, not quotes
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


def _load_research() -> tuple[dict, dict]:
    return rsrch.load_research()


def developer_rows(projects: dict, rules: dict, today: date) -> list[dict]:
    """Projects found on a developer's website (research/projects.yaml, developer_only), scored with the same rules."""
    from mpf.score import score_text
    from mpf.timing import compute
    out = []
    for key, p in (projects or {}).items():
        if not p.get("developer_only"):
            continue
        text = f"{p['title']}. {p['full_text']}"
        sc = score_text(text, rules, language="en")
        row = {"key": key, "source": "developer", "item_id": key.split(":", 1)[-1], "url": p["url"], "title": p["title"],
               "language": "en", "country": p.get("country"), "province": p.get("province") or UNKNOWN,
               "water_body": p.get("water_body") or UNKNOWN, "proponent": p.get("proponent") or UNKNOWN,
               "status_raw": p.get("stage") or UNKNOWN, "comment_deadline": None, "decision_date": None,
               "date_posted": None, "full_text": text, "raw_path": "", "is_test_set": 0, "first_seen": "", "last_seen": "",
               "extra": json.dumps({"developer_only": True}), "score": sc["score"],
               "reasons": json.dumps(sc["reasons"]), "facts": json.dumps(sc["facts"]),
               "project_types": json.dumps(sc["project_types"])}
        tm = compute({**row, "extra": {}}, today)
        row["stage"] = "under construction" if re.search(r"construction", p.get("stage") or "", re.I) else tm["stage"]
        out.append(row)
    return out


def add_research(rec: dict, proj: dict | None, orgs: dict) -> None:
    """Merge checked research into a record: named organisations (with their source), facts (procurement first),
    named people with their role, and each organisation's published website, email and phone."""
    proj = proj or {}
    st = rec.setdefault("stakeholders", {k: [] for k, _ in ROLES})
    for role in ("owner", "consultant", "contractor"):
        for e in proj.get(role) or []:
            entry = {"name": e["name"], "detail": "", "how": "Named by an official web source" if e.get("checked") == "read"
                     else "Found by a web search (page not read automatically)", "quote": "", "source": e["source"]}
            keys = [_name_key(x["name"]) for x in st.get(role) or []]
            k = _name_key(e["name"])
            if not any(k == x or k.startswith(x + " ") or x.startswith(k + " ") for x in keys):
                st.setdefault(role, []).append(entry)
    order = list(rsrch.FACT_KINDS)
    facts = sorted(proj.get("facts") or [], key=lambda f: order.index(f["kind"]) if f.get("kind") in order else 99)
    org_info = {}
    for role in ("owner", "consultant", "contractor"):
        for e in st.get(role) or []:
            o = _org_lookup(orgs, e["name"])
            if o:
                org_info[e["name"]] = o
                for pr in o.get("procurement") or []:
                    if not any(f.get("source") == pr["url"] for f in facts):
                        facts.append({"kind": "procurement", "text": pr["text"], "source": pr["url"],
                                      "checked": pr.get("checked", "search"), "short": pr.get("short", ""), "general": True})
    rec["research"] = {"facts": facts, "people": proj.get("people") or [], "orgs": org_info,
                       "developer_only": bool(proj.get("developer_only"))}
    if proj.get("developer_only"):
        rec["developer_only"] = True
        rec["source_name"] = rec["agency"] = DEVELOPER_ONLY


def _name_key(name: str) -> str:
    from mpf.stakeholders import name_key
    return name_key(re.sub(r"\s*\(.*$", "", name))


def _org_lookup(orgs: dict, name: str) -> dict | None:
    k = _name_key(name)
    for oname, o in (orgs or {}).items():
        ok = _name_key(oname)
        if ok and (ok == k or k.startswith(ok) or ok.startswith(k)):
            return {**o, "name": oname}
    return None


def procurement_routes(rec: dict) -> list[dict]:
    return [f for f in (rec.get("research") or {}).get("facts") or [] if f.get("kind") == "procurement"]


def _top_reason(row: dict) -> str:
    """The strongest relevance signal, in words ("Breakwater, jetty or groyne")."""
    try:
        reasons = json.loads(row.get("reasons") or "[]")
        facts = json.loads(row.get("facts") or "{}")
    except ValueError:
        reasons, facts = [], {}
    pos = sorted((r for r in reasons if r.get("points", 0) > 0 and not r["rule"].startswith("size:")),
                 key=lambda r: -r["points"])
    if pos:
        return pos[0]["label"]
    because = ((facts.get("iha") or {}).get("because") or "").split(";")[0].strip()
    return because


def apply_winnability(rec: dict, row: dict, scoring: dict, tiers_conf: dict, today: date) -> None:
    """Add winnability points to the general score and to every profile score, re-tier, and keep the reasons.
    The listing itself does not change: listed projects stay at least Weak, unlisted ones are not lifted."""
    try:
        sizes = json.loads(row.get("facts") or "{}").get("sizes") or []
    except ValueError:
        sizes = []
    win = winnability.assess(row, rec["stage_kind"], rec["stakeholders"], scoring, today, _extra(row), sizes)
    rel = int(rec["score"] or 0)
    rec["relevance_score"] = rel
    if rec["tier"] != "watch":
        new = winnability.adjusted(rel, win["points"], tiers_conf, cap_tier=win.get("cap_tier"))
        tier, why = tier_for({**row, "score": new}, tiers_conf)
        rec.update(score=new, tier=tier, tier_label=TIER_LABELS[tier],
                   tier_reason=(f"{rel} points for the kind of work, {'plus' if win['points'] >= 0 else 'minus'} {abs(win['points'])} for "
                                f"stage, timing and who is named" if why.startswith("score") else why))
    own = row.get("source") in PROFILE_ONLY_SOURCES
    for p in (rec.get("profiles") or {}).values():
        p["relevance"] = p["score"]
        if p["tier"] in PROFILE_TIERS:
            new = winnability.adjusted(int(p["score"]), win["points"], tiers_conf, strong_cap=own,
                                       cap_tier=win.get("cap_tier"))
            t = profile_tier(row, new, tiers_conf, rec["tier"] if not own else "notrel")
            t = {"strong": "possible"}.get(t, t) if own else t
            p.update(score=new, tier=t, tier_label=TIER_LABELS[t])
    rec["win"] = {"points": win["points"], "stage": win["stage"], "entry_point": win["entry_point"],
                  "reasons": winnability.plain_reasons(_top_reason(row), win),
                  "construction_start": win["start"]["quote"] if win.get("start") else ""}


def build_public_records(conn: sqlite3.Connection, review_rows: dict, settings: dict, today: date,
                         previous: dict | None = None) -> tuple[list[dict], dict]:
    """All records for the public site, plus counts of what was left out and why."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM items WHERE is_test_set=0 AND score IS NOT NULL")]
    # Research files (research/*.yaml) are used with the real settings; tests pass their own as settings["research"].
    research_projects, research_orgs = settings["research"] if "research" in settings else (
        _load_research() if isinstance(settings.get("sources"), dict) and settings["sources"] else ({}, {}))
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
    from mpf.config import load_scoring as _ls
    kept += developer_rows(research_projects, settings.get("scoring") or _ls(), today)
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
            rec["stakeholders"] = find_stakeholders(r, _extra(r))
            add_research(rec, research_projects.get(r["key"]), research_orgs)
            apply_winnability(rec, r, scoring, tiers_conf, today)
            records[r["key"]] = rec
    # "Probably not relevant" is never on the general list. Such a project is kept only when a buyer
    # profile lists it at Possible or better; it then appears under that profile only.
    possible_min = int(tiers_conf.get("possible_min", 35))
    for key in [k for k, rec in records.items() if rec["tier"] == "notrel"]:
        fits = [p for p in records[key]["profiles"] if listed_in_profile(records[key], p, possible_min)]
        if fits:
            # "All habitat work" is the full list: a project any profile lists is on it too, at its best profile tier.
            rec = records[key]
            best = min((rec["profiles"][p]["tier"] for p in fits), key=TIER_ORDER.index)
            label = (scoring.get("profiles") or {}).get(fits[0], {}).get("label", fits[0])
            rec.update(tier=best, tier_label=TIER_LABELS[best], listed_via=label,
                       tier_reason=f"On the list because it fits the profile: {label}")
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
            rec.setdefault("stakeholders", {k: [] for k, _ in ROLES})
            rec.setdefault("win", {})
            rec["stage_kind"] = stage_kind(rec, today) if rec.get("stage_kind") not in ("decided", "withdrawn") \
                else rec["stage_kind"]
            records[key] = rec
    return sorted(records.values(), key=_sort_key), skipped


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = r"""
/* MitiMark. Calm and oceanic: the header sits on the navy-to-sea band below it, every blue band ends in a soft
   curve, then bands alternate between the pale sea tint and white. Coral is used only for comment deadlines and
   "Strong fit". Cards have no outlines, one soft shadow, one font. */
:root{--navy:#0B3C5D;--sea:#1B7F9E;--mid:#4FA3BF;--tint:#E8F4F8;--soft:#F3F9FB;--coral:#E2725B;--text:#16313F;
--coral-text:#B4452E;--link:#156C87;--muted:#4B6573;--card:#ffffff;--band-white:#ffffff;--band-tint:var(--tint);
--line:#D5E6ED;--line-strong:#B3CEDA;--on-blue:#ffffff;--on-blue-2:#ffffff;--pill-on-navy:#E8F4F8;
--strong-bg:#FCEBE6;--possible-bg:#E3F0F5;--possible-text:#0B3C5D;--weak-bg:#EEF2F4;--weak-text:#4B6573;
--shadow:0 12px 32px rgba(11,60,93,.08);--r:22px;--r-sm:14px;--sec:96px;--curve:64px;
--track:#ffffff;--chip-on-bg:#D3EAF2;--chip-on-text:#0B3C5D;--row:#F3F9FB;
--ease:cubic-bezier(.2,.7,.2,1);color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--navy:#082C44;--text:#E3EEF2;--coral-text:#F2A28E;--link:#8ACDE2;--muted:#A6BCC6;
--card:#12303F;--band-white:#0D2533;--band-tint:#0A1C27;--soft:#102B39;--line:#1F4456;--line-strong:#2F5A6E;
--strong-bg:#3D241D;--possible-bg:#153D4F;--possible-text:#D4ECF4;--weak-bg:#1B3644;--weak-text:#BCCDD5;
--shadow:0 12px 32px rgba(0,0,0,.35);--track:#12303F;--chip-on-bg:#24566B;--chip-on-text:#ffffff;--row:#163747}}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:24px}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}*,*::before,*::after,::details-content{transition:none!important;animation:none!important}}
body{margin:0;background:var(--band-tint);color:var(--text);font:17px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
"Helvetica Neue",Arial,sans-serif;-webkit-text-size-adjust:100%;-webkit-font-smoothing:antialiased;overflow-wrap:break-word}
a{color:var(--link);text-underline-offset:3px}
a:focus-visible,button:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid var(--mid);outline-offset:2px;border-radius:8px}
time,.countdown,.num{font-variant-numeric:tabular-nums}
.wrap{max-width:1120px;margin:0 auto;padding:0 28px}
h1,h2,h3,h4{line-height:1.25;font-weight:600;color:var(--text);letter-spacing:-.01em}
[hidden]{display:none!important}
.skip{position:absolute;left:-999px;top:8px;background:var(--card);padding:10px 14px;border-radius:8px;z-index:60}
.skip:focus{left:12px}
.note,.muted{color:var(--muted)}
.note{font-size:.92rem;margin:8px 0}
.eyebrow{display:none}

/* Bands */
.band{padding:var(--sec) 0}
.band-white{background:var(--band-white)}
.band-tint{background:var(--band-tint)}
/* White bands round off into the tint behind them (the page itself is the tint), so no edge is a straight cut. */
.band-white.curve-bottom{border-radius:0 0 50% 50%/0 0 var(--curve) var(--curve)}
.band-white.curve-top{border-radius:50% 50% 0 0/var(--curve) var(--curve) 0 0}
.band h2.band-h{font-size:clamp(1.45rem,2.4vw,1.85rem);margin:0 0 14px}
.band p.band-sub{margin:0 0 40px;color:var(--muted);max-width:62ch;font-size:1.05rem}
.blue{position:relative;overflow:hidden;background:linear-gradient(165deg,var(--navy) 0%,var(--navy) 40%,var(--sea) 100%);color:var(--on-blue)}
.blue h1,.blue h2,.blue a:not(.btn){color:var(--on-blue)}
.blue>.wrap{position:relative;z-index:1}
/* The first blue band reaches up under the header (which is transparent), so the two read as one surface. */
.topbar+main>.blue:first-child,.topbar+.blue{margin-top:-200px;padding-top:calc(200px + var(--band-top,48px))}
/* Every blue band but the home hero (which ends in a wave) ends in a soft arc into the tint below. */
.blue:not(.hero){border-radius:0 0 50% 50%/0 0 var(--curve) var(--curve)}
/* Quiet decoration: faint overlapping circles in the right corner, kept in the margin beside the text column
   and below the header, so it never sits behind words. Hidden where there is no spare margin. */
.blue::before{content:"";position:absolute;z-index:0;pointer-events:none;top:200px;bottom:0;left:calc(50% + 548px);width:420px;
background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 420 420'%3E%3Cg fill='%23A9DDEE'%3E%3Ccircle cx='190' cy='150' r='150' fill-opacity='.07'/%3E%3Ccircle cx='110' cy='280' r='110' fill-opacity='.06'/%3E%3Ccircle cx='260' cy='320' r='90' fill-opacity='.07'/%3E%3C/g%3E%3C/svg%3E") left center/360px auto no-repeat;display:none}
.blue.hero::before{left:calc(50% + 200px);top:250px;bottom:auto;height:420px;width:520px;background-size:420px auto;background-position:left top}
@media (min-width:1240px){.blue::before{display:block}}

/* Header */
.topbar{position:relative;z-index:20;background:var(--navy);color:#fff}
body:has(.topbar+main>.blue:first-child:not([hidden])) .topbar,body:has(.topbar+.blue) .topbar{background:transparent}
.topbar .wrap{display:flex;align-items:center;gap:28px;min-height:72px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px;font-weight:700;font-size:1.2rem;color:#fff;text-decoration:none}
.brand .mark{width:26px;height:41px;flex:none}
nav.top{display:flex;gap:6px;flex-wrap:wrap}
nav.top a{color:#fff;text-decoration:none;font-size:.97rem;font-weight:550;padding:10px 16px;border-radius:999px;transition:background .2s,color .2s}
nav.top a:hover{background:rgba(255,255,255,.12)}
nav.top a[aria-current=page]{background:rgba(255,255,255,.16);color:#fff}
.topbar .updated{margin:0 0 0 auto;font-size:.88rem;color:var(--on-blue-2)}

/* Home hero */
.hero{--band-top:88px;padding-bottom:0}
.hero+.band{position:relative;z-index:2;margin-top:-2px}
.hero h1{color:#fff;font-size:clamp(1.85rem,4vw,2.75rem);font-weight:600;margin:0 0 22px;max-width:20ch;letter-spacing:-.02em}
.hero p.lede{margin:0;color:var(--on-blue-2);font-weight:400;font-size:clamp(1.05rem,1.8vw,1.25rem);max-width:56ch}
.hero .ctas{display:flex;flex-wrap:wrap;gap:14px;margin:32px 0 0}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;margin:56px 0 0;padding:28px 0 0;list-style:none;border-top:1px solid rgba(255,255,255,.28)}
.stats a{display:block;color:#fff;text-decoration:none}
.stats .n{display:block;font-size:clamp(2rem,4.4vw,3rem);font-weight:600;line-height:1.05;letter-spacing:-.02em}
.stats .l{display:block;margin-top:6px;font-size:1rem;color:#fff}
.stats a:hover .l{text-decoration:underline}
.wave{position:relative;z-index:1;display:block;width:100%;height:80px;margin:56px 0 -1px}
.wave path{fill:var(--band-white)}
.wave path.back{fill:var(--mid);opacity:.35}

/* Buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:inherit;font-size:1rem;font-weight:650;padding:10px 20px;min-height:48px;
border-radius:999px;border:2px solid var(--sea);background:var(--card);color:var(--link);cursor:pointer;text-decoration:none;
transition:background .2s,color .2s,border-color .2s,transform .2s var(--ease)}
.btn:hover{background:var(--soft);text-decoration:none}
.btn.primary{background:var(--sea);border-color:var(--sea);color:#fff}
.btn.primary:hover{background:var(--navy);border-color:var(--navy)}
.btn.white{background:#fff;border-color:#fff;color:#0B3C5D}
.btn.white:hover{background:var(--tint);border-color:var(--tint)}
.btn.outline-white{background:transparent;border-color:#fff;color:#fff}
.btn.outline-white:hover{background:rgba(255,255,255,.12)}
.btn.small{min-height:44px;padding:8px 16px;font-size:.95rem}
.btn.block{width:100%}
.btn.quiet{border-color:transparent;background:none;color:var(--muted);padding-left:4px;padding-right:4px}
.btn.quiet:hover{color:var(--text)}
.btn .count-dot{display:inline-flex;align-items:center;justify-content:center;min-width:24px;height:24px;padding:0 7px;border-radius:999px;background:var(--sea);color:#fff;font-size:.82rem}
.watch-btn{font:inherit;font-weight:650;font-size:.98rem;min-height:48px;padding:10px 20px;border-radius:999px;border:2px solid var(--line-strong);
background:var(--card);color:var(--text);cursor:pointer;transition:border-color .2s,background .2s}
.watch-btn:hover{border-color:var(--sea)}
.watch-btn[aria-pressed="true"]{background:var(--possible-bg);border-color:var(--sea);color:var(--possible-text)}
button.linklike{font:inherit;font-size:.95rem;color:var(--link);background:none;border:0;padding:8px 0;min-height:40px;cursor:pointer;text-decoration:underline;text-underline-offset:3px}

/* How it works */
.steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;margin:0;padding:0;list-style:none;counter-reset:step}
.steps li{background:var(--soft);border-radius:var(--r);padding:30px 28px;box-shadow:var(--shadow)}
.steps .icon{display:flex;align-items:center;justify-content:center;width:56px;height:56px;border-radius:16px;background:var(--sea);color:#fff;margin-bottom:18px}
.steps .icon svg{width:30px;height:30px}
.steps h3{font-size:1.2rem;margin:0 0 6px}
.steps h3::before{counter-increment:step;content:counter(step) ". ";color:var(--muted);font-weight:600}
.steps p{margin:0;color:var(--muted)}

/* Who it is for */
.who-for{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;margin:0;padding:0;list-style:none}
.who-for a{display:block;height:100%;background:var(--soft);border-radius:var(--r);padding:28px;text-decoration:none;color:var(--text);box-shadow:var(--shadow);
transition:transform .25s var(--ease)}
.who-for a:hover{transform:translateY(-2px)}
.who-for h3{margin:0 0 6px;font-size:1.2rem;color:var(--text)}
.who-for p{margin:0;color:var(--muted)}
.who-for .go{display:inline-block;margin-top:14px;color:var(--link);font-weight:650}

/* Project cards: compact, the whole card opens the project page */
.cards{display:grid;grid-template-columns:minmax(0,1fr);gap:18px;margin:16px 0 0;padding:0;list-style:none}
/* The Projects list: two columns on wide screens; cards in a row share one height. */
@media (min-width:1025px){#view .cards,#server-list .cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}}
.card{position:relative;background:var(--card);border-radius:var(--r);padding:22px 26px;box-shadow:var(--shadow);
min-width:0;transition:transform .25s var(--ease)}
.card:hover{transform:translateY(-2px)}
.card:target{outline:3px solid var(--mid);outline-offset:2px}
.card h4{margin:0;font-size:1.12rem;font-weight:600;line-height:1.4;padding-right:28px}
.card h4 a{color:var(--text);text-decoration:none}
.card.linked h4 a::after{content:"";position:absolute;inset:0;border-radius:var(--r)}
.card.linked::after{content:"";position:absolute;right:22px;top:24px;width:12px;height:12px;background:var(--sea);
-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M4.5 2l4 4-4 4' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/12px no-repeat;
mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M4.5 2l4 4-4 4' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/12px no-repeat}
.card .where{margin:4px 0 0;color:var(--muted);font-size:.97rem}
.card .status,.status{margin:12px 0 0;display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;font-size:.95rem}
.badge{display:inline-flex;align-items:center;gap:7px;font-size:.88rem;font-weight:650;padding:4px 12px;border-radius:999px;line-height:1.45;white-space:nowrap}
.tier-badge::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor;flex:none}
.t-strong{color:var(--coral-text);background:var(--strong-bg)}
.t-strong.tier-badge::before{background:var(--coral)}
.t-possible{color:var(--possible-text);background:var(--possible-bg)}
.t-possible.tier-badge::before{background:var(--sea)}
.t-weak,.t-watch,.t-none{color:var(--weak-text);background:var(--weak-bg)}
.stage-badge{font-weight:500;color:var(--text);background:none;border:1px solid var(--line-strong)}
.badge.score{display:inline;padding:0;background:none;color:var(--text);font-size:inherit;font-weight:700;white-space:normal}
.deadline{color:var(--coral-text);font-weight:700}
.center{text-align:center;margin:28px 0 0}

/* Projects page: step 1, the picker band */
.picker{--band-top:56px;padding-bottom:calc(var(--curve) + 40px)}
.picker .wrap{display:grid;grid-template-columns:minmax(0,560px) minmax(0,1fr);gap:16px 56px;align-items:end}
.picker .step{margin:0 0 10px;font-size:.95rem;font-weight:600;color:var(--on-blue-2);letter-spacing:.02em}
.picker label{display:block;font-size:clamp(1.5rem,3vw,2rem);font-weight:600;color:#fff;margin:0 0 18px;letter-spacing:-.01em}
.picker select{font:inherit;font-size:1.15rem;font-weight:650;color:#0B3C5D;background:#fff;border:0;border-radius:var(--r-sm);
padding:14px 48px 14px 18px;min-height:58px;width:min(100%,560px);cursor:pointer;-webkit-appearance:none;appearance:none;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 4.5l4 4 4-4' fill='none' stroke='%230B3C5D' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
background-repeat:no-repeat;background-position:right 18px center;background-size:14px}
.picker .profile-desc{margin:0 0 14px;color:#fff;font-size:1.05rem;max-width:44ch}
.picker .profile-empty{margin:0 0 14px;color:#fff;font-weight:600;max-width:44ch}

/* Projects page: step 2, the results */
.results{padding:var(--sec) 0}
.results .step{margin:0 0 6px;font-size:.95rem;font-weight:650;color:var(--muted)}
.results-bar{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px 24px}
.results-bar h2{margin:0 0 4px;font-size:clamp(1.4rem,2.3vw,1.75rem)}
#result-count{margin:4px 0 0;color:var(--muted);font-size:.97rem}
.result-actions{margin:0;display:flex;flex-wrap:wrap;gap:0 18px}
.active-filters{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px;margin:12px 0 0}
.fchip{font:inherit;font-size:.92rem;font-weight:600;min-height:40px;padding:6px 14px;border-radius:999px;border:0;cursor:pointer;background:var(--chip-on-bg);color:var(--chip-on-text);display:inline-flex;align-items:center;gap:6px}
.fchip span{font-size:1.15rem;line-height:1}
.fchip:hover{filter:brightness(.96)}
/* The buttons above the list sit in one light rounded track that hugs them; on a phone it scrolls sideways. */
.toolbar{display:flex;flex-wrap:nowrap;gap:4px;align-items:center;width:max-content;max-width:100%;margin:28px 0 0;padding:6px;
background:var(--track);border-radius:999px;box-shadow:var(--shadow);overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
.toolbar::-webkit-scrollbar{display:none}
.toolbar>*{flex:none}
.toolbar .chip{font:inherit;font-size:.95rem;font-weight:600;min-height:44px;padding:8px 18px;border-radius:999px;border:0;white-space:nowrap;
background:transparent;color:var(--text);cursor:pointer;display:inline-flex;align-items:center;gap:8px;transition:background .2s,color .2s}
.toolbar .chip:hover{background:var(--soft)}
.toolbar .chip[aria-expanded=true]{background:var(--chip-on-bg);color:var(--chip-on-text)}
.toolbar .chip .n{color:var(--coral-text);font-weight:700}
.toolbar .chip[aria-expanded=true] .n{color:var(--chip-on-text)}
.info-panel{margin:20px 0 0;background:var(--card);border-radius:var(--r);padding:28px 32px;box-shadow:var(--shadow);animation:fold-in .28s var(--ease)}
.info-panel p,.info-panel li{max-width:70ch}
.info-panel h3{margin:0 0 10px;font-size:1.2rem}
.info-panel p{margin:6px 0}
.summary-line{color:var(--muted);font-size:.95rem}
section.soon ul{list-style:none;padding:0;margin:16px 0 0;display:grid;gap:8px}
section.soon li{display:grid;grid-template-columns:6.5em minmax(0,1fr) auto;gap:2px 18px;align-items:baseline;padding:14px 20px;background:var(--row);border-radius:16px;max-width:none}
section.soon li time{color:var(--coral-text);font-weight:700}
section.soon li a{color:var(--text);font-weight:600}
section.soon .meta{color:var(--muted);font-size:.9rem;white-space:nowrap;display:flex;gap:10px;align-items:baseline}
section.soon .meta .badge{font-size:.82rem;padding:2px 10px}
section.explain ul{padding-left:1.2em;margin:8px 0}
.cov-wrap{overflow-x:auto;margin-top:12px;background:var(--row);border-radius:var(--r-sm)}
table.cov{width:100%;border-collapse:collapse;font-size:.93rem}
table.cov th,table.cov td{text-align:left;padding:10px 14px;border-top:1px solid var(--line);vertical-align:top}
table.cov thead th{border-top:0;color:var(--muted);font-weight:600}
table.cov tr.region td:first-child{padding-left:28px;color:var(--muted)}
.pill{font-size:.9rem;color:var(--muted);white-space:nowrap}
.pill.covered{color:var(--link);font-weight:650}
.pill.partial{color:var(--coral-text)}
section.country>h2,section.group>h2{display:flex;align-items:baseline;flex-wrap:wrap;gap:10px;font-size:1.3rem;margin:48px 0 0}
section.country>p.country-note{color:var(--muted);margin:4px 0;font-size:.95rem;max-width:80ch}
section.tier h3{display:flex;align-items:center;gap:10px;font-size:1.05rem;margin:28px 0 0}
section.tier h3::before{content:"";width:10px;height:10px;border-radius:50%;background:var(--line-strong);flex:none}
section.tier[data-tier=strong] h3::before{background:var(--coral)}
section.tier[data-tier=possible] h3::before{background:var(--sea)}
section.tier p.tier-desc{color:var(--muted);margin:2px 0 0 20px;font-size:.93rem}
.count{color:var(--muted);font-weight:500;font-size:.85em}
.empty{padding:32px;color:var(--muted);text-align:center;background:var(--card);border-radius:var(--r);box-shadow:var(--shadow);margin:20px 0}

/* Filters: a panel that slides in from the right, a bottom sheet on phones */
.scrim{position:fixed;inset:0;background:rgba(6,30,46,.45);z-index:70;opacity:0;transition:opacity .25s}
.scrim.on{opacity:1}
.sheet{position:fixed;top:0;right:0;bottom:0;width:min(420px,100vw);z-index:80;background:var(--card);color:var(--text);box-shadow:var(--shadow);
display:flex;flex-direction:column;transform:translateX(100%);transition:transform .3s var(--ease);border-radius:var(--r) 0 0 var(--r)}
.sheet.on{transform:none}
.sheet-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 20px 16px 24px;border-bottom:1px solid var(--line)}
.sheet-head h2{margin:0;font-size:1.3rem}
.sheet-close{font:inherit;width:48px;height:48px;border-radius:50%;border:0;background:var(--soft);color:var(--text);cursor:pointer;font-size:1.5rem;line-height:1}
.sheet-body{overflow:auto;padding:20px 24px;display:grid;gap:18px;flex:1}
.sheet-foot{padding:14px 24px;border-top:1px solid var(--line);display:flex;gap:12px}
#filters-done{flex:1}
.filters{display:grid;gap:16px;margin:0}
.filters label,.sheet-body>label,.sheet fieldset>label{display:flex;flex-direction:column;gap:6px;font-size:.92rem;font-weight:600;color:var(--muted)}
.sheet select,.sheet input[type=search]{font:inherit;font-size:1rem;color:var(--text);background:var(--soft);padding:10px 12px;border:1px solid var(--line-strong);
border-radius:var(--r-sm);width:100%;min-width:0;min-height:48px}
.sheet select:focus,.sheet input[type=search]:focus{border-color:var(--sea);background:var(--card)}
.sheet .check{flex-direction:row!important;align-items:center;gap:12px;color:var(--text)!important;font-weight:500!important;min-height:44px;cursor:pointer;font-size:1rem!important}
input[type=checkbox],input[type=radio]{accent-color:var(--sea);width:20px;height:20px;margin:0;flex:none}
fieldset.views{border:0;margin:0;padding:0;display:grid;gap:2px}
fieldset.views legend{font-size:.92rem;font-weight:600;color:var(--muted);margin-bottom:6px;padding:0}
.sheet .sep{border-top:1px solid var(--line);padding-top:14px}
.side-label{font-size:.92rem;font-weight:600;color:var(--muted);margin:0}
.downloads,.share{display:grid;gap:10px}
body.sheet-open{overflow:hidden}

/* Fold-outs (project page and About): the arrow swaps between two images, it never rotates. */
details>summary{cursor:pointer;list-style:none}
details>summary::-webkit-details-marker{display:none}
details.fold>summary::after,details.more>summary::after,.prose details>summary::after{content:"";display:inline-block;flex:none;width:14px;height:14px;margin-left:auto;background:var(--sea);
-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 4.5l4 4 4-4' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/14px no-repeat;
mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 4.5l4 4 4-4' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/14px no-repeat}
details[open].fold>summary::after,details[open].more>summary::after,.prose details[open]>summary::after{
-webkit-mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 7.5l4-4 4 4' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
mask-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 7.5l4-4 4 4' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")}
@supports (interpolate-size:allow-keywords){:root{interpolate-size:allow-keywords}
details.fold::details-content,details.more::details-content{height:0;overflow:clip;transition:height .3s var(--ease),content-visibility .3s allow-discrete}
details[open].fold::details-content,details[open].more::details-content{height:auto}}
details[open].fold>.fold-body,details[open].more>div{animation:fold-in .3s var(--ease)}
@keyframes fold-in{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
/* Bands below the first screen fade in as they scroll into view (opacity only). Off with reduced motion. */
.band.pre{opacity:0;transition:opacity .7s var(--ease)}
.band.pre.in{opacity:1}
@media (prefers-reduced-motion:reduce){.band.pre{opacity:1}.card:hover,.who-for a:hover{transform:none}}
@media print{.band.pre{opacity:1}}
details.fold{background:var(--card);border-radius:var(--r);box-shadow:var(--shadow);margin:0 0 20px}
details.fold>summary{display:flex;align-items:center;gap:12px;min-height:68px;padding:18px 28px;font-size:1.2rem;font-weight:600;border-radius:var(--r);transition:background .2s}
details.fold>summary:hover{background:var(--soft)}
details.fold>.fold-body{padding:0 28px 26px}
.fold-body p,.fold-body li{max-width:70ch}
details.more>summary{display:flex;align-items:center;gap:8px;min-height:44px;color:var(--link);font-weight:650}

/* Project page */
.page-band{--band-top:48px;padding-bottom:calc(var(--curve) + 36px)}
.crumbs{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 18px;font-size:.95rem;color:var(--on-blue-2)}
.crumbs a{color:#fff}
.crumbs span:last-child{display:none}
.page-band h1{color:#fff;margin:0 0 12px;font-size:clamp(1.5rem,3.2vw,2.2rem);font-weight:600;max-width:30ch}
.page-band .place{margin:0;color:var(--on-blue-2);font-size:1.08rem}
.page-band .status{margin-top:18px}
.page-band .t-strong{background:#FCEBE6;color:#A9412B}
.page-band .t-possible{background:#E8F4F8;color:#0B3C5D}
.page-band .t-weak,.page-band .t-watch,.page-band .t-none{background:#EEF2F4;color:#3E5663}
.page-band .deadline{color:#fff;background:rgba(226,114,91,.95);background:#B4452E;padding:4px 12px;border-radius:999px;font-size:.9rem}
.page-band p.note{color:var(--on-blue-2)}
.project-body{padding:var(--sec) 0}
article.project{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:32px;align-items:start}
.project-main{min-width:0}
.project-side{display:grid;gap:20px}
.side-card{background:var(--card);border-radius:var(--r);padding:24px 26px;box-shadow:var(--shadow)}
.side-card h2{margin:0 0 12px;font-size:1.05rem}
.side-card .actions{display:grid;gap:10px;margin:0}
.fold-body h3,.why-h{margin:18px 0 6px;font-size:1rem;color:var(--muted);font-weight:650}
p.approach{margin:0 0 12px;padding:14px 16px;background:var(--possible-bg);color:var(--possible-text);border-radius:var(--r-sm);font-size:1.05rem}
p.approach strong{font-weight:750}
p.summary{margin:0 0 10px}
ul.why,.research ul{margin:4px 0 8px;padding-left:1.2em}
ul.why li,.research li{margin:4px 0}
.getin{margin:12px 0;padding:16px 18px;background:var(--soft);border-radius:var(--r-sm)}
.getin-h{margin:0;font-weight:600}
.getin ul{margin-bottom:0}
a.fc,a.src{font-size:.92rem;white-space:nowrap}
.not-named{color:var(--muted)}
dl.facts{display:grid;grid-template-columns:minmax(0,1fr);gap:0;margin:6px 0;font-size:.97rem}
dl.facts dt{color:var(--muted);font-size:.9rem}
dl.facts dd{margin:0 0 10px;min-width:0;overflow-wrap:anywhere}
ul.quotes{margin:8px 0;padding:0;list-style:none;display:grid;gap:10px}
ul.quotes li{border-left:4px solid var(--sea);background:var(--soft);border-radius:0 var(--r-sm) var(--r-sm) 0;padding:10px 14px}
ul.who{list-style:none;padding:0;margin:0;display:grid;gap:14px}
ul.who .role{display:block;font-size:.9rem;color:var(--muted);font-weight:600}
ul.who .how{display:block;font-size:.9rem;color:var(--muted)}
ul.profile-list{list-style:none;padding:0;margin:0;display:grid;gap:12px}
ul.profile-list p.because{margin:4px 0 0;font-size:.92rem;color:var(--muted)}
p.source{margin:4px 0;color:var(--muted);font-size:.95rem}
p.attr{margin:6px 0 0;color:var(--muted);font-size:.88rem}
.scorebox{display:flex;align-items:baseline;gap:8px}
.scorebox .big{font-size:2.3rem;font-weight:600;line-height:1;letter-spacing:-.02em}
.scorebox .of{color:var(--muted)}
.meter{height:8px;border-radius:99px;background:var(--tint);overflow:hidden;margin:12px 0 8px}
.meter span{display:block;height:100%;width:calc(var(--s) * 1%);background:var(--line-strong);border-radius:99px}
article.project[data-tier=strong] .meter span{background:var(--coral)}
article.project[data-tier=possible] .meter span{background:var(--sea)}
ol.timeline{list-style:none;padding:0;margin:8px 0}
ol.timeline li{position:relative;padding:0 0 14px 22px;font-size:.95rem}
ol.timeline li::before{content:"";position:absolute;left:0;top:.45em;width:10px;height:10px;border-radius:50%;background:var(--card);border:2px solid var(--line-strong)}
ol.timeline li.official::before{background:var(--sea);border-color:var(--sea)}
ol.timeline time{display:block;font-size:.88rem;color:var(--muted);font-weight:650}
ul.news{padding-left:18px;margin:8px 0}
.gone{background:var(--strong-bg);color:var(--coral-text);padding:14px 18px;border-radius:var(--r-sm);margin:0 0 16px}
abbr[title]{text-decoration:underline dotted;text-underline-offset:3px;cursor:help}

/* About and shortlist */
.page-head{--band-top:0px}
.page-head .wrap{padding-top:56px;padding-bottom:calc(var(--curve) + 40px)}
.page-head h1{color:#fff;margin:0 0 14px;font-size:clamp(1.6rem,3.4vw,2.3rem);font-weight:600}
.page-head p{margin:0;max-width:66ch;color:var(--on-blue-2);font-size:1.05rem}
.page-head p a{color:#fff}
.page-head p.meta{margin-top:10px;font-size:.92rem}
.page-head .mark{display:none}
.about-layout{display:grid;grid-template-columns:220px minmax(0,1fr);gap:40px;align-items:start;padding-top:var(--sec);padding-bottom:var(--sec)}
nav.toc{position:sticky;top:24px;display:grid;gap:2px;font-size:.95rem}
nav.toc a{color:var(--muted);text-decoration:none;padding:8px 12px;border-radius:10px;transition:background .2s}
nav.toc a:hover{background:var(--card);color:var(--text)}
.prose{min-width:0;background:var(--card);border-radius:var(--r);padding:40px 48px;box-shadow:var(--shadow)}
.prose h2{font-size:1.35rem;margin:44px 0 12px;padding-top:32px;border-top:1px solid var(--line)}
.prose h2:first-child{border-top:0;padding-top:0;margin-top:0}
.prose h3{font-size:1.1rem;margin:24px 0 6px}
.prose p,.prose li{max-width:70ch}
.prose details{margin:10px 0}
.prose details>summary{display:flex;align-items:center;gap:8px;color:var(--link);font-weight:650;min-height:44px}
main.shortlist-main{padding-top:var(--sec);padding-bottom:var(--sec)}
.shortlist-intro p{margin:0;color:var(--muted)}
ol.shortlist{list-style:none}
ol.shortlist .card .because{margin:10px 0 0;padding:10px 14px;background:var(--soft);border-radius:var(--r-sm);font-size:.95rem}
ol.shortlist .card dl.facts{grid-template-columns:10em minmax(0,1fr);gap:4px 14px}
ol.shortlist .card dl.facts dd{margin:0}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0 4px}

/* Footer */
/* The footer rises over the end of the band above it in a soft arc. */
footer{position:relative;z-index:1;margin-top:calc(-1 * var(--curve));background:var(--navy);color:var(--on-blue-2);
padding:calc(var(--curve) + 40px) 0 56px;font-size:.95rem;border-radius:50% 50% 0 0/var(--curve) var(--curve) 0 0}
.foot-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:28px 56px}
footer p{max-width:72ch;margin:0 0 10px}
footer a{color:#fff}
footer .brand{margin-bottom:14px;font-size:1.1rem}
footer nav{display:grid;gap:4px;align-content:start}
footer nav a{text-decoration:none;padding:6px 0}
footer nav a:hover{text-decoration:underline}

@media (max-width:1000px){.topbar .updated{display:none}}
@media (max-width:900px){.steps,.who-for{grid-template-columns:minmax(0,1fr)}
.picker .wrap{grid-template-columns:minmax(0,1fr);gap:0}.picker .profile-desc,.picker .profile-empty{margin:16px 0 0;max-width:62ch}
.steps{gap:12px}.steps li{display:grid;grid-template-columns:52px minmax(0,1fr);gap:2px 18px;padding:20px 22px}
.steps .icon{grid-row:1/3;width:52px;height:52px;margin:0}.steps h3{margin:4px 0 2px}article.project{grid-template-columns:minmax(0,1fr)}
.about-layout{grid-template-columns:minmax(0,1fr);gap:16px}nav.toc{position:static;display:flex;flex-wrap:wrap;gap:4px}
nav.toc .side-label{width:100%}}
@media (max-width:640px){:root{--sec:56px;--curve:40px}body{font-size:16px}.wrap{padding:0 18px}.topbar .updated{display:none}
.topbar .wrap{gap:4px 10px;padding-top:10px;padding-bottom:6px;min-height:0}.brand .mark{width:22px;height:35px}
nav.top{gap:2px;margin:0 -12px;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;max-width:calc(100% + 24px)}
nav.top a{padding:12px 12px;min-height:48px;white-space:nowrap;font-size:.93rem}
.hero{--band-top:44px}.hero .ctas .btn{flex:1 1 100%}.stats{gap:12px;margin-top:36px;padding-top:20px}.stats .l{font-size:.86rem}
.wave{height:44px;margin-top:28px}
.hide-sm{display:none}.show-sm[hidden]{display:inline!important}
.steps{gap:12px}.steps li{display:grid;grid-template-columns:48px minmax(0,1fr);gap:2px 16px;padding:18px}
.steps .icon{grid-row:1/3;width:48px;height:48px;border-radius:14px;margin:0}.steps .icon svg{width:26px;height:26px}
.steps h3{font-size:1.08rem;margin:2px 0 2px}.steps p{font-size:.95rem}
.who-for{gap:12px}.who-for a{padding:20px}
button.linklike,.btn.small,.sheet .check,.sheet-close{min-height:48px}.sheet-close{width:48px;height:48px}
details>summary{min-height:48px;display:flex;align-items:center}.sheet select,.sheet input[type=search]{min-height:48px}
nav.toc a,footer nav a{min-height:48px;display:flex;align-items:center}
.card{padding:18px 18px}.card.linked::after{right:16px;top:22px}
.picker{--band-top:32px;padding-bottom:calc(var(--curve) + 28px)}.picker select{width:100%}
.page-band{--band-top:28px;padding-bottom:calc(var(--curve) + 24px)}.page-head .wrap{padding-top:32px;padding-bottom:calc(var(--curve) + 28px)}
section.soon li{grid-template-columns:minmax(0,1fr);padding:12px 16px}
.toolbar .chip{min-height:48px}.fchip{min-height:48px}
.sheet{top:auto;left:0;width:100%;max-height:88vh;border-radius:var(--r) var(--r) 0 0;transform:translateY(100%)}
.sheet.on{transform:none}
details.fold>summary{padding:14px 18px;font-size:1.15rem}details.fold>.fold-body{padding:0 18px 18px}
.info-panel{padding:22px 18px}.prose{padding:28px 20px}.foot-grid{grid-template-columns:minmax(0,1fr)}
ol.shortlist .card dl.facts{grid-template-columns:minmax(0,1fr)}}
"""


# Shared by the list and the project pages: countdowns and the watch list (stored only in this browser).
JS_COMMON = r"""
var MPF = (function(){
  var today = new Date(); today.setHours(0,0,0,0);
  function daysLeft(iso){ if(!iso) return null; var p = iso.split('-');
    var d = new Date(+p[0], +p[1]-1, +p[2]); return Math.round((d - today)/86400000); }
  var STAGE = {open:'Under review, open for public comment', closed:'Under review, decision pending', decided:'Approved',
               assessment:'Under review', withdrawn:'Withdrawn or rejected', unknown:'Stage not stated',
               watch:'Project page we follow'};
  var MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  // 'Dec 3', with the year only when it is not this year.
  function shortDate(iso){ var p = iso.split('-'); return MON[+p[1]-1] + ' ' + (+p[2]) + (+p[0] === today.getFullYear() ? '' : ', ' + p[0]); }
  function countdown(el){
    var dl = el.getAttribute('data-deadline'), n = daysLeft(dl), kind = el.getAttribute('data-stage');
    if (kind !== 'decided' && kind !== 'withdrawn' && kind !== 'watch' && n !== null) { kind = n >= 0 ? 'open' : 'closed'; }
    el.setAttribute('data-stage-now', kind); el._days = n;
    var cd = el.querySelector('.countdown');
    if (cd) {
      if (kind === 'watch') { /* project watch: keep "not applicable" */ }
      else if (kind === 'decided') cd.textContent = (dl ? dl + ', ' : '') + 'decision made';
      else if (n === null) cd.textContent = 'Not published';
      else if (n > 0) cd.textContent = shortDate(dl);
      else if (n === 0) cd.textContent = 'today';
      else cd.textContent = shortDate(dl) + ' (passed)';
    }
    var dlw = el.querySelector('.deadline'); if (dlw) dlw.hidden = kind !== 'open';
    var st = el.querySelector('.stage-badge');
    if (st && kind !== el.getAttribute('data-stage')) st.textContent = STAGE[kind];
    var ou = el.querySelector('.open-until'); if (ou) ou.hidden = kind !== 'open';
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
  return {shortDate: shortDate, daysLeft: daysLeft, countdown: countdown, getWatch: getWatch, initWatch: initWatch, STAGE: STAGE};
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
  if (param('open') === '1') fo.checked = true;
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
  var PSTAGE = {open: 'Under review, open for public comment', closed: 'Under review, decision pending',
                assessment: 'Under review', decided: 'Approved, may not be built yet', withdrawn: 'Withdrawn or rejected',
                unknown: 'Stage not stated', watch: 'Project page we follow'};
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
    if (productView() && k === 'decided' && c.getAttribute('data-wstage') !== 'under_construction') {
      if (st) st.textContent = 'Approved, may not be built yet';
    } else if (st && c._st) { st.textContent = c._st.text; }
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
  // The address always mirrors what is chosen now, so a filter switched off never comes back on reload.
  var OWN = ['profile', 'open', 'country', 'show', 'view', 'sort', 'group'];
  function shareUrl(){
    var base = location.href.split('#')[0].split('?')[0];
    var qs = location.search.replace(/^\?/, '').split('&').filter(function(x){
      return x && OWN.indexOf(x.split('=')[0]) < 0; });
    if (prof() !== 'all') qs.push('profile=' + encodeURIComponent(prof()));
    if (typeof fc !== 'undefined') {
      if (fc.value) qs.push('country=' + fc.value);
      if (fo.checked) qs.push('open=1');
      if (fw.checked) qs.push('show=weak');
      var v = currentView(); if (v !== 'all') qs.push('view=' + v);
      if (fsort.value !== 'default') qs.push('sort=' + fsort.value);
      if (fgroup.value !== 'country') qs.push('group=' + fgroup.value);
    }
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
  // "Open for comment only": closing soonest first, in one list (unless another sort or grouping is chosen).
  function sortMode(){ return fo.checked && fsort.value === 'default' ? 'deadline' : fsort.value; }
  function groupMode(){ return fo.checked && fgroup.value === 'country' && fsort.value === 'default' ? 'none' : fgroup.value; }
  function cmp(a, b){
    var s = sortMode(), sa = scoreOf(a), sb = scoreOf(b);
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
    var g = groupMode();
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
  var shownOrder = [], PAGE = 12, limit = PAGE;
  // apply(true) keeps how many cards are shown ("Show more"); every other change starts again at 12.
  function apply(keep){
    if (keep !== true) limit = PAGE;
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
      if (groupMode() === 'country' && sortMode() === 'default') {
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
    shownOrder.forEach(function(c, i){ c.hidden = i >= limit; });
    Array.prototype.forEach.call(view.querySelectorAll('section.tier, section.group'), function(s){
      s.hidden = !s.querySelector('.card:not([hidden])'); });
    var more = $('show-more'), left = shownOrder.length - limit;
    if (more) { more.hidden = left <= 0; more.textContent = 'Show more (' + left + ' more)'; }
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
    // One row per deadline, in aligned columns: date, project, tier and country.
    function el(tag, cls, text){ var e = document.createElement(tag); if (cls) e.className = cls;
                                 if (text !== undefined) e.textContent = text; return e; }
    soon.forEach(function(c){
      var li = el('li'), a = el('a', '', c.querySelector('h4').textContent), tier = c.querySelector('.tier-badge');
      a.href = c.querySelector('h4 a').getAttribute('href');
      var dd = c.getAttribute('data-deadline');
      var t = el('time', 'd', c._days === 0 ? 'Today' : MPF.shortDate(dd));
      t.setAttribute('datetime', dd);
      li.appendChild(t);
      li.appendChild(a);
      var meta = el('span', 'meta'); meta.appendChild(el('span', tier.className.replace('tier-badge', '').trim() + ' badge', tier.textContent));
      meta.appendChild(el('span', '', c.getAttribute('data-country-name'))); li.appendChild(meta);
      ul.appendChild(li);
    });
    if (none) none.hidden = soon.length > 0;
    line.textContent = soon.length + (soon.length === 1 ? ' deadline' : ' deadlines') +
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
    var nf = (fq.value ? 1 : 0) + (fc.value ? 1 : 0) + (fr.value ? 1 : 0) + (ft.value ? 1 : 0) + (fs.value ? 1 : 0) +
             (fo.checked ? 1 : 0) + (fw.checked ? 1 : 0) + (tf && tf.checked ? 1 : 0) + (fsort.value !== 'default' ? 1 : 0) +
             (fgroup.value !== 'country' ? 1 : 0) + (v !== 'all' ? 1 : 0), fcnt = $('filter-count');
    if (fcnt) { fcnt.textContent = nf; fcnt.hidden = !nf; }
    var cs = $('chip-soon'); if (cs) cs.textContent = soon.length;
    paintActive(shown.length, v, watch);
    try { history.replaceState(null, '', shareUrl() + location.hash); } catch (e) {}
    paintBar();
  }
  // Every filter in use is shown as a chip with its own remove button, so nothing is ever hidden silently.
  function activeFilters(v){
    var a = [], txt = function(sel){ return sel.options[sel.selectedIndex].text; };
    if (v !== 'all') a.push({label: {soon: 'Comment deadline soon', changed: 'Changed since last update', watch: 'My watch list'}[v],
                             off: function(){ $('v-all').checked = true; }});
    if (fo.checked) a.push({label: 'Open for public comment only', off: function(){ fo.checked = false; }});
    if (fc.value) a.push({label: txt(fc).replace(/ \(.*\)$/, ''), off: function(){ fc.value = ''; fillRegions(); }});
    if (fr.value) a.push({label: fr.value, off: function(){ fr.value = ''; }});
    if (ft.value) a.push({label: ft.value, off: function(){ ft.value = ''; }});
    if (fs.value) a.push({label: txt(fs), off: function(){ fs.value = ''; }});
    if (fq.value) a.push({label: 'Search: ' + fq.value, off: function(){ fq.value = ''; }});
    if (fw.checked) a.push({label: 'Weak fits shown', off: function(){ fw.checked = false; }});
    if (tf && tf.checked) a.push({label: 'Freshwater included', off: function(){ tf.checked = false; }});
    if (fsort.value !== 'default') a.push({label: 'Sorted: ' + txt(fsort), off: function(){ fsort.value = 'default'; }});
    if (fgroup.value !== 'country') a.push({label: 'Grouped: ' + txt(fgroup), off: function(){ fgroup.value = 'country'; }});
    return a;
  }
  function paintActive(n, v, watch){
    var box = $('active-filters'); if (!box) return;
    var a = activeFilters(v);
    while (box.firstChild) box.removeChild(box.firstChild);
    box.hidden = !a.length;
    a.forEach(function(f){
      var b = document.createElement('button'); b.type = 'button'; b.className = 'fchip';
      b.setAttribute('aria-label', 'Remove filter: ' + f.label);
      b.appendChild(document.createTextNode(f.label + ' '));
      var x = document.createElement('span'); x.setAttribute('aria-hidden', 'true'); x.textContent = '\u00d7'; b.appendChild(x);
      b.addEventListener('click', function(){ f.off(); apply(); var r = $('active-filters'); if (r && !r.hidden) { var c = r.querySelector('button'); if (c) c.focus(); } });
      box.appendChild(b);
    });
    if (a.length > 1) {
      var c = document.createElement('button'); c.type = 'button'; c.className = 'linklike'; c.textContent = 'Clear all filters';
      c.addEventListener('click', function(){ var r = $('reset-btn'); if (r) r.click(); });
      box.appendChild(c);
    }
    // With nothing to show, say which filters cause it and how many come back without "open for comment only".
    if (!n && a.length) {
      var names = a.map(function(f){ return f.label; }).join(', ');
      var more = '';
      if (fo.checked) { fo.checked = false;
        var k = cards.filter(function(c){ return visible(c, v, watch); }).length; fo.checked = true;
        if (k) more = ' Without "Open for public comment only" there ' + (k === 1 ? 'is 1 project.' : 'are ' + k + ' projects.'); }
      if (v !== 'watch' || watch.length) empty.textContent = 'No projects match these filters: ' + names + '.' + more +
        ' Remove a filter above, or clear all.';
    }
  }
  if (fp) {
    var want = param('profile') || stored() || 'hard-structure';   // hard-structure is the main view
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
  fc.addEventListener('change', function(){ fillRegions(); apply(); });
  [fr, ft, fs, fo, fw, fsort, fgroup].forEach(function(el){ el.addEventListener('change', apply); });
  Array.prototype.forEach.call(views, function(el){ el.addEventListener('change', apply); });
  fq.addEventListener('input', apply);
  MPF.initWatch(function(){ if (currentView() === 'watch') apply(); });
  var sm = $('show-more');
  if (sm) sm.addEventListener('click', function(){ var first = shownOrder[limit]; limit += PAGE; apply(true);
    if (first) { var a = first.querySelector('h4 a'); if (a) a.focus(); } });
  // Filters live in a panel that slides in (a bottom sheet on phones). Esc or the backdrop closes it.
  var sheet = $('filters'), scrim = $('scrim'), fbtn = $('filters-btn');
  function openSheet(){ sheet.hidden = false; scrim.hidden = false; document.body.classList.add('sheet-open');
    requestAnimationFrame(function(){ requestAnimationFrame(function(){ sheet.classList.add('on'); scrim.classList.add('on'); }); });
    setTimeout(function(){ var f = sheet.querySelector('input,select,button'); if (f) f.focus(); }, 60); }
  function closeSheet(){ if (sheet.hidden) return; sheet.classList.remove('on'); scrim.classList.remove('on');
    document.body.classList.remove('sheet-open');
    setTimeout(function(){ sheet.hidden = true; scrim.hidden = true; }, 300); if (fbtn) fbtn.focus(); }
  if (fbtn) fbtn.addEventListener('click', openSheet);
  [$('filters-close'), $('filters-done'), scrim].forEach(function(el){ if (el) el.addEventListener('click', closeSheet); });
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape') closeSheet(); });
  // Deadlines, changes, coverage and how-to-read sit behind buttons above the list; one opens at a time.
  var chips = Array.prototype.slice.call(document.querySelectorAll('.toolbar .chip'));
  function showPanel(id){
    chips.forEach(function(b){ var on = b.getAttribute('aria-controls') === id && b.getAttribute('aria-expanded') !== 'true';
      b.setAttribute('aria-expanded', on ? 'true' : 'false'); $(b.getAttribute('aria-controls')).hidden = !on; });
  }
  chips.forEach(function(b){ b.addEventListener('click', function(){ showPanel(b.getAttribute('aria-controls')); }); });
  function fromHash(){ var id = location.hash.slice(1);
    if (chips.some(function(b){ return b.getAttribute('aria-controls') === id; }) && $(id).hidden) {
      showPanel(id); var bar = document.querySelector('.toolbar'); if (bar) bar.scrollIntoView(); } }
  window.addEventListener('hashchange', fromHash);
  apply();
  fromHash();
})();
"""

JS_PAGE = r"""
(function(){
  var el = document.querySelector('.project'); if (el) MPF.countdown(el);
  MPF.initWatch();
})();
"""


def _head(title: str, description: str, prefix: str = "") -> str:
    return (f'<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<meta name="robots" content="noindex, nofollow">\n<title>{escape(title)}</title>\n'
            f'<meta name="description" content="{escape(description)}">\n<meta name="theme-color" content="{LOGO_COLOUR}">\n'
            f'{_favicon(prefix)}\n<style>{CSS}</style>')


# The mark: a map pin in sea blue with a white sea fan coral inside, growing from one point (no stem).
# mpf/assets/logo.svg is the only drawing; favicon.svg is its cut-down tab version, and `python -m mpf.icons`
# builds the PNG and ICO copies from them. write_site copies every file in mpf/assets to the site.
ASSETS = Path(__file__).parent / "assets"
LOGO_FILE = (ASSETS / "logo.svg").read_text(encoding="utf-8")
FAVICON_FILE = (ASSETS / "favicon.svg").read_text(encoding="utf-8")
LOGO_COLOUR = "#1B7F9E"
LOGO = LOGO_FILE.strip().replace('<svg xmlns="http://www.w3.org/2000/svg" ',
                                 '<svg class="mark" role="img" aria-hidden="true" focusable="false" ', 1)


def _favicon(prefix: str = "") -> str:
    from urllib.parse import quote
    return (f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,{quote(FAVICON_FILE.strip())}">\n'
            f'<link rel="icon" type="image/png" sizes="32x32" href="{prefix}favicon-32.png">\n'
            f'<link rel="icon" type="image/png" sizes="16x16" href="{prefix}favicon-16.png">\n'
            f'<link rel="apple-touch-icon" href="{prefix}apple-touch-icon.png">')


def _date(d: date) -> str:
    return d.strftime("%B %-d, %Y")


def _topbar(site: dict, updated: date, active: str = "", prefix: str = "") -> str:
    """Sticky bar on every page. prefix is "../" on project pages."""
    title = escape(site.get("title", "MitiMark"))
    links = [("projects", "projects.html", "Projects"), ("soon", "projects.html#soon", '<span class="hide-sm">Comment deadlines</span><span class="show-sm" hidden>Deadlines</span>'),
             ("coverage", "projects.html#coverage", "Coverage"), ("about", "about.html", "About")]
    current = ' aria-current="page"'
    nav = "".join(f'<a href="{prefix}{href}"{current if key == active else ""}>{label}</a>'
                  for key, href, label in links)
    return (f'<a class="skip" href="#main">Skip to content</a>\n'
            f'<header class="topbar"><div class="wrap">\n<a class="brand" href="{prefix}index.html">{LOGO}<span>{title}</span></a>\n'
            f'<nav class="top" aria-label="Main">{nav}</nav>\n'
            f'<p class="updated">Updated <time>{_date(updated)}</time></p>\n</div></header>')


# Bands that start below the first screen fade in as they scroll into view. Nothing happens with reduced motion,
# without IntersectionObserver or without JavaScript, and bands already on screen are never hidden.
REVEAL_JS = ("(function(){if(!window.IntersectionObserver||!window.matchMedia||"
             "matchMedia('(prefers-reduced-motion: reduce)').matches)return;"
             "var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){"
             "e.target.classList.add('in');io.unobserve(e.target);}});},{rootMargin:'0px 0px -8% 0px'});"
             "Array.prototype.forEach.call(document.querySelectorAll('.band:not(.blue)'),function(b){"
             "if(b.getBoundingClientRect().top>innerHeight){b.classList.add('pre');io.observe(b);}});})();")


def _footer(site: dict, prefix: str = "") -> str:
    email = escape(site.get("contact_email", ""))
    title = escape(site.get("title", "MitiMark"))
    return (
        '<footer><div class="wrap foot-grid"><div>'
        f'<a class="brand" href="{prefix}index.html">{LOGO}<span>{title}</span></a>'
        f"<p>{title} is a prototype. Flags show projects that may need marine habitat work. They do not state that "
        "mitigation is required. Always check the official notice. Compiled from public sources.</p>"
        f'<p>Contact: {escape(site.get("contact_name", ""))}, <a href="mailto:{email}">{email}</a></p></div>'
        f'<nav aria-label="Footer"><a href="{prefix}index.html">Home</a><a href="{prefix}projects.html">Projects</a>'
        f'<a href="{prefix}projects.html#soon">Comment deadlines</a>'
        f'<a href="{prefix}shortlist.html">Hard-structure shortlist</a><a href="{prefix}about.html">About and method</a><a href="{prefix}about.html#coverage">Coverage</a>'
        f'<a href="{prefix}projects.csv">All listed projects (CSV)</a></nav>'
        f"</div></footer>\n<script>{REVEAL_JS}</script>")


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
            f'{_open_until_badge(rec)}'
            f'{("<span class=" + chr(34) + "badge devonly" + chr(34) + ">" + escape(DEVELOPER_ONLY) + "</span>") if rec.get("developer_only") else ""}'
            f'<span class="badge late"{"" if rec["too_late"] else " hidden"}>Likely too late</span>'
            f'{review_badge}{lang_badge}{chg}'
            f'{"<span class=" + chr(34) + "badge fresh" + chr(34) + ">Freshwater</span>" if rec.get("freshwater") else ""}</div>')


def _open_until_badge(rec: dict) -> str:
    """'Open for comment until November 27, 2026' while the source's comment deadline is today or later.
    The page script hides it once the date has passed, between weekly updates."""
    d = rec.get("comment_deadline")
    if rec.get("stage_kind") != "open" or not d:
        return ""
    return (f'<span class="badge open-until" data-until="{escape(d)}">Open for comment until '
            f'{escape(winnability.long_date(d))}</span>\n')


def _stakeholder_rows(rec: dict) -> str:
    """Fact rows for the card: one per role, "Not named yet" when the source names no one."""
    rows = []
    for key, label in ROLES:
        entries = _role(rec, key)
        val = ("; ".join(escape(e["name"]) + (f' <span class="muted">({escape(e["detail"])})</span>' if e.get("detail") else "")
                         + (f' <a class="src" href="{escape(e["source"])}" rel="noopener">source</a>' if e.get("source") else "")
                         + (_find_contacts(e["name"]) if key != "agency_contact" else "")
                         for e in entries) if entries else f'<span class="not-named">{NOT_NAMED}</span>')
        rows.append(f"<dt>{escape(label)}</dt><dd>{val}</dd>")
    return "\n".join(rows)


def _find_contacts(org: str) -> str:
    """Link to LinkedIn's own people search for this organisation and the roles a supplier approaches. Nothing is stored."""
    name = re.sub(r"\s*\(.*$", "", org).strip()
    return (f' <a class="fc" href="{escape(rsrch.find_contacts_url(name))}" rel="noopener nofollow" target="_blank" '
            f'title="LinkedIn people search: environmental manager, project manager, procurement">Find contacts</a>')


def _checked_note(f: dict) -> str:
    return "" if f.get("checked") == "read" else ' <span class="muted">(found by web search, confirm)</span>'


def _research_html(rec: dict, full: bool = False) -> str:
    """'How to get in' (procurement routes, shown first) and other facts from official web sources."""
    r = rec.get("research") or {}
    facts = r.get("facts") or []
    if not facts and not (full and (r.get("orgs") or r.get("people"))):
        return ""
    proc = [f for f in facts if f.get("kind") == "procurement"]
    other = [f for f in facts if f.get("kind") != "procurement"]
    li = lambda f: (f'<li>{escape(f["text"])} <a href="{escape(f["source"])}" rel="noopener">source</a>{_checked_note(f)}</li>')
    out = []
    if proc:
        out.append(f'<div class="getin"><p class="getin-h">Supplier portals, tenders and events</p><ul>{"".join(li(f) for f in proc)}</ul></div>')
    shown = other if full else other[:3]
    if shown:
        out.append('<div class="devfacts"><p class="why-h">From the developer and other official sources</p><ul>' +
                   "".join(f'<li><strong>{escape(rsrch.FACT_KINDS.get(f["kind"], f["kind"]))}:</strong> {li(f)[4:]}'
                           for f in shown) + "</ul></div>")
    if full:
        orgs = []
        for name, o in (r.get("orgs") or {}).items():
            bits = [f'<a href="{escape(o["website"])}" rel="noopener">website</a>'] if o.get("website") else []
            bits += [f'{escape(e["address"])} ({escape(e.get("published_for") or "general")})' for e in o.get("emails") or []]
            bits += [escape(ph) for ph in o.get("phones") or []]
            if bits:
                orgs.append(f'<li><strong>{escape(name)}</strong>: {"; ".join(bits)} '
                            f'<a href="{escape(o["source"])}" rel="noopener">source</a></li>')
        people = [f'<li>{escape(pp["name"])}, {escape(pp["role"])} <a href="{escape(pp["source"])}" rel="noopener">source</a></li>'
                  for pp in r.get("people") or []]
        if orgs:
            out.append('<p class="why-h">Published contact details</p><ul class="orgs">' + "".join(orgs) + "</ul>")
        if people:
            out.append('<p class="why-h">People named by an official source</p><ul>' + "".join(people) + "</ul>")
    return '<div class="research">' + "".join(out) + "</div>" if out else ""


def _who_html(rec: dict) -> str:
    """Project page: every named person or organisation, with how we know (and the words, where quotes are allowed)."""
    quotes_ok = source_conf(rec.get("source")).get("quotes", True)
    items = []
    for key, label in ROLES:
        entries = (rec.get("stakeholders") or {}).get(key) or []
        if not entries:
            items.append(f'<li><span class="role">{escape(label)}</span><span class="not-named">{NOT_NAMED}</span></li>')
            continue
        for e in entries:
            q = f' &ldquo;{escape(e["quote"])}&rdquo;' if quotes_ok and e.get("quote") else ""
            items.append(f'<li><span class="role">{escape(label)}</span>{escape(e["name"])}'
                         f'{(" (" + escape(e["detail"]) + ")") if e.get("detail") else ""}'
                         f'{(" " + _find_contacts(e["name"]).strip()) if key != "agency_contact" else ""}'
                         f'<span class="how">{escape(e.get("how") or "")}{q}'
                         f'{(" " + chr(60) + "a href=" + chr(34) + escape(e["source"]) + chr(34) + " rel=" + chr(34) + "noopener" + chr(34) + ">source</a>") if e.get("source") else ""}</span></li>')
    return (f'<ul class="who">{"".join(items)}</ul>\n<p class="note">Only names an official source states, with where each came from. Contractors are '
            f'usually named only when a project goes to bid. "Find contacts" opens a LinkedIn people search for that '
            f'organisation; this site stores nothing from it.</p>')


def _win_html(rec: dict) -> str:
    """Entry point line and two or three plain reasons for the score."""
    w = rec.get("win") or {}
    if not w.get("entry_point"):
        return ""
    reasons = "".join(f"<li>{escape(x)}</li>" for x in w.get("reasons") or [])
    return (f'<div class="win"><p class="entry"><strong>Suggested approach:</strong> {escape(w["entry_point"])}</p>'
            + (f'<p class="why-h">Why this score</p><ul class="why">{reasons}</ul>' if reasons else "") + "</div>")


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
    official = "Official page" if rec["stage_kind"] == "watch" else "Developer's page" if rec.get("developer_only") else "Official notice"
    attribution = (" " + escape(rec["attribution"])) if rec.get("attribution") else ""
    return (f'<p class="source"><a href="{link}" rel="noopener">{official}</a> ({escape(rec["source_name"])}){attribution}</p>\n'
            f'{also}'
            f'<p class="attr">Source: {escape(rec.get("agency") or rec["source_name"])}'
            f'{(". Hearing by " + escape(rec["issued_by"])) if rec.get("issued_by") else ""}'
            f'{(". " + escape(rec["attribution"])) if rec.get("attribution") else ""}.</p>')


def _watch_button(rec: dict) -> str:
    return (f'<button type="button" class="watch-btn" data-id="{escape(rec["id"])}" aria-pressed="false" hidden>'
            f'Add to watch list</button>')


def why_line(rec: dict) -> str:
    """One line on why the project matters to a supplier: what the work is, plus its strongest habitat or timing signal."""
    w = rec.get("win") or {}
    reasons = [re.sub(r"\s*\([+-]?\d+\)$", "", x) for x in w.get("reasons") or []]
    what = reasons[0] if reasons else ", ".join(rec.get("types") or [])
    extra = next((x for x in reasons[1:] if re.search(r"offsetting|compensation|Fisheries Act|Consultant named|Large project|"
                                                   r"Construction expected", x)), "")
    extra = re.sub(r"\s*\((habitat work is likely needed|someone to approach)\)", "", extra)
    extra = re.sub(r":\s*(buying decisions soon|time to get in early)$", "", extra)
    line = what + (f"; {extra[0].lower() + extra[1:]}" if extra else "")
    return line[:1].upper() + line[1:] if line else ""


def best_way_in(rec: dict) -> str:
    """The single best way in, as HTML: a procurement route, else a comment deadline, else who to approach."""
    # Open routes only (a tender that has closed is no way in); project-specific routes before an owner's general one.
    proc = sorted((f for f in procurement_routes(rec) if not re.search(r"\b(closed|has closed)\b", f["text"])),
                  key=lambda f: bool(f.get("general")))
    if proc:
        f = proc[0]
        text = f.get("short") or re.split(r"(?<=\.)\s", f["text"])[0]
        return (f'{escape(text)} <a href="{escape(f["source"])}" rel="noopener">Source</a>'
                + ("" if f.get("checked") == "read" else ' <span class="muted">(found by web search, confirm)</span>'))
    ep = (rec.get("win") or {}).get("entry_point") or ""
    ep = re.sub(r"\. Habitat offsetting is mentioned: offer a habitat-positive design\.$", ".", ep)
    ep = re.sub(r"^[A-Z][\w ,-]*?:\s*", "", ep)          # drop the stage prefix: the stage is shown above
    return escape(ep[:1].upper() + ep[1:]) if ep else ""


def short_date(iso: str, today: date | None = None) -> str:
    """'Dec 3', with the year only when it is not this year ('Jan 8, 2027')."""
    d, today = date.fromisoformat(iso), today or date.today()
    return f"{d:%b} {d.day}" + (f", {d.year}" if d.year != today.year else "")


def _deadline_span(rec: dict) -> str:
    d = rec.get("comment_deadline")
    if rec.get("stage_kind") != "open" or not d:
        return ""
    return f'<span class="deadline">Comments close <span class="countdown">{escape(short_date(d))}</span></span>'


def _stage_text(rec: dict) -> str:
    ws = (rec.get("win") or {}).get("stage")
    return WIN_STAGE_TEXT.get(ws) or STAGE_LABELS.get(rec["stage_kind"], rec["stage"])


def _status_line(rec: dict) -> str:
    return (f'<p class="status"><span class="badge tier-badge t-{rec["tier"]}">{escape(rec["tier_label"])}</span>'
            f'<span class="badge stage-badge">{escape(_stage_text(rec))}</span>{_deadline_span(rec)}</p>')


def _fold(title: str, body: str) -> str:
    return f'<details class="more"><summary>{escape(title)}</summary><div>{body}</div></details>' if body.strip() else ""


def _card(rec: dict, country_name: str, less_tested: list, country_order: int = 0, site: dict | None = None,
          prefix: str = "") -> str:
    """Compact list card: name, place, fit and the comment deadline when one is open. The whole card opens the
    project page, which holds everything else (why it scored, who to contact, how to get in, sources)."""
    types = rec["types"] or []
    search = " ".join([rec["title"], rec["proponent"], rec["province"], country_name, rec["water_body"],
                       rec["summary"], " ".join(types)] +
                      [e["name"] for es in (rec.get("stakeholders") or {}).values() for e in es]).lower()
    deadline, _ = _deadline_text(rec)
    anchor = "p-" + page_slug(rec["id"])
    page = escape(rec.get("page") or "")
    title_link = (prefix + page) if page else (escape(rec["url"]) if rec["url"] else "#")
    w = rec.get("win") or {}
    if rec.get("profile_only"):
        rec = {**rec, "tier": "none", "tier_label": NOT_GENERAL}
    return f"""<article class="card linked" id="{anchor}" data-id="{escape(rec['id'])}" data-tier="{rec['tier']}" data-gtier="{rec['tier']}" data-fresh="{'1' if rec.get('freshwater') else ''}" data-source="{escape(rec.get('source') or '')}" data-country="{escape(rec.get('country') or UNKNOWN)}"
 data-country-name="{escape(country_name)}" data-country-order="{country_order}" data-region="{escape(rec['province'])}"
 data-types="{escape('|'.join(types))}" data-stage="{rec['stage_kind']}" data-wstage="{escape(w.get('stage') or '')}" data-deadline="{escape(deadline or '')}"
 data-score="{int(rec['score'])}" data-first="{escape(rec.get('first_listed') or '')}"
 data-changes="{escape('|'.join(rec.get('changes') or []))}" data-title="{escape(rec['title'].lower())}" data-search="{escape(search)}">
<h4><a href="{title_link}">{_plain(escape(rec['title']))}</a></h4>
<p class="where">{escape(_place(rec, country_name))}{(" &middot; " + escape(DEVELOPER_ONLY)) if rec.get("developer_only") else ""}{" &middot; Freshwater" if rec.get("freshwater") else ""}</p>
<p class="status"><span class="badge tier-badge t-{rec['tier']}">{escape(rec['tier_label'])}</span>{_deadline_span(rec)}</p>
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


def closing_soon(records: list[dict], days: int, today: date, key: str | None = None,
                 exclude_freshwater: bool = False) -> list[dict]:
    """Projects whose comment period closes within `days` (today included), soonest first. key: a buyer profile,
    or None for the general list."""
    out = []
    for r in records:
        d = r.get("comment_deadline")
        if r.get("stage_kind") != "open" or not d or r.get("tier") == "watch":
            continue
        left = (date.fromisoformat(d) - today).days
        if not 0 <= left <= days:
            continue
        if key is None and (r.get("profile_only") or r.get("tier") not in PUBLISHED_TIERS):
            continue
        if key is not None and not listed_in_profile(r, key):
            continue
        if exclude_freshwater and r.get("freshwater"):
            continue
        out.append(r)
    return sorted(out, key=lambda r: (r["comment_deadline"], r["title"]))


def _soon_li(rec: dict, country_name: str, today: date) -> str:
    """One row of the closing-soon list (the page script rebuilds it for the chosen profile and filters)."""
    left = (date.fromisoformat(rec["comment_deadline"]) - today).days
    txt = "Today" if left == 0 else short_date(rec["comment_deadline"], today)
    return (f'<li><time class="d" datetime="{escape(rec["comment_deadline"])}">{txt}</time>'
            f'<a href="{escape(rec.get("page") or rec["url"])}">{_plain(escape(rec["title"]))}</a>'
            f'<span class="meta"><span class="t-{rec["tier"]} badge">{escape(rec["tier_label"])}</span>'
            f'<span>{escape(country_name)}</span></span></li>')


def render_index(records: list[dict], site: dict, tiers: dict, updated: date, coverage: dict | None = None,
                 history_dates: list[str] | None = None, news_html: str = "", scoring: dict | None = None) -> str:
    coverage = coverage if coverage is not None else load_coverage()
    all_records = records
    meta = profile_meta(all_records, scoring)
    # The general list ("All habitat work") never shows projects that only a buyer profile lists.
    records = [r for r in all_records if not r.get("profile_only")]
    title = site.get("title", "MitiMark")
    desc = site.get("description", "")
    soon_days = int(site.get("closing_soon_days", 14))
    td = tier_descriptions(tiers)
    less_tested = coverage.get("less_tested_languages", [])
    soon_recs = closing_soon(records, soon_days, updated)
    n_soon = len(soon_recs)
    countries = _countries_in_order(records, coverage)
    names = {c["code"]: c["name"] for c in coverage.get("countries", [])}
    names.update({c["code"]: c["name"] for c in countries})
    soon_items = "".join(_soon_li(r, names.get(r.get("country"), r.get("country") or UNKNOWN), updated) for r in soon_recs)

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
    profile_bar = f"""<section class="blue picker js-only" aria-label="Choose your kind of work" hidden><div class="wrap">
<div class="pick"><p class="step">Step 1</p>
<label for="f-profile">I work on</label>
<select id="f-profile"><option value="{ALL}">{ALL_LABEL}</option>{prof_opts}</select></div>
<div class="pick-about"><p id="profile-desc" class="profile-desc"></p>
<p id="profile-empty" class="profile-empty" hidden>{escape(EMPTY_NOTE)}</p></div>
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
    since_html = since_panel(records, history_dates or [])
    panels = [("soon", "Comment deadlines", n_soon), ("changes", "What changed", None) if since_html else None,
              ("coverage", "Coverage", None), ("how-to-read", "How to read the list", None),
              ("news", "News", None) if news_html else None]
    chips = "".join(f'<button type="button" class="chip" aria-expanded="false" aria-controls="{k}">{label}'
                    f'{f" <span class={chr(34)}n{chr(34)} id={chr(34)}chip-soon{chr(34)}>{n}</span>" if n is not None else ""}</button>'
                    for k, label, n in filter(None, panels))
    return f"""<!doctype html>
<html lang="en">
<head>
{CARD_ANCHOR_JS}
{_head("Projects - " + title, desc)}
</head>
<body data-soon="{soon_days}">
{_topbar(site, updated, "projects")}
<main id="main">
{profile_bar}
<section class="band-tint results" id="results" aria-labelledby="results-h"><div class="wrap">
<div class="results-bar">
<div><p class="step js-only" hidden>Step 2</p><h2 id="results-h">Projects</h2><p id="result-count" aria-live="polite">{len(records)} projects listed.</p>
<p class="result-actions"><button type="button" class="linklike" id="show-weak" hidden>Also show weak fits</button>
<button type="button" class="linklike" id="show-fresh" hidden>Include freshwater</button></p>
<div class="active-filters" id="active-filters" aria-label="Filters in use" hidden></div></div>
</div>
<div class="toolbar js-only" hidden>
<button type="button" class="btn primary small" id="filters-btn" aria-haspopup="dialog" aria-controls="filters">Filters
<span class="count-dot" id="filter-count" hidden>0</span></button>
{chips}
</div>

<section class="info-panel soon" id="soon" aria-labelledby="soon-h" hidden>
<h3 id="soon-h">Public comment deadlines coming up</h3>
<p class="note">Projects where the public can still send comments to the regulator. Companies can also use the
deadline as a reason to get in touch.</p>
<p class="summary-line" id="summary-line">{n_soon} deadlines in the next {soon_days} days.</p>
<ul id="soon-list">{soon_items}</ul>
<p id="soon-none"{' hidden' if n_soon else ''}>No public comment deadlines in the next {soon_days} days.</p>
<p><a href="?open=1#results">See every project open for public comment</a></p>
</section>
{f'<section class="info-panel since" id="changes" hidden>{since_html}</section>' if since_html else ''}
<section class="info-panel coverage" id="coverage" hidden>
<h3>Coverage: {cc['covered']} covered, {cc['partial']} partial, {cc['coming_soon']} coming soon</h3>
{f'<p>{escape(bn)}</p>' if bn else ''}
<div class="cov-wrap"><table class="cov"><thead><tr><th>Place</th><th>Status</th><th>Details</th></tr></thead>
<tbody>
{coverage_rows(coverage, counts)}
</tbody></table></div>
{f'<p class="note">{escape(lt)}</p>' if lt else ''}
</section>
<section class="info-panel explain" id="how-to-read" hidden>
<h3>How to read this list</h3>
<p>Every notice is scored automatically from the words and sizes in it, then placed in a tier:</p>
<ul>
<li><strong>{TIER_LABELS['strong']}:</strong> {escape(td['strong'])}</li>
<li><strong>{TIER_LABELS['possible']}:</strong> {escape(td['possible'])}</li>
<li><strong>{TIER_LABELS['weak']}:</strong> {escape(td['weak'])} Hidden unless you tick "Also show weak fits" under Filters.</li>
</ul>
<p>Notices scoring under {int(tiers.get('weak_min', 20))}, quarries and pits, and withdrawn projects are not listed.</p>
<p>A flag means a notice may involve marine habitat work. It is not a statement that mitigation or
compensation is required. Profiles are rule-based guesses, not a statement that mitigation is required.
Always read the official notice.
<a href="about.html#buyer-profiles">How profiles work</a></p>
</section>
{f'<section class="info-panel news-panel" id="news" hidden>{news_html}</section>' if news_html else ''}
<noscript><p class="note">Turn on JavaScript for filters, search and the profile picker. Without it, every listed project is shown below.</p></noscript>

<p class="empty" id="no-match" hidden>No projects match these filters.</p>
<div id="view" hidden></div>
<div id="server-list">
{body}
</div>
<div id="profile-only" hidden>
{extra_cards}
</div>
<p class="center js-only" hidden><button type="button" class="btn" id="show-more" hidden>Show more</button></p>
</div></section>
</main>

<div class="scrim" id="scrim" hidden></div>
<aside class="sheet" id="filters" role="dialog" aria-modal="true" aria-labelledby="filters-h" hidden>
<div class="sheet-head"><h2 id="filters-h">Filters</h2><button type="button" class="sheet-close" id="filters-close" aria-label="Close filters">&times;</button></div>
<div class="sheet-body">
<fieldset class="views"><legend>Show</legend>
<label class="check"><input type="radio" name="view" id="v-all" value="all" checked> All projects</label>
<label class="check"><input type="radio" name="view" id="v-soon" value="soon"> Comment deadline soon</label>
<label class="check"><input type="radio" name="view" id="v-changed" value="changed"> Changed since last update</label>
<label class="check"><input type="radio" name="view" id="v-watch" value="watch"> My watch list</label>
</fieldset>
<form class="filters sep" onsubmit="return false" aria-label="Filter projects">
<label>Search<input type="search" id="f-search" placeholder="Name, place, proponent"></label>
<label>Country<select id="f-country">{''.join(opts)}</select></label>
<label id="f-region-label" hidden><span id="f-region-text">Province or state</span><select id="f-region"><option value="">All</option></select></label>
<label>Project type<select id="f-type"><option value="">All types</option></select></label>
<label>Stage<select id="f-stage"><option value="">All stages</option>
<option value="open">Open for public comment</option><option value="closed">Comments closed, decision pending</option>
<option value="assessment">Under review</option><option value="decided">Approved</option>
<option value="withdrawn">Withdrawn or rejected</option><option value="unknown">Stage not stated</option></select></label>
<label>Sort by<select id="f-sort"><option value="default">Best fit first</option>
<option value="deadline">Public comment deadline, soonest first</option><option value="score">Score, highest first</option>
<option value="newest">Newest listed first</option><option value="title">Name, A to Z</option></select></label>
<label>Group by<select id="f-group"><option value="country">Country</option>
<option value="type">Project type</option><option value="stage">Stage</option>
<option value="month">Month of the comment deadline</option><option value="none">No grouping</option></select></label>
<label class="check"><input type="checkbox" id="f-open"> Only projects open for public comment</label>
<label class="check"><input type="checkbox" id="t-weak"> Also show weak fits</label>
<label class="check" id="t-fresh-label" hidden><input type="checkbox" id="t-fresh"> Include freshwater (lakes and rivers)</label>
</form>
<div class="share sep"><p class="side-label">Share this view</p>
<p class="note">Your profile choice is remembered only in this browser. <a id="profile-link" href="?">Shareable link</a></p>
<button type="button" class="btn small" id="profile-copy">Copy link</button></div>
<div class="downloads sep"><p class="side-label">Download</p>
<button type="button" id="csv-btn" class="btn small">Shown projects (CSV)</button>
<a href="projects.csv">All listed projects (CSV)</a></div>
</div>
<div class="sheet-foot"><button type="button" id="reset-btn" class="btn quiet">Reset all</button>
<button type="button" class="btn primary" id="filters-done">Show projects</button></div>
</aside>
{_footer(site)}
<script type="application/json" id="csv-data">{csv_data}</script>
<script type="application/json" id="tier-names">{tier_names}</script>
<script type="application/json" id="profile-data">{prof_json}</script>
<script>{JS_COMMON}{JS}</script>
</body>
</html>
"""


# Old links pointed at index.html for the list (?profile=..., #p-..., #soon). Home sends those on to projects.html.
# A link to one card (#p-<slug>) goes straight to that project's page, which always exists.
CARD_ANCHOR_JS = ("<script>(function(){var m=/^#p-([A-Za-z0-9_-]+)$/.exec(location.hash);"
                  "if(m)location.replace('p/'+m[1]+'.html');})();</script>")
OLD_LIST_LINKS = ("<script>(function(){var s=location.search,h=location.hash,m=/^#p-([A-Za-z0-9_-]+)$/.exec(h);"
                  "if(m)location.replace('p/'+m[1]+'.html');"
                  "else if(/[?&](profile|view|open|show|country|sort|group)=/.test(s)||/^#(results|soon|coverage|tools)/.test(h))"
                  "location.replace('projects.html'+s+h);})();</script>")

WAVE = ('<svg class="wave" viewBox="0 0 1440 80" preserveAspectRatio="none" aria-hidden="true" focusable="false">'
        '<path class="back" d="M0 38C240 8 480 68 720 38S1200 8 1440 38V80H0Z"/>'
        '<path d="M0 56C300 28 520 80 760 54S1180 26 1440 50V80H0Z"/></svg>')

STEP_ICONS = {
    "scan": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 3h7l4 4v6"/><path d="M14 3v4h4"/><path d="M7 3a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h4"/><circle cx="16.5" cy="16.5" r="3.5"/><path d="M19 19l2.5 2.5"/></svg>',
    "score": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M22 20H2"/></svg>',
    "list": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6h11"/><path d="M9 12h11"/><path d="M9 18h7"/><path d="M4 6l1 1 2-2"/><path d="M4 12l1 1 2-2"/><path d="M4 18l1 1 2-2"/></svg>',
}

# Who the site is for, each linked to the profile that fits that work.
AUDIENCES = [("hard-structure", "Reef builders",
              "Breakwaters, seawalls and wharves where reef units or eco-engineered blocks could replace plain rock or concrete."),
             ("living-shoreline", "Living shoreline firms",
              "Eroding shores, salt marsh, oyster and mussel work where a living shoreline could do the job."),
             ("banking", "Mitigation banks",
              "Projects whose notices mention offsetting, compensation or habitat credits.")]


def render_home(records: list[dict], site: dict, tiers: dict, updated: date, coverage: dict | None = None,
                scoring: dict | None = None) -> str:
    """index.html: a short front page. Hero with three numbers, how it works, three top picks, who it is for."""
    coverage = coverage if coverage is not None else load_coverage()
    title = site.get("title", "MitiMark")
    meta = {m["key"]: m for m in profile_meta(records, scoring)}
    general = [r for r in records if not r.get("profile_only")]
    soon_days = int(site.get("closing_soon_days", 14))
    n_soon = len(closing_soon(general, soon_days, updated))
    n_strong = sum(1 for r in general if r["tier"] == "strong")
    names = {c["code"]: c["name"] for c in coverage.get("countries", [])}
    picks = sorted((r for r in general if r["tier"] in ("strong", "possible")), key=_sort_key)[:3]
    cards = "\n".join(_card(r, names.get(r.get("country"), r.get("country") or UNKNOWN), []) for r in picks)
    n_countries = len({r.get("country") for r in general if r.get("country")})
    who = "".join(f'<li><a href="projects.html?profile={k}#results"><h3>{escape(name)}</h3><p>{escape(line)}</p>'
                  f'<span class="go">See {meta[k]["count"]} project{"" if meta[k]["count"] == 1 else "s"}</span></a></li>'
                  for k, name, line in AUDIENCES if k in meta)
    who_band = f"""<section class="band band-white curve-top" aria-labelledby="who-h"><div class="wrap">
<h2 class="band-h" id="who-h">Who this is for</h2>
<p class="band-sub">Pick your kind of work and the list re-sorts for it.</p>
<ul class="who-for">{who}</ul>
</div></section>""" if who else ""
    return f"""<!doctype html>
<html lang="en">
<head>
{OLD_LIST_LINKS}
{_head(title, site.get("description", ""))}
</head>
<body data-soon="{soon_days}">
{_topbar(site, updated, "home")}
<main id="main">
<section class="blue hero"><div class="wrap">
<h1>Find coastal projects that need marine mitigation, before they&rsquo;re awarded.</h1>
<p class="lede">For reef builders, living shoreline firms, mitigation banks and other companies that do marine mitigation work.</p>
<p class="ctas"><a class="btn white" href="projects.html">See projects</a><a class="btn outline-white" href="#how">How it works</a></p>
<ul class="stats">
<li><a href="projects.html?profile=all#results"><span class="n">{len(general)}</span><span class="l">Projects tracked</span></a></li>
<li><a href="projects.html?profile=all#results"><span class="n">{n_strong}</span><span class="l">Strong fits</span></a></li>
<li><a href="projects.html?profile=all#soon"><span class="n">{n_soon}</span><span class="l">Deadlines this fortnight</span></a></li>
</ul>
</div>{WAVE}</section>

<section class="band band-white curve-bottom" id="how" aria-labelledby="how-h"><div class="wrap">
<h2 class="band-h" id="how-h">How it works</h2>
<p class="band-sub">Updated {escape(site.get("update_frequency", "every week"))}. Last update <time>{_date(updated)}</time>.</p>
<ol class="steps">
<li><span class="icon">{STEP_ICONS["scan"]}</span><h3>We scan public notices</h3>
<p>Official permit and environmental assessment notices from {n_countries} countries, read every week.</p></li>
<li><span class="icon">{STEP_ICONS["score"]}</span><h3>We score each project</h3>
<p>Clear rules on the type of work, habitat words in the notice and its size give each project a fit.</p></li>
<li><span class="icon">{STEP_ICONS["list"]}</span><h3>You get a weekly list</h3>
<p>Strong and possible fits, with who to contact, how to get in and the deadline to beat.</p></li>
</ol>
</div></section>

<section class="band band-tint" aria-labelledby="picks-h"><div class="wrap">
<h2 class="band-h" id="picks-h">Top picks this week</h2>
<p class="band-sub">The three best fits across all habitat work right now.</p>
<div class="cards">{cards}</div>
<p class="center"><a class="btn primary" href="projects.html">See all projects</a></p>
</div></section>
{who_band}
</main>
{_footer(site)}
<script>{JS_COMMON}
Array.prototype.forEach.call(document.querySelectorAll('.card'), MPF.countdown);
</script>
</body>
</html>
"""



CSV_COLUMNS = ["id", "title", "country", "region", "proponent", "project_types", "stage", "tier", "score",
               "reviewed_by_a_person", "comments_open", "comments_close", "comments_received", "comments_received_as_of",
               "changes_since_last_update", "first_listed", "project_page", "official_notice", "source",
               "open_for_comment_until", "owner_or_proponent", "consultant", "contractor", "agency_contact",
               "entry_point", "score_reasons", "procurement_routes"]


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
            rec.get("agency") or rec["source_name"],
            c.get("closes") if rec.get("stage_kind") == "open" and c.get("closes") else ""] + \
        [summary_line(_role(rec, k)) for k, _ in ROLES] + \
        [(rec.get("win") or {}).get("entry_point", ""), "; ".join((rec.get("win") or {}).get("reasons") or []),
         " | ".join(f'{f["text"]} ({f["source"]})' for f in procurement_routes(rec))] + \
        [_profile_cell(rec, m["key"]) for m in meta or []]


def _role(rec: dict, key: str) -> list[dict]:
    entries = (rec.get("stakeholders") or {}).get(key) or []
    if key == "owner" and not entries and rec.get("proponent") not in (None, "", UNKNOWN):
        entries = [{"name": rec["proponent"], "detail": ""}]
    return entries


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
    return (f'<h3>What changed since the last update ({escape(dates[-2])})</h3>\n'
            f'<p>{escape(", ".join(parts))}. <a href="?view=changed&amp;show=weak">Show the changed projects</a>.</p>')


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
            f'notices, have not been checked, and are never ranked as a strong or possible fit. Only the headline, '
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
        items.append(f'<li><a href="../projects.html?profile={escape(m["key"])}#results">{escape(m["label"])}</a> '
                     f'<span class="badge tier-badge t-{p["tier"]}">{escape(p["tier_label"])}</span> '
                     f'<span class="note">score {int(p["score"])}</span>'
                     f'<p class="because">Matches because: {escape(p["because"])}</p></li>')
    body = (f'<ul class="profile-list">{"".join(items)}</ul>' if items
            else '<p class="note">No buyer profile matches this project.</p>')
    return (f'<section class="side-card">\n<h2 class="side-h">Buyer profiles</h2>\n{body}\n'
            f'<p class="note">Rule-based guesses, not a statement that mitigation is required.</p>\n</section>')


def approach_first(rec: dict) -> str:
    """Who to approach first, in plain words, from the project's entry point: the owner, the engineering consultant
    or the contractor. Empty when there is no entry point (project watch)."""
    ep = (rec.get("win") or {}).get("entry_point") or ""
    name = lambda key: (", " + _role(rec, key)[0]["name"]) if _role(rec, key) else ""
    if not ep:
        return ""
    if "contact the contractor" in ep:
        return "the contractor" + name("contractor")
    if "engineering firm (" in ep:
        return "the engineering consultant" + name("consultant")
    if "late; watch" in ep:
        return "nobody yet: construction has started, so watch for later phases or repairs"
    return "the owner" + name("owner")


def _comments_side(rec: dict, deadline_text: str) -> str:
    """Public comments box, only when the registry publishes something: a date or a count."""
    c = rec.get("comments") or {}
    if rec["stage_kind"] == "watch" or not (c.get("opens") or rec.get("comment_deadline") or c.get("count") is not None):
        return ""
    rows = ""
    if c.get("opens"):
        rows += f"<dt>Opens</dt><dd>{_opens_text(rec)}</dd>"
    if rec.get("comment_deadline"):
        rows += f'<dt>Closes</dt><dd><span class="countdown">{escape(deadline_text)}</span></dd>'
    if c.get("count") is not None:
        rows += f"<dt>Comments received</dt><dd>{_comment_count_text(rec)}</dd>"
    note = f'<p class="note">{escape(c["count_note"])}</p>' if c.get("count_note") else ""
    return (f'<section class="side-card">\n<h2 class="side-h">Public comments</h2>\n<dl class="facts">{rows}</dl>\n{note}'
            '<p class="note">Only dates and counts the official registry publishes are shown. Comment text and the names of '
            'people who commented are not copied here. To comment, use the official notice.</p>\n</section>')


def render_project_page(rec: dict, site: dict, updated: date, country_name: str, less_tested: list,
                        meta: list[dict] | None = None) -> str:
    title = site.get("title", "MitiMark")
    if rec.get("profile_only"):
        rec = {**rec, "tier": "none", "tier_label": NOT_GENERAL}
    types = rec["types"] or []
    deadline, deadline_text = _deadline_text(rec)
    link = escape(rec["url"]) if rec["url"] else "#"
    quotes = "".join(f"<li>&ldquo;{escape(q)}&rdquo;</li>" for q in rec["quotes"])
    official = "Official page" if rec["stage_kind"] == "watch" else "Official notice"
    score = int(rec["score"])
    w = rec.get("win") or {}
    reasons = "".join(f"<li>{escape(x)}</li>" for x in w.get("reasons") or [])
    review = ("Checked by a person against the official notice." if rec["reviewed"]
              else "Listed automatically; not yet checked by a person.")
    changed = ((" Changed since the last update: " + escape(", ".join(CHANGE_LABELS[c] for c in rec.get("changes") or [])) + ".")
               if rec.get("changes") else "")
    notes = []
    if rec.get("freshwater"):
        notes.append("On a lake or river (freshwater).")
    if rec.get("language") in less_tested:
        notes.append(f"Notice in a less tested language ({escape(LANG_NAMES.get(rec['language'], rec['language']))}).")
    reason_note = '' if rec['tier_reason'].lower().startswith('score') else f' {escape(rec["tier_reason"])}'
    why = (f'{_summary_html(rec)}'
           f'<p><strong>Stage:</strong> {escape(_stage_text(rec))}.</p>'
           f'<div class="scorebox"><span class="big">{score}</span><span class="of">/ 100 &middot; {escape(rec["tier_label"])}</span></div>'
           f'<div class="meter" style="--s:{score}"><span></span></div><p class="note">{reason_note.strip()}</p>'
           + (f'<h3>Why it scored {score} out of 100</h3><ul class="why">{reasons}</ul>' if reasons else "")
           + _profiles_card(rec, meta).replace('<section class="side-card">', '<div class="profiles">', 1)
                 .replace('</section>', '</div>').replace('<h2 class="side-h">Buyer profiles</h2>', '<h3>Buyer profiles that list it</h3>')
           + (f'<h3>Words from the notice</h3><ul class="quotes">{quotes}</ul>' if quotes else "")
           + "".join(f'<p class="note">{n}</p>' for n in notes)
           + f'<p class="note">{review}{changed}</p>')
    who = approach_first(rec)
    getin = ((f'<p class="approach"><strong>Approach first:</strong> {escape(who)}.</p>' if who else "")
             + (f'<p><strong>Suggested approach:</strong> {escape(w["entry_point"])}</p>' if w.get("entry_point") else "")
             + _research_html(rec, full=True))
    if not getin.strip():
        getin = "<p>Read the official notice: it names who to contact.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(rec['title'] + ' - ' + title, rec.get('summary') or site.get('description', ''), '../')}
</head>
<body data-soon="{int(site.get('closing_soon_days', 30))}">
{_topbar(site, updated, "", "../")}
<main class="project" id="main" data-id="{escape(rec['id'])}" data-tier="{rec['tier']}" data-stage="{rec['stage_kind']}" data-deadline="{escape(deadline or '')}">
<section class="blue page-band"><div class="wrap">
<nav class="crumbs" aria-label="Breadcrumb"><a href="../projects.html">Projects</a><span aria-hidden="true">/</span>
<a href="../projects.html?country={escape(rec.get('country') or '')}#results">{escape(country_name)}</a><span aria-hidden="true">/</span>
<span>{escape(rec['title'])}</span></nav>
<h1>{_plain(escape(rec['title']))}</h1>
<p class="place">{escape(_place(rec, country_name))}{(" &middot; " + escape(_role(rec, "owner")[0]["name"])) if _role(rec, "owner") else ""}</p>
<p class="status"><span class="badge tier-badge t-{rec['tier']}">{escape(rec['tier_label'])}</span>{_deadline_span(rec)}</p>
</div></section>
<div class="band-tint project-body"><div class="wrap">
<article class="project" data-tier="{rec['tier']}">
<div class="project-main">
<details class="fold" open><summary>Why it matters</summary><div class="fold-body">{why}</div></details>
<details class="fold"><summary>Who to contact</summary><div class="fold-body">{_who_html(rec)}</div></details>
<details class="fold"><summary>How to get in</summary><div class="fold-body">{getin}</div></details>
{_in_the_news_html(rec).replace('<h2>', '<section class="side-card"><h2>', 1) + '</section>' if _in_the_news_html(rec) else ''}
</div>
<aside class="project-side">
<section class="side-card">
<div class="actions"><a class="btn primary" href="{link}" rel="noopener">{official} &#8599;</a>{_watch_button(rec)}{_feedback_link(rec, site)}</div>
</section>
{_comments_side(rec, deadline_text)}
<section class="side-card">
<h2 class="side-h">Key facts</h2>
<dl class="facts">
<dt>Owner or proponent</dt><dd>{escape(rec['proponent'])}</dd>
<dt>Location</dt><dd>{escape(_place(rec, country_name))}. Water body: {escape(rec['water_body'])}</dd>
<dt>Project type</dt><dd>{escape(', '.join(types) if types else UNKNOWN)}</dd>{_licences_row(rec)}
<dt>Stage in the notice</dt><dd>{_plain(escape(rec['stage']))}</dd>
</dl>
</section>
<section class="side-card">
<h2 class="side-h">Timeline</h2>
{_timeline_html(rec)}
<p class="note">Dates marked "official notice" come from the registry. Other entries are what this site saw
at each update.</p>
</section>
<section class="side-card">
<h2 class="side-h">Source</h2>
{_source_lines(rec, link)}
</section>
</aside>
</article>
</div></div>
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


PRODUCT_STAGES = {"open": "Under review, open for public comment", "closed": "Under review, decision pending",
                  "assessment": "Under review", "decided": "Approved, may not be built yet", "withdrawn": "Withdrawn or rejected",
                  "unknown": "Stage not stated", "watch": "Project page we follow"}


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
    review_badge = ("Checked by a person against the official notice." if rec["reviewed"]
                    else "Listed automatically; not yet checked by a person.")
    lang = (f' Notice in a less tested language ({escape(LANG_NAMES.get(rec["language"], rec["language"]))}).'
            if rec.get("language") in less_tested else "")
    stage = PRODUCT_STAGES.get(rec["stage_kind"], STAGE_LABELS.get(rec["stage_kind"], ""))
    general = NOT_GENERAL.lower() if rec.get("profile_only") else rec["tier_label"]
    when = rec.get("approved_on")
    approved = (f'<span class="badge approved-on"><time datetime="{escape(when)}">{escape(approved_label(when))}</time></span>'
                if when else "")
    return f"""<li class="card" id="s-{escape(page_slug(rec['id']))}" data-tier="{p['tier']}" data-stage="{rec['stage_kind']}" data-deadline="{escape(deadline or '')}">
<p class="status"><span>{rank}.</span><span class="badge tier-badge t-{p['tier']}">{escape(p['tier_label'])}</span>
<span class="badge">{escape(stage)}</span>{approved}</p>
<p class="note">Profile score {int(p['score'])} out of 100. {review_badge}{lang}</p>
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
<section class="blue page-head"><div class="wrap">
<p class="eyebrow">Shortlist &middot; {escape(m['label'])}</p>
<h1>{len(picks)} projects to look at</h1>
<p>Projects that may suit eco-engineered marine products, chosen by the rules on the
<a href="about.html#buyer-profiles">About page</a>. Strong fits come first, then possible, then weak. Within each
group, approved projects come first: they have passed environmental review but construction has not been reported, so
design and purchasing may still be ahead. Then projects still under assessment. Freshwater projects are left out.</p>
<p class="meta">Updated <time>{_date(updated)}</time>. These are rule-based guesses, not a statement that mitigation
or any product is required. Always read the official notice.</p>
</div></section>
<main class="wrap shortlist-main" id="main">
<div class="shortlist-intro">{ask}</div>
<ol class="cards shortlist">
{items}
</ol>
<p class="center"><a class="btn" href="projects.html?profile={escape(key)}#results">See every {escape(m['label'].lower())} project</a></p>
</main>
{_footer(site)}
<script>{JS_COMMON}
Array.prototype.forEach.call(document.querySelectorAll('.shortlist .card'), MPF.countdown);
</script>
</body>
</html>
"""


def _feeds_section(meta: list[dict] | None) -> str:
    links = [f'<li><a href="feeds/{COMMENT_FEED}.xml">Comment periods closing in the next 14 days (every profile)</a></li>',
             '<li><a href="feeds/all.xml">All projects on the general list</a></li>']
    links += [f'<li><a href="feeds/{escape(m["key"])}.xml">{escape(m["label"])}</a></li>' for m in meta or []]
    return ('<h2 id="feeds">RSS feeds</h2>\n<p>Each feed lists the comment periods closing in the next 14 days, '
            'then the projects that are new since the last update, with a link to the official notice and the source. Add a feed address to any feed reader. There is no '
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
<section class="blue page-head"><div class="wrap">
{LOGO}
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
listed at all. Only strong and possible fits are shown at first; weak fits can be switched on with
the "Also show weak fits" box. A high score means the notice is worth a closer look. It does not mean
that mitigation or compensation is required. Only the regulator decides that.</p>
<p>Where a source's terms allow it, each listing shows short quotes: complete sentences copied exactly from the
notice, chosen because they describe the project and its habitat signals, so you can judge for yourself. Where
they do not (at the moment, Scotland's marine licensing site), the listing shows short facts, the words that
matched and a link to the official notice instead. US Federal Register notices also show no quotes: they are
mostly about marine mammals and noise, so their cards show only what the notice says about the construction,
and a link.</p>

<h3 id="winnability">How winnable a project is</h3>
<p>The score has two parts. The first (above) asks whether the notice describes the right kind of work. The second
adds or takes away points for what decides whether a supplier can still get in: the stage (pre-application, open
for comment and under review score higher than approved or under construction), whether habitat offsetting,
compensation or a Fisheries Act authorization is mentioned, whether an engineering or environmental consultant is
named, when construction is due to start, and the size of the work. This second part changes a project's tier but never
adds a project to the list or takes one off it. Every project shows a "Best way in" line (who to approach, and when)
and two or three plain reasons for its score.</p>

<h3 id="who-is-involved">Who is involved</h3>
<p>Each project lists its owner or proponent, its engineering or environmental consultant, its contractor and the
agency contact, but only when the official source names them, with a note of where the name came from. Nothing is
guessed. A role the source does not name shows "Not named yet": contractors, for example, are usually named only when a
project goes to bid. For agency contacts only the name, job title and office are shown; phone numbers and email
addresses are on the official notice. "Download shown projects (CSV)" includes these names, for building a target
list.</p>
<p>For Strong and Possible projects, names and facts are also taken from officially published web pages: the developer's
project site, press releases, procurement notices and environmental reports. Each is linked to its source; facts found
only through a web search, on sites that do not allow automated reading, say so. Procurement routes (supplier portals,
tenders, meet-the-buyer events) appear first, under "How to get in". "Find contacts" opens a LinkedIn people search for
the organisation (environmental manager, project manager, procurement); this site stores nothing from it. Projects
marked "From the developer's website, not in a permit notice yet" come from a developer's own pages and are scored
with the same rules.</p>
<p>Offshore wind counts under the hard-structure profile: turbine foundations need scour protection and cables need cable
protection. Fixed-bottom foundations score higher than floating ones. Approved projects can be at most Possible, and
projects under construction at most Weak.</p>

<h2 id="automatic-listings-and-review">Automatic listings and review</h2>
<p>Every scored notice is published automatically. Under "Sources", each listing says either "Listed automatically; not yet
checked by a person" or, once a person has checked it against the official notice, "Checked by a person". Listings found to be
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
<p>While a comment period is open, the project carries an "Open for comment until" label with the closing date. The
label disappears once that date has passed. "Public comment deadlines coming up" at the top of the project
list, the "Only projects open for public comment" filter and the RSS feeds all use the same dates.</p>
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
it is still shown as a lead because design and purchasing may still be ahead, but it scores lower than a project that
is still under review (see <a href="#winnability">How winnable a project is</a>). The site marks a project as under
construction only when the notice says so or its works window has started; it cannot tell whether a tender has been
issued, so treat "Approved, may not be built yet" as a prompt to check.</p>
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

<h2 id="project-watch">Project pages we follow</h2>
<p>Some sources list projects rather than notices. When such a source is used, its items appear in a
separate "Project page we follow (not a notice)" section, with no comment deadline, no countdown, and never in
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
    (out_dir / "index.html").write_text(render_home(records, site_conf, tiers, updated, coverage, scoring), encoding="utf-8")
    (out_dir / "projects.html").write_text(render_index(records, site_conf, tiers, updated, coverage, h["snapshots"],
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
    for f in ASSETS.iterdir():          # logo.svg, favicon.svg/.ico, PNG icons
        if f.is_file() and not f.name.startswith("."):
            (out_dir / f.name).write_bytes(f.read_bytes())
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    scrub_dashes(out_dir)


def scrub_dashes(out_dir: Path) -> int:
    """No em dashes anywhere on the site (owner, 2026-11-19). Titles and quotes from notices that use one get a
    plain hyphen instead. Returns how many were replaced."""
    n = 0
    for path in list(out_dir.glob("*.html")) + list((out_dir / "p").glob("*.html")) + list((out_dir / "feeds").glob("*.xml")) \
            + [out_dir / "items.json", out_dir / "projects.csv"]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8-sig" if path.suffix == ".csv" else "utf-8")
        if "\u2014" in text:
            n += text.count("\u2014")
            text = re.sub(r"\s*\u2014\s*", " - ", text)
            path.write_text(text, encoding="utf-8-sig" if path.suffix == ".csv" else "utf-8")
    return n   # tells GitHub Pages to serve files as-is


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


def render_feed(items: list[dict], title: str, description: str, site_url: str, feed_path: str, updated: date,
                closing: list[dict] | None = None) -> str:
    """items: new projects. closing: projects whose comment period closes soon, listed first as their own entries."""
    from xml.sax.saxutils import escape as x, quoteattr as qa
    base = site_url.rstrip("/") + "/"
    entries = []
    for r in closing or []:
        link = r["url"] if re.match(r"https?://", r.get("url") or "") else ""
        when = winnability.long_date(r["comment_deadline"])
        lines = [f"Open for comment until {when}.", f"{r.get('tier_label')}." if r.get("tier_label") else "",
                 (r.get("win") or {}).get("entry_point", ""), f"Official notice: {link}" if link else "", source_line(r)]
        entries.append(
            "<item>"
            f"<title>{x('Comments close ' + when + ': ' + r['title'])}</title>"
            f"<link>{x(base + r['page'])}</link>"
            f'<guid isPermaLink="false">{x(r["id"] + "#comments-" + r["comment_deadline"])}</guid>'
            f"<pubDate>{_rfc822(updated.isoformat())}</pubDate>"
            f"<description>{x(chr(10).join(l for l in lines if l))}</description>"
            + (f"<source url={qa(link)}>{x(r.get('agency') or r['source_name'])}</source>" if link else "")
            + "</item>")
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
            f"<image><url>{x(base + 'logo-144.png')}</url><title>{x(title)}</title><link>{x(base)}</link>"
            "<width>144</width><height>144</height></image>"
            f"<lastBuildDate>{_rfc822(updated.isoformat())}</lastBuildDate>"
            + "".join(entries) + "</channel></rss>\n")


COMMENT_FEED = "comment-periods"


def write_feeds(out_dir: Path, records: list[dict], meta: list[dict], site: dict, updated: date) -> list[str]:
    """One feed per profile plus all.xml: comment periods closing in the next N days first, then new projects.
    comment-periods.xml: only the closing comment periods, every profile."""
    folder = out_dir / "feeds"
    folder.mkdir(exist_ok=True)
    name = site.get("title", "MitiMark")
    url = site.get("url", "")
    days = int(site.get("closing_soon_days", 14))
    written = []
    feeds = [("all", None, f"{name}: new projects", "New projects on the general list since the last update, after the "
              f"comment periods closing in the next {days} days.", False)]
    for m in meta:
        feeds.append((m["key"], m["key"], f"{name}: new projects, {m['label']}",
                      f"New projects since the last update, for the profile: {m['label']}, after the comment periods "
                      f"closing in the next {days} days.", m.get("exclude_freshwater")))
    for slug, key, title, desc, fresh in feeds:
        path = f"feeds/{slug}.xml"
        (out_dir / path).write_text(render_feed(feed_items(records, key, bool(fresh)), title, desc, url, path, updated,
                                                closing_soon(records, days, updated, key, bool(fresh))),
                                    encoding="utf-8")
        written.append(path)
    # Every listed project (general list or any profile) whose comment period closes soon.
    keys = [m["key"] for m in meta]
    closing, seen = [], set()
    for k in [None] + keys:
        for r in closing_soon(records, days, updated, k):
            if r["id"] not in seen:
                seen.add(r["id"])
                closing.append(r)
    closing.sort(key=lambda r: (r["comment_deadline"], r["title"]))
    path = f"feeds/{COMMENT_FEED}.xml"
    (out_dir / path).write_text(render_feed([], f"{name}: comment periods closing in the next {days} days",
                                            f"Projects whose public comment period closes in the next {days} days.",
                                            url, path, updated, closing), encoding="utf-8")
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
    marker = '<div class="project-main">' if '<div class="project-main">' in html else '<main class="wrap">'
    path.write_text(html.replace(marker, marker + "\n" + note, 1), encoding="utf-8")


def load_previous(out_dir: Path) -> dict:
    path = out_dir / "items.json"
    if not path.exists():
        return {}
    try:
        return {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
    except (ValueError, KeyError):
        return {}
