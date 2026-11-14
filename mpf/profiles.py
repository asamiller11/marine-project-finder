"""Buyer profiles: the same transparent score, re-weighted for one kind of habitat work.

"All habitat work" is the general score (mpf/score.py), unchanged. Each other profile in
config/scoring.yaml re-weights the general rule groups, adds its own keyword groups, and lists
"core" groups: a project with no core match is not listed for that profile. Profiles also use a
stricter negation test, skip sizes read from sentences about past works, and (hard-structure only)
keep small routine repairs at Weak unless the notice has clear habitat words.

These are rule-based guesses. A flag never states that mitigation is required.
"""
from __future__ import annotations

import copy
import json
import re

from mpf.extract import extract_sizes, find_patterns, largest_sizes, strict_negation
from mpf.score import score_text
from mpf.tiers import TIER_LABELS, tier_for

LANGS = ("en", "fr", "da", "no", "nl", "es")
ALL = "all"
ALL_LABEL = "All habitat work"
PROFILE_TIERS = ("strong", "possible", "weak")
EMPTY_NOTE = "No current projects match. Today's sources rarely describe this habitat."


def _group(conf: dict, points: int) -> dict:
    # French patterns are marked so they count only in French notices (mpf.extract.lang_ok).
    g = {"label": conf["label"], "points": points,
         "patterns": [("(?#fr)" + conf[k]) if k == "fr" else conf[k] for k in LANGS if conf.get(k)]}
    for key in ("exclude_sentence", "require_sentence"):
        if conf.get(key):
            g[key] = conf[key]
    return g


def _weighted(name: str, points: int, base: dict, extra: dict, drop: dict, group_rules: dict | None = None) -> dict:
    if name in extra:
        g = _group(extra[name], points)
    else:
        g = dict(base[name], points=points)
        if name in drop:
            g["patterns"] = [p for p in g["patterns"] if p not in drop[name]]
    g.update((group_rules or {}).get(name) or {})
    return g


def build_profiles(rules: dict) -> dict:
    """Profile key -> ready-to-score settings. rules is load_scoring() (general rules plus profiles)."""
    extra = rules.get("profile_groups") or {}
    pr = rules.get("profile_rules") or {}
    out = {}
    for key, p in (rules.get("profiles") or {}).items():
        drop = p.get("drop_patterns") or {}
        r = copy.deepcopy({k: v for k, v in rules.items() if k not in ("profiles", "profile_groups", "profile_rules")})
        gr = p.get("group_rules") or {}
        r["project_types"] = {n: _weighted(n, v, rules["project_types"], extra, drop, gr)
                              for n, v in p.get("types", {}).items() if v > 0}
        r["habitat_clues"] = {n: _weighted(n, v, rules["habitat_clues"], extra, drop, gr)
                              for n, v in p.get("clues", {}).items() if v > 0}
        out[key] = {"key": key, "label": p["label"], "short": p.get("short", ""), "core": set(p["core"]),
                    "audience": p.get("audience", "comment"), "exclude_freshwater": bool(p.get("exclude_freshwater")),
                    "rules": r, "habitat_words": set(p.get("habitat_words") or []),
                    "repair": _group(extra["routine_repair"], 0) if p.get("routine_repair_cap") and "routine_repair" in extra else None,
                    "repair_limits": pr.get("routine_repair") or {}}
    return out


class ProfileScorer:
    def __init__(self, rules: dict):
        self.profiles = build_profiles(rules)
        pr = rules.get("profile_rules") or {}
        self.negated = strict_negation(pr)
        doubt, before = pr.get("doubtful_size_sentence"), pr.get("doubtful_size_before")
        self._doubt = re.compile(doubt, re.IGNORECASE) if doubt else None
        self._before = re.compile(before, re.IGNORECASE) if before else None

    def size_ok(self, text: str, size: dict) -> bool:
        """False for a doubtful size: read from a sentence about past works, or written as a distance."""
        quote = size.get("quote") or ""
        if self._doubt and self._doubt.search(quote):
            return False
        if self._before:
            i = quote.find(size.get("matched") or "\0")
            if i >= 0 and self._before.search(quote[max(0, i - 30):i]):
                return False
        return True

    def score(self, text: str, title: str, key: str, tiers_conf: dict, language: str | None = None) -> dict:
        """{"score", "core", "because", "notes"} for one profile. score 0 = not listed for it."""
        p = self.profiles[key]
        s = score_text(text, p["rules"], negated=self.negated, size_ok=self.size_ok, language=language)
        hits = set(s["facts"]["types"]) | set(s["facts"]["habitat_clues"])
        core = hits & p["core"]
        if not core:
            return {"score": 0, "core": [], "because": "", "notes": []}
        score, notes = s["score"], []
        dropped = [x for x in extract_sizes(text) if not self.size_ok(text, x)]
        if dropped:
            notes.append(f"doubtful size reading not counted ({dropped[0]['matched']})")
        if p["repair"]:
            lim = p["repair_limits"]
            big = largest_sizes([x for x in extract_sizes(text) if self.size_ok(text, x)])
            small = bool(big) and big.get("length_m", 0) < lim.get("max_length_m", 100) \
                and big.get("area_m2", 0) < lim.get("max_area_m2", 2000) \
                and big.get("volume_m3", 0) < lim.get("max_volume_m3", 5000)
            repair = find_patterns(f"{title}. {text}", {"r": p["repair"]}, self.negated, language)
            cap = tiers_conf.get("possible_min", 35) - 1
            if repair and small and not (hits & p["habitat_words"]) and score > cap:
                notes.append("small routine repair, kept at Weak")
                score = cap
        return {"score": score, "core": sorted(core), "because": because(s["reasons"], core, notes), "notes": notes}


def because(reasons: list[dict], core: set, notes: list[str]) -> str:
    """Plain words: core matches first, then the next strongest signals. At most three, no duplicates."""
    pos = [r for r in reasons if r["points"] > 0 and not r["rule"].startswith("size:")]
    first = [r for r in pos if r["rule"].split(":", 1)[1] in core]
    rest = sorted((r for r in pos if r not in first), key=lambda r: -r["points"])
    labels = []
    for r in first + rest:
        if r["label"] not in labels:
            labels.append(r["label"])
    sizes = [r for r in reasons if r["rule"].startswith("size:") and r["points"] > 0]
    text = "; ".join(labels[:3])
    if sizes:
        text += f"; {sizes[0]['label'].replace('Size: ', 'size ')}"
    if notes:
        text += f" ({'; '.join(notes)})"
    return text


def profile_tier(row: dict, score: int, tiers_conf: dict, general_tier: str) -> str:
    """Tier for one profile. Withdrawn projects and quarries stay out, as in the general list."""
    if general_tier == "watch":
        return "watch"
    if score <= 0:
        return "notrel"
    return tier_for({**row, "score": score}, tiers_conf)[0]


# Sources shown under one profile only, scored by their own rule (stored in facts.iha), at Possible or Weak.
PROFILE_ONLY_SOURCES = {"fed_register": "hard-structure"}


def _own_rule_result(row: dict, tiers_conf: dict) -> dict:
    """Profile result for a profile-only source: never Strong, Weak allowed."""
    try:
        facts = row.get("facts") if isinstance(row.get("facts"), dict) else json.loads(row.get("facts") or "{}")
    except ValueError:
        facts = {}
    iha = facts.get("iha") or {}
    score = int(iha.get("score") or 0)
    tier = profile_tier(row, score, tiers_conf, "notrel")
    tier = {"strong": "possible"}.get(tier, tier)
    return {"tier": tier, "tier_label": TIER_LABELS[tier], "score": min(score, int(tiers_conf.get("strong_min", 60)) - 1),
            "because": iha.get("because") or "", "min": int(tiers_conf.get("weak_min", 20))}


def profile_results(row: dict, general_tier: str, scorer: ProfileScorer, tiers_conf: dict) -> dict:
    """Profile key -> {"tier", "tier_label", "score", "because"} for every profile."""
    own = PROFILE_ONLY_SOURCES.get(row.get("source"))
    if own:
        return {own: _own_rule_result(row, tiers_conf)} if own in scorer.profiles else {}
    out = {}
    for key in scorer.profiles:
        r = scorer.score(row.get("full_text") or "", row.get("title") or "", key, tiers_conf, row.get("language"))
        tier = profile_tier(row, r["score"], tiers_conf, general_tier)
        out[key] = {"tier": tier, "tier_label": TIER_LABELS[tier], "score": r["score"], "because": r["because"]}
    return out


def listed_in_profile(rec: dict, key: str, possible_min: int = 35) -> bool:
    """Shown under this profile? Projects not on the general list need Possible or better."""
    p = (rec.get("profiles") or {}).get(key)
    if not p or p["tier"] not in PROFILE_TIERS + ("watch",):
        return False
    if rec.get("tier") in PROFILE_TIERS + ("watch",):
        return True
    return p["score"] >= p.get("min", possible_min)


def is_freshwater(row: dict, conf: dict | None) -> bool:
    """Lake or inland river work: the notice names a lake and nothing salt-water, or the project is in an
    inland province or territory and does not mention Hudson Bay or James Bay. conf: scoring.yaml freshwater."""
    if not conf:
        return False
    text = row.get("full_text") or ""
    clean = re.sub(conf.get("ignore") or r"(?!x)x", " ", text)
    lake = re.search(conf["lake"], text)
    if lake and not re.search(conf["salt"], clean):
        return True
    exceptions = conf.get("inland_salt_exceptions")
    return (row.get("province") in set(conf.get("inland_regions") or [])
            and not (exceptions and re.search(exceptions, text)))
