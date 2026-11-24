"""Batch 1: freshwater label, maintenance-dredging cap, hydro rule, name-vs-structure check, product-maker
stages, the shortlist page, feedback links, clutter fixes, the About disclosure, weekly checks and page re-reads."""
import json
import re
from datetime import date, datetime
from pathlib import Path

from mpf import autoupdate, db, site
from mpf.cli import recheck_urls
from mpf.config import load_scoring, load_settings
from mpf.extract import is_name
from mpf.http import PoliteClient
from mpf.models import Item
from mpf.profiles import ProfileScorer, is_freshwater
from mpf.score import score_text
from mpf.tiers import tier_for

RULES = load_scoring()
SETTINGS_REAL = load_settings()
FRESH = RULES["freshwater"]
TIERS = SETTINGS_REAL["tiers"]
TODAY = date(2026, 11, 11)


# --- (c) false-flag fixes ---------------------------------------------------------------------

def test_freshwater_by_lake_or_inland_province():
    assert is_freshwater({"full_text": "Riprap along 145 m of the Lake Winnipeg shoreline.", "province": "Manitoba"}, FRESH)
    assert is_freshwater({"full_text": "Rip rap at Whitby Harbour.", "province": "Ontario"}, FRESH)
    # An agency name with "Oceans" in it is not salt water.
    assert is_freshwater({"full_text": "Fisheries and Oceans Canada will repair the Lake Erie pier.", "province": "unknown"}, FRESH)
    # Salt words win over a lake name; coastal provinces are not inland.
    assert not is_freshwater({"full_text": "Wharf by Long Lake road, on the tidal estuary.", "province": "unknown"}, FRESH)
    assert not is_freshwater({"full_text": "Coastal protection on the St. Lawrence River at Pessamit.", "province": "Quebec"}, FRESH)
    assert not is_freshwater({"full_text": "Port expansion at Churchill on Hudson Bay.", "province": "Manitoba"}, FRESH)
    assert not is_freshwater({"full_text": "A new marina.", "province": "Nova Scotia"}, FRESH)


SUNCOR = ("Suncor proposes maintenance dredging within the West Dock berth pocket in Burrard Inlet. The project does not "
          "propose new marine infrastructure or structural modifications to the existing wharf. The dredge footprint is "
          "about 1,500 m², with a dredge volume of about 700 m³. Fish habitat in the inlet is described.")


def test_maintenance_dredging_only_is_kept_at_weak():
    s = score_text(SUNCOR, RULES)
    assert s["score"] == 34
    assert any(r["rule"] == "cap:maintenance_dredging" for r in s["reasons"])


def test_maintenance_dredging_cap_does_not_apply_to_new_works_large_volumes_or_habitat_work():
    big = SUNCOR.replace("700 m³", "60,000 m³")
    assert score_text(big, RULES)["score"] > 34
    habitat = SUNCOR + " A fish habitat offsetting plan will be submitted."
    assert not any(r["rule"] == "cap:maintenance_dredging" for r in score_text(habitat, RULES)["reasons"])
    new = ("Maintenance dredging of 700 m³ at the harbour, and construction of a new 150 m breakwater with armour stone "
           "on the seabed.")
    assert not any(r["rule"] == "cap:maintenance_dredging" for r in score_text(new, RULES)["reasons"])


def test_hydro_stations_and_dams_are_not_listed_but_tidal_power_is():
    assert tier_for({"score": 40, "title": "Generating Station 2 Intake Structure Replacement"}, TIERS)[0] == "notrel"
    assert tier_for({"score": 40, "title": "Klinaklini Hydroelectric"}, TIERS)[0] == "notrel"
    assert tier_for({"score": 40, "title": "Tidal Generating Station at Annapolis"}, TIERS)[0] == "possible"


def test_a_structure_word_inside_a_name_is_not_a_structure():
    def m(text, word):
        return re.search(word, text, re.IGNORECASE)
    assert is_name("work in the West Dock berth pocket", m("work in the West Dock berth pocket", r"\bdock\b"))
    text = "A new dock berth will be built."
    assert not is_name(text, m(text, r"\bdock\b"))                                  # lower case: a real dock
    scorer = ProfileScorer(RULES)
    assert scorer.score(SUNCOR, "Suncor Dredging - West Dock Berth", "hard-structure", TIERS)["score"] == 0


# --- site helpers ---------------------------------------------------------------------------------

SITE = {"title": "MitiMark", "description": "Test", "contact_name": "Test Person", "contact_email": "t@example.org",
        "max_quote_chars": 300, "max_quotes": 3, "closing_soon_days": 30, "sources": [], "url": "https://example.org/",
        "feedback_form_url": "https://docs.google.com/forms/d/e/ABC123/viewform",
        "feedback_form_project_field": "entry.111", "shortlist_profile": "hard-structure", "shortlist_size": 10}
SETTINGS = {"tiers": TIERS, "duplicates": {"prefer": ["nl_ea", "iaac"], "min_title_similarity": 0.8}, "site": SITE}
BW = "The project will build a new 150 m breakwater in the harbour using armour stone to create new fish habitat."


def _records(rows):
    conn = db.connect(Path(":memory:"))
    for item, score, stage in rows:
        db.upsert_item(conn, item)
        conn.execute("UPDATE items SET score=?, project_types='[]', reasons='[]', facts='{}', stage=? WHERE key=?",
                     (score, stage, item.key))
    records, _ = site.build_public_records(conn, {}, SETTINGS, TODAY)
    return records


def _items():
    return [
        (Item(source="iaac", item_id="1", url="https://example.org/1", title="Breakwater at Test SCH",
              full_text=BW, country="CA", province="Nova Scotia"), 70, "under assessment"),
        (Item(source="nl_ea", item_id="2", url="https://example.org/2", title="Approved Breakwater",
              full_text=BW, country="CA", province="Newfoundland and Labrador"), 45, "decision made"),
        (Item(source="iaac", item_id="3", url="https://example.org/3", title="Lake Breakwater",
              full_text=BW.replace("in the harbour", "on Lake Ontario"), country="CA", province="Ontario"), 80,
         "under assessment"),
    ]


def test_shortlist_puts_approved_leads_first_and_leaves_out_freshwater():
    recs = _records(_items())
    fresh = [r for r in recs if r["freshwater"]]
    assert [r["title"] for r in fresh] == ["Lake Breakwater"]
    picks = site.shortlist(recs, "hard-structure", 10)
    # Approved projects are capped at Possible (owner, 2026-11-19), so the Strong one under review comes first.
    assert [r["title"] for r in picks] == ["Breakwater at Test SCH", "Approved Breakwater"]
    assert picks[1]["profiles"]["hard-structure"]["tier"] == "possible"


def test_shortlist_page_has_stage_feedback_source_and_no_too_late(tmp_path):
    recs = _records(_items())
    site.write_site(tmp_path, recs, SETTINGS, TODAY)
    page = (tmp_path / "shortlist.html").read_text(encoding="utf-8")
    assert "<h1>2 projects to review</h1>" in page and "Approved, may not be built yet" in page
    assert "Likely too late" not in page and "Lake Breakwater" not in page
    assert page.count('<p class="attr">Source: ') == 2 and "Matches because: " in page
    assert "viewform?usp=pp_url&amp;entry.111=nl_ea%3A2" in page
    assert '<meta name="robots" content="noindex, nofollow">' in page


def test_feedback_link_is_hidden_while_the_placeholder_is_set():
    assert site.feedback_url({"id": "iaac:1"}, SETTINGS_REAL["site"]) == ""
    assert "YOUR_FORM_ID" in SETTINGS_REAL["site"]["feedback_form_url"]
    plain = dict(SITE, feedback_form_project_field="entry.0000000000")
    assert site.feedback_url({"id": "iaac:1"}, plain) == SITE["feedback_form_url"]


def test_cards_hide_unpublished_counts_explain_jargon_and_mark_freshwater(tmp_path):
    recs = _records(_items())
    site.write_site(tmp_path, recs, SETTINGS, TODAY)
    html = (tmp_path / "projects.html").read_text(encoding="utf-8")
    assert "<dt>Comments received</dt>" not in html
    assert '<abbr title="Small Craft Harbour: a fishing or recreational harbour run by Fisheries and Oceans Canada">SCH</abbr>' in html
    assert 'data-fresh="1"' in html and "&middot; Freshwater" in html             # the card says so
    assert "On a lake or river (freshwater)." in (tmp_path / "p" / "iaac-3.html").read_text(encoding="utf-8")
    assert 'id="t-fresh"' in html and 'id="show-weak"' in html and "' weak ' + (hw === 1" in html and 'id="country-bar"' in html
    assert "Approved, may not be built yet" in html and "productView()" in html
    assert "transform:rotate" not in html
    page = (tmp_path / "p" / "iaac-1.html").read_text(encoding="utf-8")
    assert "<dt>Comments received</dt>" not in page          # nothing published: the row is left out (owner, 2026-11-22)
    assert "Feedback on this project" in page


def test_about_names_econcrete_once_and_nowhere_else(tmp_path):
    recs = _records(_items())
    site.write_site(tmp_path, recs, SETTINGS, TODAY)
    about = (tmp_path / "about.html").read_text(encoding="utf-8")
    assert about.count("ECOncrete") == 1 and "uses only public sources" in about
    for name in ("index.html", "shortlist.html", "p/iaac-1.html"):
        assert "ECOncrete" not in (tmp_path / name).read_text(encoding="utf-8")
    assert '<h2 id="stages-and-leads">' in about and '<h2 id="terms-used">' in about
    assert "port plan" not in about.lower()


# --- (e) weekly checks and page re-reads ----------------------------------------------------------

def test_weekly_interval_from_settings():
    assert SETTINGS_REAL["update"]["days_between_updates"] == 7
    now = datetime(2026, 11, 15, 9)
    assert autoupdate.due_for_update("2026-11-08T09:00:00", now, 7)
    assert not autoupdate.due_for_update("2026-11-09T09:00:00", now, 7)


def test_recheck_list_is_listed_projects_not_decided_yet(tmp_path):
    items = [{"url": "u1", "source": "iaac", "stage_kind": "open"},
             {"url": "u2", "source": "nl_ea", "stage_kind": "decided"},
             {"url": "u3", "source": "scot_marine", "stage_kind": "assessment"},
             {"url": "u4", "source": "es_boe", "stage_kind": "assessment"}]
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items))
    assert recheck_urls(path) == {"u1", "u3"}
    assert recheck_urls(tmp_path / "missing.json") == set()


def test_rechecked_pages_use_the_recheck_age_and_are_counted(tmp_path):
    client = PoliteClient({"http": {"user_agent": "T/1"}, "update": {"recheck_open_pages_hours": 144}}, tmp_path)
    seen = []

    def fake(url, max_age, max_bytes=None, headers=None):
        seen.append((url, max_age))
        client.network_requests += 1
        return 200, b"ok"
    client._get_raw = fake
    client.refresh_urls = {"https://example.org/open"}
    client.get("https://example.org/open")
    client.get("https://example.org/decided")
    assert seen == [("https://example.org/open", 144.0), ("https://example.org/decided", None)]
    assert client.rechecked == 1


def _sl(title, tier, score, stage_kind):
    return {"title": title, "tier": "possible", "freshwater": False, "stage_kind": stage_kind,
            "profiles": {"hard-structure": {"tier": tier, "score": score}}}


def test_shortlist_sorts_by_tier_then_approved_first_then_score():
    recs = [_sl("Weak approved", "weak", 30, "decided"),
            _sl("Possible open", "possible", 50, "open"),
            _sl("Strong open low", "strong", 61, "open"),
            _sl("Possible approved", "possible", 40, "decided"),
            _sl("Strong open high", "strong", 90, "open"),
            _sl("Strong approved", "strong", 65, "decided")]
    picks = site.shortlist(recs, "hard-structure", 10)
    assert [r["title"] for r in picks] == ["Strong approved", "Strong open high", "Strong open low",
                                           "Possible approved", "Possible open", "Weak approved"]
    assert [r["title"] for r in site.shortlist(recs, "hard-structure", 2)] == ["Strong approved", "Strong open high"]


def test_approved_label_reads_may_not_be_built_yet_everywhere(tmp_path):
    recs = _records(_items())
    site.write_site(tmp_path, recs, SETTINGS, TODAY)
    for name in ("index.html", "shortlist.html", "about.html"):
        html = (tmp_path / name).read_text(encoding="utf-8")
        assert "a lead\"" not in html and "Approved: a lead" not in html, name
    assert "Approved, may not be built yet" in (tmp_path / "about.html").read_text(encoding="utf-8")
    assert "Strong fits come first" in (tmp_path / "shortlist.html").read_text(encoding="utf-8")


def test_publish_doc_matches_weekly_schedule():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "how_to_publish.md").read_text(encoding="utf-8")
    assert "every 7 days" in doc and "1st and 15th" in doc and "only republishes" in doc
