"""British Columbia Environmental Assessment Office (EAO), public API.

What we read (robots.txt allows it; only /admin and /analytics are disallowed):
- /api/public/search?dataset=Project        all projects (about 360) in one request
- /api/public/search?dataset=CommentPeriod  recent public comment periods, with dates
Only projects that are still active, or had a comment period recently, are kept.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Iterator

from mpf.extract import extract_water_body
from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, parse_date, today

API = "https://projects.eao.gov.bc.ca/api/public/search"
PROJECTS_URL = API + "?dataset=Project&pageSize=500&pageNum=0&populate=false"
PERIODS_URL = API + "?dataset=CommentPeriod&pageSize=100&pageNum=0&sortBy=-dateStarted&populate=true"
_FINISHED = ("complete", "withdrawn", "terminated", "operation", "care & maintenance", "decommission")


def _results(payload: bytes) -> list[dict]:
    data = json.loads(payload)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0].get("searchResults", [])
    return []


def _name(obj) -> str:
    if isinstance(obj, dict):
        return clean(obj.get("name") or "")
    return clean(obj or "")


def comment_periods_by_project(periods: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for cp in periods:
        proj = cp.get("project")
        pid = proj.get("_id") if isinstance(proj, dict) else proj
        if not pid:
            continue
        out.setdefault(str(pid), []).append({
            "start": parse_date(cp.get("dateStarted")), "end": parse_date(cp.get("dateCompleted")),
            "label": clean(cp.get("informationLabel") or ""), "instructions": clean(cp.get("instructions") or ""),
            "url": cp.get("metURL") or ""})
    return out


def project_to_item(p: dict, periods: list[dict]) -> Item:
    pid = str(p.get("_id"))
    name = clean(p.get("name")) or UNKNOWN
    phase = _name(p.get("currentPhaseName")) or clean(p.get("status") or "") or UNKNOWN
    ends = sorted(x["end"] for x in periods if x["end"])
    deadline = ends[-1] if ends else None
    centroid = p.get("centroid") or []
    lon = lat = None
    if len(centroid) == 2:
        try:
            lon, lat = float(centroid[0]), float(centroid[1])
        except (TypeError, ValueError):
            pass
    decision = p.get("eacDecision")
    text_parts = [name, clean(p.get("description") or ""), clean(p.get("location") or "")]
    text_parts += [x["instructions"] for x in periods if x["instructions"]]
    text = "\n".join(t for t in text_parts if t)
    return Item(
        source="bc_eao", item_id=pid, url=f"https://projects.eao.gov.bc.ca/p/{pid}", title=name,
        language="en", country="CA", province="British Columbia", water_body=extract_water_body(text),
        lat=lat, lon=lon, proponent=_name(p.get("proponent")) or UNKNOWN,
        project_type_raw=clean(p.get("type") or "") or UNKNOWN,
        date_posted=parse_date(p.get("dateAdded")), comment_deadline=deadline,
        decision_date=parse_date(p.get("decisionDate")), status_raw=phase, full_text=text,
        extra={"region": p.get("region"), "sector": p.get("sector"), "decision": _name(decision),
               "comment_periods": periods, "link_ok": True},
    )


def keep(p: dict, periods: list[dict], lookback_days: int) -> bool:
    """Active projects, or any project with a comment period that ended recently."""
    cutoff = (today() - timedelta(days=lookback_days)).isoformat()
    if any(x["end"] and x["end"] >= cutoff for x in periods):
        return True
    phase = (_name(p.get("currentPhaseName")) + " " + clean(p.get("status") or "")).lower()
    return not any(word in phase for word in _FINISHED)


class BCEAOSource(Source):
    name = "bc_eao"
    country = "CA"

    def fetch(self) -> Iterator[Item]:
        try:
            projects = _results(self.client.get(PROJECTS_URL, list_page=True))
            periods = _results(self.client.get(PERIODS_URL, list_page=True))
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
            return
        by_project = comment_periods_by_project(periods)
        lookback = int(self.conf.get("lookback_days", 730))
        kept = [p for p in projects if keep(p, by_project.get(str(p.get("_id")), []), lookback)]
        self.log.append(f"{len(projects)} projects, {len(periods)} comment periods, {len(kept)} active or recent")
        for p in kept:
            yield project_to_item(p, by_project.get(str(p.get("_id")), []))
