"""Who is involved in a project: owner or proponent, consultant, contractor and agency contact.

Only names the source itself states are recorded, each with the words it came from. Nothing is guessed:
a role the notice does not name shows "Not named yet" (contractors are usually named only when a project
goes to bid). Read from the notice text and documents already saved, plus the registry's own fields
(the proponent field, the federal registry's Contacts box). No extra requests.

For agency contacts only the person's name, job title and office are kept. Phone numbers and email
addresses are not copied: the project page links to the official notice, which has them.
Names of people who commented on a project are never read (see mpf/comments.py).
"""
from __future__ import annotations

import html as htmllib
import re
from pathlib import Path

from mpf.models import UNKNOWN

ROLES = [
    ("owner", "Owner or proponent"),
    ("consultant", "Engineering or environmental consultant"),
    ("contractor", "Contractor"),
    ("agency_contact", "Agency contact"),
]
ROLE_LABELS = dict(ROLES)
NOT_NAMED = "Not named yet"

# Words that make a phrase an organisation name (not a person, not a sentence).
_ORG_WORD = re.compile(
    r"\b(Ltd|Limited|Inc|Incorporated|LLC|L\.L\.C|LLP|plc|PLC|GmbH|S\.A|A/S|AS|BV|N\.V|Corp|Corporation|Company|Co|Group|"
    r"Consulting|Consultants?|Engineering|Engineers|Environmental|Associates|Partners(?:hip)?|Services|Solutions|"
    r"Contracting|Contractors?|Construction|Constructors|Marine|Ingénierie|Ingénieurs|Conseil|Experts?-conseils)\b\.?")
# Firms that often appear without a company suffix.
KNOWN_FIRMS = ["Stantec", "WSP", "AECOM", "Hatch", "CBCL", "Golder", "Jacobs", "Dillon Consulting", "Tetra Tech",
               "SLR Consulting", "Englobe", "Arcadis", "Royal HaskoningDHV", "Mott MacDonald", "Atkins", "AtkinsRéalis",
               "Fugro", "RPS", "Xodus", "GHD", "Moffatt & Nichol", "Anchor QEA", "W.F. Baird", "Baird & Associates",
               "Arup", "HDR", "Kiewit", "COWI", "Ramboll", "Sweco", "ABPmer", "Intertek", "EnviroCentre", "Cura Terrae",
               "Pennecon", "Kleinfelder", "Royal Haskoning", "Tetra Tech EBA", "McNally", "Dexter", "Englobe Corp"]
_KNOWN = re.compile(r"\b(" + "|".join(re.escape(f) for f in sorted(KNOWN_FIRMS, key=len, reverse=True)) + r")\b")

# One capitalised name: words that start with a capital (or "&", "and", "of", "de", "du", "des"), at most 10.
_NAME = (r"((?:[A-ZÀ-Ý][\w&'’.\-]*|&)(?:,? (?:[A-ZÀ-Ý(][\w&'’.\-()]*|&|and|of|for|de|du|des|la|le|et)){0,9})")
# Text after a name that ends it.
_NAME_END = re.compile(r"\s+(?:with|exclusively|on behalf|for the|in |under|to |and is|is |are |has |have |\d)")


_LABEL = re.compile(r"\s+(?:Address|Telephone|Tel|Email|E-mail|Phone|Fax|Name of|Contact|Website)\b.*$")
_SENTENCE_END = re.compile(r"(?<!\bLtd)(?<!\bInc)(?<!\bCo)(?<!\bCorp)(?<!\bSt)(?<!\bDr)(?<!\bU\.S)\.\s+[A-ZÀ-Ý].*$")


def _clean_name(name: str) -> str:
    name = _NAME_END.split(name, 1)[0]
    name = _LABEL.sub("", name)
    name = re.sub(r"\)\.?\s.*$", ")", name)
    name = _SENTENCE_END.sub("", name)
    name = re.sub(r"[\s,;:(]+$", "", name)
    # Trailing lowercase joining words ("Peel Ports Group and") are not part of the name.
    name = re.sub(r"(?:\s+(?:and|of|for|de|du|des|la|le|et|&))+$", "", name)
    # A bracketed abbreviation at the end stays ("Peel Ports Group Ltd (PPG)"); an unclosed bracket goes.
    if name.count("(") > name.count(")"):
        name = name[:name.rfind("(")].rstrip()
    return name.strip(" ,.;:")


def is_org(name: str) -> bool:
    """True when the phrase reads as an organisation: a company word or a known firm, more than one letter."""
    if not name or len(name) < 3 or len(name) > 90:
        return False
    if re.match(r"(?i)^(the|a|an|this|these|its|our|their|any|all|each|such)\b", name):
        return False
    return bool(_ORG_WORD.search(name) or _KNOWN.search(name))


def _quote(text: str, start: int, end: int, width: int = 70) -> str:
    """The words a name was read from, trimmed to about one line."""
    a, b = max(0, start - 20), min(len(text), end + 10)
    snippet = re.sub(r"\s+", " ", text[a:b]).strip()
    # Never copy phone numbers or email addresses.
    snippet = re.sub(r"\S+@\S+|\(?\+?\d[\d ().\-]{6,}\d", "[…]", snippet)
    return ("…" if a else "") + snippet[: width * 2] + ("…" if b < len(text) else "")


def _entry(name: str, how: str, quote: str = "", detail: str = "") -> dict:
    return {"name": name, "detail": detail, "how": how, "quote": quote}


_SUFFIX = re.compile(r"\b(ltd|limited|inc|incorporated|llc|llp|plc|corp|corporation|company|co)\b\.?", re.I)


def name_key(name: str) -> str:
    """Compare names without brackets, company suffixes, case or punctuation ("Ltd" = "Limited")."""
    n = re.sub(r"\([^)]*\)", " ", name or "")
    n = _SUFFIX.sub(" ", n)
    return re.sub(r"[^a-z0-9à-ÿ]+", " ", n.lower()).strip()


def _add(out: dict, role: str, e: dict) -> None:
    k = name_key(e["name"])
    if not k:
        return
    for x in out[role]:
        xk = name_key(x["name"])
        if xk == k or xk.startswith(k + " ") or k.startswith(xk + " "):
            return
    if len(out[role]) < 3:
        out[role].append(e)


# --- consultant -------------------------------------------------------------------------------------------
_PREPARED = re.compile(r"\b(?:has been |was |been )?prepared by\s+" + _NAME + r"(?:\s+\([^)]{0,60}\))?"
                       r"(?:\s+(?:on behalf of|for)\s+" + _NAME + r")?")
_AGENT = re.compile(r"\b2\.1 Agent\s+(?!As per\b)" + _NAME)
_CONSULTANT_IS = re.compile(r"\b(?:consultant|consulting engineers?|engineering firm|design consultant)s?"
                            r"(?:\s*\([^)]*\))?\s*(?:,|:|is|was|has been|retained|hired|appointed)?\s*" + _NAME)
_NAMED_CONSULTANT = re.compile(_NAME + r"\s*\((?:the )?[“\"]?(?:Consultant|Engineer|Design Engineer)[”\"]?\)")
_EXCLUSIVE_FOR = re.compile(r"exclusively for the (?:benefit and )?use of\s+" + _NAME)
_PREPARED_FOR = re.compile(r"\bPrepared for\s+" + _NAME)
_CLIENT = re.compile(r"Contract with\s+" + _NAME + r"\s*\(\s*[“\"]the Client")
_NAME_OF = re.compile(r"Name of (?:the )?Proponent\s*:\s*" + _NAME)

# --- contractor -------------------------------------------------------------------------------------------
_CONTRACTOR = [
    re.compile(r"\bcontract (?:was |has been |had been )?awarded to\s+" + _NAME),
    re.compile(r"\b(?:the )?(?:general |prime |main |marine )?contractor(?: for the (?:project|work)s?)?"
               r"(?:\s+is|\s+will be|,|:)\s+" + _NAME),
    re.compile(_NAME + r",? (?:has been|was) (?:awarded the contract|retained as (?:the )?contractor|selected as (?:the )?contractor)"),
    re.compile(r"\b(?:construction|the work|the works|work) (?:will be|is being|is to be) (?:carried out|completed|undertaken|performed) by\s+" + _NAME),
    re.compile(r"\b2\.5 Contractor and Vessel Details\s+(?!MD-|MS-|\d)" + _NAME),
]

# --- agency contact ---------------------------------------------------------------------------------------
_TITLE_WORDS = r"(?!(?:Acting|Senior|Regional|Director|Manager|Head|Chief|Officer|Office|Environmental|Environment)\b)"
_PERSON = (r"((?:Dr\.? |Mr\.? |Ms\.? |Mrs\.? )?[A-ZÀ-Ý][a-zà-ÿ'’\-]+(?: [A-ZÀ-Ý]\.)?(?: " + _TITLE_WORDS +
           r"[A-ZÀ-Ý][a-zà-ÿ'’\-]+){1,2})")
_FURTHER_INFO = re.compile(r"FOR FURTHER INFORMATION CONTACT:\s*" + _PERSON + r",\s*([^,()\d]{3,80}?),\s*([A-Z][A-Za-z ]{1,40}?)\s*[,(.]")
_SUBMITTED_TO = re.compile(r"(?:must|should|may|can) be (?:submitted|sent|directed|addressed) to:?\s*" + _PERSON +
                           r",?\s+((?:Acting |Senior |Regional |A/)?[A-Z][\w ,&'’\-]{3,90}?)(?=\s+\d|\s+P\.?O\.?|\s+Email|\s+Tel|[.;])")
_PLEASE_CONTACT = re.compile(r"please contact\s+" + _PERSON + r",\s+((?:Acting )?(?:Director|Manager|Head|Chief|Coordinator|"
                             r"Officer|Analyst|Specialist|Engineer)[\w ,&'’\-]{0,90}?)(?=,? at\b|\s+\(|[.;])")
_MD_LOT = re.compile(r"Marine Directorate\s*[–-]\s*Licensing Operations Team|\bMD-LOT\b")


_WORD = r"[A-ZÀ-Ý][a-zà-ÿ'’]+(?:[A-Z][a-zà-ÿ'’]+)?(?:-[A-ZÀ-Ý][a-zà-ÿ'’]+)?"
_PERSON_LINE = re.compile(r"^(?:Dr\.? |Mr\.? |Ms\.? |Mrs\.? )?" + _WORD + r"(?: [A-ZÀ-Ý]\.)?(?: " + _WORD + r"){1,2}$")
_NOT_PERSON = re.compile(r"\b(Canada|Canadian|Harbours?|Fisheries|Department|Office|Public|Forces|Director|Directorate|Agency|"
                         r"Branch|Division|Region|Group|Services|Environment|Environmental|Box|Street|Road|Avenue|Floor|"
                         r"Place|Suite|Base|Halifax|Ministry|Government|Authority|Council|Nation|Team|Section|Unit|Centre|"
                         r"Center|Station|Relations|Media|Project|Park|Programs?|Headquarters|Review)\b")
_ADDRESS = re.compile(r"\d|\bP\.?\s?O\.? Box\b|\b(Street|Road|Avenue|Floor|Suite|Boulevard|Drive|Place|Crescent|rue|Stn)\b|"
                      r"\b[A-Z]\d[A-Z] ?\d[A-Z]\d\b", re.I)


def looks_like_person(name: str) -> bool:
    return bool(_PERSON_LINE.match((name or "").strip())) and not _NOT_PERSON.search(name)


def _iaac_contacts_from_html(html: str, title: str = "") -> list[dict]:
    """The federal registry's Contacts box: office lines, usually one 'Name, Title' line, then address, phone and
    email lines. Gives one contact: the person with their title and office, or the office alone."""
    m = re.search(r'<address id="contact-info">(.*?)</address>', html or "", re.S)
    if not m:
        return []
    lines = [re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", x))).strip() for x in re.split(r"<br\s*/?>", m.group(1))]
    lines = [x for x in lines if x and "@" not in x and not _ADDRESS.search(x) and x.lower() != (title or "").lower().strip()
             and not re.match(r"(?i)(telephone|tel|phone|fax|email|e-mail|courriel|téléphone|toll[- ]free)\b", x)]
    person, job, offices = "", "", []
    for line in lines:
        name, _, rest = (x.strip() for x in line.partition(","))
        if looks_like_person(name):
            if not person:
                person, job = name, rest
            elif name == person and not job:
                job = rest
            continue
        if line not in offices:
            offices.append(line)
    if not person and not offices:
        return []
    return [{"organization": ", ".join(offices[:2]), "name": person, "title": job}]


def parse_iaac_contacts(html: str, title: str = "") -> list[dict]:
    return _iaac_contacts_from_html(html, title)


def _saved_iaac_contacts(row: dict) -> list[dict]:
    """Older saved records have no contacts field yet: read the saved copy of the page (no request)."""
    path = row.get("raw_path") or ""
    try:
        return _iaac_contacts_from_html(Path(path).read_text(encoding="utf-8", errors="replace"), row.get("title") or "") if path else []
    except OSError:
        return []


def find_stakeholders(row: dict, extra: dict | None = None) -> dict:
    """Role -> list of {"name", "detail", "how", "quote"}. An empty list means the source names no one."""
    extra = extra or {}
    text = re.sub(r"\s+", " ", row.get("full_text") or "")
    source = row.get("source") or ""
    out = {k: [] for k, _ in ROLES}

    # Owner or proponent: the registry's own field first.
    prop = (row.get("proponent") or "").strip()
    if prop and prop != UNKNOWN:
        _add(out, "owner", _entry(prop, "Proponent named by the registry"))
    if source == "noaa_ita":
        m = re.match(r"^(?:Reissuance|Modification|Renewal|Extension) of\s+(.+?)['’]s?\s", row.get("title") or "") or \
            re.match(r"^(.+?)['’]s?\s", row.get("title") or "")
        if m and len(m.group(1)) < 90:
            _add(out, "owner", _entry(m.group(1).strip(), "Authorization holder named in the title", row.get("title") or ""))
    for rx in (_NAME_OF, _EXCLUSIVE_FOR, _PREPARED_FOR, _CLIENT):
        for m in rx.finditer(text):
            name = _clean_name(m.group(1))
            if is_org(name):
                _add(out, "owner", _entry(name, "Named in the notice documents", _quote(text, m.start(), m.end())))

    # Consultant.
    owners = {name_key(e["name"]) for e in out["owner"]}
    for m in _PREPARED.finditer(text):
        name = _clean_name(m.group(1))
        if is_org(name) and name_key(name) not in owners:
            _add(out, "consultant", _entry(name, "Prepared the project documents", _quote(text, m.start(), m.end())))
        if m.group(2):
            client = _clean_name(m.group(2))
            if is_org(client):
                _add(out, "owner", _entry(client, "Named in the notice documents", _quote(text, m.start(), m.end())))
    for rx, how in ((_AGENT, "Agent on the licence"), (_CONSULTANT_IS, "Named as consultant"),
                    (_NAMED_CONSULTANT, "Named as consultant")):
        for m in rx.finditer(text):
            name = _clean_name(m.group(1))
            if is_org(name) and name_key(name) not in owners:
                _add(out, "consultant", _entry(name, how, _quote(text, m.start(), m.end())))

    # Contractor.
    for rx in _CONTRACTOR:
        for m in rx.finditer(text):
            name = _clean_name(m.group(1))
            if is_org(name) and name_key(name) not in owners and not re.match(r"(?i)^(a|an|the) ", name):
                _add(out, "contractor", _entry(name, "Named as contractor", _quote(text, m.start(), m.end())))

    # Agency contact.
    contacts = extra.get("contacts")
    if contacts is None and source == "iaac":
        contacts = _saved_iaac_contacts(row)
    for c in contacts or []:
        if c.get("name"):
            _add(out, "agency_contact", _entry(c["name"], "Contact listed on the registry page",
                                               detail=", ".join(x for x in (c.get("title"), c.get("organization")) if x)))
        elif c.get("organization"):
            office, _, more = c["organization"].partition(", ")
            _add(out, "agency_contact", _entry(office, "Contact office listed on the registry page", detail=more))
    for m in _FURTHER_INFO.finditer(text):
        _add(out, "agency_contact", _entry(m.group(1), "Contact named in the notice",
                                           _quote(text, m.start(), m.end()), f"{m.group(2).strip()}, {m.group(3).strip()}"))
    for rx in (_SUBMITTED_TO, _PLEASE_CONTACT):
        for m in rx.finditer(text):
            _add(out, "agency_contact", _entry(m.group(1), "Contact named in the notice", _quote(text, m.start(), m.end()),
                                               re.sub(r"\s+", " ", m.group(2)).strip(" ,")))
    if not out["agency_contact"]:
        m = _MD_LOT.search(text)
        if m:
            _add(out, "agency_contact", _entry("Marine Directorate Licensing Operations Team (MD-LOT)",
                                               "Licensing team named in the notice", _quote(text, m.start(), m.end()),
                                               "Scottish Government"))
    return out


def summary_line(entries: list[dict]) -> str:
    """'AECOM Limited' or 'Not named yet'."""
    if not entries:
        return NOT_NAMED
    return "; ".join(e["name"] + (f" ({e['detail']})" if e.get("detail") else "") for e in entries)


def filled_counts(records: list[dict]) -> dict:
    """Role -> number of records with at least one name for it."""
    return {k: sum(1 for r in records if (r.get("stakeholders") or {}).get(k)) for k, _ in ROLES}
