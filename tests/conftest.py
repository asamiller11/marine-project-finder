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
