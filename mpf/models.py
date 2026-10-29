"""The Item record that every source produces."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

UNKNOWN = "unknown"


@dataclass
class Item:
    # Where it came from
    source: str                       # e.g. "iaac", "nl_ea", "dfo_far", "drop"
    item_id: str                      # unique within the source
    url: str
    title: str = UNKNOWN
    language: str = UNKNOWN           # "en", "fr" or "unknown"
    country: str = UNKNOWN            # ISO code, e.g. "CA"
    province: str = UNKNOWN
    water_body: str = UNKNOWN
    lat: float | None = None
    lon: float | None = None
    proponent: str = UNKNOWN
    project_type_raw: str = UNKNOWN   # the source's own category, if it has one

    # Dates are YYYY-MM-DD strings, or None when the source does not say.
    date_posted: str | None = None
    comment_deadline: str | None = None
    decision_date: str | None = None
    status_raw: str = UNKNOWN         # the source's own status words
    stage: str = UNKNOWN              # our plain-English stage, see mpf/timing.py

    full_text: str = ""               # the original-language text we score
    raw_path: str = ""                # cached copy of the original page or file
    is_test_set: bool = False         # True for the DFO CSV rows (scorer testing only)
    extra: dict = field(default_factory=dict)   # anything source-specific

    @property
    def key(self) -> str:
        return f"{self.source}:{self.item_id}"

    def to_dict(self) -> dict:
        return asdict(self)
