"""Coverage config (config/coverage.yaml): countries, regions, statuses and sources."""
from __future__ import annotations

from pathlib import Path

import yaml

from mpf.config import ROOT

STATUS_LABELS = {"covered": "Covered", "partial": "Partial", "coming_soon": "Coming soon"}


def load_coverage(path: Path | None = None) -> dict:
    path = path or ROOT / "config" / "coverage.yaml"
    with open(path, encoding="utf-8") as f:
        cov = yaml.safe_load(f)
    problems = check_coverage(cov)
    if problems:
        raise ValueError("config/coverage.yaml: " + "; ".join(problems))
    return cov


def check_coverage(cov: dict) -> list[str]:
    """Catch mistakes such as an unknown status, or a 'covered' place with no source."""
    problems = []
    known = set(cov.get("sources", {}))
    for c in cov.get("countries", []):
        if not isinstance(c.get("code"), str):
            problems.append(f"{c.get('name', '?')}: country code must be quoted text, e.g. code: \"NO\" "
                            f"(unquoted NO is read as false)")
        for place in [c] + list(c.get("regions", [])):
            name = place.get("name", "?")
            status = place.get("status")
            if status not in STATUS_LABELS:
                problems.append(f"{name}: unknown status '{status}'")
            srcs = place.get("sources", []) or []
            if status in ("covered", "partial") and not srcs:
                problems.append(f"{name}: status '{status}' needs at least one source")
            if status == "coming_soon" and srcs:
                problems.append(f"{name}: 'coming_soon' cannot list sources")
            for s in srcs:
                if s not in known:
                    problems.append(f"{name}: source '{s}' is not defined under sources")
    return problems


def country_by_code(cov: dict) -> dict[str, dict]:
    return {c["code"]: c for c in cov.get("countries", [])}


def source_names(cov: dict, keys: list[str]) -> str:
    srcs = cov.get("sources", {})
    return ", ".join(srcs[k]["name"] for k in keys if k in srcs)
