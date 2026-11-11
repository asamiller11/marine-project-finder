"""US Federal Register reader (NOAA incidental harassment notices), tested on saved API responses only."""
import json
from pathlib import Path

import pytest

from mpf.config import load_settings
from mpf.http import Blocked
from mpf.sources import LIVE_SOURCES, SOURCES
from mpf.sources.fed_register import (FedRegisterSource, doc_to_item, is_noaa, parse_results, project_and_place,
                                      search_url, stage_of)

SAMPLES = Path(__file__).parent / "samples"
PAGE1 = (SAMPLES / "fed_register_iha_10.json").read_bytes()
PAGE2 = (SAMPLES / "fed_register_iha_page2.json").read_bytes()


def _docs(data=PAGE1):
    return parse_results(data)[1]


def test_switched_off_and_not_in_the_scheduled_run():
    conf = load_settings()["sources"]["fed_register"]
    assert conf["enabled"] is False
    assert "fed_register" in SOURCES and "fed_register" not in LIVE_SOURCES


def test_search_url_is_small_stable_and_on_the_api_path():
    url = search_url(per_page=10)
    assert url.startswith("https://www.federalregister.gov/api/v1/documents.json?")
    assert "per_page=10" in url and "order=newest" in url
    assert url == search_url(per_page=10)          # same address every time, so the cache works


def test_parse_counts_and_keeps_only_noaa():
    total, docs = parse_results(PAGE1)
    assert total == 946 and len(docs) == 10
    kept = [d for d in docs if is_noaa(d)]
    assert len(kept) == 9                           # the Fish and Wildlife Service polar bear rule is dropped
    assert not any("Polar Bears" in d["title"] for d in kept)


def test_item_fields_from_a_proposed_notice():
    doc = next(d for d in _docs() if "Seawalk" in d["title"])
    it = doc_to_item(doc)
    assert it.source == "fed_register" and it.country == "US" and it.language == "en"
    assert it.title == "City and Borough of Juneau's Seawalk Extension Project, Juneau, Alaska"
    assert it.province == "Alaska" and it.water_body == "Juneau"
    assert it.proponent == "City and Borough of Juneau"
    assert it.stage == "comment period open" and it.comment_deadline == "2026-11-12"
    assert it.decision_date is None
    assert it.url.startswith("https://www.federalregister.gov/documents/2026/10/13/")
    assert "Seawalk" in it.full_text                 # the text we score is the notice's own words


def test_item_fields_from_an_issued_notice():
    doc = next(d for d in _docs() if "Seward" in d["title"] and "issuance" in d["action"])
    it = doc_to_item(doc)
    assert it.stage.startswith("authorized") and it.decision_date == "2026-10-22"
    assert it.proponent == "Turnagain Marine Services" and it.project_type_raw == "dock, terminal"


def test_stage_words():
    assert stage_of({"action": "Notice; proposed incidental harassment authorization; request for comments",
                     "comments_close_on": "2026-11-19"}) == "comment period open"
    assert stage_of({"action": "Notice; issuance of incidental harassment authorization."}).startswith("authorized")
    assert stage_of({"action": "Final rule; notification of issuance of Letter of Authorization."}).startswith("authorized")


def test_project_is_after_the_last_incidental_to():
    title = ("Takes of Marine Mammals Incidental to Specified Activities; Taking Marine Mammals Incidental to the "
             "Ferry Berth Construction in Tongass Narrows in Ketchikan, Alaska")
    assert project_and_place(title) == ("Ferry Berth Construction in Tongass Narrows in Ketchikan, Alaska", "Alaska")


def test_page_two_kinds_of_work():
    items = [doc_to_item(d) for d in _docs(PAGE2) if is_noaa(d)]
    by_title = {i.title: i for i in items}
    berth = by_title["Ferry Berth Construction in Tongass Narrows in Ketchikan, Alaska"]
    assert berth.project_type_raw == "ferry, berth" and berth.water_body == "Ketchikan"


class _Client:
    def __init__(self, data=None, error=None):
        self.data, self.error, self.calls = data, error, []

    def get(self, url, list_page=False, **kw):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.data

    def cached_path(self, url):
        return None


def test_fetch_makes_one_request_and_yields_noaa_items():
    client = _Client(PAGE1)
    src = FedRegisterSource(client, {}, {"per_page": 10})
    items = list(src.fetch())
    assert len(client.calls) == 1 and "per_page=10" in client.calls[0]
    assert len(items) == 9 and all(i.source == "fed_register" for i in items)
    assert "946 matching documents" in src.log[0]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_fetch_stops_on_a_block_and_does_not_retry(status):
    client = _Client(error=Blocked(f"https://www.federalregister.gov/api/v1/documents.json answered HTTP {status}."))
    src = FedRegisterSource(client, {}, {})
    assert list(src.fetch()) == []
    assert len(client.calls) == 1 and src.log[0].startswith("STOPPED")
