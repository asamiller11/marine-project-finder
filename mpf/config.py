"""Loads the YAML config files."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

# The project folder (one level above this file's folder).
ROOT = Path(__file__).resolve().parent.parent


def load_settings(path: Path | None = None) -> dict:
    path = path or ROOT / "config" / "settings.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_scoring(path: Path | None = None) -> dict:
    """Scoring rules, with the per-language patterns merged into their groups."""
    path = path or ROOT / "config" / "scoring.yaml"
    with open(path, encoding="utf-8") as f:
        rules = yaml.safe_load(f)
    extra = rules.get("language_patterns") or {}
    rules["marine_context"] = list(rules["marine_context"]) + list((extra.get("marine_context") or {}).values())
    for group in ("project_types", "habitat_clues"):
        for name, per_lang in (extra.get(group) or {}).items():
            rules[group][name]["patterns"] = list(rules[group][name]["patterns"]) + list(per_lang.values())
    return rules


def data_dir(settings: dict) -> Path:
    d = ROOT / settings.get("data_dir", "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def drop_dir(settings: dict) -> Path:
    d = ROOT / settings.get("drop_dir", "drop")
    d.mkdir(parents=True, exist_ok=True)
    return d


def today_override() -> str | None:
    """Tests can pin "today" with MPF_TODAY=YYYY-MM-DD so day counts are stable."""
    return os.environ.get("MPF_TODAY")
