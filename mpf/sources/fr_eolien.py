"""France: eoliennesenmer.fr, the government's offshore wind site. PROJECT WATCH ONLY.

These are project pages, not notices: there are no comment deadlines here. Items are marked
as project watch, shown in their own "Project watch (not a notice feed)" section, with no
countdown, and they never appear in the "closing soon" list.

What we read (robots.txt allows it):
- /parcs-et-projets-eoliens-en-france   the list of offshore wind parks and projects
- each project page (one level below a maritime façade)
"""
from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from mpf.extract import extract_water_body
from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean

BASE = "https://www.eoliennesenmer.fr"
LIST_URL = BASE + "/parcs-et-projets-eoliens-en-france"
FACADES = {
    "facade-manche-mer-du-nord": "Manche Est - mer du Nord",
    "facade-nord-atlantique-manche-ouest": "Nord Atlantique - Manche Ouest",
    "facade-sud-atlantique": "Sud Atlantique",
    "facade-mediterranee": "Méditerranée",
}
# Stage only from clear, unambiguous sentences about this park. Anything else is "unknown":
# mentions of past public debates, tenders or planned commissioning dates are not a current stage.
STAGES = [
    (r"\ble parc (éolien )?(en mer )?(est|a été) (mis )?en service\b", "in operation (project watch)"),
    (r"\ble parc (éolien )?(en mer )?est (actuellement )?en (cours de )?construction\b", "under construction (project watch)"),
]


def project_links(html: str) -> list[str]:
    """Project pages sit at least two levels below /facades-maritimes-en-france/."""
    soup = BeautifulSoup(html, "html.parser")
    out = set()
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"]).split("#")[0]
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "facades-maritimes-en-france":
            out.add(url)
    return sorted(out)


def parse_project(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "form"]):
        t.decompose()
    main = soup.find("main") or soup.find(id=re.compile("content", re.I)) or soup
    h1 = main.find("h1") or soup.find("h1")
    paras = [clean(p.get_text(" ")) for p in main.find_all(["p", "li"])]
    text = "\n".join(p for p in paras if len(p) > 40)
    return {"title": clean(h1.get_text(" ")) if h1 else UNKNOWN, "text": text}


def stage_for(text: str) -> str:
    for pattern, label in STAGES:
        if re.search(pattern, text, re.I):
            return label
    return UNKNOWN


class FREolienSource(Source):
    name = "fr_eolien"
    country = "FR"

    def fetch(self) -> Iterator[Item]:
        try:
            links = project_links(self.client.get(LIST_URL, list_page=True).decode("utf-8", "replace"))
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
            return
        links = links[: int(self.conf.get("max_projects", 40))]
        self.log.append(f"{len(links)} project pages")
        for url in links:
            try:
                page = parse_project(self.client.get(url).decode("utf-8", "replace"))
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except requests.RequestException as e:
                self.log.append(f"skipped {url}: {type(e).__name__}")
                continue
            if not page["text"]:
                continue
            yield self._make_item(url, page)

    def _make_item(self, url: str, page: dict) -> Item:
        parts = [p for p in urlparse(url).path.split("/") if p]
        facade = FACADES.get(parts[1], UNKNOWN) if len(parts) > 1 else UNKNOWN
        text = page["title"] + "\n" + page["text"][:15000]
        return Item(
            source="fr_eolien", item_id="/".join(parts[1:]), url=url, title=page["title"], language="fr",
            country="FR", province=facade, water_body=extract_water_body(text), proponent=UNKNOWN,
            project_type_raw="Offshore wind (project watch)", comment_deadline=None,
            status_raw=stage_for(page["text"]), full_text=text,
            raw_path=str(self.client.cached_path(url) or ""),
            extra={"watch": True, "facade": facade, "link_ok": True},
        )
