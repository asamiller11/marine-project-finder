"""Drop folder: files you save by hand (PDF, HTML, text).

Use it for sources we do not fetch automatically (for example Quebec, after you check the
page in your browser). Put the file in the drop/ folder. Optionally add a small YAML file
with the same name plus ".yaml" to fill in facts the file itself does not show:

    drop/baie-x-quai.pdf
    drop/baie-x-quai.pdf.yaml   <- optional, for example:
        url: https://www.ree.environnement.gouv.qc.ca/...
        title: Reconstruction du quai de ...
        province: Quebec
        proponent: ...
        comment_deadline: 2026-11-30
        decision_date:
        stage: public comment open

Anything you leave out stays "unknown".
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterator

import yaml
from bs4 import BeautifulSoup

from mpf.extract import extract_coordinates, extract_water_body
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, guess_language, parse_date

SUPPORTED = {".pdf", ".html", ".htm", ".txt", ".text", ".md"}


def read_file_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from mpf.sources.nl_ea import pdf_text
        return pdf_text(path.read_bytes(), max_pages=40)
    raw = path.read_bytes().decode("utf-8", errors="replace")
    if suffix in (".html", ".htm"):
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        return clean((soup.find("main") or soup).get_text(" "))
    return clean(raw)


class DropFolderSource(Source):
    name = "drop"
    country = "unknown"

    def __init__(self, client, settings, source_settings, folder: Path):
        super().__init__(client, settings, source_settings)
        self.folder = folder

    def fetch(self) -> Iterator[Item]:
        files = sorted(p for p in self.folder.iterdir()
                       if p.suffix.lower() in SUPPORTED and not p.name.lower().startswith("readme"))
        self.log.append(f"{len(files)} files in {self.folder}")
        for path in files:
            meta_path = path.with_name(path.name + ".yaml")
            meta = {}
            if meta_path.exists():
                meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
            try:
                text = read_file_text(path)
            except Exception as e:
                self.log.append(f"could not read {path.name}: {type(e).__name__}")
                continue
            digest = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
            lat, lon = extract_coordinates(text)
            title = meta.get("title") or path.stem.replace("-", " ").replace("_", " ")
            yield Item(
                source=self.name, item_id=f"{path.name}:{digest}",
                url=meta.get("url") or path.resolve().as_uri(), title=title,
                language=meta.get("language") or guess_language(text),
                country=meta.get("country") or ("CA" if meta.get("province") else UNKNOWN),
                province=meta.get("province") or UNKNOWN,
                water_body=meta.get("water_body") or extract_water_body(text),
                lat=meta.get("lat", lat), lon=meta.get("lon", lon),
                proponent=meta.get("proponent") or UNKNOWN,
                project_type_raw=meta.get("project_type") or UNKNOWN,
                date_posted=parse_date(str(meta.get("date_posted") or "")),
                comment_deadline=parse_date(str(meta.get("comment_deadline") or "")),
                decision_date=parse_date(str(meta.get("decision_date") or "")),
                status_raw=meta.get("stage") or UNKNOWN,
                full_text=f"{title}\n{text}", raw_path=str(path.resolve()),
                extra={"sidecar": meta_path.name if meta else None},
            )
