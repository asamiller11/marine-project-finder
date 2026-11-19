"""Buyer profiles: each profile in English and one other language, the stricter reading rules
(negation, conditions, doubtful sizes, dam bulkheads and piers, shellfish tied to works), the
routine-repair cap, and how profiles reach the site (picker, cards data, project pages, CSV, About).
"All habitat work" must stay exactly the general score."""
import csv
import json
from datetime import date
from pathlib import Path

import pytest

from mpf import db, site
from mpf.config import load_scoring
from mpf.models import Item
from mpf.profiles import EMPTY_NOTE, ProfileScorer, listed_in_profile
from mpf.score import score_text

TIERS = {"strong_min": 60, "possible_min": 35, "weak_min": 20,
         "not_relevant_title_patterns": [r"\bquarr(y|ies)\b"]}
RULES = load_scoring()
SCORER = ProfileScorer(RULES)
PROFILES = ["hard-structure", "living-shoreline", "seagrass", "kelp", "coral", "banking"]


def scores(text, title="Project"):
    return {p: SCORER.score(text, title, p, TIERS)["score"] for p in PROFILES}


def one(text, profile, title="Project"):
    return SCORER.score(text, title, profile, TIERS)


def test_profiles_are_configured_in_order():
    assert list(SCORER.profiles) == PROFILES


# --- each profile, English and another language -------------------------------------------

@pytest.mark.parametrize("profile,text,low", [
    ("hard-structure", "The project will extend the harbour breakwater by 250 m using armour stone, with textured "
                       "habitat panels on the seaward face to create new fish habitat.", 60),
    ("hard-structure", "El proyecto consiste en la ampliación del dique de abrigo del puerto con escollera y un "
                       "arrecife artificial.", 60),
    ("living-shoreline", "The project will build a living shoreline with an oyster reef and salt marsh restoration "
                         "along 300 m of eroding coastal shoreline.", 60),
    ("living-shoreline", "Restauration des marais et récifs d'huîtres; les huîtres seront réimplantées sur le "
                         "littoral de la baie.", 35),
    ("seagrass", "Dredging of the harbour channel will remove 20,000 m3 of sediment next to eelgrass beds; an "
                 "eelgrass transplant and offsetting plan is proposed.", 60),
    ("seagrass", "Projektet omfatter opmudring i havnen. Ålegræs udplantning er planlagt som kompensation.", 35),
    ("kelp", "The breakwater rock will be seeded to support kelp forest restoration on a new rocky reef in the bay.", 35),
    ("kelp", "Utfylling i sjø ved havna. Tareskog i området skal restaureres etter anlegget.", 35),
    ("coral", "The marine terminal footprint on the seabed avoids cold-water coral and sponge reef areas "
              "identified in the survey.", 35),
    ("coral", "El dragado del puerto evitará las zonas de coralígeno y corales en el fondo marino.", 35),
    ("banking", "Port expansion with infill of 5 ha of intertidal habitat. A habitat banking site will provide "
                "offset credits under a fish habitat offsetting plan.", 60),
    ("banking", "Havenuitbreiding met landaanwinning in de Noordzee. Natuurcompensatie en compenserende "
                "maatregelen worden uitgevoerd.", 60),
])
def test_each_profile_matches_its_own_work(profile, text, low):
    r = one(text, profile)
    assert r["score"] >= low and r["core"] and r["because"]


def test_profiles_need_a_core_match():
    # A breakwater notice says nothing about seagrass, kelp, coral or compensation.
    s = scores("The project will extend the harbour breakwater by 250 m using armour stone.")
    assert s["hard-structure"] >= 35
    assert s["seagrass"] == s["kelp"] == s["coral"] == s["banking"] == s["living-shoreline"] == 0


def test_matches_because_is_plain_words():
    r = one("The project will extend the harbour breakwater by 250 m using armour stone.", "hard-structure")
    assert r["because"].startswith("Breakwater, jetty or groyne")
    assert "Rock armour, riprap or armour units" in r["because"] and "size 250 m" in r["because"]


def test_place_names_and_word_parts_do_not_match():
    assert scores("Wharf repairs at Coral Harbour on the sea.")["coral"] == 0
    assert scores("Bekendtgørelse om tilskud til cykelprojekter ved havnen og kysten.")["kelp"] == 0


# --- stricter reading -----------------------------------------------------------------------

def test_negated_and_conditional_keywords_do_not_count():
    neg = ("Maintenance dredging of the berth in the bay. The project does not propose new marine infrastructure "
           "or changes to the existing wharf.")
    assert one(neg, "hard-structure")["score"] == 0
    # The general score keeps today's rule, which does count this wharf.
    assert "Wharf, pier or large dock" in score_text(neg, RULES)["project_types"]
    for text in ("Highway widening near the coast. If required, riprap may be placed at culvert outlets.",
                 "Highway widening near the coast. Riprap will be placed where required.",
                 "Highway widening near the coast. Where required, riprap will be placed at the outlets.",
                 "Élargissement de la route côtière. Un enrochement sera ajouté si nécessaire."):
        assert one(text, "hard-structure")["score"] == 0, text
    assert one("Highway widening near the coast. Riprap will be placed at the outlets.", "hard-structure")["score"] > 0


def test_size_from_past_works_or_a_distance_is_not_counted():
    past = ("Installation of riprap shoreline protection along 145 m of the bay shoreline. The design matches "
            "about 4 km of works completed between 2010 and 2025.")
    r = one(past, "hard-structure")
    assert "size 145 m" in r["because"] and "4 km" in r["because"] and r["score"] < 60
    near = "Installation of riprap shoreline protection along the bay. Several residences are within 250 m of the site."
    r = one(near, "hard-structure")
    assert "doubtful size reading not counted (250 m)" in r["because"] and "size 250" not in r["because"]


def test_fort_alexander_case_is_not_strong():
    text = ("Slope stabilization and installation of an engineered riprap erosion control blanket along approximately "
            "145 m of the Lake Winnipeg shoreline at the mouth of the Winnipeg River (Traverse Bay). There are also "
            "several residences within 250 m of the site, along the shoreline and highway. The design is consistent "
            "with that of approximately 4 km of works along the Traverse Bay shoreline, within the reserve, that were "
            "completed in several contracts between 2010 and 2025.")
    assert 35 <= one(text, "hard-structure")["score"] < 60


def test_bulkhead_and_piers_at_dams_and_intakes_do_not_count():
    dam = "Work in the river channel, downstream of the bulkhead at the generating station intake."
    assert scores(dam)["hard-structure"] == 0 and scores(dam)["living-shoreline"] == 0
    piers = "Repairs to the upstream faces of the piers of the generating station intake structure."
    assert scores(piers)["hard-structure"] == 0
    assert scores("A new steel bulkhead will replace the failing seawall along the harbour.")["hard-structure"] >= 20
    assert scores("A new 200 m pier will be built in the harbour.")["hard-structure"] >= 35


def test_shellfish_counts_for_living_shoreline_only_when_tied_to_the_works():
    assert scores("Dredging in the harbour. The harbour supports fish, shellfish and other marine species.")[
        "living-shoreline"] == 0
    assert scores("Oysters in the work area will be relocated and the oyster reef restored after the dredging "
                  "in the harbour.")["living-shoreline"] >= 35


# --- routine repairs (hard-structure) -------------------------------------------------------

REPAIR = "Repair of the existing breakwater and wharf at the small craft harbour with new armour stone along {}."


def test_small_routine_repair_stays_weak():
    r = one(REPAIR.format("80 m"), "hard-structure", title="Breakwater repair")
    assert r["score"] == 34 and "small routine repair, kept at Weak" in r["because"]


def test_repair_cap_does_not_apply_when_large_unsized_or_with_habitat_words():
    assert one(REPAIR.format("150 m"), "hard-structure", "Breakwater repair")["score"] >= 35        # 100 m or more
    assert one(REPAIR.format("80 m") + " New fish habitat will be created.", "hard-structure",
               "Breakwater repair")["score"] >= 35                                               # habitat words
    no_size = "Repair of the existing wharf and breakwater at the small craft harbour, with new armour stone."
    assert one(no_size, "hard-structure", "Wharf repair")["score"] >= 35                         # no size: not "small"
    assert one("Repair works at the harbour on 3,000 m² of breakwater with new armour stone.", "hard-structure",
               "Breakwater repair")["score"] >= 35                                               # 2,000 m² or more


def test_repair_limits_live_in_config():
    assert RULES["profile_rules"]["routine_repair"] == {"max_length_m": 100, "max_area_m2": 2000, "max_volume_m3": 5000}


# --- the site --------------------------------------------------------------------------------

TODAY = date(2026, 10, 28)
SETTINGS = {
    "tiers": TIERS,
    "duplicates": {"prefer": ["nl_ea", "iaac", "drop"], "min_title_similarity": 0.8},
    "site": {"title": "MitiMark", "description": "Test description", "contact_name": "Test Person",
             "contact_email": "contact@example.org", "max_quote_chars": 300, "max_quotes": 3,
             "closing_soon_days": 30, "sources": [], "url": "https://example.org/"},
}
BREAKWATER = "The project will extend the harbour breakwater by 250 m using armour stone to create new fish habitat."


def _db(rows):
    conn = db.connect(Path(":memory:"))
    for item, score in rows:
        db.upsert_item(conn, item)
        conn.execute("UPDATE items SET score=?, project_types='[]', reasons='[]', facts='{}', stage='unknown' "
                     "WHERE key=?", (score, item.key))
    return conn


def _items():
    return [
        (Item(source="iaac", item_id="1", url="https://example.org/1", title="Harbour Breakwater Extension",
              full_text=BREAKWATER, country="CA"), 70),
        # General score 12 (not on the general list) but Possible or better for hard-structure: joins that profile.
        (Item(source="iaac", item_id="2", url="https://example.org/2", title="Coastal Erosion Protection",
              full_text="Installation of a rock revetment using armourstone along the coastal highway embankment.",
              country="CA"), 12),
        # General score 12 and only Weak for hard-structure: stays out everywhere.
        (Item(source="iaac", item_id="3", url="https://example.org/3", title="Wharf Paving",
              full_text="Paving of the wharf deck at the harbour.", country="CA"), 12),
    ]


def _records():
    records, skipped = site.build_public_records(_db(_items()), {}, SETTINGS, TODAY)
    return {r["id"]: r for r in records}, skipped


def test_profile_projects_need_possible_and_are_also_on_the_full_list():
    # "All habitat work" is the full list (owner, 2026-11-19): a project a profile lists is on it too.
    recs, skipped = _records()
    assert "iaac:2" in recs and not recs["iaac:2"].get("profile_only") and recs["iaac:2"]["tier"] == "possible"
    assert recs["iaac:2"]["listed_via"] == "Hard-structure eco-engineering"
    assert recs["iaac:2"]["profiles"]["hard-structure"]["score"] >= 35
    assert listed_in_profile(recs["iaac:2"], "hard-structure")
    assert "iaac:3" not in recs and skipped["not_relevant"] == 1
    assert not recs["iaac:1"].get("profile_only") and recs["iaac:1"]["tier"] == "strong"


def test_all_habitat_work_is_the_full_list_and_profiles_are_subsets():
    from mpf.profiles import listed_in_profile as lp
    recs, _ = _records()
    html = site.render_index(list(recs.values()), SETTINGS["site"], TIERS, TODAY)
    server = html.split('<div id="server-list">')[1].split('<div id="profile-only"')[0]
    assert "Harbour Breakwater Extension" in server and "Coastal Erosion Protection" in server
    everyone = set(recs)
    for p in PROFILES:
        assert {k for k, r in recs.items() if lp(r, p)} <= everyone
    assert "|| 'hard-structure'" in html          # hard-structure is the default view
    assert "t-notrel" not in html
    assert '<span class="n" id="stat-total">2</span><span class="l">Projects listed</span>' in html  # the full list


def test_banner_figures_follow_the_profile_and_are_labelled():
    recs, _ = _records()
    html = site.render_index(list(recs.values()), SETTINGS["site"], TIERS, TODAY)
    assert 'Figures for: <strong id="stats-for-name">All habitat work</strong>' in html
    assert "function paintStats()" in html and "set('stats-for-name'" in html
    for i in ("stat-total", "stat-strong", "stat-possible", "stat-soon", "stat-countries"):
        assert f'id="{i}"' in html
    assert 'data-source="iaac"' in html                       # lets the banner count sources per profile


def test_one_empty_deadline_line_and_compact_phone_banner():
    recs, _ = _records()
    html = site.render_index(list(recs.values()), SETTINGS["site"], TIERS, TODAY)
    assert "line.hidden = soon.length === 0;" in html
    assert '<li class="stat-countries">' in html


def test_banner_and_about_do_not_promise_quotes_for_every_source(tmp_path):
    recs, _ = _records()
    site.write_site(tmp_path, list(recs.values()), SETTINGS, TODAY)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    about = (tmp_path / "about.html").read_text(encoding="utf-8")
    assert "links to the official notice" in html and "exact sentences that triggered it" not in html
    assert "Where a source's terms allow it, each listing shows short quotes" in about


def test_picker_link_and_card_data():
    recs, _ = _records()
    html = site.render_index(list(recs.values()), SETTINGS["site"], TIERS, TODAY)
    assert '<label for="f-profile">I work on:</label>' in html
    assert '<option value="all">All habitat work</option>' in html
    assert '<option value="kelp">Kelp and other seaweed (no current matches)</option>' in html
    assert 'id="profile-link"' in html and "localStorage" in html and "profile=" in html
    assert EMPTY_NOTE in html and "not a statement that mitigation is required" in html
    data = json.loads(html.split('id="profile-data">')[1].split("</script>")[0])
    assert [m["key"] for m in data["profiles"]] == PROFILES
    tier, score, because = data["cards"]["iaac:1"]["hard-structure"]
    assert tier == "strong" and score >= 60 and because.startswith("Breakwater")
    assert "kelp" not in data["cards"]["iaac:1"]
    assert 'class="because" hidden' in html and 'class="general-line" hidden' in html


def test_project_page_lists_matching_profiles_and_keeps_source(tmp_path):
    recs, _ = _records()
    site.write_site(tmp_path, list(recs.values()), SETTINGS, TODAY)
    page = (tmp_path / "p" / "iaac-1.html").read_text(encoding="utf-8")
    assert "<h2 class=\"side-h\">Buyer profiles</h2>" in page and "Hard-structure eco-engineering" in page
    assert "Matches because: Breakwater" in page and "Kelp and other seaweed" not in page
    assert '<p class="attr">Source: ' in page
    other = (tmp_path / "p" / "iaac-2.html").read_text(encoding="utf-8")
    assert "On the list because it fits the profile: Hard-structure eco-engineering" in other


def test_csv_has_a_column_per_profile(tmp_path):
    recs, _ = _records()
    site.write_site(tmp_path, list(recs.values()), SETTINGS, TODAY)
    rows = list(csv.DictReader(open(tmp_path / "projects.csv", encoding="utf-8-sig")))
    by_id = {r["id"]: r for r in rows}
    assert by_id["iaac:1"]["profile_hard_structure"].startswith("Strong fit (")
    assert by_id["iaac:1"]["profile_kelp"] == ""
    assert by_id["iaac:2"]["tier"] == "Possible fit"
    assert by_id["iaac:2"]["profile_hard_structure"].startswith("Possible fit (")


def test_about_explains_profiles(tmp_path):
    recs, _ = _records()
    site.write_site(tmp_path, list(recs.values()), SETTINGS, TODAY)
    about = (tmp_path / "about.html").read_text(encoding="utf-8")
    assert '<h2 id="buyer-profiles">Buyer profiles</h2>' in about and 'href="#buyer-profiles"' in about
    assert "rule-based guesses" in about
    assert "It is not a statement that mitigation, compensation or any particular\nmeasure is required" in about
    assert "—" not in about


def test_listing_summary_counts_every_listed_project():
    recs, _ = _records()
    assert site.listing_summary(list(recs.values())) == "2 on the general list"
