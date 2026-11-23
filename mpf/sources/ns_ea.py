"""Nova Scotia environmental assessment projects (novascotia.ca/nse/ea).

Used with the Nova Scotia Environmental Assessment Branch's written permission (requested 2026-11-17, granted;
see config/settings.yaml). robots.txt allows these paths.

What we read, about once a week:
- /nse/ea/projects.asp : two HTML tables, "Projects under review" and "Completed reviews (2000 - current)",
  each row a project name (linked), proponent and registration date.
- each project page registered in the last lookback_days: proponent, location, class, status, the description,
  the Minister's decision date and the public comment deadline.
Attached PDFs are not downloaded.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Iterator
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from mpf.extract import extract_water_body
from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, guess_language, parse_date, today

LIST_URL = "https://novascotia.ca/nse/ea/projects.asp"
_DATE = r"(\d{1,2} [A-Z][a-z]+ \d{4})"


def parse_project_list(html: str) -> list[dict]:
    """Every project row, with under_review True for the first table."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table"):
        cap = clean(table.caption.get_text()) if table.caption else ""
        review = "under review" in cap.lower()
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            a = tds[0].find("a", href=True) if tds else None
            if len(tds) < 3 or not a or a["href"].strip() in ("", "/nse/ea/PAGE"):
                continue
            href = a["href"].strip()
            url = urljoin(LIST_URL, href if href.endswith((".asp", "/")) else href + "/")
            slug = re.sub(r"\.asp$", "", url.rstrip("/").rsplit("/", 1)[-1])
            rows.append({"id": slug, "url": url, "title": clean(a.get_text()), "proponent": clean(tds[1].get_text()) or UNKNOWN,
                         "registered": parse_date(clean(tds[2].get_text())), "under_review": review})
    return rows


def parse_project_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#centreContent") or soup
    text = clean(main.get_text(" "))
    out = {"proponent": UNKNOWN, "location": UNKNOWN, "class": UNKNOWN, "status": UNKNOWN}
    for key, label in (("proponent", "Proponent"), ("location", "Location"), ("class", "Class"), ("status", "Status")):
        m = re.search(label + r":\s*(.+?)(?=\s+(?:Proponent|Location|Class|Status):|\s{2}|$)", main.get_text("\n"))
        if m:
            out[key] = clean(m.group(1).split("\n")[0]) or UNKNOWN
    desc = ""
    h = main.find(lambda t: t.name in ("h2", "h3") and clean(t.get_text()) == "Description")
    if h:
        parts = []
        for sib in h.find_all_next():
            if sib.name in ("h2", "h3"):
                break
            if sib.name == "p":
                parts.append(clean(sib.get_text(" ")))
        desc = " ".join(p for p in parts if p)
    out["description"] = desc
    m = re.search(r"comments? on the \w+ will be accepted until\s*" + _DATE, text, re.I)
    out["comment_deadline"] = parse_date(m.group(1)) if m else None
    m = re.search(r"On or before " + _DATE + r",? the Minister", text)
    out["decision_due"] = parse_date(m.group(1)) if m else None
    m = re.search(r"On " + _DATE + r",? the Minister[^.]*?(approved|released|rejected|decided)", text)
    out["decided"] = parse_date(m.group(1)) if m else None
    out["text"] = text
    return out


class NSEASource(Source):
    name = "ns_ea"
    country = "CA"

    def fetch(self) -> Iterator[Item]:
        try:
            html = self.client.get(LIST_URL, list_page=True).decode("utf-8", "replace")
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
            return
        rows = parse_project_list(html)
        cutoff = (today() - timedelta(days=int(self.conf.get("lookback_days", 540)))).isoformat()
        recent = [r for r in rows if r["under_review"] or (r["registered"] and r["registered"] >= cutoff)]
        self.log.append(f"{len(rows)} projects listed, {len(recent)} under review or registered since {cutoff}")
        for row in recent:
            try:
                page = self.client.get(row["url"]).decode("utf-8", "replace")
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except requests.RequestException as e:
                self.log.append(f"skipped {row['id']}: {e}")
                continue
            yield self.make_item(row, page)

    def make_item(self, row: dict, page: str) -> Item:
        p = parse_project_page(page)
        body = p["description"] or p["text"]
        text = clean(f"{row['title']}. {p['location']}. {body}")
        decided = p["decided"]
        return Item(
            source=self.name, item_id=row["id"], url=row["url"], title=row["title"],
            language=guess_language(body), country=self.country, province="Nova Scotia",
            water_body=extract_water_body(text),
            proponent=p["proponent"] if p["proponent"] != UNKNOWN else row["proponent"],
            date_posted=row["registered"], comment_deadline=p["comment_deadline"],
            decision_date=decided or p["decision_due"],
            status_raw=p["status"] if p["status"] != UNKNOWN else ("Under Review" if row["under_review"] else "Completed"),
            full_text=text, raw_path=str(self.client.cached_path(row["url"]) or ""),
            extra={"location": p["location"], "ea_class": p["class"], "decision_made": bool(decided)},
        )
