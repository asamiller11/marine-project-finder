"""Australia, EPBC Act Public Portal (written permission, 2026-11-24), read with the hidden browser."""
from datetime import date

import pytest

from mpf.config import load_settings
from mpf.http import Blocked, PoliteClient
from mpf.sources import CHECK_URLS, LIVE_SOURCES, SOURCES
from mpf.sources.au_epbc import AUEPBCSource, item_id, parse_comment_rows, parse_detail, parse_referral_row
from mpf.timing import compute

# Rows and fields as the page scripts return them (copied from the live portal on 2026-11-24).
REFERRAL_ROW = {"EPBC Number": "2026/10673", "Project": "Gold Coast Oceanway Project",
                "Proposer/Approval Holder": "Gold Coast City Council",
                "Location": "Mermaid Esplanade and Los Angeles Esplanade, Mermaid Beach",
                "Industry Type": "Tourism and Recreation", "Valid Date": "4/11/2026",
                "Project Status": "Awaiting Delegate Decision", "Primary Jurisdiction": "Queensland",
                "_link": "https://epbcpublicportal.environment.gov.au/all-referrals/project-referral-summary/?id=e690f974"}
DETAIL = {"ticketnumber": "2026/10673", "mara_personproposingaction_name": "Gold Coast City Council",
          "mara_industrytype_name": "Tourism and Recreation", "mara_primarymarajurisdiction_name": "Queensland",
          "mara_location": "Mermaid Esplanade and Los Angeles Esplanade, Mermaid Beach and Jefferson Esplanade, Palm Beach.",
          "mara_proposedactionoverview": "<p>City of Gold Coast is proposing the renewal and extension of the Gold Coast "
                                         "Oceanway, a coastal path&nbsp;along the beach front.</p><p>Second part.</p>",
          "_status": "Awaiting Delegate Decision"}


def test_registered_with_the_filed_permission():
    settings = load_settings()
    assert "au_epbc" in SOURCES and "au_epbc" in LIVE_SOURCES and "au_epbc" in CHECK_URLS
    conf = settings["sources"]["au_epbc"]
    assert conf["enabled"] is True
    perm = conf["permission"]
    assert perm["basis"] == "a" and perm["date"] == "2026-11-24" and "publicly available" in perm["quote"]
    assert settings["http"]["host_delays"]["epbcpublicportal.environment.gov.au"] >= 5


def test_referral_row_and_bad_rows():
    r = parse_referral_row(REFERRAL_ROW)
    assert r["epbc"] == "2026/10673" and r["valid"] == "2026-11-04" and r["jurisdiction"] == "Queensland"
    assert r["status"] == "Awaiting Delegate Decision" and r["url"].endswith("?id=e690f974")
    assert parse_referral_row({**REFERRAL_ROW, "EPBC Number": "EPBC Number"}) is None     # header-like row
    assert parse_referral_row({**REFERRAL_ROW, "_link": ""}) is None
    assert item_id("2026/10673") == "2026-10673"


def test_comment_rows_keep_the_first_deadline_per_project():
    rows = [{"EPBC Number": "2026/10695", "Notice Name": "Invitation for Public Comment on Referral",
             "Start Date": "23/11/2026", "Due Date": "8/12/2026", "_link": "https://x/a"},
            {"EPBC Number": "2026/10695", "Notice Name": "Later notice", "Due Date": "20/12/2026", "_link": "https://x/b"},
            {"EPBC Number": "", "Due Date": "1/12/2026"}]
    out = parse_comment_rows(rows)
    assert list(out) == ["2026/10695"] and out["2026/10695"]["due"] == "2026-12-08"
    assert out["2026/10695"]["start"] == "2026-11-23"


def test_detail_strips_html_and_item_has_deadline():
    d = parse_detail(DETAIL)
    assert d["description"] == ("City of Gold Coast is proposing the renewal and extension of the Gold Coast Oceanway, "
                                "a coastal path along the beach front. Second part.")
    assert d["proponent"] == "Gold Coast City Council" and d["industry"] == "Tourism and Recreation"
    src = AUEPBCSource(client=None, settings={}, source_settings={})
    item = src.make_item(parse_referral_row(REFERRAL_ROW), d, {"notice": "Invitation", "due": "2026-12-08", "url": "u"})
    assert item.key == "au_epbc:2026-10673" and item.country == "AU" and item.province == "Queensland"
    assert item.comment_deadline == "2026-12-08" and item.project_type_raw == "Tourism and Recreation"
    assert "coastal path" in item.full_text and item.extra["decision_made"] is False


def test_referral_decision_made_is_not_a_final_decision():
    """EPBC 'Referral Decision Made' only decides whether a full assessment is needed."""
    src = AUEPBCSource(client=None, settings={}, source_settings={})
    row = {**parse_referral_row(REFERRAL_ROW), "status": "Referral Decision Made"}
    item = src.make_item(row, parse_detail(DETAIL), None).to_dict()
    assert compute(item, date(2026, 11, 24))["stage"] != "decision made"
    final = src.make_item({**row, "status": "Approved"}, parse_detail(DETAIL), None).to_dict()
    assert compute(final, date(2026, 11, 24))["stage"] == "decision made"
    # Other sources keep the old rule.
    other = {**item, "source": "ns_ea"}
    assert compute(other, date(2026, 11, 24))["stage"] == "decision made"


def test_browser_visit_keeps_the_polite_rules(tmp_path, monkeypatch):
    c = PoliteClient(load_settings(), tmp_path)
    waits = []
    monkeypatch.setattr(c, "_wait", lambda host, delay: waits.append(delay))
    monkeypatch.setattr(c, "_robots_for", lambda url: type("Rb", (), {
        "allowed": lambda self, p: not p.startswith("/private"), "crawl_delay": None})())
    c.browser_visit("https://epbcpublicportal.environment.gov.au/all-referrals/")
    assert c.network_requests == 1 and waits[0] >= 5
    with pytest.raises(Blocked):
        c.browser_visit("https://epbcpublicportal.environment.gov.au/private/x")
    c.refused_hosts["epbcpublicportal.environment.gov.au"] = "HTTP 429"
    with pytest.raises(Blocked):
        c.browser_visit("https://epbcpublicportal.environment.gov.au/all-referrals/")


def test_no_browser_means_a_skip_note_not_an_error(tmp_path, monkeypatch):
    import mpf.browser
    monkeypatch.setattr(mpf.browser, "_PATTERNS", [str(tmp_path / "nothing-*")])
    src = AUEPBCSource(PoliteClient(load_settings(), tmp_path), load_settings(), {})
    assert list(src.fetch()) == []
    assert src.log and src.log[0].startswith("skipped: Playwright's Chromium was not found")


def test_only_clearly_marine_projects_are_kept():
    from mpf.sources.au_epbc import is_marine
    # Title names marine work.
    assert is_marine("Sandy Bay Ferry Terminal", "")
    assert is_marine("Dungeness Breakwater and Dredging", "")
    # Two different marine things in the description.
    assert is_marine("Bribie Island Bridge Upgrade Project", "Piers in the intertidal and subtidal zones.")
    # Inland projects that mention one marine word, or only a coastal place name, are left out.
    assert not is_marine("Darnum Battery Energy Storage System", "Connects to the Gippsland offshore wind zone.")
    assert not is_marine("Placid Waters South Residential Development", "Near the foreshore reserve.")
    assert not is_marine("Sunshine Motorway Interchange", "Sunshine Coast, Moreton Bay. Endangered koala offset plan.")
    assert not is_marine("North Pine Dam Improvement", "Works on the dam shoreline; tidal; decommissioning.")


def test_fetch_leaves_out_inland_projects(tmp_path, monkeypatch):
    import json as _json
    import mpf.sources.au_epbc as mod
    rows = [{**REFERRAL_ROW, "Valid Date": "1/11/2026"},
            {**REFERRAL_ROW, "EPBC Number": "2026/10001", "Project": "Kingswood Battery Energy Storage System",
             "Valid Date": "1/11/2026", "_link": "https://x/?id=2"}]
    inland = {**DETAIL, "ticketnumber": "2026/10001", "mara_location": "Kingswood",
              "mara_proposedactionoverview": "A battery. Endangered species offset plan."}

    class FakeBrowser:
        def __init__(self, client): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(mod, "Browser", FakeBrowser)
    monkeypatch.setattr(mod.AUEPBCSource, "_read_list",
                        lambda self, b, url, stop_before: rows if url == mod.REFERRALS_URL else [])
    monkeypatch.setattr(mod.AUEPBCSource, "_read_detail",
                        lambda self, b, url: inland if url.endswith("?id=2") else DETAIL)
    monkeypatch.setattr(mod, "today", lambda: date(2026, 11, 24))
    client = type("C", (), {"cache_dir": tmp_path})()
    src = mod.AUEPBCSource(client, {}, {})
    items = list(src.fetch())
    assert [i.title for i in items] == ["Gold Coast Oceanway Project"]
    assert any("1 projects left out" in line for line in src.log)
    assert len(_json.loads((tmp_path / "au_epbc_details.json").read_text())) == 2   # both pages still cached
