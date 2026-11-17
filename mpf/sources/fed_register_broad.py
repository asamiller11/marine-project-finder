"""United States: the Federal Register API with broader construction and restoration searches.

The existing fed_register source reads only NOAA pile-driving notices. This one runs a few more searches
(breakwaters, living shorelines, wharf and pier construction, habitat restoration plans) and keeps notices from any
agency. Public domain; robots.txt allows /api/v1/. The website's HTML pages answer automated clients with a bot check,
so only the API is used. One request per search term per run (list pages, cached 20 hours). OFF by default.
"""
from __future__ import annotations

import json
from typing import Iterator

from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.sources.fed_register import STATE_RE, search_url
from mpf.textutil import clean

DEFAULT_TERMS = ['breakwater construction', '"living shoreline"', 'wharf construction', 'pier replacement',
                 '"restoration plan" marsh']
SKIP_TYPES = {"Rule", "Proposed Rule", "Presidential Document"}   # rules (safety zones) are not projects


def parse_results(data: bytes) -> list[dict]:
    return json.loads(data.decode("utf-8")).get("results") or []


class FedRegisterBroadSource(Source):
    name = "fed_register_broad"
    country = "US"

    def fetch(self) -> Iterator[Item]:
        seen = set()
        per_page = int(self.conf.get("per_page", 10))
        for term in self.conf.get("terms") or DEFAULT_TERMS:
            try:
                results = parse_results(self.client.get(search_url(term, per_page), list_page=True))
            except Blocked as e:
                self.log.append(f"STOPPED: {e}")
                return
            for d in results:
                if d.get("document_number") in seen or d.get("type") in SKIP_TYPES:
                    continue
                seen.add(d["document_number"])
                yield self.make_item(d, term)
        self.log.append(f"{len(seen)} notices from {len(self.conf.get('terms') or DEFAULT_TERMS)} searches")

    def make_item(self, d: dict, term: str = "") -> Item:
        text = "\n".join(t for t in (clean(d.get("title") or ""), clean(d.get("abstract") or "")) if t)
        state = STATE_RE.search(text)
        agencies = "; ".join(a.get("name") or "" for a in d.get("agencies") or [] if a.get("name"))
        return Item(source=self.name, item_id=d["document_number"], url=d.get("html_url") or "",
                    title=clean(d.get("title") or UNKNOWN), language="en", country=self.country,
                    province=state.group(1) if state else UNKNOWN, proponent=agencies or UNKNOWN,
                    date_posted=d.get("publication_date"), comment_deadline=d.get("comments_close_on"),
                    status_raw=d.get("type") or UNKNOWN, full_text=text,
                    extra={"search": term, "action": d.get("action")})
