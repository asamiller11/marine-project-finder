"""Round 3: Spain read but not shown, Scotland exclusions and merged licences, the application form's own wording
not scored, the Federal Register under the hard-structure profile only, and French words only in French notices."""
import json
from datetime import date
from pathlib import Path

from mpf import db, site
from mpf.config import load_scoring, load_settings
from mpf.coverage import load_coverage
from mpf.models import Item
from mpf.profiles import listed_in_profile, profile_results, ProfileScorer
from mpf.score import score_text
from mpf.sources import scot_marine
from mpf.sources.fed_register import iha_because, iha_score
from mpf.tiers import tier_for

R = load_scoring()
S = load_settings()
TIERS = S["tiers"]
TODAY = date(2026, 11, 13)


# Spain: read and scored, not on the site; the coverage page says why.
def test_spain_is_read_but_not_public():
    assert S["sources"]["es_boe"]["enabled"] is True and S["sources"]["es_boe"]["public"] is False
    es = next(c for c in load_coverage()["countries"] if c["code"] == "ES")
    assert es["status"] == "coming_soon" and "still read every week" in es["note"]


def test_non_public_source_never_reaches_the_site():
    conn = db.connect(Path(":memory:"))
    text = "Construcción de un nuevo muelle de 300 metros en el puerto, con dragado de la dársena."
    db.upsert_item(conn, Item(source="es_boe", item_id="1", url="https://example.org/1", title="Nuevo muelle",
                              full_text=text, country="ES", language="es"))
    conn.execute("UPDATE items SET score=50, project_types='[]', reasons='[]', facts='{}', stage='unknown'")
    settings = dict(S, sources={"es_boe": {"enabled": True, "public": False}})
    records, _ = site.build_public_records(conn, {}, settings, TODAY)
    assert records == []
    settings = dict(S, sources={"es_boe": {"enabled": True}})
    records, _ = site.build_public_records(conn, {}, settings, TODAY)
    assert len(records) == 1


# Scotland: notices that are not a construction project stay off the list.
def test_scotland_exclusions():
    for t in ("European Protected Species Application- Cables- Eastern Green Link 1- EPS",
              "Basking Shark Licence- Subsea Cable Replacement - Loch a Choire, Loch Linnhe",
              "Marine Farm - Basta Voe, South Voe, Shetland",
              "Decision Notice - Safety Zone Application – Beatrice Offshore Wind Farm, Moray Firth",
              "Consultee Representations – MachairWind Offshore Wind Farm"):
        assert tier_for({"score": 60, "title": t}, TIERS)[0] == "notrel", t
    assert tier_for({"score": 60, "title": "Capital Dredging and Sea Deposit – Port of Dundee"}, TIERS)[0] == "strong"


def _doc(title, updated, url):
    return {"title": title, "updated": updated, "url": url}


def test_licences_for_one_job_are_merged_and_listed():
    groups = {"00012001": [_doc("Marine Licence Application - Subsea Cable Replacement - Loch a Choire, Loch Linnhe - 00012001", "2026-11-01", "a")],
              "eps": [_doc("European Protected Species Licence- Subsea Cable Replacement - Loch a Choire, Loch Linnhe", "2026-11-05", "b")],
              "bs": [_doc("Basking Shark Licence- Subsea Cable Replacement - Loch a Choire, Loch Linnhe", "2026-11-06", "c")],
              "alone": [_doc("European Protected Species Application- Cables- Eastern Green Link 1- EPS", "2026-11-06", "d")]}
    merged = scot_marine.merge_licences(groups)
    assert sorted(d["url"] for d in merged["00012001"]) == ["a", "b", "c"]
    assert [d["url"] for d in merged["alone"]] == ["d"] and len(merged) == 2
    src = scot_marine.ScotMarineSource(type("C", (), {"cached_path": lambda self, u: None})(), {}, {})
    item = src._make_item("00012001", sorted(merged["00012001"], key=lambda d: d["updated"], reverse=True),
                          {"fields": {}, "pdfs": []})
    assert item.title == "Subsea Cable Replacement - Loch a Choire, Loch Linnhe"
    assert item.extra["licence_types"] == ["Marine licence", "Basking shark licence", "European Protected Species licence"]


def test_licences_row_on_the_card():
    assert "<dt>Licences</dt><dd>Marine licence; Basking shark licence</dd>" in \
        site._licences_row({"licence_types": ["Marine licence", "Basking shark licence"]})
    assert site._licences_row({"licence_types": ["Marine licence"]}) == ""


# Scotland: the form's printed wording is not scored, the applicant's answers are.
FORM = ("When a proposed future dredging level is known this must also be shown on all sections.\n"
        "Bridges over tidal waters: An elevation with longitudinal and cross-sections of the bridge must show the dimensions.\n"
        "(c) Where the proposed work involves salt marsh feeding, beach replenishment or land reclamation the description "
        "of the substances.\nDisposal of Dredged Substance(s) or Object(s)\n"
        "3.3.5 The Licensee must minimise suspension of sediment.\n")


def test_form_instructions_are_dropped():
    own = "Replacement of 40 m of timber pontoon with aluminium pontoons in the harbour."
    kept = scot_marine.strip_form_text(FORM + own)
    assert own in kept and "dredging level" not in kept and "Bridges over tidal" not in kept and "Licensee" not in kept
    assert not ({"type:dredging", "type:infill_causeway"} & {r["rule"] for r in score_text(kept, R)["reasons"]})


def test_wording_shared_by_several_applications_is_dropped():
    shared = "Section 4 Details of the works to be carried out under this application form version three."
    texts = [shared + "\nProject A builds a new slipway.", shared + "\nProject B dredges the berth.",
             shared + "\nProject C repairs the pier."]
    common = scot_marine.common_sentences(texts, 3)
    out = scot_marine.strip_form_text(texts[0], common)
    assert "Project A builds a new slipway." in out and "Section 4" not in out


# Federal Register: hard-structure profile only, Possible or Weak, never habitat wording.
def _fr_row(score, because="pile driving, 230 piles; wharf, pier or large dock"):
    return {"source": "fed_register", "title": "Seawalk Extension Project, Juneau, Alaska", "full_text": "x",
            "facts": json.dumps({"iha": {"score": score, "because": because}}), "language": "en"}


def test_federal_register_only_under_hard_structure_and_never_strong():
    scorer = ProfileScorer(R)
    out = profile_results(_fr_row(80), "notrel", scorer, TIERS)
    assert list(out) == ["hard-structure"] and out["hard-structure"]["tier"] == "possible"
    assert out["hard-structure"]["score"] < TIERS["strong_min"]


def test_federal_register_weak_is_listed_under_the_profile():
    scorer = ProfileScorer(R)
    rec = {"tier": "notrel", "profiles": profile_results(_fr_row(24), "notrel", scorer, TIERS)}
    assert rec["profiles"]["hard-structure"]["tier"] == "weak" and listed_in_profile(rec, "hard-structure")
    rec = {"tier": "notrel", "profiles": profile_results(_fr_row(12), "notrel", scorer, TIERS)}
    assert not listed_in_profile(rec, "hard-structure")


def test_because_line_names_construction_only():
    text = ("\nDescription of Proposed Activity\nThe city proposes to install 230 24-inch steel pipe piles by vibratory and "
            "impact pile driving to extend the seawalk and the dock. The project will avoid eelgrass and fish habitat.\n"
            "Description of Marine Mammals in the Area\nEndangered whales.\n")
    s = iha_score("Seawalk Extension Project, Juneau, Alaska", "", text, R)
    assert s["because"].startswith("pile driving, 230 piles")
    assert not any(w in s["because"].lower() for w in ("habitat", "eelgrass", "fish", "endangered", "mitigation"))
    assert not any(r["rule"].startswith("habitat:") for r in s["reasons"])
    assert iha_because({}, 0, False) == "in-water construction named in the notice"


def test_federal_register_shows_no_quotes():
    assert load_coverage()["sources"]["fed_register"]["quotes"] is False
    us = next(c for c in load_coverage()["countries"] if c["code"] == "US")
    assert "fed_register" in us["sources"] and "hard-structure profile only" in us["note"]


# French words count only in French notices (or when the language is unknown).
def test_french_words_only_in_french_notices():
    catalan = ("Concesión administrativa titularidad de Creuers del Port de Barcelona, S.A., "
               "destinada a la explotación de una estación marítima.")
    assert "type:port_terminal" not in {r["rule"] for r in score_text(catalan, R, language="es")["reasons"]}
    french = "Agrandissement du port de Gaspé et réfection du quai."
    assert "type:port_terminal" in {r["rule"] for r in score_text(french, R, language="fr")["reasons"]}
    assert "type:port_terminal" in {r["rule"] for r in score_text(french, R)["reasons"]}          # unknown language
    assert "type:wharf_dock" not in {r["rule"] for r in score_text("Le quai sera refait.", R, language="en")["reasons"]}


def test_country_order_is_the_same_for_general_and_profile_only_cards(tmp_path):
    import re
    conn = db.connect(Path(":memory:"))
    gb = Item(source="scot_marine", item_id="1", url="https://example.org/gb", title="Capital Dredging - Port of Dundee",
              full_text="Capital dredging of the harbour berth.", country="GB", province="Scotland", language="en")
    us = Item(source="fed_register", item_id="2", url="https://example.org/us", title="Seawalk Extension Project, Juneau, Alaska",
              full_text="x", country="US", province="Alaska", language="en")
    ca = Item(source="iaac", item_id="3", url="https://example.org/ca", title="Wharf Reconstruction at Hermitage SCH",
              full_text="Wharf reconstruction in the harbour.", country="CA", province="Newfoundland and Labrador", language="en")
    for it in (gb, us, ca):
        db.upsert_item(conn, it)
    conn.execute("UPDATE items SET score=30, project_types='[]', reasons='[]', facts='{}', stage='unknown' "
                 "WHERE source IN ('scot_marine', 'iaac')")
    conn.execute("UPDATE items SET score=0, project_types='[]', reasons='[]', stage='unknown', facts=? WHERE source='fed_register'",
                 (json.dumps({"iha": {"score": 41, "because": "pile driving, 230 piles"}}),))
    settings = dict(S, sources={"scot_marine": {"enabled": True}, "fed_register": {"enabled": True},
                                "iaac": {"enabled": True}})
    records, _ = site.build_public_records(conn, {}, settings, TODAY)
    assert {r["country"] for r in records} == {"CA", "GB", "US"}
    site.write_site(tmp_path, records, settings, TODAY)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    found = re.findall(r'data-country="(\w+)"[^>]*?data-country-order="(\d+)"', html)
    by_country = {}
    for c, o in found:
        by_country.setdefault(c, set()).add(o)
    assert all(len(v) == 1 for v in by_country.values()), by_country      # one order per country
    assert by_country["GB"] != by_country["US"]                            # different countries never share one
