"""The review file, review/review.csv. Every scored item is published automatically;
this file lets you mark items you have checked.

The "publish" column:
  yes    shown with a "Reviewed by a person" badge
  no     hidden everywhere
  blank  shown with an "Automatic, not yet reviewed" badge

`python -m mpf review` writes or refreshes the file. Your publish choices and edited
summaries are kept; scores, tiers, stages and deadlines are refreshed.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path

from mpf.extract import largest_sizes
from mpf.models import UNKNOWN
from mpf.tiers import TIER_LABELS, tier_for

COLUMNS = ["id", "title", "country", "score", "tier", "publish", "summary", "auto_summary",
           "stage", "comment_deadline", "province", "url"]

# Earlier versions pre-filled "summary" with the automatic text, which always starts like this.
# Such text is not yours, so it is treated as blank (the site then uses the current automatic text).
_AUTO_TEXT = re.compile(r"^(Project (by .+? )?in .+?|Notice( from .+?)?( about .+?)?)\.( The (notice|page) mentions .+\.)?$")


def own_summary(row: dict | None) -> str:
    """The summary YOU wrote in the review file, or '' if the column is blank or automatic."""
    s = ((row or {}).get("summary") or "").strip()
    return "" if not s or _AUTO_TEXT.match(s) else s
YES = {"yes", "y", "1", "true", "x"}
NO = {"no", "n", "0", "false"}


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


_TYPE_WORDS = {
    "port_terminal": "port or terminal", "wharf_dock": "wharf or dock", "breakwater_jetty": "breakwater or jetty",
    "dredging": "dredging", "shoreline_protection": "shoreline protection", "marina_small_craft": "small craft harbour",
    "infill_causeway": "infill or causeway", "offshore_wind": "offshore wind", "piles": "pile",
}


# Titles of notices that are not construction projects: laws, regulations, funding schemes, guidance.
_NOT_A_PROJECT = re.compile(
    # Whole words (English, Spanish, French)
    r"\b(regulations?|act|bill|by-?law|policy|strategy|guidance|guidelines|funding|grants?|"
    r"convenio|ley|real decreto|reglamento|subvenci\w*|ayudas|licencia para la prestación|"
    r"décret|arrêté|loi|subvention|appel à projets)\b"
    # Danish, Norwegian and Dutch words also inside compounds ("Miljøskadebekendtgørelse",
    # "kystbeskyttelsesloven"), so no word boundary in front.
    r"|bekendtgørelse|lovforslag|vejledning|pulje|tilskud|forskrift|veileder|regeling|subsidie|besluit"
    r"|loven\b|\blov om\b|\bwet\b", re.IGNORECASE)


def is_construction_project(row: dict) -> bool:
    return not _NOT_A_PROJECT.search(row.get("title") or "")


def auto_summary(row: dict, noun: str = "notice") -> str:
    """A factual starting summary built only from what the tool extracted.
    It says what the notice MENTIONS, never what will happen. Edit it in the review file.
    Laws, regulations and funding schemes start with "Notice from", not "Project by"."""
    facts = json.loads(row.get("facts") or "{}")
    who = row.get("proponent") or UNKNOWN
    where_parts = [p for p in (row.get("water_body"), row.get("province")) if p and p != UNKNOWN]
    where = ", ".join(where_parts) if where_parts else "an unknown location"
    if not is_construction_project(row):
        place = f" about {where}" if where_parts else ""
        first = f"Notice from {who}{place}." if who != UNKNOWN else f"Notice{place or ' from an unknown authority'}."
    else:
        first = f"Project by {who} in {where}." if who != UNKNOWN else f"Project in {where}."
    types = [_TYPE_WORDS[t] for t in facts.get("types", []) if t in _TYPE_WORDS]
    second = f" The {noun} mentions {_join(types)} work" if types else ""
    big = largest_sizes(facts.get("sizes", []))
    size_bits = []
    if "volume_m3" in big:
        size_bits.append(f"about {big['volume_m3']:,.0f} m3 of material")
    if "area_m2" in big:
        size_bits.append(f"an area of about {big['area_m2']:,.0f} m2")
    if "length_m" in big and not size_bits:
        size_bits.append(f"a length of about {big['length_m']:,.0f} m")
    if second and size_bits:
        second += f" and {_join(size_bits)}."
    elif second:
        second += "."
    elif size_bits:
        second = f" The {noun} mentions {_join(size_bits)}."
    return first + second


def read_review(path: Path) -> dict[str, dict]:
    """Rows by id. Files from before tiers existed used "no" as the default for every
    row, so in those files "no" means "not reviewed" and is read as blank."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        old_format = "tier" not in (reader.fieldnames or [])
        rows = {}
        for row in reader:
            if not row.get("id"):
                continue
            if old_format and (row.get("publish") or "").strip().lower() in NO:
                row["publish"] = ""
            rows[row["id"]] = row
        return rows


def decision(row: dict | None) -> str:
    """'yes', 'no' or '' (not reviewed)."""
    value = ((row or {}).get("publish") or "").strip().lower()
    return "yes" if value in YES else "no" if value in NO else ""


def approved_ids(path: Path) -> list[str]:
    return [i for i, row in read_review(path).items() if decision(row) == "yes"]


def write_review(conn: sqlite3.Connection, path: Path, tier_conf: dict, enabled: set | None = None) -> dict:
    """Write the review file with every scored item from the sources that are switched on."""
    old = read_review(path)
    rows = conn.execute("SELECT * FROM items WHERE is_test_set=0 AND score IS NOT NULL "
                        "ORDER BY score DESC").fetchall()
    if enabled is not None:
        rows = [r for r in rows if r["source"] in enabled]
    out, new, seen = [], 0, set()
    for r in rows:
        r = dict(r)
        seen.add(r["key"])
        prev = old.get(r["key"], {})
        if not prev:
            new += 1
        tier, _ = tier_for(r, tier_conf)
        if json.loads(r.get("extra") or "{}").get("watch"):
            tier = "watch"
        out.append({
            "id": r["key"], "title": r["title"], "country": r.get("country") or UNKNOWN,
            "score": r["score"], "tier": TIER_LABELS[tier],
            "publish": decision(prev),
            "summary": own_summary(prev),
            "auto_summary": r.get("summary_en") or auto_summary(r, "page" if json.loads(r.get("extra") or "{}").get("watch") else "notice"),
            "stage": r["stage"], "comment_deadline": r["comment_deadline"] or UNKNOWN,
            "province": r["province"], "url": r["url"],
        })
    # Keep rows you already decided on, even if the item is not in this run's data.
    for key, prev in old.items():
        if key not in seen and decision(prev):
            row = {c: prev.get(c, "") for c in COLUMNS}
            row["publish"] = decision(prev)
            out.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(out)
    return {"rows": len(out), "new": new,
            "yes": sum(1 for r in out if r["publish"] == "yes"),
            "no": sum(1 for r in out if r["publish"] == "no")}
