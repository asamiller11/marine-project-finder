"""United Kingdom, Scotland: marine.gov.scot marine licence documentation.

What we read (robots.txt allows it, with a 10 second crawl delay, which we honour):
- /data/marine-licence-documentation?items_per_page=60       newest licence documents
- /mslot-document-categories/pre-application-consultation    pre-application consultations
- each project's newest document page (/?q=node/<n>), for its stage, type and reference
Several documents usually belong to one licence (same reference number), so we group them
into one item per licence or project and keep the newest document's details.
"""
from __future__ import annotations

import re
from typing import Iterator

import requests
from bs4 import BeautifulSoup

from mpf.extract import extract_water_body
from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, parse_date

BASE = "https://marine.gov.scot"
LISTINGS = ["/data/marine-licence-documentation?items_per_page=60",
            "/mslot-document-categories/pre-application-consultation"]
_REF = re.compile(r"\b(\d{8})\b")
_DOC_WORDS = re.compile(r"^(Marine Licence( Variation| Application)?|Consultation (Representations|Responses?)|"
                        r"Pre-Application Consultation Report|Application( Supporting Documents)?|Licence|Scoping (Report|Opinion)|"
                        r"Consent Decision|Public Notice)\s*[-–:]\s*", re.I)


def parse_listing(html: str) -> list[dict]:
    """Rows with title, updated date and document link."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table tbody tr") or soup.select(".views-row")
    out = []
    for r in rows:
        a = r.find("a", href=True)
        if not a:
            continue
        title = clean(a.get_text(" "))
        date_text = clean(r.get_text(" ").replace(a.get_text(" "), " "))
        href = a["href"] if a["href"].startswith("http") else BASE + a["href"]
        out.append({"title": title, "url": href, "updated": parse_date(date_text)})
    return out


def project_key(title: str) -> str:
    """Same licence reference = same project. Otherwise the title without the document-type words."""
    m = _REF.search(title)
    if m:
        return m.group(1)
    t = _DOC_WORDS.sub("", title)
    return re.sub(r"\W+", " ", t.lower()).strip()


def project_name(title: str) -> str:
    t = _DOC_WORDS.sub("", title)
    t = re.sub(r"\s*[-–]\s*(Marine Licence Granted|Licence Granted|\d{8})\b.*$", "", t)
    return clean(t) or clean(title)


def parse_document(html: str) -> dict:
    """Label: value fields on a document page (Document type, Stage, Type of application, ...)."""
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for f in soup.select(".field"):
        label = f.select_one(".field-label, .field__label")
        if not label:
            continue
        key = clean(label.get_text(" ")).rstrip(":")
        value = clean(f.get_text(" ").replace(label.get_text(" "), " ", 1))
        if key and value and key not in fields:
            fields[key] = value
    h1 = soup.find("h1")
    return {"title": clean(h1.get_text(" ")) if h1 else "", "fields": fields}


def stage_words(stage: str, title: str) -> str:
    s = (stage + " " + title).lower()
    if "granted" in s or s.startswith("licence") or "consent decision" in s:
        return "decision made"
    if "pre-application" in s:
        return "pre-application consultation"
    if "consultation" in s or "representation" in s:
        return "in progress (consultation stage)"
    if "application" in s:
        return "in progress (application)"
    return stage or UNKNOWN


class ScotMarineSource(Source):
    name = "scot_marine"
    country = "GB"

    def fetch(self) -> Iterator[Item]:
        groups: dict[str, list[dict]] = {}
        for path in LISTINGS:
            try:
                html = self.client.get(BASE + path, list_page=True).decode("utf-8", "replace")
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except requests.RequestException as e:
                self.log.append(f"listing skipped ({path}): {type(e).__name__}")
                continue
            for row in parse_listing(html):
                groups.setdefault(project_key(row["title"]), []).append(row)
        cap = int(self.conf.get("max_projects", 40))
        ordered = sorted(groups.items(), key=lambda kv: max(r["updated"] or "" for r in kv[1]), reverse=True)[:cap]
        self.log.append(f"{sum(len(v) for v in groups.values())} documents in {len(groups)} projects; reading {len(ordered)}")
        for key, docs in ordered:
            docs = sorted(docs, key=lambda r: r["updated"] or "", reverse=True)
            newest = docs[0]
            try:
                page = parse_document(self.client.get(newest["url"]).decode("utf-8", "replace"))
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except requests.RequestException as e:
                self.log.append(f"skipped {newest['url']}: {type(e).__name__}")
                continue
            yield self._make_item(key, docs, page)

    def _make_item(self, key: str, docs: list[dict], page: dict) -> Item:
        f = page["fields"]
        newest = docs[0]
        name = project_name(newest["title"])
        stage = f.get("Stage document submitted", "")
        text = "\n".join([name] + [d["title"] + "." for d in docs] +
                         [f"{k}: {v}." for k, v in f.items() if k in ("Type of application", "Document type")])
        ref = f.get("Reference") or (key if _REF.fullmatch(key) else "")
        return Item(
            source="scot_marine", item_id=ref or key[:80], url=newest["url"], title=name, language="en",
            country="GB", province="Scotland", water_body=extract_water_body(name), proponent=UNKNOWN,
            project_type_raw=f.get("Type of application", UNKNOWN), date_posted=parse_date(f.get("Date submitted")) or newest["updated"],
            comment_deadline=None, status_raw=stage_words(stage, newest["title"]), full_text=text,
            raw_path=str(self.client.cached_path(newest["url"]) or ""),
            extra={"reference": ref, "documents": [{"title": d["title"], "url": d["url"], "updated": d["updated"]} for d in docs],
                   "document_type": f.get("Document type", ""), "link_ok": True},
        )
