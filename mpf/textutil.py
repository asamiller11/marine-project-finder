"""Small text helpers: whitespace, dates, language guess, evidence snippets."""
from __future__ import annotations

import re
from datetime import date, datetime

from mpf.config import today_override

_ZERO_WIDTH = re.compile("[​‌‍﻿]")


def clean(text: str | None) -> str:
    """Collapse whitespace and drop invisible characters."""
    if not text:
        return ""
    text = _ZERO_WIDTH.sub("", text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def today() -> date:
    o = today_override()
    return date.fromisoformat(o) if o else date.today()


_FR_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
}


def parse_date(text: str | None) -> str | None:
    """Turn a date written in English, French or ISO form into YYYY-MM-DD.
    Returns None when we cannot read it (we never guess)."""
    if not text:
        return None
    t = clean(text.replace(",", " "))
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    # No \b before the year: NL writes "Released2025-04-10" with no space.
    m = re.search(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", t)
    if m:
        return m.group(0)
    # European day-month-year: "20-12-2026" (Denmark), "16/10/2026" (Scotland).
    m = re.search(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?!\d)", t)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    # "November 9 2026" or "9 November 2026" (also short month names like "Nov.")
    for pat, order in ((r"\b([A-Za-z]{3,9})\.? (\d{1,2}) (\d{4})\b", "mdy"),
                       (r"\b(\d{1,2}) ([A-Za-z]{3,9})\.? (\d{4})\b", "dmy")):
        for m in re.finditer(pat, t):
            mon, day = (m.group(1), m.group(2)) if order == "mdy" else (m.group(2), m.group(1))
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    return datetime.strptime(f"{mon} {day} {m.group(3)}", fmt).date().isoformat()
                except ValueError:
                    pass
    m = re.search(r"\b(\d{1,2})(?:er)? ([a-zéû]+) (\d{4})\b", t.lower())
    if m and m.group(2) in _FR_MONTHS:
        return date(int(m.group(3)), _FR_MONTHS[m.group(2)], int(m.group(1))).isoformat()
    return None


_FR_WORDS = set("le la les des du de et est pour dans une un sur au aux par avec projet travaux qui sont".split())
_EN_WORDS = set("the and of to in for is on with by project works be will are from that this".split())


def guess_language(text: str) -> str:
    """Rough guess: 'en', 'fr' or 'unknown', by counting common short words."""
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿœ']+", (text or "").lower()[:5000])
    fr = sum(w in _FR_WORDS for w in words)
    en = sum(w in _EN_WORDS for w in words)
    if fr + en < 5:
        return "unknown"
    return "fr" if fr > en else "en"


def snippet(text: str, start: int, end: int, max_len: int = 300) -> str:
    """Return the sentence around text[start:end], trimmed to max_len characters."""
    dot, newline = text.rfind(". ", 0, start), text.rfind("\n", 0, start)
    if dot < 0 and newline < 0:
        s = 0
    elif dot > newline:
        s = dot + 2          # skip ". "
    else:
        s = newline + 1      # skip "\n"
    right_candidates = [i for i in (text.find(". ", end), text.find("\n", end)) if i >= 0]
    e = min(right_candidates) + 1 if right_candidates else len(text)
    s_text = text[s:e].strip()
    if len(s_text) <= max_len:
        return s_text
    # Too long: keep a window centred on the match.
    half = max_len // 2
    ws = max(s, start - half)
    we = min(e, ws + max_len)
    return ("..." if ws > s else "") + text[ws:we].strip() + ("..." if we < e else "")


def days_between(a: str | None, b: date) -> int | None:
    """Days from date b until ISO date a (negative when a is in the past)."""
    if not a:
        return None
    return (date.fromisoformat(a) - b).days
