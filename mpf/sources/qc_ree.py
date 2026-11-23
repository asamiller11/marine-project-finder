"""Québec Registre des évaluations environnementales (www.ree.environnement.gouv.qc.ca).

Used with the Québec government's written authorization (requested 2026-11-17 from the copyright office, granted;
see config/settings.yaml). The site has no robots.txt, so nothing is disallowed.

What we read, about once a week:
- the registry's own advanced search form (resultats_recherche_avancee.asp, sent as POST like the site's form), once
  per activity in ACTIVITIES and procedure in PROCEDURES. Each result row: project name (linked), initiator,
  activity, municipality and the date the file last changed in the registry.
- each project page last changed within lookback_days: file number, opening date, region, municipality, initiator,
  activity, and the titles of the documents filed (the page has no description, so the document titles and the
  project name are the text we score). Documents themselves (PDFs) are not downloaded.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Iterator
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, today

BASE = "https://www.ree.environnement.gouv.qc.ca/"
SEARCH_URL = BASE + "resultats_recherche_avancee.asp"
ACTIVITIES = ["Port, quai et terminal portuaire", "Travaux dans des milieux humides et hydriques"]
# Southern Québec, normal and accelerated procedures (the northern procedures use other forms).
PROCEDURES = [("region;11;Me", "region;11;"), ("region;61;Pa", "region;61;")]
_DECIDED = re.compile(r"\bd[ée]cret\b|d[ée]cision du gouvernement|autorisation ministérielle d[ée]livr", re.I)
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for a in soup.find_all("a", href=re.compile(r"projet\.asp\?no_dossier=")):
        tr = a.find_parent("tr")
        if tr is None:
            continue
        cells = [clean(td.get_text(" ")) for td in tr.find_all("td")]
        dates = _ISO.findall(" ".join(cells[4:])) if len(cells) > 4 else []
        no = a["href"].split("no_dossier=")[-1].strip()
        rows.append({"id": no, "url": urljoin(BASE, a["href"]), "title": clean(a.get_text()),
                     "initiator": cells[1] if len(cells) > 1 else UNKNOWN, "activity": cells[2] if len(cells) > 2 else "",
                     "municipality": cells[3] if len(cells) > 3 else "", "changed": max(dates) if dates else None})
    return rows


def parse_project(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    lines = (soup.body or soup).get_text("\n", strip=True).split("\n")

    def after(label: str) -> str:
        # Field labels end in " :" ("Procédure :"); a section heading can carry the same word without it.
        for exact in (True, False):
            for i, line in enumerate(lines):
                hit = line.replace("\xa0", " ").rstrip().endswith(":") if exact else True
                if hit and line.rstrip(" :\xa0") == label and i + 1 < len(lines):
                    return clean(lines[i + 1])
        return ""

    region = municipality = ""
    for i, line in enumerate(lines):
        if line == "Localisation" and i + 2 < len(lines):
            region, municipality = lines[i + 1].rstrip(" :"), clean(lines[i + 2])
            break
    docs = [clean(line) for line in lines if re.match(r"^PR\d", line)]
    text = " ".join(docs)
    return {"opened": after("Date d'ouverture du dossier"), "changed": after("Dernière modification au registre"),
            "name": after("Nom du projet"), "initiator": after("Initiateur du projet"),
            "activity": after("Secteur d'activité"), "procedure": after("Procédure"), "region": region,
            "municipality": municipality, "documents": docs, "decided": bool(_DECIDED.search(text)),
            "hearing": "audience publique" in text.lower()}


class QCREESource(Source):
    name = "qc_ree"
    country = "CA"

    def fetch(self) -> Iterator[Item]:
        rows: dict[str, dict] = {}
        try:
            for activity in self.conf.get("activities", ACTIVITIES):
                for procedure, region in PROCEDURES:
                    html = self.client.post_form(SEARCH_URL, {"procedure": procedure, "region_adm": region,
                                                              "municipalite": "", "activite": activity})
                    for r in parse_results(html.decode("utf-8", "replace")):
                        rows.setdefault(r["id"], r)
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
            return
        except requests.RequestException as e:
            self.log.append(f"search failed: {e}")
            return
        cutoff = (today() - timedelta(days=int(self.conf.get("lookback_days", 540)))).isoformat()
        # "9999-99-999" is the registry's own test file.
        recent = [r for r in rows.values() if r["changed"] and r["changed"] >= cutoff and not r["id"].startswith("9999")]
        self.log.append(f"{len(rows)} projects found, {len(recent)} changed since {cutoff}")
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
        p = parse_project(page)
        title = p["name"] or row["title"]
        place = ", ".join(x for x in (p["municipality"] or row["municipality"], p["region"]) if x)
        text = clean(f"{title}. {p['activity'] or row['activity']}. {place}. " + " ".join(p["documents"]))
        return Item(
            source=self.name, item_id=row["id"], url=row["url"], title=title, language="fr", country=self.country,
            province="Quebec", proponent=p["initiator"] or row["initiator"] or UNKNOWN,
            project_type_raw=p["activity"] or row["activity"], date_posted=p["opened"] or None,
            status_raw="Décision rendue" if p["decided"] else "En évaluation",
            full_text=text, raw_path=str(self.client.cached_path(row["url"]) or ""),
            extra={"place": place, "last_changed": p["changed"] or row["changed"], "procedure": p["procedure"],
                   "decision_made": p["decided"], "public_hearing": p["hearing"], "document_count": len(p["documents"])},
        )
