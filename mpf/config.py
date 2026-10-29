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
    path = path or ROOT / "config" / "scoring.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


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
