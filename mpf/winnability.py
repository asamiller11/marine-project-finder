"""Winnability: how likely a listed project is to be winnable for a buyer, on top of how relevant it is.

The relevance score (mpf/score.py, mpf/profiles.py) says whether a notice describes the right kind of work.
This adds or takes away points for the things that decide whether a supplier can still get in:
  - stage: pre-application, open for comment and under review beat approved or under construction,
  - habitat offsetting, compensation or a Fisheries Act authorization is mentioned,
  - an engineering or environmental consultant is named (someone to approach),
  - construction start timing, when the notice gives it,
  - project size, when the notice gives it.
Points live in config/scoring.yaml under `winnability:`.

It never adds or removes a project from a list: a listed project stays at least Weak, and a project
under the Weak line is not lifted onto a list. Every project also gets an "Entry point" line and two or
three plain reasons for its score.
"""
from __future__ import annotations

import re
from datetime import date

from mpf.extract import find_patterns, largest_sizes

DEFAULTS = {
    "stage": {"pre_application": 10, "open": 10, "assessment": 8, "closed": 4, "decided": -8,
              "under_construction": -15, "unknown": 0},
    "habitat_offsetting": 8,
    "engineering_firm": 6,
    "construction_start": {"within_months": 30, "soon": 6, "later": 2},
    "large_size": {"length_m": 100, "area_m2": 5000, "volume_m3": 10000, "points": 4},
    # The highest tier a project at this stage can reach, whatever its relevance (owner, 2026-11-19).
    "tier_caps": {"decided": "possible", "under_construction": "weak"},
    "max_bonus": 25,
    "max_penalty": 15,
    "offset_groups": ["offsetting", "fisheries_act", "hadd"],
}

MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                           "september", "october", "november", "december"])}
SEASONS = {"early": 2, "winter": 1, "spring": 4, "mid": 6, "summer": 7, "late": 10, "fall": 10, "autumn": 10}
_MONTH = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
_START = re.compile(
    r"\b(?:construction(?: activities| work)?|the (?:specified )?activities|(?:the )?works?|in-water work|the project)\s+"
    r"(?:is|are|will|would|may|could)\s+(?:currently\s+)?(?:be\s+)?(?:expected|anticipated|scheduled|planned|proposed)?\s*"
    r"(?:to\s+)?(?:begin|start|commence|occur|take place)\s+(?:in|on|as early as|between|from|by|during)?\s*(?:the\s+)?"
    r"(?:(early|late|mid|spring|summer|fall|autumn|winter)[ -](?:of\s+)?)?(?:" + _MONTH + r"\s+(?:\d{1,2},?\s+)?)?(\d{4})",
    re.IGNORECASE)
_START_FR = re.compile(r"\b(?:les travaux|la construction)\s+(?:devraient|doivent|vont|pourraient)\s+(?:débuter|commencer)\s+"
                       r"(?:en|au|à l.)\s*(?:(printemps|été|automne|hiver)\s+)?(\d{4})", re.IGNORECASE)
_NOAA_EFFECTIVE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})")
_UNDER_CONSTRUCTION = re.compile(r"\bunder construction\b|\bconstruction (?:has |have )?(?:began|begun|started|commenced)\b|"
                                 r"\bconstruction is (?:now )?(?:underway|under way|ongoing|in progress)\b", re.IGNORECASE)
_PRE_APPLICATION = re.compile(r"pre-application|\bscreening opinion\b|\bscoping (?:opinion|report|request)\b|\bscreening (?:opinion )?request\b|"
                              r"initial project description|\bplanning phase\b", re.IGNORECASE)


# A size counts as "large project" only when its sentence is about the marine works themselves.
_SIZE_GOOD = re.compile(r"breakwater|wharf|wharves|pier|seawall|sea wall|revetment|dredg|berth|jetty|quay|terminal|seabed|"
                        r"reclamation|infill|shoreline|harbour|harbor|dock|armour|armor|riprap|cable|wind farm|causeway|"
                        r"bulkhead|construction area|footprint|project area|marina|slipway|ramp|groyne|rock", re.IGNORECASE)
_SIZE_BAD = re.compile(r"\broad\b|pipeline|\bpipes?\b|distance|away from|\bfrom the\b|completed|existing|previous|"
                       r"\bkm (?:offshore|from|south|north|east|west)|offshore, to the", re.IGNORECASE)


def size_counts(size: dict) -> bool:
    q = size.get("quote") or ""
    return bool(_SIZE_GOOD.search(q)) and not _SIZE_BAD.search(q)


def conf_from(rules: dict | None) -> dict:
    conf = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in ((rules or {}).get("winnability") or {}).items():
        conf[k] = {**conf[k], **v} if isinstance(conf.get(k), dict) and isinstance(v, dict) else v
    return conf


def construction_start(text: str, extra: dict | None = None, source: str = "") -> dict | None:
    """{"year", "month", "quote"} when the notice says when construction starts, else None."""
    extra = extra or {}
    if source == "noaa_ita" and extra.get("effective"):
        m = _NOAA_EFFECTIVE.match(str(extra["effective"]))
        if m:
            return {"year": int(m.group(3)), "month": int(m.group(1)),
                    "when": f"{[k for k, v in MONTHS.items() if v == int(m.group(1))][0].title()} {m.group(3)}",
                    "quote": f"Authorization effective {extra['effective']}"}
    t = re.sub(r"\s+", " ", text or "")
    m = _START.search(t)
    if m:
        season, month, year = m.group(1), m.group(2), int(m.group(3))
        mo = MONTHS[month.lower()] if month else SEASONS.get((season or "").lower())
        when = f"{month.title()} {year}" if month else (f"{season.lower()} {year}" if season else str(year))
        return {"year": year, "month": mo, "when": when, "quote": m.group(0).strip()}
    m = _START_FR.search(t)
    if m:
        return {"year": int(m.group(2)), "month": {"printemps": 4, "été": 7, "automne": 10, "hiver": 1}.get(
            (m.group(1) or "").lower()), "when": m.group(2), "quote": m.group(0).strip()}
    return None


def _months_until(start: dict, today: date) -> int:
    return (start["year"] - today.year) * 12 + ((start.get("month") or 6) - today.month)


def win_stage(row: dict, stage_kind: str, start: dict | None, today: date) -> str:
    text = f"{row.get('status_raw') or ''} {row.get('stage') or ''} {row.get('title') or ''}"
    if _UNDER_CONSTRUCTION.search(text) or _UNDER_CONSTRUCTION.search(row.get("full_text") or ""):
        return "under_construction"
    if stage_kind == "decided":
        return "under_construction" if start and _months_until(start, today) < 0 else "decided"
    if stage_kind == "open":
        return "open"
    if _PRE_APPLICATION.search(text):
        return "pre_application"
    if stage_kind in ("assessment", "closed"):
        return stage_kind
    return "unknown"


STAGE_REASON = {
    "pre_application": "Early: pre-application stage, before the design is fixed",
    "open": "Early: public comment period open",
    "assessment": "Early: still under review, design not final",
    "closed": "Comment period closed, decision still pending",
    "decided": "Already approved: the design is mostly set",
    "under_construction": "Under construction or works window started: late to get in",
    "unknown": "",
}


def long_date(iso: str | None) -> str:
    """'November 27, 2026' from '2026-11-27'."""
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return iso or ""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _short(name: str, n: int = 60) -> str:
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


def entry_point(stage: str, rec: dict, stakeholders: dict, offsets: bool, start: dict | None) -> str:
    plain = lambda n: re.sub(r"\s*\(.*$", "", n) if n else n      # "Thistle Wind Partners (Qair ...)" -> the name
    owner = plain((stakeholders.get("owner") or [{}])[0].get("name"))
    firm = plain((stakeholders.get("consultant") or [{}])[0].get("name"))
    contractor = plain((stakeholders.get("contractor") or [{}])[0].get("name"))
    who = f"the proponent ({_short(owner)})" if owner else "the proponent"
    if stage == "open":
        line = f"Comment period open until {long_date(rec.get('comment_deadline'))}: submit a comment or contact {who}"
    elif stage == "pre_application":
        line = (f"Pre-application: contact the engineering firm ({_short(firm)})" if firm
                else f"Pre-application: contact {who} before the design is fixed")
    elif stage in ("assessment", "closed"):
        lead = "Design stage" if stage == "assessment" else "Decision pending"
        line = (f"{lead}: contact the engineering firm ({_short(firm)})" if firm
                else f"{lead}: contact {who}; no engineering firm is named yet")
    elif stage == "decided":
        line = (f"Approved: contact the engineering firm ({_short(firm)}) before tender" if firm
                else f"Approved: contact {who} and watch for the construction tender")
    elif stage == "under_construction":
        line = (f"Under construction: contact the contractor ({_short(contractor)})" if contractor
                else "Under construction: late; watch for later phases or repairs")
    else:
        line = f"Stage unclear: read the notice, then contact {who}"
    if offsets and stage not in ("under_construction",):
        line += ". Habitat offsetting is mentioned: offer a habitat-positive design"
    return line + "."


def assess(row: dict, stage_kind: str, stakeholders: dict, rules: dict | None, today: date,
           extra: dict | None = None, sizes: list[dict] | None = None) -> dict:
    """{"points", "stage", "reasons": [(points, text)], "entry_point", "start"} for one project.
    sizes: the sizes the relevance scorer kept (facts.sizes); none for sources scored by their own rule."""
    conf = conf_from(rules)
    text = row.get("full_text") or ""
    start = construction_start(text, extra, row.get("source") or "")
    stage = win_stage(row, stage_kind, start, today)
    reasons = []
    sp = int(conf["stage"].get(stage, 0))
    if STAGE_REASON.get(stage):
        reasons.append((sp, STAGE_REASON[stage]))

    groups = {k: rules["habitat_clues"][k] for k in conf["offset_groups"] if k in ((rules or {}).get("habitat_clues") or {})}
    offsets = find_patterns(text, groups, language=row.get("language")) if groups else {}
    if offsets:
        label = next(iter(offsets.values()))["label"]
        reasons.append((int(conf["habitat_offsetting"]), f"{label} (marine mitigation is likely needed)"))

    firm = (stakeholders.get("consultant") or [])
    if firm:
        reasons.append((int(conf["engineering_firm"]), f"Consultant named: {_short(firm[0]['name'])} (someone to approach)"))

    cs = conf["construction_start"]
    if start and stage not in ("under_construction",):
        months = _months_until(start, today)
        when = start.get("when") or str(start["year"])
        if 0 <= months <= int(cs["within_months"]):
            reasons.append((int(cs["soon"]), f"Construction expected to start {when}: buying decisions soon"))
        elif months > int(cs["within_months"]):
            reasons.append((int(cs["later"]), f"Construction expected to start {when}: time to get in early"))
        else:
            reasons.append((0, f"Stated construction start ({when}) has passed"))

    ls = conf["large_size"]
    sizes = [x for x in sizes or [] if size_counts(x)]
    big = largest_sizes(sizes)
    for kind in ("length_m", "area_m2", "volume_m3"):
        if big.get(kind) is not None and big[kind] >= float(ls.get(kind, 1e18)):
            q = next((s for s in sorted(sizes, key=lambda s: -s["value"]) if s["kind"] == kind), None)
            reasons.append((int(ls["points"]), f"Large project ({q['matched'] if q else kind})"))
            break

    total = sum(p for p, _ in reasons)
    total = max(-int(conf["max_penalty"]), min(int(conf["max_bonus"]), total))
    cap = (conf.get("tier_caps") or {}).get(stage)
    if cap:
        reasons.append((0, {"possible": "Approved, so kept at Possible or below",
                            "weak": "Under construction, so kept at Weak"}.get(cap, "")))
    return {"points": total, "stage": stage, "reasons": reasons, "cap_tier": cap, "start": start, "offsets": bool(offsets),
            "entry_point": entry_point(stage, {"comment_deadline": row.get("comment_deadline")}, stakeholders,
                                       bool(offsets), start)}


def adjusted(relevance: int, points: int, tiers_conf: dict, strong_cap: bool = False, cap_tier: str | None = None) -> int:
    """Relevance plus winnability. A listed project stays listed (at least Weak); one under the Weak line is
    not lifted. strong_cap: sources that are never Strong (profile-only sources) stay under the Strong line.
    cap_tier: the highest tier allowed at this stage ("possible" for approved, "weak" for under construction)."""
    weak_min = int(tiers_conf.get("weak_min", 20))
    if relevance < weak_min:
        return relevance
    s = max(weak_min, min(100, relevance + points))
    if strong_cap or cap_tier in ("possible", "weak"):
        s = min(s, int(tiers_conf.get("strong_min", 60)) - 1)
    if cap_tier == "weak":
        s = min(s, int(tiers_conf.get("possible_min", 35)) - 1)
    return s


def plain_reasons(relevance_reason: str, win: dict, n: int = 3) -> list[str]:
    """Two or three plain reasons: what the project is (relevance), then the strongest winnability factors."""
    out = [relevance_reason] if relevance_reason else []
    ranked = sorted((r for r in win["reasons"] if r[1] and not (relevance_reason and r[1].startswith(relevance_reason))),
                    key=lambda r: (-abs(r[0]), r[0] < 0))
    for pts, text in ranked:
        if len(out) >= n:
            break
        out.append(text + (f" ({'+' if pts > 0 else ''}{pts})" if pts else ""))
    return out
