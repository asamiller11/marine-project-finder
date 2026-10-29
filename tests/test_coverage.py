"""Countries, regions and the Coverage panel."""
import json
from datetime import date
from pathlib import Path

from mpf import db, site
from mpf.coverage import check_coverage, load_coverage
from mpf.models import Item

TODAY = date(2026, 10, 28)
COV = {
    "sources": {"iaac": {"name": "Federal registry", "url": "https://example.org/iaac", "languages": ["en", "fr"]},
                "nl_ea": {"name": "Provincial registry", "url": "https://example.org/nl", "languages": ["en"]}},
    "less_tested_languages": ["fr"],
    "countries": [
        {"code": "CA", "name": "Canada", "region_label": "Province or territory", "status": "partial",
         "sources": ["iaac", "nl_ea"], "note": "Federal plus one province.",
         "regions": [{"name": "Newfoundland and Labrador", "status": "covered", "sources": ["nl_ea", "iaac"]},
                     {"name": "Quebec", "status": "partial", "sources": ["iaac"], "note": "Federal only."}]},
        {"code": "US", "name": "United States", "region_label": "State", "status": "coming_soon", "sources": [],
         "regions": [{"name": "New York", "status": "coming_soon"}]},
        {"code": "NO", "name": "Norway", "status": "coming_soon", "sources": []},
    ],
}
SITE = {"title": "Marine Project Finder", "description": "d", "contact_name": "T", "contact_email": "t@example.org"}
TIERS = {"strong_min": 60, "possible_min": 35, "weak_min": 20}


def test_real_coverage_config_is_valid():
    cov = load_coverage()
    assert cov["countries"][0]["code"] == "CA"
    assert any(c["code"] == "NO" for c in cov["countries"])        # quoted, not read as false


def test_coverage_check_catches_mistakes():
    bad = {"sources": {"a": {"name": "A", "url": "u"}}, "countries": [
        {"code": False, "name": "Norway", "status": "coming_soon", "sources": []},
        {"code": "XX", "name": "Nowhere", "status": "covered", "sources": []},
        {"code": "YY", "name": "Typo", "status": "done", "sources": ["a"]},
        {"code": "ZZ", "name": "Mixed", "status": "coming_soon", "sources": ["a"]},
        {"code": "WW", "name": "Ghost", "status": "partial", "sources": ["missing"]}]}
    problems = " | ".join(check_coverage(bad))
    for text in ("must be quoted", "needs at least one source", "unknown status", "cannot list sources",
                 "not defined"):
        assert text in problems


def _record(key, country, province, score=50, language="en"):
    return {"id": key, "title": key, "url": "u", "source": "iaac", "source_name": "Federal registry",
            "proponent": "P", "country": country, "province": province, "language": language,
            "water_body": "unknown", "types": [], "stage": "under assessment", "stage_kind": "assessment",
            "comment_deadline": None, "decision_date": None, "score": score, "tier": "possible",
            "tier_label": "Possible", "tier_reason": "score", "reviewed": False, "summary": "s", "quotes": [],
            "too_late": False, "also_listed": []}


def test_country_sections_selector_and_region_filter():
    recs = [_record("a", "CA", "Quebec", language="fr"), _record("b", "CA", "Newfoundland and Labrador")]
    html = site.render_index(recs, SITE, TIERS, TODAY, COV)
    assert '<section class="country" data-country="CA"' in html
    assert 'data-country="US"' not in html.split("<form")[1].split("</form>")[1]   # no US section or cards
    assert '<option value="CA" data-region-label="Province or territory">Canada</option>' in html
    assert '<option value="US" disabled>United States (coming soon)</option>' in html
    assert '<option value="NO" disabled>Norway (coming soon)</option>' in html
    assert 'id="f-region-label" hidden' in html                     # shown by the script for CA or US only
    assert 'data-region="Quebec"' in html
    assert "Less tested language (fr)" in html


def test_coverage_panel_on_home_and_about():
    home = site.render_index([], SITE, TIERS, TODAY, COV)
    about = site.render_about(SITE, TIERS, TODAY, COV)
    for page in (home, about):
        assert "Coming soon" in page and "Partial" in page and "Covered" in page
        assert "Sources: Federal registry." in page                 # Quebec names its source
        assert "Less tested languages: French" in page
    assert "Coverage: 0 covered, 1 partial, 2 coming soon" in home
    assert "Marine Project Finder" in home and "Canada</h1>" not in home


def test_coming_soon_country_gets_no_items():
    conn = db.connect(Path(":memory:"))
    db.upsert_item(conn, Item(source="iaac", item_id="1", url="u", title="Wharf", country="CA",
                              province="Quebec"))
    conn.execute("UPDATE items SET score=50, project_types='[]', reasons='[]', facts='{}', stage='x'")
    records, _ = site.build_public_records(conn, {}, {"tiers": TIERS, "site": {}}, TODAY)
    html = site.render_index(records, SITE, TIERS, TODAY, COV)
    assert html.count('<section class="country"') == 1
    assert json.dumps([r["country"] for r in records]) == '["CA"]'
