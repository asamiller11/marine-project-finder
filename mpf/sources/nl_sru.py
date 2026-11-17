"""Netherlands: Officiële bekendmakingen (official notices) through the SRU search interface.

The repository host's robots.txt disallows all robots, so this source may run only under exception (b) of the source
rule: the national open-data portal publishes the dataset as CC0 with a documented SRU interface and a manual
(https://data.overheid.nl/dataset/officiele-bekendmakingen). The exception is written in config/settings.yaml under
sources.nl_sru.robots_exception; without it every request is refused. A 401, 403 or 429 still stops the source.

What we read: one SRU search per term, notices modified in the last lookback_days, newest records only
(maximumRecords). Title, authority, type and date come from the record; the text we score is the title (Dutch
notices put the whole description in the title). OFF by default.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterator
from urllib.parse import quote
from xml.etree import ElementTree as ET

from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, today

BASE = "https://repository.overheid.nl/sru"
NS = {"sru": "http://docs.oasis-open.org/ns/search-ws/sruResponse", "dcterms": "http://purl.org/dc/terms/",
      "overheid": "http://standaarden.overheid.nl/owms/terms/"}
DEFAULT_TERMS = ["kademuur", "damwand haven", "zeewering", "oeverbescherming", "steiger haven", "baggeren haven"]


def search_url(term: str, since: str, n: int) -> str:
    q = f'c.product-area==officielepublicaties AND cql.textAndIndexes="{term}" AND dt.modified>={since}'
    return f"{BASE}?query={quote(q)}&maximumRecords={n}"


def parse_records(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for rec in root.iter("{%s}record" % NS["sru"]):
        g = lambda path: (rec.find(path, NS).text or "").strip() if rec.find(path, NS) is not None else ""
        ident = g(".//dcterms:identifier")
        link = ""
        hv = rec.find(".//dcterms:hasVersion", NS)
        if hv is not None:
            link = hv.get("resourceIdentifier") or ""
        out.append({"id": ident, "title": clean(g(".//dcterms:title")), "authority": g(".//overheid:authority"),
                    "type": g(".//dcterms:type"), "modified": g(".//dcterms:modified"),
                    "url": link or (f"https://zoek.officielebekendmakingen.nl/{ident}.html" if ident else "")})
    return out


class NLSRUSource(Source):
    name = "nl_sru"
    country = "NL"

    def fetch(self) -> Iterator[Item]:
        since = (today() - timedelta(days=int(self.conf.get("lookback_days", 30)))).isoformat()
        n = int(self.conf.get("max_records", 50))
        seen = set()
        for term in self.conf.get("terms") or DEFAULT_TERMS:
            try:
                recs = parse_records(self.client.get(search_url(term, since, n), list_page=True))
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            for r in recs:
                if r["id"] and r["id"] not in seen:
                    seen.add(r["id"])
                    yield self.make_item(r)
        self.log.append(f"{len(seen)} notices modified since {since}")

    def make_item(self, r: dict) -> Item:
        return Item(source=self.name, item_id=r["id"], url=r["url"], title=r["title"][:300] or UNKNOWN,
                    language="nl", country=self.country, proponent=r["authority"] or UNKNOWN,
                    date_posted=r["modified"] or None, status_raw=r["type"] or UNKNOWN, full_text=r["title"],
                    extra={"authority": r["authority"]})
