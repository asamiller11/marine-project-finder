"""New readers (BC, Denmark, Scotland, Spain, France project watch), using made-up samples that
copy only each site's structure (tests/samples; the JSON samples are invented too)."""
import json
from datetime import date
from pathlib import Path

from mpf import db, site
from mpf.config import load_scoring
from mpf.models import Item
from mpf.score import score_text
from mpf.sources import bc_eao, dk_hoering, es_boe, fr_eolien, scot_marine
from mpf.timing import compute

TODAY = date(2026, 10, 28)


def test_bc_projects_comment_periods_and_active_filter(samples):
    projects = bc_eao._results((samples / "bc_eao_projects.json").read_bytes())
    periods = bc_eao.comment_periods_by_project(bc_eao._results((samples / "bc_eao_periods.json").read_bytes()))
    kept = [p for p in projects if bc_eao.keep(p, periods.get(p["_id"], []), 730)]
    assert [p["name"] for p in kept] == ["Example Harbour Terminal Expansion"]     # the finished mine is dropped
    item = bc_eao.project_to_item(kept[0], periods["aaa111"])
    assert item.comment_deadline == "2026-11-20"
    assert item.proponent == "Example Port Ltd." and item.province == "British Columbia"
    assert (item.lat, item.lon) == (49.2, -123.1)
    assert item.url == "https://projects.eao.gov.bc.ca/p/aaa111"


def test_denmark_feed_fields_and_danish_scoring(samples, rules):
    entries = dk_hoering.parse_feed((samples / "dk_feed.xml").read_text(encoding="utf-8"))
    assert [e["id"] for e in entries] == ["90001", "90002"]
    harbour = dk_hoering.entry_to_item(entries[0])
    assert harbour.comment_deadline == "2026-11-20" and harbour.proponent == "Kystdirektoratet"
    assert harbour.language == "da" and harbour.country == "DK" and harbour.status_raw == "active"
    s = score_text(harbour.full_text, rules)
    assert {"port_terminal", "wharf_dock", "dredging"} <= set(s["facts"]["types"])
    assert {"eelgrass", "offsetting"} <= set(s["facts"]["habitat_clues"])
    assert s["flagged"]
    farm = dk_hoering.entry_to_item(entries[1])
    assert farm.status_raw == "archived"                      # not "decision made"
    assert score_text(farm.full_text, rules)["score"] < 20     # not marine


def test_scotland_listing_grouping_and_document_fields(samples):
    rows = scot_marine.parse_listing((samples / "scot_listing.html").read_text(encoding="utf-8"))
    assert rows[0]["url"] == "https://marine.gov.scot/?q=node/90010" and rows[0]["updated"] == "2026-10-16"
    keys = [scot_marine.project_key(r["title"]) for r in rows]
    assert keys[0] == keys[1] == "00099001"                   # two documents, one licence
    assert scot_marine.project_name(rows[2]["title"]) == "Example Offshore Wind Farm – East of Testshire"
    doc = scot_marine.parse_document((samples / "scot_document.html").read_text(encoding="utf-8"))
    assert doc["fields"]["Stage document submitted"] == "Licence"
    assert doc["fields"]["Reference"] == "00099001"
    assert scot_marine.stage_words("Licence", rows[0]["title"]) == "decision made"
    assert scot_marine.stage_words("Pre-application consultation", "x") == "pre-application consultation"


def test_spain_summary_filter_sections_and_marine_titles(samples):
    items = es_boe.walk_summary(json.loads((samples / "es_summary.json").read_text(encoding="utf-8")))
    assert {i["id"] for i in items} == {"BOE-A-2099-0001", "BOE-A-2099-0002", "BOE-B-2099-0003", "BOE-A-2099-0004"}
    kept = [i["id"] for i in items if es_boe.wanted(i)]
    # Port EIA resolution and port authority notice kept; inland road and Section II appointment dropped.
    assert kept == ["BOE-A-2099-0001", "BOE-B-2099-0003"]
    port_eia = next(i for i in items if i["id"] == "BOE-A-2099-0001")
    assert es_boe.stage_for(port_eia).startswith("decision made")
    notice = next(i for i in items if i["id"] == "BOE-B-2099-0003")
    assert es_boe.stage_for(notice).startswith("public information")


def test_spain_item_text_and_spanish_scoring(samples, rules):
    text = es_boe.item_text((samples / "es_item.html").read_text(encoding="utf-8"))
    assert "praderas de posidonia" in text
    s = score_text("Ampliación del puerto de Ejemplo\n" + text, rules)
    assert {"wharf_dock", "dredging"} <= set(s["facts"]["types"])
    assert {"eelgrass", "offsetting"} <= set(s["facts"]["habitat_clues"])


def test_spain_uses_html_pages_not_disallowed_xml():
    import inspect
    src = inspect.getsource(es_boe)
    assert "xml.php?id=" not in src.split('"""', 2)[2]        # only mentioned in the docstring


def test_france_project_links_page_and_stage(samples):
    links = fr_eolien.project_links((samples / "fr_list.html").read_text(encoding="utf-8"))
    assert links == ["https://www.eoliennesenmer.fr/facades-maritimes-en-france/facade-manche-mer-du-nord/exemple-sur-mer",
                     "https://www.eoliennesenmer.fr/facades-maritimes-en-france/facade-mediterranee/golfe-exemple/EX1"]
    page = fr_eolien.parse_project((samples / "fr_project.html").read_text(encoding="utf-8"))
    assert page["title"] == "Parc éolien en mer d'Exemple-sur-Mer"
    assert fr_eolien.stage_for(page["text"]) == "under construction (project watch)"   # "Le parc est actuellement en construction"


def _row(item, score, facts=None, stage="x", types=None):
    conn = db.connect(Path(":memory:"))
    db.upsert_item(conn, item)
    conn.execute("UPDATE items SET score=?, project_types=?, reasons='[]', facts=?, stage=? WHERE key=?",
                 (score, json.dumps(types or []), json.dumps(facts or {}), stage, item.key))
    return conn


SETTINGS = {"tiers": {"strong_min": 60, "possible_min": 35, "weak_min": 20}, "site": {}}


def test_project_watch_has_no_countdown_and_is_not_closing_soon():
    item = Item(source="fr_eolien", item_id="x", url="https://example.org/fr", title="Parc éolien en mer d'Exemple",
                language="fr", country="FR", province="Méditerranée", status_raw="under construction (project watch)",
                full_text="Parc éolien en mer", extra={"watch": True, "link_ok": True})
    t = compute({**item.to_dict(), "extra": item.extra}, TODAY)
    assert t["stage"] == "under construction (project watch)" and t["days_to_deadline"] is None and not t["too_late"]
    conn = _row(item, 5, facts={"types": ["offshore_wind"]}, stage=t["stage"])     # low score: watch is not tiered
    records, skipped = site.build_public_records(conn, {}, SETTINGS, TODAY)
    rec = records[0]
    assert rec["tier"] == "watch" and rec["stage_kind"] == "watch" and skipped["not_relevant"] == 0
    html = site.render_index(records, {"title": "T", "description": "d"}, SETTINGS["tiers"], TODAY)
    assert "Project watch (not a notice feed)" in html
    assert "not applicable (project watch, not a notice)" in html
    assert 'data-deadline=""' in html
    assert "0 comment deadlines in the next 30 days" in html


def test_non_english_summary_from_keywords_or_not_translated():
    da = Item(source="dk_hoering", item_id="1", url="u", title="Udvidelse af Eksempel Havn", language="da", country="DK")
    conn = _row(da, 50, facts={"types": ["port_terminal", "dredging"], "sizes": []})
    rec = site.build_public_records(conn, {}, SETTINGS, TODAY)[0][0]
    assert rec["summary_note"] == "keywords" and "mentions port or terminal and dredging work" in rec["summary"]
    html = site.render_index([rec], {"title": "T", "description": "d"}, SETTINGS["tiers"], TODAY)
    assert "built from keywords found in the Danish text. Not a translation." in html
    es = Item(source="es_boe", item_id="2", url="u", title="Anuncio", language="es", country="ES")
    conn = _row(es, 40, facts={"types": [], "sizes": []})
    rec = site.build_public_records(conn, {}, SETTINGS, TODAY)[0][0]
    assert rec["summary_note"] == "not_translated" and rec["summary"] == ""
    html = site.render_index([rec], {"title": "T", "description": "d"}, SETTINGS["tiers"], TODAY)
    assert "Not translated</span>" in html


def test_review_file_has_country_column(tmp_path):
    from mpf import review
    conn = _row(Item(source="dk_hoering", item_id="1", url="u", title="A", country="DK"), 50)
    review.write_review(conn, tmp_path / "r.csv", SETTINGS["tiers"])
    header = (tmp_path / "r.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("id,title,country,score,tier,publish")
    assert ",DK," in (tmp_path / "r.csv").read_text(encoding="utf-8")


def test_france_stage_only_from_clear_wording():
    for vague in ("Sa mise en service est prévue en 2028.",
                  "Le débat public qui s'est tenu en 2021 a conduit à identifier la zone.",
                  "Cet appel d'offres permettra de désigner le futur exploitant.",
                  "Le lauréat a été attribué en 2024 pour une zone voisine."):
        assert fr_eolien.stage_for(vague) == "unknown", vague
    assert fr_eolien.stage_for("Le parc est en service depuis 2024.") == "in operation (project watch)"
    assert fr_eolien.stage_for("Le parc a été mis en service en 2023.") == "in operation (project watch)"


def test_watch_deadline_text_is_not_overwritten_by_the_script():
    js = site.JS
    assert "if (kind === 'watch')" in js and js.index("if (kind === 'watch')") < js.index("cd.textContent = 'unknown'")


def test_source_names_come_from_coverage_config():
    assert site.source_display_name("es_boe").startswith("Boletín Oficial del Estado")
    assert site.source_display_name("not_a_source") == "Public source"
