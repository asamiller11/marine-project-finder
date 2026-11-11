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
        status_raw=clean(doc.get("action")) or UNKNOWN, stage=stage,
        full_text="\n".join(t for t in (title, abstract, clean(doc.get("dates"))) if t),
        extra={"notice_title": title, "doc_type": doc.get("type"), "effective_on": doc.get("effective_on")},
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
            item.raw_path = str(self.client.cached_path(url) or "")
            yield item
