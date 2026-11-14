"""Round 4: moorings count only with a structure larger than one float, the two Ardersier notices are one entry,
and the About page says Federal Register cards show no quotes."""
import json
from datetime import date
from pathlib import Path

from mpf import db, site
from mpf.config import load_scoring, load_settings
from mpf.models import Item
from mpf.score import score_text
from mpf.sources import scot_marine

R = load_scoring()
S = load_settings()
TODAY = date(2026, 11, 14)

DOCK = ("New dock installation – 2820 Panorama Drive. The gangway, ramp and mooring float will be constructed off "
        "site and installed using a crane on a floating barge in the ocean. The float would be held in place in the "
        "water by two steel piles.")


def _rules(r):
    return {x["rule"] for x in r["reasons"]}


def test_one_private_dock_float_gets_no_mooring_points():
    s = score_text(DOCK, R, language="en")
    assert "type:moorings" not in _rules(s)
    assert s["score"] < 20


def test_moorings_count_with_a_larger_structure():
    for extra in ("The marina has 40 berths.", "New pontoons and floats will be added.",
                  "A marine terminal with four shore-based mooring bollards.", "Repairs to the wharf."):
        s = score_text(f"Moorings and dock works in the harbour. {extra}", R, language="en")
        assert "type:moorings" in _rules(s), extra


def test_a_single_pontoon_is_still_one_float():
    s = score_text("A new pontoon with moorings in the bay.", R, language="en")
    assert "type:moorings" not in _rules(s)


def _doc(title, url, updated):
    return {"title": title, "url": url, "updated": updated}


def test_ardersier_notices_are_merged_into_one_entry():
    groups = {
        "00011871, 00011872 & 00011873": [_doc("Additional Information - Ardersier Port Expansion Project - Ardersier Port",
                                               "https://marine.gov.scot/?q=node/28283", "2026-10-29")],
        "additional information public notice ardersier port expansion phase 2": [
            _doc("Additional Information Public Notice - Ardersier Port Expansion Phase 2",
                 "https://marine.gov.scot/?q=node/28304", "2026-11-06")],
        "moorings dalgety bay": [_doc("Moorings - Dalgety Bay", "https://marine.gov.scot/?q=node/1", "2026-11-01")],
    }
    out = scot_marine.merge_same_project(groups, S["sources"]["scot_marine"]["same_project"])
    assert len(out) == 2 and "moorings dalgety bay" in out
    merged = out["additional information public notice ardersier port expansion phase 2"]
    assert {d["url"] for d in merged} == {"https://marine.gov.scot/?q=node/28283", "https://marine.gov.scot/?q=node/28304"}


def test_no_phrase_list_changes_nothing():
    groups = {"a": [_doc("Moorings - A Bay", "u1", "2026-11-01")], "b": [_doc("Moorings - B Bay", "u2", "2026-11-02")]}
    assert scot_marine.merge_same_project(groups, []) == groups


def test_older_notice_carried_by_a_newer_entry_is_not_shown():
    conn = db.connect(Path(":memory:"))
    text = "Port expansion with a new quay wall, dredging of the harbour and rock armour along the shore."
    old = Item(source="scot_marine", item_id="old", url="https://marine.gov.scot/?q=node/28283",
               title="Ardersier Port Expansion Project", full_text=text, country="GB", language="en",
               extra={"documents": [{"title": "x", "url": "https://marine.gov.scot/?q=node/28283", "updated": "2026-10-29"}]})
    new = Item(source="scot_marine", item_id="new", url="https://marine.gov.scot/?q=node/28304",
               title="Ardersier Port Expansion Phase 2", full_text=text, country="GB", language="en",
               extra={"documents": [{"title": "y", "url": "https://marine.gov.scot/?q=node/28304", "updated": "2026-11-06"},
                                    {"title": "x", "url": "https://marine.gov.scot/?q=node/28283", "updated": "2026-10-29"}]})
    db.upsert_item(conn, old)
    db.upsert_item(conn, new)
    conn.execute("UPDATE items SET score=45, project_types='[]', reasons='[]', facts='{}', stage='unknown'")
    conn.execute("UPDATE items SET last_seen='2026-11-13' WHERE item_id='old'")
    conn.execute("UPDATE items SET last_seen='2026-11-14' WHERE item_id='new'")
    settings = dict(S, sources={"scot_marine": {"enabled": True}})
    records, skipped = site.build_public_records(conn, {}, settings, TODAY)
    assert [r["title"] for r in records] == ["Ardersier Port Expansion Phase 2"]
    assert skipped["merged_duplicates"] == 1


def test_about_page_names_the_federal_register_for_no_quotes():
    src = Path(site.__file__).read_text()
    assert "US Federal Register notices also show no quotes" in src
