"""California CEQAnet (State Clearinghouse database of CEQA documents).

Used under the State Clearinghouse's reply of 2026-11-24 (see config/settings.yaml): CEQAnet information is public
data under the site's Conditions of Use (Ownership section). The reply also asks that resource-intensive or bulk
activity happen outside business hours (after 6pm Pacific), so this source only reads outside Monday to Friday,
8am to 6pm Pacific time (in_business_hours below). robots.txt allows everything except the attachment zip downloads, which we never fetch.

What we read, about once a week:
- /Search?StartRange=...&EndRange=...&ProjectIssue=Coastal+Zone : documents received in the last lookback_days that
  the lead agency tagged with the "Coastal Zone" issue (about 15 to 20 a month).
- /Search/Recent : the 100 newest documents; only rows whose title names water or shoreline work are opened.
- each chosen document page (/<SCH number>/<document number>): lead agency, document type, received date,
  description, county, waterways, coordinates and the state review period. Contact people's names, phones and
  emails are on the page but are never stored.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterator
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from mpf.extract import extract_water_body
from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, parse_date, today

PACIFIC = ZoneInfo("America/Los_Angeles")


def in_business_hours(now: datetime | None = None) -> bool:
    """True Monday to Friday, 8am to 6pm Pacific: the State Clearinghouse asked for reads after 6pm Pacific."""
    t = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    return t.weekday() < 5 and 8 <= t.hour < 18


BASE = "https://ceqanet.lci.ca.gov/"
RECENT_URL = BASE + "Search/Recent"
COASTAL_URL = BASE + "Search?StartRange={start}&EndRange={end}&ProjectIssue=Coastal+Zone"
# Titles worth opening from the Recent list (the Coastal Zone search is opened in full).
WATER_WORDS = re.compile(r"\b(dock|pier|wharf|wharves|berth|harbou?r|marina|port|seawall|sea wall|breakwater|jetty|groin|"
                         r"revetment|bulkhead|levee|shoreline|coast(al)?|beach|bay|estuar\w*|lagoon|slough|marsh|wetland|"
                         r"channel|dredg\w*|riprap|rock slope|bluff|ocean|tidal|ferry|boat)\b", re.I)
_SKIP_LABELS = {"Name", "Agency Name", "Job Title", "Contact Types", "Address", "Phone", "Email"}


def _us_date(s: str) -> str | None:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", clean(s))
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else parse_date(s)


def parse_results(html: str) -> list[dict]:
    """Rows of a search or Recent page: SCH number, document URL, type, lead agency, received date, title."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        a = tr.find("a", href=re.compile(r"^/\d{10}"))
        if len(tds) < 5 or not a:
            continue
        cells = [clean(td.get_text(" ")) for td in tds]
        href = a["href"]
        rows.append({"sch": href.strip("/").split("/")[0], "url": urljoin(BASE, href), "doc_id": href.strip("/").replace("/", "-"),
                     "type": cells[1], "agency": cells[2], "received": _us_date(cells[3]), "title": cells[4]})
    return rows


def parse_document(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup.body or soup
    fields: dict[str, str] = {}
    for strong in main.find_all("strong"):
        label = clean(strong.get_text())
        col = strong.find_parent("div")
        nxt = col.find_next_sibling("div") if col else None
        if not label or nxt is None or label in _SKIP_LABELS or label in fields:
            continue
        fields[label] = clean(nxt.get_text(" "))
    lat = lon = None
    m = re.search(r"(\d+)°(\d+)'([\d.]+)\"N\s+(\d+)°(\d+)'([\d.]+)\"W", fields.get("Coordinates", ""))
    if m:
        d = [float(x) for x in m.groups()]
        lat = round(d[0] + d[1] / 60 + d[2] / 3600, 5)
        lon = -round(d[3] + d[4] / 60 + d[5] / 3600, 5)
    return {"fields": fields, "lat": lat, "lon": lon,
            "review_end": _us_date(fields.get("Public Review Period End") or fields.get("State Review Period End") or "")}


class CEQAnetSource(Source):
    name = "ceqanet"
    country = "US"

    def fetch(self) -> Iterator[Item]:
        if in_business_hours():
            self.log.append("Not read this time: CEQAnet is read only after 6pm Pacific on weekdays (or at weekends), "
                            "as the State Clearinghouse asked. Earlier entries are kept.")
            return
        end = today()
        start = end - timedelta(days=int(self.conf.get("lookback_days", 30)))
        chosen: dict[str, dict] = {}
        try:
            coastal = parse_results(self.client.get(COASTAL_URL.format(start=start.isoformat(), end=end.isoformat()),
                                                    list_page=True).decode("utf-8", "replace"))
            recent = parse_results(self.client.get(RECENT_URL, list_page=True).decode("utf-8", "replace"))
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
            return
        for r in coastal:
            chosen.setdefault(r["doc_id"], r)
        watery = [r for r in recent if WATER_WORDS.search(r["title"])]
        for r in watery:
            chosen.setdefault(r["doc_id"], r)
        self.log.append(f"{len(coastal)} Coastal Zone documents since {start}, {len(watery)} of {len(recent)} recent "
                        f"documents name water work; {len(chosen)} opened")
        for row in chosen.values():
            try:
                page = self.client.get(row["url"]).decode("utf-8", "replace")
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except requests.RequestException as e:
                self.log.append(f"skipped {row['doc_id']}: {e}")
                continue
            yield self.make_item(row, page)

    def make_item(self, row: dict, page: str) -> Item:
        d = parse_document(page)
        f = d["fields"]
        desc = f.get("Document Description", "")
        place = ", ".join(x for x in (f.get("Cities"), f.get("Counties")) if x)
        text = clean(f"{row['title']}. {desc} Development: {f.get('Development Types', '')}. "
                     f"Waterways: {f.get('Waterways', '')}.")
        water = f.get("Waterways") or extract_water_body(text)
        return Item(
            source=self.name, item_id=row["doc_id"], url=row["url"], title=f.get("Document Title") or row["title"],
            language="en", country=self.country, province="California", water_body=water or UNKNOWN,
            lat=d["lat"], lon=d["lon"], proponent=f.get("Lead Agency") or row["agency"] or UNKNOWN,
            project_type_raw=f.get("Document Type") or row["type"], date_posted=row["received"],
            comment_deadline=d["review_end"], status_raw=f.get("Document Type") or row["type"],
            full_text=text, raw_path=str(self.client.cached_path(row["url"]) or ""),
            extra={"sch": row["sch"], "place": place, "document_type": f.get("Document Type") or row["type"]},
        )
