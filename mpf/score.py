"""Transparent rule-based score: "likely needs marine habitat work", 0 to 100.

Every point comes with a reason and the exact sentence it came from. The rules and
weights live in config/scoring.yaml, so you can change them without touching code.
A high score means "worth a human look", never "mitigation will be required".
"""
from __future__ import annotations

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


def score_text(text: str, rules: dict) -> dict:
    caps = rules["caps"]
    reasons = []

    # 1. Project type: best match counts fully, each extra type adds 3 (max +6).
    types = find_patterns(text, rules["project_types"])
    type_points = 0
    if types:
        ranked = sorted(types.items(), key=lambda kv: -kv[1]["points"])
        type_points = ranked[0][1]["points"] + min(6, 3 * (len(ranked) - 1))
        for i, (name, hit) in enumerate(ranked):
            pts = hit["points"] if i == 0 else (3 if i <= 2 else 0)
            reasons.append({"rule": f"type:{name}", "label": hit["label"], "points": pts,
                            "matched": hit["matched"], "quote": hit["quote"]})

    # 2. Habitat clues: each group once, total capped.
    clues = find_patterns(text, rules["habitat_clues"])
    habitat_points = min(caps["habitat_points_max"], sum(h["points"] for h in clues.values()))
    for name, hit in sorted(clues.items(), key=lambda kv: -kv[1]["points"]):
        reasons.append({"rule": f"habitat:{name}", "label": hit["label"], "points": hit["points"],
                        "matched": hit["matched"], "quote": hit["quote"]})

    # 3. Size.
    sizes = extract_sizes(text)
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

    # 5. No marine setting mentioned: cap the score.
    marine = has_marine_context(text, rules["marine_context"])
    if not marine and total > caps["no_marine_context_max"]:
        reasons.append({"rule": "cap:no_marine_context", "label": "No marine setting mentioned (score capped)",
                        "points": caps["no_marine_context_max"] - total, "matched": "", "quote": ""})
        total = caps["no_marine_context_max"]

    total = max(0, min(100, int(round(total))))
    return {
        "score": total,
        "flagged": total >= rules["flag_threshold"],
        "reasons": reasons,
        "project_types": [types[k]["label"] for k in types],
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
