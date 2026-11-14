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


def test_switched_on_and_in_the_scheduled_run():
    conf = load_settings()["sources"]["fed_register"]
    assert conf["enabled"] is True
    assert "fed_register" in SOURCES and "fed_register" in LIVE_SOURCES


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


def test_fetch_reads_the_list_then_each_full_text_once():
    client = _Client(PAGE1)
    src = FedRegisterSource(client, {}, {"per_page": 10})
    items = list(src.fetch())
    assert "per_page=10" in client.calls[0]
    assert len(client.calls) == 1 + 9 and all("/documents/full_text/text/" in u for u in client.calls[1:])
    assert len(items) == 9 and all(i.source == "fed_register" for i in items)
    assert "946 matching documents" in src.log[0]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_fetch_stops_on_a_block_and_does_not_retry(status):
    client = _Client(error=Blocked(f"https://www.federalregister.gov/api/v1/documents.json answered HTTP {status}."))
    src = FedRegisterSource(client, {}, {})
    assert list(src.fetch()) == []
    assert len(client.calls) == 1 and src.log[0].startswith("STOPPED")


# Its own score (fix 6): title and project description only, plus pile counts; boilerplate ignored.
from mpf.config import load_scoring  # noqa: E402
from mpf.sources.fed_register import iha_score, pile_count, project_description  # noqa: E402

RULES = load_scoring()
BOILER = ("\nBackground\nThe MMPA prohibits the take of marine mammals (e.g., vessels, dredging, aircraft, construction). "
          "Mitigation measures protect Endangered species. Level B harassment may occur within 24,100 m.\n")
DESC = ("\nDescription of Proposed Activity\nOverview\nThe Navy proposes to remove 102 degraded 14-inch steel H-piles "
        "and install 112 20-inch square concrete replacement piles at the east and west wharfs.\n"
        "Description of Marine Mammals in the Area of Specified Activities\nEndangered whales.\n")
TABLE = "\nEstimated Take\nFender Piles (16'' steel pipe piles)      600\n   7,356.4   1,000\nFender Piles\n"


def test_description_section_only():
    d = project_description(BOILER + DESC + TABLE)
    assert "112 20-inch" in d and "Endangered" not in d and "24,100" not in d


def test_pile_count_from_sentences_not_tables():
    assert pile_count("install 230 24-inch steel pipe piles and 5 42-inch piles") == 230
    assert pile_count(TABLE) == 0
    assert pile_count("work in 2026 piles and steel") == 0            # a year is not a count


def test_iha_score_ignores_boilerplate():
    s = iha_score("TRF Wharf Interface Repairs Project at Kings Bay, Georgia", "", BOILER + DESC + TABLE, RULES)
    rules = {r["rule"] for r in s["reasons"]}
    assert "habitat:species_at_risk" not in rules and "habitat:mitigation_general" not in rules
    assert "type:dredging" not in rules            # the stock "(e.g., vessels, dredging ...)" list is not counted
    assert s["facts"]["piles"] == 112 and "size:piles" in rules
    assert 20 <= s["score"] < 60


def test_not_construction_titles_score_zero():
    for t in ("Office of Naval Research's Arctic Research Activities in the Beaufort Sea",
              "HEX Operating, LLC Natural Gas Activities in Cook Inlet, Alaska",
              "Marine Geophysical Survey in the Northwest Atlantic"):
        assert iha_score(t, "", BOILER + DESC, RULES)["score"] == 0


def test_more_piles_more_points():
    small = iha_score("Pier repairs", "", DESC.replace("112 20-inch", "6 20-inch"), RULES)["score"]
    big = iha_score("Pier repairs", "", DESC.replace("112 20-inch", "230 20-inch"), RULES)["score"]
    assert big > small
