"""Pull structured facts out of notice text: sizes, coordinates, water body, keyword hits.

Everything returned comes with the exact text it was read from, so a person can check it.
"""
from __future__ import annotations

import re

from mpf.models import UNKNOWN
from mpf.textutil import snippet

# A number like 1,200 / 1 200 / 12.5 / 12,5 (French decimal comma).
NUM = r"(\d{1,3}(?:[ , ]\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)"
RANGE = r"(?<![\d.,])" + NUM + r"(?:\s*(?:to|-|–|à)\s*" + NUM + r")?"


def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace(" ", " ")
    if re.fullmatch(r"\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?", s):   # thousands separators
        return float(s.replace(",", "").replace(" ", ""))
    return float(s.replace(",", "."))


# (kind, unit regex, multiplier to our base unit)
_UNITS = [
    ("volume_m3", r"(?:m³|m ?3\b|cubic met(?:re|er)s?|met(?:re|er)s? cubed|mètres? cubes?)", 1.0),
    ("volume_m3", r"(?:cubic yards?|cu\.? ?yds?\b|yd³)", 0.7646),
    ("area_m2", r"(?:km²|km2\b|square kilomet(?:re|er)s?)", 1_000_000.0),
    # "m 2" with a space is how some PDFs and web pages render m².
    ("area_m2", r"(?:m²|m ?2\b|sq\.? ?m\b|square met(?:re|er)s?|mètres? carrés?)", 1.0),
    ("area_m2", r"(?:hectares?|\bha\b)", 10_000.0),
    ("area_m2", r"(?:ft²|sq\.? ?ft\b|square f(?:ee|oo)t)", 0.0929),
    ("area_m2", r"(?:acres?)", 4046.86),
    ("power_mw", r"(?:GW\b|gigawatts?)", 1000.0),
    ("power_mw", r"(?:MW\b|megawatts?|mégawatts?)", 1.0),
    ("slips", r"(?:boat slips|slips|berths|moorings|boat spaces|places à quai|amarrages)", 1.0),
    ("length_m", r"(?:km\b|kilomet(?:re|er)s?|kilomètres?)", 1000.0),
    ("length_m", r"(?:linear f(?:ee|oo)t|lf\b|ft\b|feet|foot|pieds)", 0.3048),
    ("length_m", r"(?:m\b|metres?|meters?|mètres?|m-long|metre-long)", 1.0),
]

# A length followed by these words is a distance, depth or height, not the project size.
_NOT_A_SIZE = re.compile(
    r"^\W{0,3}(?:\w+\W+){0,2}?(north|south|east|west|northeast|northwest|southeast|southwest|from|away|off|"
    r"offshore|upstream|downstream|deep|depth|wide|width|high|height|above|below|tall|nord|sud|est|ouest|"
    r"de profondeur|profond|de large|de haut)\b",
    re.IGNORECASE,
)


def extract_sizes(text: str) -> list[dict]:
    """Find sizes such as '250 m of wharf', '45,000 m³ of dredging', '400 MW'.
    For a range like '20 to 25 metres' we keep the larger number."""
    found, taken = [], []
    for kind, unit, mult in _UNITS:
        pattern = re.compile(RANGE + r"\s?-?\s?" + unit, re.IGNORECASE)
        for m in pattern.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue          # already used by a more specific unit (e.g. m³ before m)
            if kind == "length_m" and _NOT_A_SIZE.match(text[m.end():m.end() + 40]):
                continue
            if kind in ("length_m", "area_m2") and re.search(r"\d+\.\d{4,}", m.group(0)):
                continue          # a coordinate like 49.130668, not a size
            a, b = _to_float(m.group(1)), _to_float(m.group(2))
            value = max(v for v in (a, b) if v is not None) * mult
            if kind == "slips" and value > 5000:
                continue
            taken.append((m.start(), m.end()))
            found.append({"kind": kind, "value": round(value, 1), "matched": m.group(0).strip(),
                          "quote": snippet(text, m.start(), m.end())})
    return found


def largest_sizes(sizes: list[dict]) -> dict:
    out: dict[str, float] = {}
    for s in sizes:
        out[s["kind"]] = max(out.get(s["kind"], 0), s["value"])
    return out


def size_summary(sizes: list[dict]) -> str:
    """Short human text, e.g. 'length 250 m; dredging 45,000 m3'. 'unknown' if none."""
    big = largest_sizes(sizes)
    if not big:
        return UNKNOWN
    names = {"length_m": ("length", "m"), "area_m2": ("area", "m2"), "volume_m3": ("volume", "m3"),
             "power_mw": ("capacity", "MW"), "slips": ("slips/berths", "")}
    parts = []
    for k in ("length_m", "area_m2", "volume_m3", "power_mw", "slips"):
        if k in big:
            label, unit = names[k]
            parts.append(f"{label} {big[k]:,.0f} {unit}".strip())
    return "; ".join(parts)


def extract_coordinates(text: str) -> tuple[float | None, float | None]:
    """Decimal coordinates written like '49.130668, -123.067900'. Canada-shaped values only."""
    for m in re.finditer(r"(-?\d{2}\.\d{3,})\s*[,/ ]\s*(-?\d{2,3}\.\d{3,})", text):
        lat, lon = float(m.group(1)), float(m.group(2))
        if 40 <= lat <= 84 and -142 <= lon <= -50:
            return lat, lon
        if 40 <= lat <= 84 and 50 <= lon <= 142:      # longitude written without the minus sign
            return lat, -lon
    return None, None


_WATER_EN = (r"((?:[A-Z][\w'’.\-]*\s(?:(?:aux|de|du|des|la|le|à)\s)?){1,3}"
             r"(?:Harbour|Harbor|Bay|River|Basin|Strait|Sound|Inlet|Cove|Arm|Gulf|Channel|Estuary|Passage|Fjord))\b")
_WATER_FR = (r"\b((?:baie|fleuve|rivière|havre|golfe|bassin|anse|détroit|estuaire|port)\s"
             r"(?:de la\s|de l'|du\s|des\s|de\s|d')?[A-Z][\w'’\-]+(?:[\s\-][A-Z][\w'’\-]+){0,2})")
_NOT_WATER = re.compile(r"^(The|This|A|An|In|At|On|Of|From|Project|Proposed)\s", re.IGNORECASE)


def extract_water_body(text: str) -> str:
    """Most often named water body, e.g. 'Fraser River' or 'baie de Gaspé'. 'unknown' if none."""
    counts: dict[str, int] = {}
    for pat in (_WATER_EN, _WATER_FR):
        for m in re.finditer(pat, text):
            name = _NOT_WATER.sub("", m.group(1).strip())
            if len(name) > 3:
                counts[name] = counts.get(name, 0) + 1
    if not counts:
        return UNKNOWN
    return max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


# "No dredging is required", "without blasting", "aucun dragage": the keyword is negated.
_NEGATION = re.compile(r"\b(no|not|without|nor|aucun|aucune|pas de|sans)\b[^.;:]{0,25}$", re.IGNORECASE)


def is_negated(text: str, start: int) -> bool:
    return bool(_NEGATION.search(text[max(0, start - 40):start]))


_CLAUSE_END = re.compile(r"[.;:!?\n]")


def clause_around(text: str, start: int, end: int) -> tuple[str, str]:
    """(text of the clause before the match, text of the clause after it). Clauses end at . ; : ! ? or a new line."""
    before = text[max(0, start - 300):start]
    cut = max((m.end() for m in _CLAUSE_END.finditer(before)), default=0)
    after = text[end:end + 300]
    m = _CLAUSE_END.search(after)
    return before[cut:], after[:m.start()] if m else after


def sentence_around(text: str, start: int, end: int) -> str:
    """The whole sentence holding the match (used by exclude_sentence and require_sentence rules)."""
    left = max((m.end() for m in re.finditer(r"[.!?\n]", text[max(0, start - 600):start])), default=0)
    left = max(0, start - 600) + left
    m = re.search(r"[.!?\n]", text[end:end + 600])
    return text[left:end + (m.end() if m else 600)]


def strict_negation(conf: dict):
    """A stricter negation test used only by buyer profiles (general scoring keeps is_negated).
    A match does not count when, in the same clause, a negation word comes before it ("does not propose
    ... the existing wharf") or a condition word comes before or after it ("if required", "where necessary")."""
    before = re.compile("|".join(conf.get("negation_before", []) + conf.get("conditional_before", [])), re.IGNORECASE)
    after_pats = conf.get("conditional_after", [])
    after = re.compile("|".join(after_pats), re.IGNORECASE) if after_pats else None

    def negated(text: str, start: int, end: int | None = None) -> bool:
        if is_negated(text, start):
            return True
        b, a = clause_around(text, start, end if end is not None else start)
        return bool(before.search(b)) or bool(after and after.search(a[:80]))
    return negated


_NAME_BEFORE = re.compile(r"\b[A-Z][a-z]+\s+$")
_NAME_AFTER = re.compile(r"^\s+(?:berth|road|street|lane|avenue|park|drive)\b", re.IGNORECASE)


def is_name(text: str, m: re.Match) -> bool:
    """A capitalised structure word inside a name, such as "West Dock berth" or "Dock Road": not a structure."""
    word = m.group(0).strip()
    return bool(word[:1].isupper() and _NAME_BEFORE.search(text[max(0, m.start() - 30):m.start()])
                and _NAME_AFTER.match(text[m.end():m.end() + 20]))


def _match_counts(text: str, m: re.Match, rule: dict, negated) -> bool:
    if negated is is_negated:
        if is_negated(text, m.start()):
            return False
    elif negated(text, m.start(), m.end()):
        return False
    if rule.get("exclude_names") and is_name(text, m):
        return False
    if rule.get("exclude_sentence") or rule.get("require_sentence"):
        s = sentence_around(text, m.start(), m.end())
        if rule.get("exclude_sentence") and re.search(rule["exclude_sentence"], s, re.IGNORECASE):
            return False
        if rule.get("require_sentence") and not re.search(rule["require_sentence"], s, re.IGNORECASE):
            return False
    return True


def lang_ok(pattern: str, language: str | None) -> bool:
    """French patterns (marked "(?#fr)") count only in French notices, or when the language is not known."""
    return not pattern.startswith("(?#fr)") or language in (None, "", "unknown", "fr")


def find_patterns(text: str, groups: dict, negated=is_negated, language: str | None = None) -> dict:
    """For each rule group in the scoring config, the first non-negated match and its sentence.
    Optional per-group keys (buyer profiles only): exclude_sentence (skip a match whose sentence matches)
    and require_sentence (count a match only when its sentence also matches)."""
    hits = {}
    for name, rule in groups.items():
        for pat in rule["patterns"]:
            if not lang_ok(pat, language):
                continue
            m = next((m for m in re.finditer(pat, text, re.IGNORECASE) if _match_counts(text, m, rule, negated)), None)
            if m:
                hits[name] = {"label": rule.get("label", name), "points": rule["points"],
                              "matched": m.group(0), "quote": snippet(text, m.start(), m.end())}
                break
    return hits


def has_marine_context(text: str, patterns: list[str], language: str | None = None) -> dict | None:
    for pat in patterns:
        if not lang_ok(pat, language):
            continue
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return {"matched": m.group(0), "quote": snippet(text, m.start(), m.end())}
    return None
