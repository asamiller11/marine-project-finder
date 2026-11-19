"""United States, California: California Coastal Commission monthly meeting agendas, coastal development permit items.

Written permission: Joshua Smith, Public Information Officer, California Coastal Commission, by email on
19 November 2026: "I don't see why you couldn't use the public information on the Coastal Commission agenda for your
website." The request described the use: project name, applicant, location, hearing date, up to three short quoted
sentences, a link back, a source credit, the agenda checked about once a month, possibly in a paid subscription.
Record: private/permission_requests_2026-11.md. robots.txt on www.coastal.ca.gov allows all robots.

The agenda page loads each month from the Commission's agenda API (api.coastal.ca.gov/agendas/v1/<year>/<month>),
the same data the page shows. Only permit items are kept (Coastal Permit Applications, Permit Amendments, Permit
Extensions and permit items on the Consent Calendar), and only the permitted fields: project name, applicant,
location, hearing date, the item description (for scoring and at most three short quotes) and a link back to the
agenda item. Staff report text is not read.

To match the described use ("checked about once a month"), the agenda is fetched at most once every
`min_days_between_reads` days (default 28); in between, the saved copy is used. Two months are read: this month and
next month (agendas appear a few weeks before the meeting).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Iterator

from mpf.http import Blocked
from mpf.models import Item
from mpf.sources.base import Source
from mpf.textutil import clean, today

API = "https://api.coastal.ca.gov/agendas/v1/{year}/{month}"
AGENDA_LINK = "https://www.coastal.ca.gov/meetings/agenda/#/{year}/{month:02d}/{day}"
PERMIT_CATEGORY = re.compile(r"coastal permit applications?|permit amendments?|permit extensions?|consent calendar", re.I)
PERMIT_ITEM = re.compile(r"\b(Application|Permit) No\.?\s*[\w-]+", re.I)
_TITLE = re.compile(r"(?:Application|Permit)(?: Amendment)? No\.?\s*([\w-]+)\s*\(((?:[^()]|\([^()]*\))+)\)", re.I)
_APPLICANT = re.compile(r"^(?:Application|Request) (?:of|by)\s+(?:the\s+)?(.+?)\s+(?:to|for)\b", re.I)


def months_to_read(d: date) -> list[tuple[int, int]]:
    nxt = (d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1)
    return [(d.year, d.month), nxt]


def parse_agenda(data: list | dict, year: int, month: int) -> list[dict]:
    """Permit items in one month's agenda: number, project, applicant, place, hearing date, description, link."""
    out = []
    for agenda in data if isinstance(data, list) else [data]:
        for day in agenda.get("days") or []:
            d = day.get("date")
            for dist in day.get("districts") or []:
                district = clean(((dist.get("name") or {}).get("english")) or "")
                for cat in dist.get("categories") or []:
                    if not PERMIT_CATEGORY.search(((cat.get("title") or {}).get("english")) or ""):
                        continue
                    for it in cat.get("items") or []:
                        title = clean(((it.get("title") or {}).get("english")) or "")
                        if not PERMIT_ITEM.search(title):
                            continue
                        blurb = clean(((it.get("blurb") or {}).get("english")) or "")
                        m = _TITLE.search(title)
                        number = m.group(1) if m else re.sub(r"\W+", "-", title)[:40]
                        name = m.group(2) if m else title
                        a = _APPLICANT.search(blurb)
                        out.append({
                            "number": number, "name": name, "applicant": a.group(1) if a else "",
                            "district": district, "hearing": f"{year}-{month:02d}-{int(d):02d}" if d else None,
                            "description": blurb, "result": clean(str(it.get("result") or "")),
                            "url": AGENDA_LINK.format(year=year, month=month, day=d) if d else
                            f"https://www.coastal.ca.gov/meetings/agenda/#/{year}/{month:02d}"})
    return out


class CACoastalSource(Source):
    name = "ca_coastal"
    country = "US"

    def _get(self, url: str) -> bytes:
        """At most once every N days (the use described in the permission request); the saved copy otherwise."""
        days = int(self.conf.get("min_days_between_reads", 28))
        saved = self.client.fetched_at(url)
        fresh = False
        if saved:
            try:
                fresh = (datetime.now() - datetime.fromisoformat(saved[:19])).days < days
            except ValueError:
                fresh = False
        return self.client.get(url, list_page=not fresh)

    def fetch(self) -> Iterator[Item]:
        now = today()
        n = 0
        for year, month in months_to_read(now):
            url = API.format(year=year, month=month)
            try:
                raw = self._get(url)
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except Exception as e:      # a month not published yet answers with an error page
                self.log.append(f"{year}-{month:02d}: no agenda ({e.__class__.__name__})")
                continue
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                self.log.append(f"{year}-{month:02d}: agenda not readable")
                continue
            for p in parse_agenda(data, year, month):
                n += 1
                decided = bool(re.search(r"approv|denied|deny", p["result"], re.I))
                held = bool(p["hearing"]) and p["hearing"] < now.isoformat()
                status = ("decision made" if decided else
                          f"hearing held {p['hearing']} (decision made or postponed; see the agenda)" if held else
                          f"application under review, Coastal Commission hearing {p['hearing']}")
                place = p["name"].split(",")[-1].strip() if "," in p["name"] else p["name"]
                text = f"{p['name']}. {p['description']}"
                yield Item(source=self.name, item_id=p["number"], url=p["url"], title=p["name"],
                           language="en", country=self.country, province="California",
                           proponent=p["applicant"] or "unknown", project_type_raw=p["district"],
                           date_posted=None, decision_date=p["hearing"], status_raw=status, full_text=text,
                           extra={"hearing_date": p["hearing"], "district": p["district"], "place": place,
                                  "application_no": p["number"], "link_ok": True})
        self.log.append(f"{n} coastal development permit items in this month's and next month's agendas")
