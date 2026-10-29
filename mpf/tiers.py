"""Tiers (Strong match, Possible, Weak, Probably not relevant) and duplicate merging.

Thresholds and rules live in config/settings.yaml under `tiers:` and `duplicates:`.
"""
from __future__ import annotations

import re

TIER_ORDER = ["strong", "possible", "weak", "notrel", "watch"]
TIER_LABELS = {"strong": "Strong match", "possible": "Possible", "weak": "Weak", "notrel": "Probably not relevant",
               "watch": "Project watch (not a notice feed)"}


def tier_for(row: dict, conf: dict) -> tuple[str, str]:
    """Return (tier key, short reason). Rules come before the score."""
    stage = (row.get("stage") or "").lower()
    title = row.get("title") or ""
    if stage.startswith("withdrawn"):
        return "notrel", "withdrawn or rejected"
    for pat in conf.get("not_relevant_title_patterns", []):
        if re.search(pat, title, re.IGNORECASE):
            return "notrel", "quarry or pit (rule)"
    score = row.get("score") or 0
    if score >= conf.get("strong_min", 60):
        return "strong", f"score {score}"
    if score >= conf.get("possible_min", 35):
        return "possible", f"score {score}"
    if score >= conf.get("weak_min", 20):
        return "weak", f"score {score}"
    return "notrel", f"score {score} (under {conf.get('weak_min', 20)})"


_DROP_TAIL = re.compile(r"\s*[–\-:]\s*(public comments? invited|notice of intent|public notice).*$", re.IGNORECASE)
_STOP = {"project", "the", "of", "at", "and", "a", "in", "on", "for", "proposed", "phase"}


def title_tokens(title: str) -> set[str]:
    t = _DROP_TAIL.sub("", (title or "").replace("​", ""))
    words = re.findall(r"[a-z0-9àâçéèêëîïôûùüÿœ']+", t.lower())
    return {w for w in words if w not in _STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_duplicates(rows: list[dict], conf: dict) -> dict[str, str]:
    """Map each duplicate's key to the key of the entry we keep.
    Only items from DIFFERENT sources in the same province (or unknown) are merged."""
    prefer = conf.get("prefer", ["nl_ea", "iaac", "drop"])
    min_sim = conf.get("min_title_similarity", 0.8)

    def rank(row):
        return prefer.index(row["source"]) if row["source"] in prefer else len(prefer)

    ordered = sorted(rows, key=lambda r: (rank(r), -(r.get("score") or 0)))
    losers: dict[str, str] = {}
    for i, keep in enumerate(ordered):
        if keep["key"] in losers:
            continue
        for other in ordered[i + 1:]:
            if other["key"] in losers or other["source"] == keep["source"]:
                continue
            same_place = "unknown" in (keep.get("province"), other.get("province")) or \
                keep.get("province") == other.get("province")
            if same_place and similarity(keep.get("title", ""), other.get("title", "")) >= min_sim:
                losers[other["key"]] = keep["key"]
    return losers
