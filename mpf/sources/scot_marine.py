"""United Kingdom, Scotland: marine.gov.scot marine licence documentation.

What we read (robots.txt allows it, with a 10 second crawl delay, which we honour):
- /data/marine-licence-documentation?items_per_page=60       newest licence documents
- /mslot-document-categories/pre-application-consultation    pre-application consultations
- each project's newest document page (/?q=node/<n>), for its stage, type and reference
- the first PDF attached to that page (the application, screening request or licence), up to max_pdf_mb, for the
  project description the one-line document title lacks. Its text is used for scoring only: Scotland shows no
  quotes (config/coverage.yaml, quotes: false).
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


# The application PDF is a form. Its printed guidance ("When a proposed future dredging level is known this must also be
# shown", "Bridges over tidal waters: ...", "shellfish harvesting areas") appears in every application and must not be
# scored. Sentences that read like form instructions are dropped; the applicant's own answers stay.
_FORM = re.compile(r"\be\.g\.|\bi\.e\.|\bplease\b|\bprovide (the|a|details|an?)\b|\bmust (also )?(be|show|include|state)\b|"
                   r"\bwhere (relevant|applicable|appropriate|the proposed)|\bif (applicable|relevant|known|yes|no)\b|"
                   r"\bshould (be|include|show)\b|\byou (must|should|will|may|need)\b|\bapplicants?\b|\bthis form\b|"
                   r"\bguidance\b|\bnotes?:|\bsee (section|note|annex|guidance)|\bactivities where\b|\bsuch as\b|"
                   r"\b(tick|delete) (as appropriate|the box)|\bthe licensing authority\b|\bwill not be (backdated|accepted)|"
                   r"\bthe licensee\b|"
                   # Headings and lists printed on every version of the form or licence.
                   r"construction of a new sea outfall|disposal of dredged substance|land reclamation,? (or )?marine/harbour|"
                   r"special areas? of conservation|licensable activit|materials to be used|maximum amounts? as specified|"
                   r"determination of a marine licence|indicate the maximum depth", re.I)


def _sentences(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text or "") if p.strip()]


def _norm_sentence(s: str) -> str:
    return re.sub(r"[\d\W_]+", " ", s.lower()).strip()


def common_sentences(texts: list[str], min_docs: int = 3) -> set[str]:
    """Sentences (normalised) that appear in at least min_docs different documents: the form's own wording."""
    from collections import Counter
    seen = Counter()
    for t in texts:
        seen.update({_norm_sentence(s) for s in _sentences(t) if len(_norm_sentence(s)) >= 12})
    return {s for s, n in seen.items() if n >= min_docs}


def strip_form_text(text: str, common: set[str] | None = None) -> str:
    """The applicant's own text from an application form: drop the form's printed instructions, and any sentence
    that appears in several other applications (headings, category lists, standard licence conditions)."""
    common = common or set()
    return "\n".join(p for p in _sentences(text) if not _FORM.search(p) and _norm_sentence(p) not in common)


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


# Species licences for a job ("European Protected Species Licence- Subsea Cable Replacement - Loch a Choire") are
# merged into that job's entry, which then lists every licence type.
_SPECIES = re.compile(r"^(European Protected Species|Basking Shark)\s+(Licen[cs]e(\s+Application)?|Application)\s*[-–:]\s*", re.I)


def is_species_licence(title: str) -> bool:
    return bool(_SPECIES.match(title or ""))


def licence_type(title: str) -> str:
    m = _SPECIES.match(title or "")
    if not m:
        return "Marine licence"
    return "European Protected Species licence" if m.group(1).lower().startswith("european") else "Basking shark licence"


def job_key(title: str) -> str:
    """The job a document belongs to, without any species-licence prefix, for merging."""
    return re.sub(r"\W+", " ", _SPECIES.sub("", project_name(title)).lower()).strip()


def merge_licences(groups: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Join groups of documents that are licences for the same job into the job's main group. A job with only
    species licences stays as it is."""
    buckets: dict[str, list[str]] = {}
    for key, docs in groups.items():
        newest = max(docs, key=lambda r: r["updated"] or "")
        buckets.setdefault(job_key(_SPECIES.sub("", newest["title"])), []).append(key)
    out: dict[str, list[dict]] = {}
    for keys in buckets.values():
        main = [k for k in keys if any(not is_species_licence(project_name(d["title"])) for d in groups[k])]
        if len(keys) > 1 and main:
            main.sort(key=lambda k: max(d["updated"] or "" for d in groups[k]), reverse=True)
            out[main[0]] = [d for k in keys for d in groups[k]]
        else:
            for k in keys:
                out[k] = groups[k]
    return out


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
    pdfs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/sites/default/files/" in href and href.lower().split("?")[0].endswith(".pdf"):
            href = href if href.startswith("http") else BASE + href
            if href not in pdfs:
                pdfs.append(href)
    return {"title": clean(h1.get_text(" ")) if h1 else "", "fields": fields, "pdfs": pdfs}


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
        groups = merge_licences(groups)
        cap = int(self.conf.get("max_projects", 40))
        ordered = sorted(groups.items(), key=lambda kv: max(r["updated"] or "" for r in kv[1]), reverse=True)[:cap]
        self.log.append(f"{sum(len(v) for v in groups.values())} documents in {len(groups)} projects; reading {len(ordered)}")
        pending = []
        for key, docs in ordered:
            docs = sorted(docs, key=lambda r: r["updated"] or "", reverse=True)
            # The job's own newest document, not a species licence merged into it.
            newest = next((d for d in docs if not is_species_licence(project_name(d["title"]))), docs[0])
            try:
                page = parse_document(self.client.get(newest["url"]).decode("utf-8", "replace"))
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            except requests.RequestException as e:
                self.log.append(f"skipped {newest['url']}: {type(e).__name__}")
                continue
            raw = ""
            if page["pdfs"] and self.conf.get("fetch_pdf", True):
                raw = self._pdf_text(page["pdfs"][0])
                if raw is None:         # blocked: stop the source
                    return
            pending.append((key, docs, page, raw))
        # Wording shared by several applications is the form, not the project: drop it before scoring.
        common = common_sentences([raw for *_, raw in pending if raw], int(self.conf.get("form_min_docs", 3)))
        for key, docs, page, raw in pending:
            yield self._make_item(key, docs, page, clean(strip_form_text(raw, common)) if raw else "")

    def _pdf_text(self, url: str) -> str | None:
        """Raw text of the first attached PDF; '' when too large or unreadable; None when the site refuses us."""
        from mpf.sources.nl_ea import pdf_text
        max_bytes = int(float(self.conf.get("max_pdf_mb", 5)) * 1024 * 1024)
        try:
            data = self.client.get(url, max_bytes=max_bytes)
        except Blocked as e:
            if "larger than" in str(e):
                self.log.append(f"PDF too large, skipped: {url}")
                return ""
            self.log.append(f"STOPPED: {e}")
            return None
        except requests.RequestException as e:
            self.log.append(f"PDF skipped {url}: {type(e).__name__}")
            return ""
        try:
            return pdf_text(data, max_pages=int(self.conf.get("max_pdf_pages", 15)))
        except Exception as e:   # a damaged or scanned PDF must not stop the run
            self.log.append(f"PDF unreadable {url}: {type(e).__name__}")
            return ""

    def _make_item(self, key: str, docs: list[dict], page: dict, pdf_txt: str = "") -> Item:
        f = page["fields"]
        newest = next((d for d in docs if not is_species_licence(project_name(d["title"]))), docs[0])
        licences = sorted({licence_type(project_name(d["title"])) for d in docs},
                          key=lambda t: (t != "Marine licence", t))
        name = project_name(newest["title"])
        stage = f.get("Stage document submitted", "")
        text = "\n".join([name] + [d["title"] + "." for d in docs] +
                         [f"{k}: {v}." for k, v in f.items() if k in ("Type of application", "Document type")] +
                         ([pdf_txt] if pdf_txt else []))
        ref = f.get("Reference") or (key if _REF.fullmatch(key) else "")
        return Item(
            source="scot_marine", item_id=ref or key[:80], url=newest["url"], title=name, language="en",
            country="GB", province="Scotland", water_body=extract_water_body(name), proponent=UNKNOWN,
            project_type_raw=f.get("Type of application", UNKNOWN), date_posted=parse_date(f.get("Date submitted")) or newest["updated"],
            comment_deadline=None, status_raw=stage_words(stage, newest["title"]), full_text=text,
            raw_path=str(self.client.cached_path(newest["url"]) or ""),
            extra={"reference": ref, "documents": [{"title": d["title"], "url": d["url"], "updated": d["updated"]} for d in docs],
                   "document_type": f.get("Document type", ""), "link_ok": True,
                   "pdf_url": page["pdfs"][0] if page.get("pdfs") else None, "used_pdf_text": bool(pdf_txt),
                   "licence_types": licences if len(licences) > 1 else []},
        )
