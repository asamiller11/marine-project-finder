"""Public comment tracking: when a comment period opens and closes, and how many comments were received.

Only facts the official registry publishes are shown. Anything else is "Not published".
Comment text, commenter names and other personal details are never read into the site.

Per source (see COUNT_STATUS below for the reason shown on the site):
- iaac:        opening date when the notice states it; closing date from the open comment list;
               comment total from the "View comments  Total: N" line on the project page, with a
               link to the registry's own comments page.
- nl_ea:       closing date only ("Deadline for public comments"). No count is published.
- dk_hoering:  closing date only (Høringsfrist in the feed). Responses are not counted in the feed.
- scot_marine: no comment dates or counts; representations are published as redacted PDF bundles.
- es_boe:      no calendar dates (periods are given in working days) and no counts.
"""
from __future__ import annotations

import re
from datetime import date

from mpf.textutil import parse_date

NOT_PUBLISHED = "Not published"

# Why a comment count is or is not shown, per source. Shown on project pages and on the About page.
COUNT_STATUS = {
    "iaac": "The registry shows the number of comments received on some project pages (mostly larger "
            "assessments). This project page shows none.",
    "nl_ea": "The registry publishes the comment deadline but not the number of comments received.",
    "dk_hoering": "The portal's feeds give the hearing deadline but not the number of responses.",
    "scot_marine": "Representations are published as PDF documents, not as a count.",
    "es_boe": "BOE notices give the comment period in working days and publish no count.",
    "drop": "Added by hand; no comment data is read.",
}

_MONTH_DATE = (r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
               r"\s+\d{1,2},\s+\d{4}")
_FR_DATE = r"\d{1,2}(?:er)?\s+[a-zéû]+\s+\d{4}"
_ANY_DATE = rf"(?:{_MONTH_DATE}|{_FR_DATE}|\d{{4}}-\d{{2}}-\d{{2}})"

# The notice states the start of the period.
_STARTS = [
    re.compile(rf"start of (?:the )?public comment period\s*:?\s*({_ANY_DATE})", re.I),
    re.compile(rf"public comments? invited\s*:?\s*({_ANY_DATE})", re.I),
    re.compile(rf"comment period (?:runs |is open )?from\s+({_ANY_DATE})\s+(?:to|until)\s+{_ANY_DATE}", re.I),
    re.compile(rf"\bfrom\s+({_ANY_DATE})\s+(?:to|until)\s+{_ANY_DATE}[^.]{{0,40}}comment", re.I),
    re.compile(rf"commentaires[^.]{{0,60}}\bdu\s+({_FR_DATE})\s+au\s+{_FR_DATE}", re.I),
]
# A dated update ("October 26, 2026 - ... inviting public comments until November 14, 2026"), possibly
# after a heading on the same line ("Notice of intent ... September 22, 2026 - ...").
# The period opens with that notice, so the notice date is shown, labelled as the notice date.
_NOTICE_LINE = re.compile(rf"({_MONTH_DATE})\s*[-–]\s*(.+)$", re.M)
_INVITES = re.compile(r"invit\w* (?:the )?public (?:to provide )?comments?|public comments? (?:are )?invited|"
                      r"inviting public comments|public comment period", re.I)
_COUNT = re.compile(r'id="commentCount"[^>]*>\s*Total:\s*([\d,]+)\s*<', re.I)


def comment_opens(text: str, closes: str | None) -> tuple[str | None, str]:
    """(opening date, how we know it): "stated" when the notice gives the start, "notice date" when
    a dated notice invites comments. (None, "") when the notice does not say."""
    if not closes:
        return None, ""
    for pat in _STARTS:
        for m in pat.finditer(text or ""):
            d = parse_date(m.group(1))
            if d and d <= closes:
                return d, "stated"
    for m in _NOTICE_LINE.finditer(text or ""):
        if _INVITES.search(m.group(2)):
            d = parse_date(m.group(1))
            if d and d <= closes:
                return d, "notice date"
    return None, ""


def parse_comment_count(html: str) -> int | None:
    """The comment total on an IAAC project page, or None when the page does not show one."""
    m = _COUNT.search(html or "")
    return int(m.group(1).replace(",", "")) if m else None


def comment_info(row: dict, extra: dict, today: date) -> dict:
    """Everything the site shows about public comments for one item."""
    source = row.get("source") or ""
    closes = row.get("comment_deadline") or None
    opens, opens_how = comment_opens(row.get("full_text") or "", closes)
    count = extra.get("comment_count") if source == "iaac" else None
    days_left = (date.fromisoformat(closes) - today).days if closes else None
    return {
        "opens": opens,
        "opens_how": opens_how,
        "closes": closes,
        "days_left": days_left,
        "count": count,
        "count_as_of": extra.get("comment_count_as_of") if count is not None else None,
        "comments_url": extra.get("comments_url") if count is not None else None,
        "count_note": "" if count is not None else COUNT_STATUS.get(source, "This source publishes no comment data."),
    }
