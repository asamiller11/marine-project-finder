"""Denmark: Høringsportalen, the national consultation portal (official Atom feeds).

Høringsportalen says of its feeds: "Brug dem frit i din feed-læser eller på din hjemmeside"
(use them freely in your feed reader or on your website). Each feed holds the 25 most
recently updated hearings. We read the all-hearings feed plus the feeds of marine and
coastal authorities and topics, and keep entries in one list (no detail pages needed:
each entry already states its authority, type, deadline and status).
Text is Danish; the scorer uses the Danish keyword rules.
"""
from __future__ import annotations

import html as htmlmod
import re
from typing import Iterator

from bs4 import BeautifulSoup

from mpf.extract import extract_water_body
from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, parse_date

BASE = "https://hoeringsportalen.dk/Syndication/"
DEFAULT_FEEDS = [
    "HearingsFeed",
    "HearingsByAuthorityFeed?authorityId=712",   # Kystdirektoratet
    "HearingsByAuthorityFeed?authorityId=634",   # Miljøstyrelsen
    "HearingsByAuthorityFeed?authorityId=655",   # Energistyrelsen
    "HearingsByAuthorityFeed?authorityId=656",   # Søfartsstyrelsen
    "HearingsByAuthorityFeed?authorityId=1764",  # Trafikstyrelsen
    "HearingsByAuthorityFeed?authorityId=702",   # Naturstyrelsen
    "HearingsByFormAreaFeed?formAreaId=17",      # Miljø
    "HearingsByFormAreaFeed?formAreaId=18",      # Natur og klima
    "HearingsByFormAreaFeed?formAreaId=27",      # Trafikinfrastruktur
]


def parse_feed(xml: str) -> list[dict]:
    """Entries with id, title, link, authority, type, deadline, status and description."""
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        def tag(name):
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", entry, re.S)
            return htmlmod.unescape(m.group(1)).strip() if m else ""
        link = re.search(r'<link rel="alternate" href="([^"]+)"', entry)
        content_html = tag("content")
        text = clean(BeautifulSoup(content_html, "html.parser").get_text(" "))
        fields = {}
        for label in ("Myndighed", "Type", "Høringsfrist", "Status"):
            m = re.search(label + r":\s*(.+?)(?=\s+(?:Myndighed|Type|Høringsfrist|Status):|$)", text)
            fields[label] = clean(m.group(1)) if m else ""
        description = re.split(r"\s+Myndighed:", text)[0].strip()
        url = link.group(1) if link else tag("id")
        hid = re.search(r"/Details/(\d+)", url or "")
        out.append({
            "id": hid.group(1) if hid else (url or tag("title")),
            "title": clean(tag("title")), "url": url, "updated": parse_date(tag("updated")),
            "authority": fields["Myndighed"] or clean(re.sub(r"<[^>]+>", " ", tag("author"))) or UNKNOWN,
            "type": fields["Type"] or UNKNOWN, "deadline": parse_date(fields["Høringsfrist"]),
            "status": fields["Status"] or UNKNOWN, "description": description,
        })
    return out


def entry_to_item(e: dict) -> Item:
    text = "\n".join(t for t in (e["title"], e["description"]) if t)
    return Item(
        source="dk_hoering", item_id=str(e["id"]), url=e["url"], title=e["title"] or UNKNOWN, language="da",
        country="DK", province=UNKNOWN, water_body=extract_water_body(text), proponent=e["authority"],
        project_type_raw=e["type"], date_posted=e["updated"], comment_deadline=e["deadline"],
        # "Arkiv" means the hearing is archived, not that a decision was made.
        status_raw={"Aktiv": "active", "Arkiv": "archived"}.get(e["status"], e["status"]), full_text=text,
        extra={"hearing_type": e["type"], "status_da": e["status"], "link_ok": bool(e["url"])},
    )


class DKHoeringSource(Source):
    name = "dk_hoering"
    country = "DK"

    def fetch(self) -> Iterator[Item]:
        seen = {}
        for path in self.conf.get("feeds", DEFAULT_FEEDS):
            try:
                xml = self.client.get(BASE + path, list_page=True).decode("utf-8", "replace")
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            for e in parse_feed(xml):
                seen.setdefault(e["id"], e)
        self.log.append(f"{len(seen)} distinct hearings from {len(self.conf.get('feeds', DEFAULT_FEEDS))} feeds")
        for e in seen.values():
            yield entry_to_item(e)
