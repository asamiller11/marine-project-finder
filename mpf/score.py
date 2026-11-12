"""Transparent rule-based score: "likely needs marine habitat work", 0 to 100.

Every point comes with a reason and the exact sentence it came from. The rules and
weights live in config/scoring.yaml, so you can change them without touching code.
A high score means "worth a human look", never "mitigation will be required".
"""
from __future__ import annotations

import re

from mpf.extract import (extract_sizes, find_patterns, has_marine_context, largest_sizes,
                         size_summary)


def _size_points(big: dict, table: dict) -> list[tuple[str, int]]:
    out = []
    for kind, steps in table.items():
        value = big.get(kind)
        if value is None:
            continue
        pts = 0
        for threshold, points in steps:
            if value >= threshold:
                pts = max(pts, points)
        if pts:
            out.append((kind, pts))
    return out


def score_text(text: str, rules: dict, negated=None, size_ok=None) -> dict:
    """negated and size_ok are only used by buyer profiles (mpf/profiles.py). Left as None, the score is
    exactly the general score."""
    caps = rules["caps"]
    reasons = []
    fp = (lambda t, g: find_patterns(t, g, negated)) if negated else find_patterns

    # 1. Project type: best match counts fully, each extra type adds 3 (max +6).
    types = fp(text, rules["project_types"])
    # A more specific group can replace a general one (a wharf being rebuilt is not also "a wharf").
    for name in list(types):
        gone = rules["project_types"][name].get("replaces")
        if gone:
            types.pop(gone, None)
    type_points = 0
    if types:
        ranked = sorted(types.items(), key=lambda kv: -kv[1]["points"])
        type_points = ranked[0][1]["points"] + min(6, 3 * (len(ranked) - 1))
        for i, (name, hit) in enumerate(ranked):
            pts = hit["points"] if i == 0 else (3 if i <= 2 else 0)
            reasons.append({"rule": f"type:{name}", "label": hit["label"], "points": pts,
                            "matched": hit["matched"], "quote": hit["quote"]})

    # 2. Habitat clues: each group once, total capped.
    clues = fp(text, rules["habitat_clues"])
    habitat_points = min(caps["habitat_points_max"], sum(h["points"] for h in clues.values()))
    for name, hit in sorted(clues.items(), key=lambda kv: -kv[1]["points"]):
        reasons.append({"rule": f"habitat:{name}", "label": hit["label"], "points": hit["points"],
                        "matched": hit["matched"], "quote": hit["quote"]})

    # 3. Size.
    sizes = extract_sizes(text)
    if size_ok is not None:
        sizes = [s for s in sizes if size_ok(text, s)]
    big = largest_sizes(sizes)
    size_pts = _size_points(big, rules["size_points"])
    size_points = min(caps["size_points_max"], sum(p for _, p in size_pts))
    for kind, pts in size_pts:
        quote = next(s for s in sorted(sizes, key=lambda s: -s["value"]) if s["kind"] == kind)
        reasons.append({"rule": f"size:{kind}", "label": f"Size: {quote['matched']}", "points": pts,
                        "matched": quote["matched"], "quote": quote["quote"]})

    # 4. Small project penalty: only a short length found, nothing else large.
    penalty = 0
    small = rules.get("small_project", {})
    if big and set(big) == {"length_m"} and big["length_m"] < small.get("max_length_m", 30):
        penalty = small.get("penalty", 10)
        q = next(s for s in sizes if s["kind"] == "length_m" and s["value"] == big["length_m"])
        reasons.append({"rule": "size:small", "label": f"Small project ({q['matched']})",
                        "points": -penalty, "matched": q["matched"], "quote": q["quote"]})

    total = type_points + habitat_points + size_points - penalty

    # 4b. Sizes alone (no project type, no habitat clue) cannot list a project.
    size_only_max = caps.get("size_only_max")
    if size_only_max is not None and not types and not clues and total > size_only_max:
        reasons.append({"rule": "cap:size_only", "label": "Only sizes found, no marine work named (score capped)",
                        "points": size_only_max - total, "matched": "", "quote": ""})
        total = size_only_max

    # 5. No marine setting mentioned: cap the score.
    marine = has_marine_context(text, rules["marine_context"])
    if not marine and total > caps["no_marine_context_max"]:
        reasons.append({"rule": "cap:no_marine_context", "label": "No marine setting mentioned (score capped)",
                        "points": caps["no_marine_context_max"] - total, "matched": "", "quote": ""})
        total = caps["no_marine_context_max"]

    # 6. Maintenance dredging only (no new structure), small, no clear habitat words: kept at Weak.
    md = rules.get("maintenance_dredging")
    if md and re.search(md["pattern"], text) and total > md.get("cap_score", 34):
        others = set(types) - {"dredging"}
        no_new = not others or re.search(md.get("no_new_structure") or r"(?!x)x", text)
        vol = big.get("volume_m3")
        if no_new and vol is not None and vol < md.get("max_volume_m3", 5000) \
                and not (set(clues) & set(md.get("habitat_words") or [])):
            reasons.append({"rule": "cap:maintenance_dredging",
                            "label": "Maintenance dredging only, no new structure (kept at Weak)",
                            "points": md.get("cap_score", 34) - total, "matched": "", "quote": ""})
            total = md.get("cap_score", 34)

    total = max(0, min(100, int(round(total))))
    return {
        "score": total,
        "flagged": total >= rules["flag_threshold"],
        "reasons": reasons,
        "project_types": list(dict.fromkeys(types[k]["label"] for k in types)),
        "size_summary": size_summary(sizes),
        "facts": {"sizes": sizes, "marine_context": marine,
                  "habitat_clues": sorted(clues), "types": sorted(types)},
    }


def reason_lines(reasons: list[dict]) -> list[str]:
    """Plain-English lines like '+15 New habitat...: "...quote..."'."""
    lines = []
    for r in reasons:
        sign = "+" if r["points"] >= 0 else ""
        quote = f': "{r["quote"]}"' if r.get("quote") else ""
        lines.append(f"{sign}{r['points']} {r['label']}{quote}")
    return lines
