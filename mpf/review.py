"""The review step: nothing is published until you approve it in review/review.csv.

`python -m mpf review` writes or refreshes the review file. Your earlier decisions
(publish, confidence, summary) are kept; scores, stages and deadlines are refreshed.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from mpf.extract import largest_sizes
from mpf.models import UNKNOWN

# The first five columns are the ones you edit or need; the rest help you decide.
COLUMNS = ["id", "title", "score", "confidence", "publish", "summary",
           "stage", "comment_deadline", "province", "url"]
YES = {"yes", "y", "1", "true", "x"}


def confidence_for(score: int, conf: dict) -> str:
    if score >= conf.get("confidence_high", 60):
        return "High"
    if score >= conf.get("confidence_medium", 35):
        return "Medium"
    return "Low"


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


_TYPE_WORDS = {
    "port_terminal": "port or terminal", "wharf_dock": "wharf or dock", "breakwater_jetty": "breakwater or jetty",
    "dredging": "dredging", "shoreline_protection": "shoreline protection", "marina_small_craft": "small craft harbour",
    "infill_causeway": "infill or causeway", "offshore_wind": "offshore wind", "piles": "pile",
}


def auto_summary(row: dict) -> str:
    """A factual starting summary built only from what the tool extracted.
    It says what the notice MENTIONS, never what will happen. Edit it in the review file."""
    facts = json.loads(row.get("facts") or "{}")
    who = row.get("proponent") or UNKNOWN
    where_parts = [p for p in (row.get("water_body"), row.get("province")) if p and p != UNKNOWN]
    where = ", ".join(where_parts) if where_parts else "an unknown location"
    first = f"Project by {who} in {where}." if who != UNKNOWN else f"Project in {where}."
    types = [_TYPE_WORDS[t] for t in facts.get("types", []) if t in _TYPE_WORDS]
    second = f" The notice mentions {_join(types)} work" if types else ""
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
        second = f" The notice mentions {_join(size_bits)}."
    return first + second


def read_review(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {row["id"]: row for row in csv.DictReader(f) if row.get("id")}


def approved_ids(path: Path) -> list[str]:
    return [i for i, row in read_review(path).items() if (row.get("publish") or "").strip().lower() in YES]


def write_review(conn: sqlite3.Connection, path: Path, conf: dict) -> dict:
    """Write the review file. Returns counts for the summary message."""
    old = read_review(path)
    min_score = conf.get("min_score", 20)
    rows = conn.execute(
        "SELECT * FROM items WHERE is_test_set=0 AND score IS NOT NULL "
        "AND stage NOT LIKE 'withdrawn%' ORDER BY score DESC").fetchall()
    out, new = [], 0
    seen = set()
    for r in rows:
        r = dict(r)
        if r["score"] < min_score and r["key"] not in old:
            continue
        seen.add(r["key"])
        prev = old.get(r["key"], {})
        if not prev:
            new += 1
        suggested = r["llm_confidence"].capitalize() if r.get("llm_confidence") in ("high", "medium", "low") \
            else confidence_for(r["score"], conf)
        out.append({
            "id": r["key"], "title": r["title"], "score": r["score"],
            "confidence": prev.get("confidence") or suggested,
            "publish": prev.get("publish") or "no",
            "summary": prev.get("summary") or r.get("summary_en") or auto_summary(r),
            "stage": r["stage"], "comment_deadline": r["comment_deadline"] or UNKNOWN,
            "province": r["province"], "url": r["url"],
        })
    # Keep rows you already reviewed even if the item disappeared from this run.
    for key, prev in old.items():
        if key not in seen:
            out.append({c: prev.get(c, "") for c in COLUMNS})
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(out)
    approved = sum(1 for r in out if str(r["publish"]).strip().lower() in YES)
    return {"rows": len(out), "new": new, "approved": approved}
