"""United States: the Federal Register API, for NOAA Fisheries incidental take notices.

When in-water construction (pile driving at a dock, wharf, pier or terminal) may disturb marine mammals, NOAA
Fisheries (the National Marine Fisheries Service) publishes a proposed Incidental Harassment Authorization (IHA)
with a 30-day comment period, then a notice that it has issued it. These notices describe the work, the place
and the dates, often months before construction.

Terms: Federal Register documents are works of the US federal government and are in the public domain. The
API is public and needs no key. robots.txt allows /api/v1/ (checked by the polite client before every host).

What we read: /api/v1/documents.json, one page of the newest matches for a search term (default: incidental
harassment authorization + pile driving), with only the fields we use. One request per run, page size from
config (start small). No full texts are fetched yet: the abstract carries the title, place and kind of work.

OFF by default (config/settings.yaml, sources.fed_register.enabled: false) until the owner approves the results.
"""
from __future__ import annotations

import json
import re
from typing import Iterator
from urllib.parse import urlencode

import requests

from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean

API = "https://www.federalregister.gov/api/v1/documents.json"
DEFAULT_TERM = '"incidental harassment authorization" pile driving'
FIELDS = ["document_number", "title", "type", "action", "abstract", "publication_date", "html_url", "agencies",
          "dates", "comments_close_on", "effective_on"]
NOAA = re.compile(r"National Oceanic and Atmospheric Administration|National Marine Fisheries Service", re.I)

STATES = ["Alabama", "Alaska", "California", "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Louisiana",
          "Maine", "Maryland", "Massachusetts", "Mississippi", "New Hampshire", "New Jersey", "New York",
          "North Carolina", "Oregon", "Rhode Island", "South Carolina", "Texas", "Virginia", "Washington",
          "District of Columbia", "Puerto Rico", "Guam", "American Samoa", "Northern Mariana Islands",
          "U.S. Virgin Islands", "Virgin Islands"]
STATE_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in STATES) + r")\b")
# Titles read "Takes of Marine Mammals Incidental to Specified Activities; Taking Marine Mammals Incidental to the
# Seawalk Extension Project, Juneau, Alaska": the project is after the LAST "Incidental to".
INCIDENTAL_TO = re.compile(r"Incidental to (?:the )?", re.I)
# Who asked: "a request from the City and Borough of Juneau (CBJ)", "an ... (IHA) to Turnagain Marine Services (TMC)".
PROPONENT = re.compile(r"(?:request from|\(IHA\) to|authorization to|upon request from)\s+(?:the\s+)?"
                       r"([A-Z][A-Za-z0-9.&'\- ]+?)\s*\(", re.S)
# "in Seward, Alaska", ", Juneau, Alaska", "at ... Kings Bay, Georgia", "in Puget Sound, Washington".
PLACE = re.compile(r"(?:\bin|\bat|,)\s+((?:[A-Z][\w.'\-]*\s?){1,5}),\s+(?:" + "|".join(re.escape(s) for s in STATES) + r")\b")
WORKS = re.compile(r"pile driving|pile removal|pile replacement|piles?\b|vibratory|dock|wharf|wharves|pier|terminal|"
                   r"jetty|dolphin|breakwater|bulkhead|seawall|seawalk|waterfront|ferry|marina|bridge|berth|quay|"
                   r"dredg\w*|levee|revetment|restoration|outfall|launch ramp|boat ramp", re.I)


def search_url(term: str = DEFAULT_TERM, per_page: int = 10, page: int = 1) -> str:
    """The API address for one page of the newest documents matching term. Fixed parameter order, so the cache
    key is the same every time."""
    params = [("conditions[term]", term), ("order", "newest"), ("per_page", str(per_page)), ("page", str(page))]
    params += [("fields[]", f) for f in FIELDS]
    return API + "?" + urlencode(params)


def full_text_url(doc: dict) -> str:
    """The notice's plain-text full text (allowed by robots.txt): /documents/full_text/text/YYYY/MM/DD/<number>.txt"""
    y, m, d = (doc.get("publication_date") or "0000-00-00").split("-")
    return f"https://www.federalregister.gov/documents/full_text/text/{y}/{m}/{d}/{doc.get('document_number')}.txt"


def parse_results(data: bytes) -> tuple[int, list[dict]]:
    """(total matches, results on this page)."""
    doc = json.loads(data.decode("utf-8"))
    return int(doc.get("count") or 0), list(doc.get("results") or [])


def is_noaa(doc: dict) -> bool:
    names = " ".join((a.get("name") or a.get("raw_name") or "") for a in doc.get("agencies") or [])
    return bool(NOAA.search(names))


def project_and_place(title: str) -> tuple[str, str]:
    """From a notice title, the project words after the last 'Incidental to' and the US state it names."""
    parts = INCIDENTAL_TO.split(title or "")
    project = clean(parts[-1]) if len(parts) > 1 else clean(title)
    s = STATE_RE.findall(project) or STATE_RE.findall(title or "")
    return project, (s[-1] if s else UNKNOWN)


def proponent_of(abstract: str) -> str:
    m = PROPONENT.search(abstract or "")
    return clean(m.group(1)) if m else UNKNOWN


def place_of(text: str) -> str:
    """The town or water body just before the state name, e.g. 'Seward' or 'Puget Sound'."""
    found = PLACE.findall(text or "")
    return clean(found[-1]) if found else UNKNOWN


def works_of(text: str) -> str:
    seen: list[str] = []
    for w in WORKS.findall(text or ""):
        w = w.lower()
        if w not in seen:
            seen.append(w)
    return ", ".join(seen) or UNKNOWN


def stage_of(doc: dict) -> str:
    """Plain stage words the timing code understands: proposed IHAs are open for comment, issued ones are decided."""
    action = (doc.get("action") or "").lower()
    if "proposed" in action or "request for comments" in action:
        return "comment period open" if doc.get("comments_close_on") else "under assessment"
    if "issuance" in action or "issued" in action or "final" in action:
        return "authorized (incidental harassment authorization issued)"
    return "under assessment"


def doc_to_item(doc: dict) -> Item:
    title = clean(doc.get("title")) or UNKNOWN
    project, state = project_and_place(title)
    abstract = clean(doc.get("abstract"))
    stage = stage_of(doc)
    decided = stage.startswith("authorized")
    return Item(
        source="fed_register", item_id=str(doc.get("document_number")), url=doc.get("html_url") or "",
        title=project or title, language="en", country="US", province=state,
        water_body=place_of(project), proponent=proponent_of(abstract),
        project_type_raw=works_of(project + " " + abstract),
        date_posted=doc.get("publication_date"), comment_deadline=doc.get("comments_close_on"),
        decision_date=doc.get("publication_date") if decided else None,
        # Plain words the stage logic understands ("authorized" = decided); the notice's own action is kept in extra.
        status_raw="authorized (incidental harassment authorization issued)" if decided else
                   ("proposed authorization; public comments requested" if doc.get("comments_close_on") else "under assessment"),
        stage=stage,
        full_text="\n".join(t for t in (title, abstract, clean(doc.get("dates"))) if t),
        extra={"notice_title": title, "doc_type": doc.get("type"), "effective_on": doc.get("effective_on"),
               "action": clean(doc.get("action")), "abstract": abstract, "document_number": doc.get("document_number"),
               "link_ok": True},
    )


class FedRegisterSource(Source):
    name = "fed_register"
    country = "US"

    def fetch(self) -> Iterator[Item]:
        url = search_url(self.conf.get("term", DEFAULT_TERM), int(self.conf.get("per_page", 10)))
        try:
            data = self.client.get(url, list_page=True)
        except Blocked as e:            # 401, 403, 429 or robots.txt: stop, never work around it
            self.log.append(f"STOPPED: {e}")
            return
        total, results = parse_results(data)
        kept = [d for d in results if is_noaa(d)]
        self.log.append(f"{total} matching documents; read the newest {len(results)}, kept {len(kept)} from NOAA")
        for doc in kept:
            item = doc_to_item(doc)
            body = ""
            if self.conf.get("fetch_full_text", True):
                try:
                    body = self.client.get(full_text_url(doc)).decode("utf-8", "replace")   # fetched once, cached
                except Blocked as e:
                    self.log.append(f"STOPPED: {e}")
                    return
                except requests.RequestException as e:
                    self.log.append(f"full text skipped {doc.get('document_number')}: {type(e).__name__}")
            if body:
                item.full_text = item.full_text + "\n" + body
                item.raw_path = str(self.client.cached_path(full_text_url(doc)) or "")
            else:
                item.raw_path = str(self.client.cached_path(url) or "")
            yield item


# ---------------------------------------------------------------------------
# Its own score (fix 6). Every IHA notice repeats the same marine-mammal wording ("mitigation measures",
# "Endangered", "vessels, dredging, aircraft, construction") and gives noise distances ("24,100 m"), so the general
# score is fooled. Here only the title and the project-description section count, plus the number of piles.

_SECTION = re.compile(r"\n\s*(Description of (the )?(Proposed|Specified) Activit(y|ies)( and Anticipated Impacts)?|"
                      r"Summary of (the )?Specified Activit(y|ies)( and Anticipated Impacts)?|Detailed Description of the "
                      r"(Specified )?Activit(y|ies))\s*\n", re.I)
_SECTION_END = re.compile(r"\n\s*(Description of (the )?Marine Mammals|Potential Effects|Estimated Take|Determinations|"
                          r"Proposed Mitigation|Description of Mitigation)\b", re.I)
# Notices that are not construction: research, surveys, exploration, training, launches, fishing.
NOT_CONSTRUCTION = re.compile(r"\b(research|survey|surveys|geophysical|seismic|training|testing|exercises?|"
                              r"natural gas activities|oil and gas|exploration|launch(es)?|fisheries|sonar)\b", re.I)
# A count in a sentence ("230 24-inch steel pipe piles", "14 24-in permanent\nconcrete piles"), not across table
# columns (numbers separated by long runs of spaces or by indented line breaks).
_GAP = r"(?:[ \t]{1,2}|[ \t]?\n)"
_PILES = re.compile(r"(?<![\d.,])\b(\d{1,4}(?:,\d{3})?)[ \t]{1,2}(?:\(\d+\)[ \t])?(?:[\w\-.\"”'/]+" + _GAP + r"){0,6}?piles\b", re.I)
_YEARISH = re.compile(r"^(19|20)\d\d$")
PILE_POINTS = [(200, 20), (50, 15), (10, 8)]     # [at least this many piles, points]


def project_description(text: str, limit: int = 15000) -> str:
    """The notice's own description of the work (first matching section), without the boilerplate around it."""
    m = _SECTION.search(text or "")
    if not m:
        return ""
    rest = text[m.end():m.end() + limit * 2]
    end = _SECTION_END.search(rest)
    return rest[:end.start() if end else limit][:limit]


def pile_count(text: str) -> int:
    """The largest pile count stated in the text ('230 24-inch steel pipe piles' -> 230). 0 if none."""
    best = 0
    for m in _PILES.finditer(text or ""):
        n = m.group(1).replace(",", "")
        if _YEARISH.match(n):
            continue
        if int(n) < 5000:
            best = max(best, int(n))
    return best


def iha_score(title: str, abstract: str, full_text: str, rules: dict) -> dict:
    """Score for one NOAA incidental harassment notice: structure types from the title and description, plus pile
    points. Same shape as mpf.score.score_text's result, so the rest of the tool can use it."""
    from mpf.extract import find_patterns
    desc = project_description(full_text)
    work = "\n".join(t for t in (title, abstract, desc) if t)
    reasons = []
    if NOT_CONSTRUCTION.search(title or ""):
        reasons.append({"rule": "cap:not_construction", "label": "Not construction (research, survey, exploration ...)",
                        "points": 0, "matched": NOT_CONSTRUCTION.search(title).group(0), "quote": title})
        return {"score": 0, "flagged": False, "reasons": reasons, "project_types": [], "size_summary": "",
                "facts": {"piles": 0, "description_found": bool(desc)}, "because": "not construction"}
    types = find_patterns(work, rules["project_types"])
    for name in list(types):
        gone = rules["project_types"][name].get("replaces")
        if gone:
            types.pop(gone, None)
    ranked = sorted(types.items(), key=lambda kv: -kv[1]["points"])
    type_points = (ranked[0][1]["points"] + min(6, 3 * (len(ranked) - 1))) if ranked else 0
    for i, (name, hit) in enumerate(ranked):
        reasons.append({"rule": f"type:{name}", "label": hit["label"], "points": hit["points"] if i == 0 else (3 if i <= 2 else 0),
                        "matched": hit["matched"], "quote": hit["quote"]})
    # No habitat words: these notices are about marine mammals and noise, and the listing must never suggest habitat
    # work. Only counts stated in the description: tables elsewhere hold noise distances and take numbers.
    piles = pile_count(desc)
    pts = next((p for n, p in PILE_POINTS if piles >= n), 0)
    if pts:
        reasons.append({"rule": "size:piles", "label": f"{piles} piles", "points": pts, "matched": str(piles), "quote": ""})
    total = max(0, min(100, type_points + pts))
    return {"score": total, "flagged": total >= rules["flag_threshold"], "reasons": reasons,
            "project_types": list(dict.fromkeys(types[k]["label"] for k in types)), "size_summary": f"{piles} piles" if piles else "",
            "facts": {"piles": piles, "description_found": bool(desc), "types": sorted(types),
                      "pile_driving": bool(re.search(r"\bpile driving\b", work, re.I))},
            "because": iha_because(types, piles, bool(re.search(r"\bpile driving\b", work, re.I)))}


def iha_because(types: dict, piles: int, pile_driving: bool) -> str:
    """What the notice says about the construction, e.g. 'pile driving, 230 piles; wharf, pier or large dock'.
    Never mentions habitat."""
    first = ", ".join(x for x in ("pile driving" if pile_driving else "", f"{piles} piles" if piles else "") if x)
    labels = [t["label"].lower() for k, t in types.items() if k != "piles"]
    parts = [p for p in [first] + list(dict.fromkeys(labels)) if p]
    return "; ".join(parts) if parts else "in-water construction named in the notice"
