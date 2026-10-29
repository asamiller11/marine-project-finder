"""The common interface every source follows.

To add a new source (for example Norway, Denmark or France later):
1. Make a new file in this folder with a class that subclasses Source.
2. Give it a short `name`, a `country` code, and a `fetch()` method that yields Items.
3. Register it in SOURCES in mpf/sources/__init__.py and add a block under
   `sources:` in config/settings.yaml.
The rest of the tool (scoring, review page, digest) works on Items, so it needs no changes.
"""
from __future__ import annotations

from typing import Iterator

from mpf.http import PoliteClient
from mpf.models import Item


class Source:
    name = "base"
    country = "unknown"

    def __init__(self, client: PoliteClient, settings: dict, source_settings: dict):
        self.client = client
        self.settings = settings
        self.conf = source_settings or {}
        self.log: list[str] = []      # short notes shown at the end of a run

    def fetch(self) -> Iterator[Item]:
        raise NotImplementedError
