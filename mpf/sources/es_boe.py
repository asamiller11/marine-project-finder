"""Spain: Boletín Oficial del Estado (BOE), open data API.

Reuse is allowed by BOE's conditions ("permiten la reutilización de los documentos ... para
fines comerciales y no comerciales"), provided the meaning is not distorted and BOE is cited
as the source. We do both: exact quotes only, and every item links to its BOE page.

What we read:
- /datosabiertos/api/boe/sumario/AAAAMMDD   the daily summary (JSON, via the documented
  Accept header), for the last few weeks. A 404 means no BOE that day.
- /diario_boe/txt.php?id=...                the official HTML text of each kept item.
  (The XML address /diario_boe/xml.php is disallowed by robots.txt, so we never use it.)
We keep Section III (other provisions, which includes impact-assessment resolutions) and
Section V-B (other official notices, which includes port authority and public-information
notices), and only titles that look marine.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Iterator

import requests
from bs4 import BeautifulSoup

from mpf.extract import extract_water_body
from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, today

SUMMARY = "https://www.boe.es/datosabiertos/api/boe/sumario/{}"
ACCEPT_JSON = {"Accept": "application/json"}
SECTIONS = ("III.", "V. Anuncios. - B.")
# A title must mention a marine setting or a marine authority to be kept.
MARINE = re.compile(r"\bpuerto|\bportuari|\bcostas?\b|\blitoral|\bplaya|\bdársena|\bmuelle|\bdique|\bdragado|"
                    r"\bmarin[oa]s?\b|\bmarítimo|\beólic[oa] marin|\bacuicultura|\bemisario|\bbahía|\bría\b|"
                    r"\bdominio público marítimo", re.I)
KINDS = re.compile(r"impacto ambiental|información pública|Autoridad Portuaria|Puertos del Estado|concesión|Costas", re.I)


def walk_summary(data: dict) -> list[dict]:
    """Flatten the summary into items with their section and department names."""
    out = []
    diarios = data.get("data", {}).get("sumario", {}).get("diario", [])
    for diario in diarios if isinstance(diarios, list) else [diarios]:
        for sec in _list(diario.get("seccion")):
            sec_name = clean(sec.get("nombre", ""))
            for dep in _list(sec.get("departamento")):
                dep_name = clean(dep.get("nombre", ""))
                for holder in [dep] + _list(dep.get("epigrafe")):
                    for it in _list(holder.get("item")):
                        if isinstance(it, dict) and it.get("identificador"):
                            out.append({"id": it["identificador"], "title": clean(it.get("titulo", "")),
                                        "url_html": it.get("url_html") or "", "section": sec_name,
                                        "department": dep_name})
    return out


def _list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def wanted(it: dict) -> bool:
    if not it["section"].startswith(SECTIONS):
        return False
    blob = it["title"] + " " + it["department"]
    return bool(MARINE.search(blob)) and bool(KINDS.search(blob))


def item_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find(id="textoxslt") or soup.find(id="DOdocText") or soup.find("main") or soup
    for t in node(["script", "style"]):
        t.decompose()
    return clean(node.get_text("\n"))


def stage_for(it: dict) -> str:
    t = it["title"].lower()
    if "formula declaración de impacto ambiental" in t or "formula informe de impacto ambiental" in t:
        return "decision made (environmental impact statement issued)"
    if "información pública" in t:
        return "public information period (dates in the notice)"
    if "aprobación" in t or "otorga" in t:
        return "decision made"
    return UNKNOWN


class ESBOESource(Source):
    name = "es_boe"
    country = "ES"

    def fetch(self) -> Iterator[Item]:
        days = int(self.conf.get("lookback_days", 21))
        cap = int(self.conf.get("max_items", 60))
        found, read_days = [], 0
        for n in range(days):
            day = today() - timedelta(days=n)
            if day.weekday() == 6:      # no BOE on Sundays
                continue
            try:
                payload = self.client.get(SUMMARY.format(day.strftime("%Y%m%d")), headers=ACCEPT_JSON)
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except requests.HTTPError:
                continue                # 404: no BOE that day
            read_days += 1
            for it in walk_summary(json.loads(payload)):
                if wanted(it):
                    it["date"] = day.isoformat()
                    found.append(it)
        self.log.append(f"{read_days} daily summaries read, {len(found)} marine items in Sections III and V-B")
        for it in found[:cap]:
            if not it["url_html"]:
                continue
            try:
                text = item_text(self.client.get(it["url_html"]).decode("utf-8", "replace"))
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except requests.RequestException as e:
                self.log.append(f"skipped {it['id']}: {type(e).__name__}")
                continue
            yield self._make_item(it, text)

    def _make_item(self, it: dict, text: str) -> Item:
        full = it["title"] + "\n" + text[:20000]
        return Item(
            source="es_boe", item_id=it["id"], url=it["url_html"], title=it["title"], language="es", country="ES",
            # The department is the BOE section the notice appears under, not the applicant.
            province=UNKNOWN, water_body=extract_water_body(full), proponent=UNKNOWN,
            project_type_raw=it["section"], date_posted=it["date"], comment_deadline=None,
            status_raw=stage_for(it), full_text=full, raw_path=str(self.client.cached_path(it["url_html"]) or ""),
            extra={"section": it["section"], "department": it["department"], "link_ok": bool(text)},
        )
