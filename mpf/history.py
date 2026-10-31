"""Snapshot history between updates: what is new, which deadlines changed, what is now decided.

Kept in site/history.json, so it is uploaded with the site and survives between updates.
It holds only facts already on the public site (no page text):

  {"snapshots": ["2026-10-15", "2026-10-29"],     dates of updates, oldest first
   "base": {id: state}, "base_date": "...",        state at the previous update
   "current": {id: state},                         state at the latest update
   "events": {id: [{"date", "type", "text"}]}}     each project's timeline

Publishing twice on the same day replaces that day's snapshot instead of adding one, so
"since last update" always compares with the previous update day.
"""
from __future__ import annotations

import json
from pathlib import Path

STATE_FIELDS = ("comment_deadline", "stage_kind", "tier", "title")
CHANGE_LABELS = {"new": "New since last update", "deadline": "Deadline changed", "decided": "Now decided"}


def state_of(rec: dict) -> dict:
    return {f: rec.get(f) for f in STATE_FIELDS}


def load(path: Path) -> dict:
    try:
        h = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        h = {}
    h.setdefault("snapshots", [])
    h.setdefault("base", {})
    h.setdefault("base_date", None)
    h.setdefault("current", {})
    h.setdefault("events", {})
    return h


def diff(before: dict | None, after: dict, first_snapshot: bool) -> list[dict]:
    """Events between two states of one project. On the very first snapshot nothing is 'new'."""
    if before is None:
        return [] if first_snapshot else [{"type": "new", "text": "First listed"}]
    out = []
    b, a = before.get("comment_deadline"), after.get("comment_deadline")
    if b != a:
        if b and a:
            out.append({"type": "deadline", "text": f"Comment deadline changed from {b} to {a}"})
        elif a:
            out.append({"type": "deadline", "text": f"Comment deadline published: {a}"})
        else:
            out.append({"type": "deadline", "text": f"Comment deadline {b} no longer shown"})
    if after.get("stage_kind") == "decided" and before.get("stage_kind") != "decided":
        out.append({"type": "decided", "text": "Decision made"})
    elif before.get("stage_kind") != after.get("stage_kind") and after.get("stage_kind") not in ("open", "closed"):
        # open -> closed happens by itself as days pass, so it is not an event.
        out.append({"type": "stage", "text": f"Stage now: {after.get('stage_kind')}"})
    if before.get("tier") != after.get("tier"):
        out.append({"type": "tier", "text": f"Tier changed from {before.get('tier')} to {after.get('tier')}"})
    return out


def record_snapshot(history: dict, records: list[dict], day: str) -> dict:
    """Add (or replace) the snapshot for `day` and return the updated history."""
    h = history
    if h["snapshots"] and h["snapshots"][-1] == day:
        # Same day again: recompute this day's events from the previous update's state.
        for evs in h["events"].values():
            evs[:] = [e for e in evs if e["date"] != day]
        before_all = h["base"]
        first = len(h["snapshots"]) == 1
    else:
        h["base"], h["base_date"] = h["current"], (h["snapshots"][-1] if h["snapshots"] else None)
        before_all = h["base"]
        first = not h["snapshots"]
        h["snapshots"].append(day)
    current = {}
    for rec in records:
        st = state_of(rec)
        current[rec["id"]] = st
        for e in diff(before_all.get(rec["id"]), st, first):
            h["events"].setdefault(rec["id"], []).append({"date": day, **e})
    h["current"] = current
    h["events"] = {k: v for k, v in h["events"].items() if v}
    return h


def annotate(records: list[dict], history: dict) -> None:
    """Add "changes" (labels since the last update) and "timeline" (all events) to each record."""
    latest = history["snapshots"][-1] if history["snapshots"] else None
    for rec in records:
        evs = history["events"].get(rec["id"], [])
        rec["timeline"] = evs
        rec["changes"] = sorted({e["type"] for e in evs if e["date"] == latest and e["type"] in CHANGE_LABELS},
                                key=list(CHANGE_LABELS).index)
        rec["first_listed"] = next((e["date"] for e in evs if e["type"] == "new"), None) or \
            (history["snapshots"][0] if history["snapshots"] and rec["id"] in history.get("current", {}) else None)


def save(path: Path, history: dict) -> None:
    path.write_text(json.dumps(history, ensure_ascii=False, indent=0), encoding="utf-8")
