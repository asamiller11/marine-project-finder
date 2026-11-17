"""Batch 3 readers (2026-11-17), on saved sample responses. All four are OFF in config."""
import sqlite3

import pytest

from mpf.config import load_settings
from mpf.sources import LIVE_SOURCES, SOURCES, fed_register_broad, fr_mce, nl_sru, noaa_ita
from mpf.site import source_display_name, source_line

NEW = ["noaa_ita", "fed_register_broad", "fr_mce", "nl_sru"]


def test_new_sources_are_registered_and_only_noaa_is_on():
    s = load_settings()["sources"]
    for n in NEW:
        assert n in SOURCES and n in LIVE_SOURCES
    assert s["noaa_ita"]["enabled"] is True
    assert not any(s[n]["enabled"] for n in ("fed_register_broad", "fr_mce", "nl_sru"))


def test_each_has_a_real_name_for_the_source_line():
    for n in NEW:
        name = source_display_name(n)
        assert name != "Public source"
        assert source_line({"source_name": name, "agency": ""}).startswith("Source: ")


def test_noaa_page(samples):
    rows = noaa_ita.parse_page((samples / "noaa_ita_construction.html").read_text(encoding="utf-8"))
    first = rows[0]
    assert first["section"] == "Authorizations In Process"
    assert "Kings Bay" in first["title"] and first["comment"] == "11/20/2026"
    assert first["url"].startswith("https://www.fisheries.noaa.gov/action/")
    it = noaa_ita.NOAAITASource(None, {}, {}).make_item(first)
    assert it.country == "US" and it.province == "Georgia" and it.comment_deadline == "2026-11-20"
    assert {r["section"] for r in rows} == {"Authorizations In Process", "Active Authorizations"} and len(rows) == 70


def test_fed_register_broad(samples):
    res = fed_register_broad.parse_results((samples / "fedreg_broad_breakwater.json").read_bytes())
    items = [fed_register_broad.FedRegisterBroadSource(None, {}, {}).make_item(d) for d in res
             if d.get("type") not in fed_register_broad.SKIP_TYPES]
    assert items and all(i.url.startswith("https://www.federalregister.gov/") for i in items)
    assert not any("Safety Zone" in i.title for i in items)          # a rule, skipped


def test_nl_sru_records(samples):
    recs = nl_sru.parse_records((samples / "nl_sru_kademuur.xml").read_bytes())
    assert len(recs) == 5 and recs[0]["id"] == "gmb-2015-111143"
    assert recs[0]["url"] == "https://zoek.officielebekendmakingen.nl/gmb-2015-111143.html"
    it = nl_sru.NLSRUSource(None, {}, {}).make_item(recs[0])
    assert it.country == "NL" and it.language == "nl" and it.proponent == "Amersfoort"


def test_nl_sru_query_has_date_filter():
    u = nl_sru.search_url("kademuur", "2026-10-18", 50)
    assert u.startswith("https://repository.overheid.nl/sru?") and "2026-10-18" in u and "maximumRecords=50" in u


def test_fr_mce_reads_a_geopackage_and_keeps_coastal_measures(tmp_path):
    db = tmp_path / "m.gpkg"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
    con.execute("INSERT INTO gpkg_contents VALUES ('l_mesure', 'features')")
    con.execute("CREATE TABLE l_mesure (fid INTEGER, nom_projet TEXT, libelle_mesure TEXT, commune TEXT, geom BLOB)")
    con.execute("INSERT INTO l_mesure VALUES (1, 'Extension du port de Brest', 'Restauration d''herbier de zostère', 'Brest', x'00')")
    con.execute("INSERT INTO l_mesure VALUES (2, 'Déviation routière', 'Plantation de haies', 'Vichy', x'00')")
    con.commit(); con.close()
    rows = fr_mce.read_measures(db)
    assert len(rows) == 2 and "geom" not in rows[0]
    src = fr_mce.FRMCESource(None, {}, {})
    items = [i for i in (src.make_item(r) for r in rows) if i]
    assert [i.title for i in items] == ["Extension du port de Brest: Restauration d'herbier de zostère"]


def test_fr_mce_picks_the_newest_geopackage():
    import json
    data = json.dumps({"resources": [
        {"format": "gpkg", "created_at": "2026-10-13", "url": "old"}, {"format": "zip", "created_at": "2026-11-05", "url": "z"},
        {"format": "gpkg", "created_at": "2026-11-05", "url": "new"}]}).encode()
    assert fr_mce.newest_gpkg(data)["url"] == "new"


# --- 2026-11-17 owner decisions -------------------------------------------------------------------------------

def test_noaa_active_is_approved_and_finished_windows_are_skipped():
    assert noaa_ita.window_ended("9/1/2025 - 8/31/2026", "2026-11-17")
    assert not noaa_ita.window_ended("9/1/2026 - 8/31/2027", "2026-11-17")
    assert not noaa_ita.window_ended("--", "2026-11-17")
    r = {"section": "Active Authorizations", "title": "Cold Bay Ferry Terminal Reconstruction Project in Cold Bay, Alaska",
         "status": "Active", "comment": "Closed", "issued": "3/16/2026", "effective": "5/1/2028 - 4/30/2029",
         "summary": "", "url": "https://www.fisheries.noaa.gov/action/x"}
    it = noaa_ita.NOAAITASource(None, {}, {}).make_item(r)
    from mpf.timing import compute
    from datetime import date
    assert it.decision_date == "2026-03-16"
    assert compute(it.to_dict(), date(2026, 11, 17))["stage"] == "decision made"


def test_noaa_is_shown_under_hard_structure_only():
    from mpf.profiles import PROFILE_ONLY_SOURCES, _own_rule_result
    assert PROFILE_ONLY_SOURCES["noaa_ita"] == "hard-structure"
    res = _own_rule_result({"facts": {"iha": {"score": 23, "because": "Port or marine terminal"}}, "title": "x"},
                           {"weak_min": 20, "possible_min": 35, "strong_min": 60})
    assert res["tier"] == "weak" and res["score"] == 23


def test_iaac_recent_changes_page(samples):
    from mpf.sources import iaac
    rows = iaac.parse_recent_changes((samples / "iaac_recent_changes.html").read_text(encoding="utf-8"))
    assert len(rows) == 50 and rows[0] == ("90624", "2026-11-16")
    assert ("80913", "2026-11-12") in rows          # an old project number, changed recently
