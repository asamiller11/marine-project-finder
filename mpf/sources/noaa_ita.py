"""United States: NOAA Fisheries "Incidental Take Authorizations for Construction Activities" page.

One public page (robots.txt allows it) listing every construction project (wharves, piers, seawalls, ferry terminals)
whose builder asked NOAA Fisheries for permission to disturb marine mammals: "In Process", "Active" and "Expired".
Each entry has a title, a status table (status, public comment, date issued, effective dates), a one-sentence
summary and a link to its documents. Works of the US federal government: public domain.

One request per run (a list page, re-read at most every 20 hours). In-process and active entries are kept;
expired ones are skipped. Shown under the hard-structure
profile only (like fed_register), never on the general list.
"""
from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.sources.fed_register import STATE_RE
from mpf.textutil import clean, today

PAGE = ("https://www.fisheries.noaa.gov/national/marine-mammal-protection/"
        "incidental-take-authorizations-construction-activities")


def parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("div", class_="field--name-body") or soup
    out, section = [], ""
    for el in body.find_all(["h2", "h3"]):
        if el.name == "h2":
            section = clean(el.get_text())
            continue
        title = clean(el.get_text())
        table, summary, link = None, "", ""
        for sib in el.find_next_siblings():
            if sib.name in ("h2", "h3"):
                break
            if sib.name == "table" and table is None:
                table = sib
            elif sib.name == "p" and not summary:
                summary = re.sub(r"^Summary:\s*", "", clean(sib.get_text(" ")))
            elif sib.name == "ul" and not link:
                a = sib.find("a", href=True)
                link = a["href"] if a else ""
        cells = [clean(td.get_text(" ")) for td in table.find_all("td")] if table else []
        values = cells[4:8] if len(cells) >= 8 else ["", "", "", ""]
        out.append({"section": section, "title": title, "status": values[0], "comment": values[1],
                    "issued": values[2], "effective": values[3], "summary": summary, "url": link})
    return out


def window_ended(effective: str, today_iso: str) -> bool:
    """True when the authorization's last effective date is before today."""
    dates = re.findall(r"\d{1,2}/\d{1,2}/\d{4}", effective or "")
    return bool(dates) and (_date(dates[-1]) or "9999") < today_iso


def _date(text: str) -> str | None:
    m = re.search(r"\d{1,2}/\d{1,2}/\d{4}", text or "")
    if not m:
        return None
    mo, d, y = m.group(0).split("/")
    return f"{y}-{int(mo):02d}-{int(d):02d}"


class NOAAITASource(Source):
    name = "noaa_ita"
    country = "US"

    def fetch(self) -> Iterator[Item]:
        try:
            html = self.client.get(PAGE, list_page=True).decode("utf-8", "replace")
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
            return
        rows = parse_page(html)
        keep = [r for r in rows if r["url"] and not r["section"].lower().startswith("expired")
                and not window_ended(r["effective"], today().isoformat())]
        self.log.append(f"{len(rows)} authorizations on the page, {len(keep)} in process or active "
                        f"(an authorization whose work window has ended is skipped: the work is probably done)")
        for r in keep:
            yield self.make_item(r)

    def make_item(self, r: dict) -> Item:
        slug = r["url"].rstrip("/").rsplit("/", 1)[-1]
        state = STATE_RE.search(r["title"]) or STATE_RE.search(r["summary"])
        deadline = _date(r["comment"])
        text = "\n".join(t for t in (r["title"], r["summary"]) if t)
        return Item(source=self.name, item_id=slug[:120], url=r["url"], title=r["title"], language="en",
                    country=self.country, province=state.group(1) if state else UNKNOWN,
                    comment_deadline=deadline, decision_date=_date(r["issued"]),
                    status_raw=("authorized (NOAA Fisheries incidental take authorization issued)"
                                if r["section"].lower().startswith("active") else "in process: " + (r["status"] or "")),
                    full_text=text,
                    extra={"section": r["section"], "effective": r["effective"]})
