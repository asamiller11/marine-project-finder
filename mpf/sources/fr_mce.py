"""France: prescribed biodiversity compensation measures (Cerema), "France entière", on data.gouv.fr.

Licence Ouverte 2.0 (commercial reuse allowed, with the source and date). robots.txt allows the data.gouv.fr API.
The dataset page lists a GeoPackage each month (about 56 MB). A GeoPackage is an SQLite file, so Python reads it
without new packages.

What we read: the dataset's API record (1 request, list page), then the newest GeoPackage only when the month
changes (1 large request, size-capped by max_mb). We keep measures near the sea (commune or text naming a coastal
or marine setting) and turn each into an Item: what was required, for which project, where, when.
OFF by default: the monthly file is a bulk download, so the owner decides.
"""
from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator

from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean

DATASET_API = ("https://www.data.gouv.fr/api/1/datasets/"
               "mesures-compensatoires-prescrites-des-atteintes-a-la-biodiversite-france-entiere/")
MARINE = re.compile(r"\b(mer|marin|maritime|littoral|côti|estran|herbier|zostère|port|portuaire|estuaire|lagune|"
                    r"baie|récif|dune|salin|vasière|pré salé|posidonie)\w*", re.I)


def newest_gpkg(dataset_json: bytes) -> dict | None:
    d = json.loads(dataset_json.decode("utf-8"))
    files = [r for r in d.get("resources") or [] if (r.get("format") or "").lower() == "gpkg"]
    files.sort(key=lambda r: r.get("created_at") or r.get("last_modified") or "", reverse=True)
    return files[0] if files else None


def read_measures(path: Path) -> list[dict]:
    """Rows of the first feature table, as dicts (geometry left out)."""
    con = sqlite3.connect(str(path))
    try:
        tables = [r[0] for r in con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features'")]
        if not tables:
            return []
        cur = con.execute(f'SELECT * FROM "{tables[0]}"')
        cols = [c[0] for c in cur.description]
        return [{c: v for c, v in zip(cols, row) if not isinstance(v, (bytes, bytearray))} for row in cur]
    finally:
        con.close()


def _pick(row: dict, *names: str) -> str:
    low = {k.lower(): v for k, v in row.items()}
    for n in names:
        for k, v in low.items():
            if n in k and v not in (None, ""):
                return clean(str(v))
    return ""


class FRMCESource(Source):
    name = "fr_mce"
    country = "FR"

    def fetch(self) -> Iterator[Item]:
        try:
            res = newest_gpkg(self.client.get(DATASET_API, list_page=True))
            if not res:
                self.log.append("no GeoPackage listed")
                return
            max_bytes = int(float(self.conf.get("max_mb", 80)) * 1024 * 1024)
            data = self.client.get(res["url"], max_bytes=max_bytes)
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
            return
        with tempfile.NamedTemporaryFile(suffix=".gpkg") as f:
            f.write(data)
            f.flush()
            rows = read_measures(Path(f.name))
        kept = 0
        for row in rows:
            item = self.make_item(row, res["url"])
            if item:
                kept += 1
                yield item
        self.log.append(f"{len(rows)} measures in {res.get('title')}, {kept} with a marine or coastal setting")

    def make_item(self, row: dict, file_url: str = "") -> Item | None:
        project = _pick(row, "projet", "nom_proj", "intitule")
        measure = _pick(row, "mesure", "description", "libelle", "nature")
        commune = _pick(row, "commune", "nom_com")
        text = "\n".join(t for t in (project, measure, _pick(row, "milieu", "habitat", "type")) if t)
        if not MARINE.search(text + " " + commune):
            return None
        ident = _pick(row, "id_mesure", "id", "fid") or str(abs(hash(text)))
        return Item(source=self.name, item_id=ident, url=_pick(row, "url", "lien") or DATASET_API.replace("/api/1", ""),
                    title=(f"{project}: {measure}" if project else measure)[:300] or UNKNOWN, language="fr",
                    country=self.country, province=_pick(row, "departement", "dept", "region") or UNKNOWN,
                    proponent=_pick(row, "maitre", "porteur", "beneficiaire") or UNKNOWN,
                    date_posted=(_pick(row, "date_prescription", "date_acte", "date") or None),
                    status_raw="compensation measure prescribed", full_text=text,
                    extra={"commune": commune, "file": file_url})
