import os
import sys
import warnings
from pathlib import Path

import pytest

# Make "import mpf" work when running pytest from the project folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

# Pin "today" so day counts in tests never change.
os.environ["MPF_TODAY"] = "2026-10-28"

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture
def samples():
    return SAMPLES


@pytest.fixture
def rules():
    from mpf.config import load_scoring
    return load_scoring()


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _no_research_files(monkeypatch):
    """Site builds in tests do not read research/*.yaml (tests that need research pass settings["research"])."""
    from mpf import site
    monkeypatch.setattr(site, "_load_research", lambda: ({}, {}))
