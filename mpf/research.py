"""Developer and owner research: officially published facts that help a supplier decide whether and how to pursue a
project, kept in research/projects.yaml and research/organizations.yaml (checked by a person, every fact with its
source link).

What is kept: owner or developer, consultants and contractors an official source names; each organisation's website and
general or business-development email and phone; procurement routes (supplier portals, tenders, meet-the-buyer events);
size, foundation or structure type, timeline, consenting status and biodiversity commitments. People only when an
official source names them with their role, linked to that source. Nothing from LinkedIn or other personal-data sites,
no guessed emails.

Pages are read with the same polite client as every source (robots.txt obeyed, crawl delays, hard stop on 401, 403 and
429; see docs/source_rules.md).

The weekly update (mpf/update.py) calls weekly_check():
  - new Strong or Possible projects with no research entry go into private/research_queue.md, to be researched;
  - the saved developer pages of Strong projects are re-read (at most once a week each); when a page changed and its
    new text mentions procurement or contractors, that goes into the queue too. Nothing is published automatically.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

import yaml

from mpf.config import ROOT

RESEARCH_DIR = ROOT / "research"
PROJECTS_FILE = RESEARCH_DIR / "projects.yaml"
ORGS_FILE = RESEARCH_DIR / "organizations.yaml"
QUEUE_FILE = ROOT / "private" / "research_queue.md"

# Kinds of fact, in the order the card shows them. Procurement routes come first: the best way for a supplier to get in.
FACT_KINDS = {
    "procurement": "Procurement route",
    "contractor": "Contractors and suppliers chosen",
    "structure": "Size and structure",
    "timeline": "Timeline",
    "consent": "Consenting status",
    "biodiversity": "Environmental and biodiversity commitments",
    "contact": "Named project contact",
}
PROCUREMENT = re.compile(
    r"supplier (portal|registration|database|register)|supply chain (portal|event|day|registration|opportunit)|"
    r"meet the buyer|procurement|tenders?\b|invitation to tender|\bITT\b|expressions? of interest|pre-?qualification|"
    r"bid opportunit|bids? (and|&) tenders|CanadaBuys|Find a Tender|Public Contracts Scotland|SAM\.gov|"
    r"contract notice|request for (proposals?|quotations?)|\bRFP\b|\bRFQ\b", re.I)
CONTRACTOR = re.compile(r"contract (was |has been )?awarded|awarded (the |a )?contract|appointed|selected as|"
                        r"preferred (bidder|supplier)|\bEPCI?\b|main contractor|framework agreement", re.I)
STRUCTURE = re.compile(r"\b\d[\d,.]*\s?(MW|GW|turbines?|m\b|metres?|km|hectares?|ha\b)|monopiles?|jacket|gravity[- ]base|"
                       r"floating|suction buckets?|fixed[- ]bottom|scour protection|rock (placement|armour)", re.I)
TIMELINE = re.compile(r"construction (is |will |to )?(start|begin|commence)|first power|commissioning|operational by|"
                      r"\b20(2[6-9]|3\d)\b", re.I)
CONSENT = re.compile(r"\bconsent(ed)?\b|section 36|marine licen[cs]e|scoping|EIA ?R?\b|environmental (impact )?assessment|"
                     r"planning (application|permission)|approved|determination", re.I)
BIODIVERSITY = re.compile(r"biodiversity|nature[- ](positive|inclusive)|net gain|habitat (creation|enhancement)|"
                          r"\breefs?\b|eco-?engineer|marine (life|ecology)", re.I)
SIGNALS = {"procurement": PROCUREMENT, "contractor": CONTRACTOR, "structure": STRUCTURE, "timeline": TIMELINE,
           "consent": CONSENT, "biodiversity": BIODIVERSITY}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d ()\-]{8,}\d")

# Roles worth searching for on LinkedIn ("Find contacts"). The site only links to LinkedIn's own people search; it
# stores nothing from it.
CONTACT_ROLES = ["environmental manager", "project manager", "procurement"]


def find_contacts_url(org: str) -> str:
    """LinkedIn people search for an organisation and the roles a supplier would approach."""
    q = f'"{org}" ' + " OR ".join(f'"{r}"' for r in CONTACT_ROLES)
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote_plus(q)


def page_text(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text) if 20 <= len(s.strip()) <= 400]


def scan(text: str, kinds: tuple = tuple(SIGNALS)) -> dict:
    """Kind -> up to 4 sentences that mention it (for a person to read; never published as is)."""
    out = {}
    for s in sentences(text):
        for k in kinds:
            if SIGNALS[k].search(s) and len(out.setdefault(k, [])) < 4 and s not in out[k]:
                out[k].append(s)
    return out


def org_contacts(text: str) -> dict:
    """Emails and phone numbers printed on an organisation's own page (general or business addresses only: a person
    reviews these before they are saved)."""
    return {"emails": sorted(set(m.group(0) for m in _EMAIL.finditer(text)))[:6],
            "phones": sorted(set(m.group(0).strip() for m in _PHONE.finditer(text)))[:4]}


# --- the research files ----------------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def load_research() -> tuple[dict, dict]:
    return load_yaml(PROJECTS_FILE), load_yaml(ORGS_FILE)


def problems(projects: dict, orgs: dict) -> list[str]:
    """Rule checks for the research files (health check and tests): every fact and every named person has a source
    link; no LinkedIn or personal-data sites as sources; developer-only projects carry the facts the site needs."""
    out = []
    bad_host = re.compile(r"linkedin\.com|rocketreach|zoominfo|apollo\.io|lusha|signalhire|contactout", re.I)

    def need_url(where: str, d: dict):
        url = (d or {}).get("source") or ""
        if not re.match(r"https?://", url):
            out.append(f"{where}: no source link")
        elif bad_host.search(url):
            out.append(f"{where}: source is a personal-data site")

    for key, p in (projects or {}).items():
        for role in ("owner", "consultant", "contractor"):
            for i, e in enumerate(p.get(role) or []):
                need_url(f"{key} {role} {i + 1}", e)
        for i, f in enumerate(p.get("facts") or []):
            if f.get("kind") not in FACT_KINDS:
                out.append(f"{key} fact {i + 1}: unknown kind {f.get('kind')!r}")
            need_url(f"{key} fact {i + 1}", f)
        for i, c in enumerate(p.get("people") or []):
            if not c.get("role"):
                out.append(f"{key} person {i + 1}: no role")
            need_url(f"{key} person {i + 1}", c)
        if p.get("developer_only"):
            for k in ("title", "country", "url", "full_text"):
                if not p.get(k):
                    out.append(f"{key}: developer-only project without {k}")
    for name, o in (orgs or {}).items():
        need_url(f"organisation {name}", o)
        for e in o.get("emails") or []:
            if re.match(r"(?i)^[a-z]+\.[a-z]+@", e.get("address", "")) and not e.get("published_for"):
                out.append(f"organisation {name}: {e.get('address')} looks personal; say what it is published for")
    return out


# --- weekly check ----------------------------------------------------------------------------------------

def _state_path(settings: dict) -> Path:
    from mpf.config import data_dir
    return data_dir(settings) / "research_state.json"


def weekly_check(settings: dict, records: list[dict], client=None, today: date | None = None, log=print) -> dict:
    """Queue new Strong/Possible projects for research and re-read saved developer pages of Strong projects."""
    from mpf.http import Blocked, PoliteClient
    from mpf.config import data_dir
    today = today or date.today()
    projects, _ = load_research()
    state_path = _state_path(settings)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    queue = []
    listed = lambda r: r.get("tier") in ("strong", "possible") or any(
        (p or {}).get("tier") in ("strong", "possible") for p in (r.get("profiles") or {}).values())
    for r in records:
        if listed(r) and r["id"] not in projects and not r.get("developer_only"):
            queue.append(f"- NEW, research it: [{r['title']}]({r['url']}) ({r['id']}, {r.get('tier_label', '')})")
    client = client or PoliteClient(settings, data_dir(settings) / "cache")
    strong = {r["id"] for r in records if r.get("tier") == "strong" or any(
        (p or {}).get("tier") == "strong" for p in (r.get("profiles") or {}).values())}
    pages, read, stopped = [], 0, []
    for key in sorted(strong):
        pages += [(key, u) for u in (projects.get(key) or {}).get("watch_pages") or []]
    for key, url in pages:
        try:
            html = client.get(url, list_page=True).decode("utf-8", "replace")
        except Blocked as e:
            stopped.append(f"{url}: {e}")
            continue
        except Exception as e:        # a developer page that fails is reported, never retried another way
            stopped.append(f"{url}: {e.__class__.__name__}")
            continue
        read += 1
        text = page_text(html)
        h = hashlib.sha256(text.encode()).hexdigest()
        old = state.get(url, {})
        if old.get("hash") and old["hash"] != h:
            hits = scan(text, ("procurement", "contractor"))
            new = [s for k in hits for s in hits[k] if s not in (old.get("seen") or [])]
            if new:
                queue.append(f"- CHANGED, check for procurement or contractor news: {url} ({key})\n" +
                             "".join(f"  - \"{s[:200]}\"\n" for s in new[:4]))
        seen = [s for v in scan(text, ("procurement", "contractor")).values() for s in v]
        state[url] = {"hash": h, "checked": today.isoformat(), "seen": seen}
    state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    QUEUE_FILE.parent.mkdir(exist_ok=True)
    head = (f"# Research queue ({today.isoformat()})\n\nWritten by the weekly update. Nothing here is on the site until "
            f"a person checks it and adds it to research/projects.yaml with its source link.\n\n")
    body = "\n".join(queue) if queue else "Nothing new this week."
    if stopped:
        body += "\n\n## Pages not read (blocked or failed; not retried)\n" + "\n".join(f"- {s}" for s in stopped)
    QUEUE_FILE.write_text(head + body + "\n", encoding="utf-8")
    log(f"Research check: {read} developer pages re-read, {len(queue)} items queued in {QUEUE_FILE.name}"
        + (f", {len(stopped)} pages not read" if stopped else ""))
    return {"queued": len(queue), "read": read, "stopped": stopped}
