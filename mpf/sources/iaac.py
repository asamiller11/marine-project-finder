"""Canadian Impact Assessment Registry (IAAC).

What we read (robots.txt allows these, with a 5 second crawl delay):
- /050/evaluations/participation/comment : one page listing every open comment period
  and its end date. This is our early signal.
- /050/evaluations/sitemaps/projects     : list of all project page URLs. We read the
  newest N (by reference number) to also catch recent projects whose comments closed.
- /050/evaluations/proj/<id>             : one project. Clean label/value HTML.

Text on project pages is in the language the proponent submitted (English or French).
"""
from __future__ import annotations

import re
from typing import Iterator

import requests
from bs4 import BeautifulSoup

from mpf.extract import extract_coordinates, extract_water_body
from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, guess_language, parse_date

BASE = "https://iaac-aeic.gc.ca"
COMMENT_URL = BASE + "/050/evaluations/participation/comment?culture=en-CA"
SITEMAP_URL = BASE + "/050/evaluations/sitemaps/projects"


def project_url(ref: str) -> str:
    return f"{BASE}/050/evaluations/proj/{ref}?culture=en-CA"


def parse_comment_list(html: str) -> dict[str, dict]:
    """Return {reference number: {title, notices: [{title, url, ends}], provinces}}."""
    soup = BeautifulSoup(html, "html.parser")
    # Map panel id -> province name from the left-hand menu.
    panel_names = {}
    menu = soup.find(id="province-list")
    if menu:
        for a in menu.find_all("a", attrs={"data-target": True}):
            badge = a.find("span")
            if badge:
                badge.extract()
            panel_names[a["data-target"]] = clean(a.get_text())
    out: dict[str, dict] = {}
    for panel in soup.find_all("section", class_="province-panel"):
        province = panel_names.get(panel.get("id", ""), UNKNOWN)
        for lead in panel.find_all("a", class_="lead", href=re.compile(r"/proj/\d+")):
            ref = re.search(r"/proj/(\d+)", lead["href"]).group(1)
            entry = out.setdefault(ref, {"title": "", "notices": [], "provinces": set()})
            if not entry["title"]:   # keep the first title seen (the "All" panel)
                entry["title"] = re.sub(r"\s*-\s*\d+$", "", clean(lead.get_text()))
            if province not in ("All", UNKNOWN):
                entry["provinces"].add(province)
            li = lead.find_parent("li")
            for sub in li.find_all("li"):
                a = sub.find("a", href=True)
                ends_div = sub.find("div")
                ends = parse_date(ends_div.get_text()) if ends_div else None
                notice = {"title": clean(a.get_text()) if a else "",
                          "url": BASE + a["href"] if a and a["href"].startswith("/") else (a["href"] if a else ""),
                          "ends": ends}
                if notice not in entry["notices"]:
                    entry["notices"].append(notice)
    for entry in out.values():
        entry["provinces"] = sorted(entry["provinces"])
    return out


def parse_sitemap_refs(xml_text: str) -> list[str]:
    return re.findall(r"/050/evaluations/proj/(\d+)", xml_text)


def _labelled_values(main) -> dict[str, str]:
    """The project page lists facts as <h3 class="h5">Label</h3>value."""
    out = {}
    for h in main.find_all("h3", class_="h5"):
        label = clean(h.get_text())
        li = h.find_parent("li")
        if not li:
            continue
        sub = li.find("ul")
        if sub:
            value = "; ".join(clean(x.get_text()) for x in sub.find_all("li"))
        else:
            value = clean(li.get_text().replace(h.get_text(), "", 1))
        out[label] = value
    return out


def parse_project_page(html: str) -> dict:
    """Pull the facts we need from one project page. Missing facts stay 'unknown'."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    h1 = main.find("h1")
    title = clean(h1.get_text()) if h1 else UNKNOWN
    summary_div = main.find("div", class_="project-summary-content")
    summary = ""
    if summary_div:
        summary = clean(summary_div.get_text(" "))
        summary = re.sub(r"^(The proposed project|Le projet proposé)\s*", "", summary)
        summary = re.sub(r"\s*(More|Plus)$", "", summary)
    latest_div = main.find("div", id="latestUpdates")
    latest = clean(latest_div.get_text(" ")) if latest_div else ""
    facts = _labelled_values(main)
    location = facts.get("Location", "")
    m = re.search(r"\(([^()]+)\)\s*$", location)
    province = m.group(1) if m else UNKNOWN
    return {
        "title": title,
        "summary": summary,
        "latest_update": latest,
        "nature": facts.get("Nature of activity", UNKNOWN) or UNKNOWN,
        "status": facts.get("Assessment status", UNKNOWN) or UNKNOWN,
        "start_date": parse_date(facts.get("Start date")),
        "proponent": facts.get("Proponent", UNKNOWN) or UNKNOWN,
        "authorities": facts.get("Authorities", UNKNOWN) or UNKNOWN,
        "assessment_type": facts.get("Assessment type", UNKNOWN) or UNKNOWN,
        "province": province,
        "location": location or UNKNOWN,
    }


def _deadline_from_text(text: str) -> str | None:
    """'Written comments to be received by November 1, 2026' and similar phrasings."""
    m = re.search(r"(?:received by|until|no later than|before|avant le|au plus tard le|jusqu.au)\s+"
                  r"([^.;]{4,40}\d{4})", text, re.IGNORECASE)
    return parse_date(m.group(1)) if m else None


class IAACSource(Source):
    name = "iaac"
    country = "CA"

    def fetch(self) -> Iterator[Item]:
        # 1. The open comment list (re-read at most once per cache period).
        try:
            comment_html = self.client.get(COMMENT_URL, list_page=True).decode("utf-8", "replace")
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
            return
        open_items = parse_comment_list(comment_html)
        self.log.append(f"{len(open_items)} projects with open comment periods")

        # 2. Newest N projects from the sitemap.
        refs = list(open_items.keys())
        n_recent = int(self.conf.get("recent_projects", 0))
        if n_recent:
            sitemap = self.client.get(SITEMAP_URL, list_page=True).decode("utf-8", "replace")
            newest = sorted({int(r) for r in parse_sitemap_refs(sitemap)}, reverse=True)[:n_recent]
            refs += [str(r) for r in newest if str(r) not in open_items]

        # 3. Each project page (fetched once, then served from cache forever).
        for ref in refs:
            url = project_url(ref)
            try:
                html = self.client.get(url).decode("utf-8", "replace")
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except requests.RequestException as e:
                self.log.append(f"skipped {ref}: {e}")
                continue
            yield self._make_item(ref, url, html, open_items.get(ref))

    def _make_item(self, ref: str, url: str, html: str, open_info: dict | None) -> Item:
        p = parse_project_page(html)
        text = "\n".join(t for t in (p["title"], p["summary"], p["latest_update"]) if t)
        lat, lon = extract_coordinates(text)

        deadline = None
        notices = []
        if open_info:
            notices = open_info["notices"]
            ends = [n["ends"] for n in notices if n["ends"]]
            deadline = max(ends) if ends else None
        if not deadline:
            deadline = _deadline_from_text(p["latest_update"])

        province = p["province"]
        if province == UNKNOWN and open_info and open_info["provinces"]:
            province = open_info["provinces"][0]

        return Item(
            source=self.name, item_id=ref, url=url, title=p["title"],
            language=guess_language(p["summary"] or text), country=self.country, province=province,
            water_body=extract_water_body(text), lat=lat, lon=lon, proponent=p["proponent"],
            project_type_raw=p["nature"], date_posted=p["start_date"], comment_deadline=deadline,
            decision_date=None, status_raw=p["status"], full_text=text,
            raw_path=str(self.client.cached_path(url) or ""),
            extra={"assessment_type": p["assessment_type"], "authorities": p["authorities"],
                   "open_comment": bool(open_info), "notices": notices, "location": p["location"]},
        )
