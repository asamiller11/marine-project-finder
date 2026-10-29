"""Compare the tool's flags with your hand labels.

Labels file (labels/labels.csv), one row per item you judged:
    key,label,notes
    iaac:90821,0,small riprap repair
    nl_ea:2434,1,new fish habitat at Pikes Island
label: 1 (or yes/good) = a real lead for habitat work; 0 (or no/not) = not a lead.
Make a starting file with:  python -m mpf label-template
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

POSITIVE = {"1", "yes", "y", "good", "lead", "true"}
NEGATIVE = {"0", "no", "n", "not", "not a lead", "false"}

# DFO "Primary Work" values that are marine construction (for the recall-only check).
MARINE_WORK = {"Breakwaters", "Wharves", "Docks", "Piers", "Shoreline Protection", "Groynes",
               "Causeways", "Boat Launches/Ramps", "Beaches", "Dredging / Excavation"}


def read_labels(path: Path) -> dict[str, int]:
    labels = {}
    if not path.exists():
        return labels
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            value = (row.get("label") or "").strip().lower()
            if value in POSITIVE:
                labels[row["key"].strip()] = 1
            elif value in NEGATIVE:
                labels[row["key"].strip()] = 0
    return labels


def confusion(scores: dict[str, int], labels: dict[str, int], threshold: int) -> dict:
    tp = fp = fn = tn = 0
    for key, truth in labels.items():
        if key not in scores:
            continue
        flagged = scores[key] >= threshold
        if flagged and truth:
            tp += 1
        elif flagged and not truth:
            fp += 1
        elif truth:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {"threshold": threshold, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall}


def _pct(x):
    return "n/a" if x is None else f"{x:.0%}"


def run_hand_label_eval(conn: sqlite3.Connection, labels_path: Path, threshold: int) -> str:
    labels = read_labels(labels_path)
    lines = []
    if not labels:
        return (f"No labels found in {labels_path}. Run 'python -m mpf label-template', fill in the "
                f"label column with 1 or 0, and save it as {labels_path.name}.")
    rows = conn.execute("SELECT key, score, title FROM items WHERE is_test_set=0 AND score IS NOT NULL").fetchall()
    scores = {r["key"]: r["score"] for r in rows}
    titles = {r["key"]: r["title"] for r in rows}
    missing = [k for k in labels if k not in scores]
    c = confusion(scores, labels, threshold)
    lines.append(f"Hand labels: {len(labels)} ({sum(labels.values())} leads, "
                 f"{len(labels) - sum(labels.values())} not leads); {len(missing)} not found in the database")
    lines.append(f"At flag threshold {threshold}: precision {_pct(c['precision'])}, recall {_pct(c['recall'])} "
                 f"(TP {c['tp']}, FP {c['fp']}, FN {c['fn']}, TN {c['tn']})")
    lines.append("")
    lines.append("Threshold sweep (to help pick the cut-off):")
    lines.append("  thr  precision  recall   TP  FP  FN")
    for t in range(20, 75, 5):
        s = confusion(scores, labels, t)
        lines.append(f"  {t:>3}  {_pct(s['precision']):>9}  {_pct(s['recall']):>6}  {s['tp']:>3} {s['fp']:>3} {s['fn']:>3}")
    lines.append("")
    misses = [k for k, v in labels.items() if v == 1 and k in scores and scores[k] < threshold]
    false_flags = [k for k, v in labels.items() if v == 0 and k in scores and scores[k] >= threshold]
    if misses:
        lines.append("Leads the tool MISSED (look at these to add keywords):")
        lines += [f"  {k}  score {scores[k]}  {titles[k]}" for k in misses]
    if false_flags:
        lines.append("Flagged but NOT leads (look at these to tighten rules):")
        lines += [f"  {k}  score {scores[k]}  {titles[k]}" for k in false_flags]
    return "\n".join(lines)


def run_dfo_recall(conn: sqlite3.Connection, threshold: int) -> str:
    """Recall only. Every DFO row was authorized, so there are no negatives to measure precision."""
    rows = conn.execute("SELECT key, score, project_type_raw, facts FROM items WHERE is_test_set=1 "
                        "AND score IS NOT NULL").fetchall()
    marine = [r for r in rows if r["project_type_raw"] in MARINE_WORK
              and json.loads(r["facts"] or "{}").get("marine_context")]
    if not marine:
        return "DFO test set: no scored rows yet. Run ingest and score first."
    caught = sum(1 for r in marine if r["score"] >= threshold)
    return (f"DFO test set (recall only, NOT precision): {caught} of {len(marine)} marine-type authorizations "
            f"flagged at threshold {threshold} = {caught / len(marine):.0%}.\n"
            f"Note: these short DFO descriptions rarely say 'offsetting' even though every one was authorized, "
            f"so this mostly tests the project-type and size rules.")
