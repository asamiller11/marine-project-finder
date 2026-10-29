"""Newfoundland and Labrador environmental assessment registry.

What we read (robots.txt allows it; no crawl delay set, so we use our default):
- /eccc/env-assessment/projects-list/ : one HTML table of every project since 2000
  (registration number, title, proponent, date registered, status, release date).
- /eccc/projects/project-<n>/          : one project, with a description and a dated list
  such as "October 20, 2026 – Deadline for public comments."
- Optionally the short "Summary of Registration" PDF linked from the project page.
"""
from __future__ import annotations

import io
import re
from datetime import timedelta
from typing import Iterator

import requests
from bs4 import BeautifulSoup

from mpf.extract import extract_coordinates, extract_water_body
from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, guess_language, parse_date, today

LIST_URL = "https://www.gov.nl.ca/eccc/env-assessment/projects-list/"


def parse_project_list(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="dataTable") or soup.find("table")
    rows = []
    if not table:
        return rows
    for tr in table.find_all("tr"):
        # The site's HTML leaves <td> tags unclosed, so cells end up nested inside each
        # other. Taking every <td> in page order, and only each cell's own text, undoes that.
        cells = [(td, clean("".join(td.find_all(string=True, recursive=False))))
                 for td in tr.find_all("td")]
        if len(cells) < 4:
            continue
        reg = cells[0][1]
        title_cell = cells[1][0]
        a = title_cell.find("a", href=True)
        em = title_cell.find("em")
        proponent = clean(em.get_text()).replace("Proponent:", "").strip() if em else UNKNOWN
        status_text = cells[3][1]
        release = parse_date(cells[4][1]) if len(cells) > 4 else None
        rows.append({
            "reg": reg, "url": a["href"] if a else "", "title": clean(a.get_text()) if a else UNKNOWN,
            "proponent": proponent or UNKNOWN, "registered": parse_date(cells[2][1]),
            "status": re.sub(r"\d{4}-\d{2}-\d{2}", "", status_text).strip() or UNKNOWN,
            "release_date": release or parse_date(status_text),
        })
    return rows


# Phrases in the dated list on a project page, and what they tell us.
_DEADLINE = re.compile(r"deadline for (public )?comments?|comments? (are )?due|comment deadline", re.I)
_DECISION_DUE = re.compile(r"decision is due|decision due", re.I)
_DECISION_MADE = re.compile(r"(released|minister.s decision|decision letter|not required|"
                            r"environmental (impact statement|preview report) (is )?required)", re.I)


def parse_project_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    art = soup.find("article") or soup
    h1 = art.find("h1")
    content = art.find("div", class_="entry-content") or art
    prop = content.find("h3")
    proponent = clean(prop.get_text()).replace("Proponent:", "").strip() if prop else UNKNOWN

    # Description: the paragraphs between "Project Description:" and the dated list.
    desc_parts, started = [], False
    for el in content.find_all(["p", "ul"], recursive=False):
        txt = clean(el.get_text(" "))
        if el.name == "p" and re.match(r"project description", txt, re.I):
            started = True
            rest = re.sub(r"^project description:?\s*", "", txt, flags=re.I)
            if rest:
                desc_parts.append(rest)
            continue
        if started and el.name == "ul":
            break
        if started:
            desc_parts.append(txt)
    if not desc_parts:   # older pages without the heading: take paragraphs before "Documents:"
        for el in content.find_all("p", recursive=False):
            txt = clean(el.get_text(" "))
            if txt.lower().startswith("documents"):
                break
            if len(txt) > 80:
                desc_parts.append(txt)

    events = []
    for li in content.find_all("li"):
        txt = clean(li.get_text(" "))
        m = re.match(r"^([A-Z][a-z]+\.? \d{1,2},? \d{4})\s*[–\-:]\s*(.+)$", txt)
        if m:
            events.append({"date": parse_date(m.group(1)), "text": m.group(2)})

    deadlines = [e["date"] for e in events if e["date"] and _DEADLINE.search(e["text"])]
    due = [e["date"] for e in events if e["date"] and _DECISION_DUE.search(e["text"])]
    made = [e for e in events if e["date"] and _DECISION_MADE.search(e["text"])
            and not _DECISION_DUE.search(e["text"])]

    summary_pdf = None
    for a in content.find_all("a", href=True):
        if re.search(r"summary", a.get_text(), re.I) and a["href"].lower().endswith(".pdf"):
            summary_pdf = a["href"]
            break

    return {
        "title": clean(h1.get_text()) if h1 else UNKNOWN,
        "proponent": proponent or UNKNOWN,
        "description": "\n".join(desc_parts),
        "events": events,
        "comment_deadline": max(deadlines) if deadlines else None,
        "decision_due": max(due) if due else None,
        "decision_made": max(made, key=lambda e: e["date"]) if made else None,
        "summary_pdf": summary_pdf,
    }


def _is_two_columns(page) -> bool:
    """True when words sit in a left and a right column with almost none crossing the middle."""
    words = page.extract_words()
    if len(words) < 40:
        return False
    mid = page.width / 2
    left = sum(1 for w in words if w["x1"] < mid)
    right = sum(1 for w in words if w["x0"] > mid)
    crossing = len(words) - left - right
    return crossing < 0.03 * len(words) and left > 0.2 * len(words) and right > 0.2 * len(words)


def pdf_text(data: bytes, max_pages: int = 15) -> str:
    import pdfplumber   # imported here so the rest works even if pdfplumber is missing
    out = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            if _is_two_columns(page):
                # Read the left column, then the right, so sentences are not interleaved.
                mid = page.width / 2
                for box in ((0, 0, mid, page.height), (mid, 0, page.width, page.height)):
                    out.append(page.crop(box).extract_text() or "")
            else:
                out.append(page.extract_text() or "")
    return clean("\n".join(out))


class NLEASource(Source):
    name = "nl_ea"
    country = "CA"

    def fetch(self) -> Iterator[Item]:
        try:
            html = self.client.get(LIST_URL, list_page=True).decode("utf-8", "replace")
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
            return
        rows = parse_project_list(html)
        cutoff = (today() - timedelta(days=int(self.conf.get("lookback_days", 540)))).isoformat()
        recent = [r for r in rows if r["registered"] and r["registered"] >= cutoff and r["url"]]
        self.log.append(f"{len(rows)} projects listed, {len(recent)} registered since {cutoff}")
        for row in recent:
            try:
                page = self.client.get(row["url"]).decode("utf-8", "replace")
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except requests.RequestException as e:
                self.log.append(f"skipped {row['reg']}: {e}")
                continue
            yield self._make_item(row, page)

    def _make_item(self, row: dict, page_html: str) -> Item:
        p = parse_project_page(page_html)
        # The NL site's links are irregular (project-2434, projects-2437, project_2364, project-2383-2).
        # We use the link exactly as NL publishes it, and check the page is the right project.
        if not re.search(r"Registration Number:\s*" + re.escape(row["reg"]) + r"\b", page_html):
            self.log.append(f"WARNING: page for {row['reg']} does not show that registration number: {row['url']}")
        pdf_txt = ""
        if p["summary_pdf"] and self.conf.get("fetch_summary_pdf", True):
            max_bytes = int(float(self.conf.get("max_pdf_mb", 5)) * 1024 * 1024)
            try:
                pdf_txt = pdf_text(self.client.get(p["summary_pdf"], max_bytes=max_bytes))
            except Blocked as e:
                self.log.append(f"summary PDF skipped for {row['reg']}: {e}")
            except Exception as e:     # a broken PDF should not stop the run
                self.log.append(f"summary PDF unreadable for {row['reg']}: {type(e).__name__}")

        title = p["title"] if p["title"] != UNKNOWN else row["title"]
        text = "\n".join(t for t in (title, p["description"], pdf_txt) if t)
        lat, lon = extract_coordinates(text)
        decision_date = p["decision_due"]
        if p["decision_made"]:
            decision_date = p["decision_made"]["date"]
        elif row["release_date"]:
            decision_date = row["release_date"]
        return Item(
            source=self.name, item_id=row["reg"], url=row["url"], title=title,
            language=guess_language(p["description"] or text), country=self.country,
            province="Newfoundland and Labrador", water_body=extract_water_body(text), lat=lat, lon=lon,
            proponent=p["proponent"] if p["proponent"] != UNKNOWN else row["proponent"],
            date_posted=row["registered"], comment_deadline=p["comment_deadline"],
            decision_date=decision_date, status_raw=row["status"], full_text=text,
            raw_path=str(self.client.cached_path(row["url"]) or ""),
            extra={"events": p["events"], "summary_pdf": p["summary_pdf"],
                   "decision_made": p["decision_made"], "used_pdf_text": bool(pdf_txt)},
        )
