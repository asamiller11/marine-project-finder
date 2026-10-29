"""Turns dates and status words into a plain-English stage and a timing label.

Stages used:
  "public comment open until <date>"
  "comment closed <date>, decision pending"
  "under assessment" / "registered"
  "decision made" / "authorized (Fisheries Act)"
  "withdrawn or rejected"
"too_late" is True when a decision is made, or the decision date is within
`too_late_days`, so you can see how early the tool really catches things.
"""
from __future__ import annotations

import re
from datetime import date

from mpf.models import UNKNOWN
from mpf.textutil import days_between

_DONE = re.compile(r"released|completed?|decision (made|posted|issued)|approved|authori[sz]ed|"
                   r"determination made|not likely to cause|ea not required|closed", re.I)
_DEAD = re.compile(r"withdrawn|rejected|cancell?ed|terminated|not to proceed|expired", re.I)
_ACTIVE = re.compile(r"in progress|active|ongoing|under review|en cours", re.I)


def compute(row: dict, today: date, closing_soon_days: int = 14, too_late_days: int = 7) -> dict:
    status = row.get("status_raw") or UNKNOWN
    deadline = row.get("comment_deadline")
    decision = row.get("decision_date")
    days_left = days_between(deadline, today)
    days_to_decision = days_between(decision, today)

    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    if extra.get("watch"):
        # Project watch: a project page, not a notice. No deadline, no countdown, never "too late".
        return {"stage": status if status != UNKNOWN else "project watch",
                "timing": "project watch (not a notice feed)", "days_to_deadline": None, "too_late": False}

    if row.get("is_test_set"):
        return {"stage": "authorized (Fisheries Act)", "timing": "test set (historic)",
                "days_to_deadline": None, "too_late": True}

    if _DEAD.search(status):
        stage, too_late = "withdrawn or rejected", True
    elif days_left is not None and days_left >= 0:
        stage, too_late = f"public comment open until {deadline}", False
    elif _DONE.search(status) or (row.get("source") == "nl_ea" and _decision_already_made(row)):
        stage, too_late = "decision made", True
    elif days_left is not None and days_left < 0:
        stage, too_late = f"comment closed {deadline}, decision pending", False
    elif re.search(r"public information|consultation", status, re.I):
        stage, too_late = status, False     # a stated consultation stage without a computable date
    elif _ACTIVE.search(status):
        stage, too_late = "under assessment", False
    elif row.get("date_posted"):
        stage, too_late = "registered", False
    else:
        stage, too_late = UNKNOWN, False

    # A decision that is due very soon (or already past) means it is probably too late.
    if days_to_decision is not None and days_to_decision <= too_late_days:
        too_late = True

    if days_left is not None and days_left >= 0:
        timing = f"comment closes in {days_left} days" + (" (closing soon)" if days_left <= closing_soon_days else "")
    elif days_to_decision is not None and days_to_decision >= 0:
        timing = f"decision due in {days_to_decision} days"
    elif days_left is not None:
        timing = f"comment closed {-days_left} days ago"
    else:
        timing = "no dates published"
    if too_late:
        timing += "; likely too late"
    return {"stage": stage, "timing": timing, "days_to_deadline": days_left, "too_late": too_late}


def _decision_already_made(row: dict) -> bool:
    extra = row.get("extra") or {}
    if isinstance(extra, dict) and extra.get("decision_made"):
        return True
    return False
