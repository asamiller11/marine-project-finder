"""DFO Fisheries Act Registry open dataset (CSV, Open Government Licence - Canada).

Used ONLY as a scorer test set. Every row is a project that received a Fisheries Act
authorization (2019-08 to 2023-06), so every row is a "positive" example. That means it
can tell us recall (how many we catch), never precision. Rows are marked is_test_set and
are hidden from the review page and digest.
"""
from __future__ import annotations

import csv
import io
from typing import Iterator

from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, guess_language, parse_date


def _f(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_csv(data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def row_to_item(row: dict, url: str) -> Item:
    title = clean(row.get("Title/Titre")) or UNKNOWN
    desc = clean(row.get("Project Desc/Desc du projet"))
    loc = clean(row.get("Location Desc/Autre info géo"))
    text = "\n".join(t for t in (title, desc, loc) if t)
    pid = row.get("P_C_A_01_ID") or row.get("REF_FILE_NO") or title
    return Item(
        source="dfo_far", item_id=str(pid), url=url, title=title,
        language=guess_language(desc or title), country="CA",
        province=clean(row.get("Province")) or UNKNOWN,
        water_body=clean(row.get("Watershed/Nom du Bassin")) or UNKNOWN,
        lat=_f(row.get("Latitude / Latitude")), lon=_f(row.get("Longitude / Longitude")),
        proponent=clean(row.get("Proponent(Organization)")) or UNKNOWN,
        project_type_raw=clean(row.get("Primary Work")) or clean(row.get("Main Category")) or UNKNOWN,
        date_posted=parse_date(row.get("Auth Date/Date de auth")),
        decision_date=parse_date(row.get("Auth Date/Date de auth")),
        status_raw=clean(row.get("Authorization Type")) or UNKNOWN,
        full_text=text, is_test_set=True,
        extra={"ref_file_no": row.get("REF_FILE_NO"), "dfo_status": row.get("Status"),
               "region": row.get("REGION")},
    )


class DFOFARSource(Source):
    name = "dfo_far"
    country = "CA"

    def fetch(self) -> Iterator[Item]:
        url = self.conf["csv_url"]
        try:
            data = self.client.get(url)       # one file, fetched once, cached
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
            return
        rows = parse_csv(data)
        self.log.append(f"{len(rows)} authorization rows (test set only)")
        for row in rows:
            item = row_to_item(row, url)
            item.raw_path = str(self.client.cached_path(url) or "")
            yield item
