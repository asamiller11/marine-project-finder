"""Site generator, tiers and review file: missing fields, closed periods, French text, long quotes,
tiers, toggles, duplicates, bad links, and yes/no/blank review decisions."""
import csv
import json
from datetime import date
from pathlib import Path

from mpf import db, review, site
from mpf.models import Item
from mpf.tiers import find_duplicates, tier_for

TODAY = date(2026, 10, 28)
SETTINGS = {
    "tiers": {"strong_min": 60, "possible_min": 35, "weak_min": 20,
              "not_relevant_title_patterns": [r"\bquarr(y|ies)\b", r"\b(gravel|sand|borrow) pit\b"]},
    "duplicates": {"prefer": ["nl_ea", "iaac", "drop"], "min_title_similarity": 0.8},
    "site": {"title": "Marine Project Finder: Canada", "description": "Test description",
             "contact_name": "Test Person", "contact_email": "contact@example.org",
             "max_quote_chars": 180, "max_quotes": 3, "closing_soon_days": 30, "sources": []},
}


def _db_with(rows):
    conn = db.connect(Path(":memory:"))
    for item, scored in rows:
        db.upsert_item(conn, item)
        conn.execute("UPDATE items SET score=?, project_types=?, reasons=?, facts=?, stage=? WHERE key=?",
                     (scored["score"], json.dumps(scored.get("types", [])), json.dumps(scored.get("reasons", [])),
                      json.dumps(scored.get("facts", {})), scored.get("stage", "unknown"), item.key))
    return conn


def _reason(quote, matched, rule="habitat:fish_habitat", points=10):
    return {"rule": rule, "label": "x", "points": points, "matched": matched, "quote": quote}


def _publish(conn, review_rows=None):
    records, skipped = site.build_public_records(conn, review_rows or {}, SETTINGS, TODAY)
    return records, skipped, site.render_index(records, SETTINGS["site"], SETTINGS["tiers"], TODAY)


# --- tiers -------------------------------------------------------------------

def test_tier_thresholds_and_rules():
    t = SETTINGS["tiers"]
    assert tier_for({"score": 60, "title": "A wharf"}, t)[0] == "strong"
    assert tier_for({"score": 59, "title": "A wharf"}, t)[0] == "possible"
    assert tier_for({"score": 35, "title": "A wharf"}, t)[0] == "possible"
    assert tier_for({"score": 34, "title": "A wharf"}, t)[0] == "weak"
    assert tier_for({"score": 20, "title": "A wharf"}, t)[0] == "weak"
    assert tier_for({"score": 19, "title": "A wharf"}, t)[0] == "notrel"
    assert tier_for({"score": 80, "title": "Harbour Quarry Extension"}, t) == ("notrel", "quarry or pit (rule)")
    assert tier_for({"score": 80, "title": "Wharf", "stage": "withdrawn or rejected"}, t)[0] == "notrel"


def test_three_tiers_published_and_not_relevant_left_out(tmp_path):
    rows = [(Item(source="iaac", item_id=str(i), url=f"u{i}", title=f"Project {i}"), {"score": s})
            for i, s in enumerate([75, 40, 25, 5])]
    rows.append((Item(source="iaac", item_id="q", url="uq", title="Harbour Quarry"), {"score": 80}))
    records, skipped, html = _publish(_db_with(rows))
    assert [r["tier"] for r in records] == ["strong", "possible", "weak"]
    assert skipped["not_relevant"] == 2                      # score 5, and the quarry rule
    for label in ("Strong match", "Possible", "Weak"):
        assert label in html
    assert "Project 3" not in html and "Harbour Quarry" not in html
    assert 'id="t-weak"' in html and "Show weaker matches" in html
    assert "t-notrel" not in html and "Show probably not relevant" not in html
    assert "are not listed" in html
    assert "Score 75/100" in html
    assert "It is not a statement that mitigation or\ncompensation is required" in html
    assert "2 projects shown. 0 comment deadlines in the next 30 days." in html   # Weak hidden by default
    site.write_site(tmp_path, records, SETTINGS, TODAY)
    data = json.loads((tmp_path / "items.json").read_text(encoding="utf-8"))
    assert {r["tier"] for r in data} == {"strong", "possible", "weak"}
    assert "Project 3" not in (tmp_path / "items.json").read_text(encoding="utf-8")


def test_closing_soon_uses_all_published_tiers_not_the_toggle():
    # The closing-soon script must not depend on the weak toggle (tierShown), only on open deadlines.
    js = site.JS
    soon_part = js[js.index("// Closing soon"):js.index("if (none)")]
    assert "tierShown" not in soon_part and "notrel" not in js


# --- review decisions ----------------------------------------------------------

def test_yes_badge_no_hidden_blank_automatic():
    rows = [(Item(source="iaac", item_id=str(i), url=f"u{i}", title=t), {"score": 50})
            for i, t in enumerate(["Checked one", "Rejected one", "Untouched one"])]
    conn = _db_with(rows)
    decisions = {"iaac:0": {"publish": "yes"}, "iaac:1": {"publish": "no"}, "iaac:2": {"publish": ""}}
    records, skipped, html = _publish(conn, decisions)
    titles = {r["title"]: r for r in records}
    assert "Rejected one" not in html and skipped["hidden_by_you"] == 1
    assert titles["Checked one"]["reviewed"] and not titles["Untouched one"]["reviewed"]
    assert html.count("Reviewed by a person</span>") == 1
    assert html.count("Automatic, not yet reviewed</span>") == 1


def test_review_file_blank_default_and_old_format_migration(tmp_path):
    conn = _db_with([(Item(source="iaac", item_id="1", url="u1", title="A"), {"score": 50}),
                     (Item(source="iaac", item_id="2", url="u2", title="B"), {"score": 10})])
    path = tmp_path / "review.csv"
    # An old-format file (no "tier" column) where "no" was only the default.
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "title", "score", "confidence", "publish", "summary"])
        w.writerow(["iaac:1", "A", "50", "Medium", "yes", "My summary."])
        w.writerow(["iaac:2", "B", "10", "Low", "no", ""])
    counts = review.write_review(conn, path, SETTINGS["tiers"])
    rows = {r["id"]: r for r in csv.DictReader(open(path, encoding="utf-8"))}
    assert rows["iaac:1"]["publish"] == "yes" and rows["iaac:1"]["summary"] == "My summary."
    assert rows["iaac:2"]["publish"] == ""             # old default "no" became "not reviewed"
    assert rows["iaac:1"]["tier"] == "Possible" and rows["iaac:2"]["tier"] == "Probably not relevant"
    assert counts == {"rows": 2, "new": 0, "yes": 1, "no": 0}
    # In the new format, a "no" you type is kept and hides the item.
    rows["iaac:2"]["publish"] = "no"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=review.COLUMNS)
        w.writeheader()
        w.writerows(rows.values())
    review.write_review(conn, path, SETTINGS["tiers"])
    assert review.read_review(path)["iaac:2"]["publish"] == "no"


# --- duplicates and bad links ---------------------------------------------------

def test_duplicates_merge_and_keep_nl():
    nl = Item(source="nl_ea", item_id="2434", url="https://nl.example/2434",
              title="Port aux Basques Navigation Improvement Project", province="Newfoundland and Labrador")
    fed = Item(source="iaac", item_id="90808", url="https://iaac.example/90808",
               title="Port aux Basques Navigation Improvement Project – Public comments invited",
               province="Newfoundland and Labrador", comment_deadline="2026-10-28")
    other = Item(source="iaac", item_id="1", url="u1", title="Port aux Basques Ferry Terminal Paving",
                 province="Newfoundland and Labrador")
    conn = _db_with([(nl, {"score": 96}), (fed, {"score": 26}), (other, {"score": 30})])
    records, skipped, html = _publish(conn)
    ids = [r["id"] for r in records]
    assert "nl_ea:2434" in ids and "iaac:90808" not in ids and "iaac:1" in ids
    assert skipped["merged_duplicates"] == 1
    merged = next(r for r in records if r["id"] == "nl_ea:2434")
    assert merged["also_listed"][0]["url"] == "https://iaac.example/90808"
    assert "Also listed:" in html and "comment deadline 2026-10-28" in html


def test_same_title_different_province_is_not_merged():
    a = {"key": "nl_ea:1", "source": "nl_ea", "title": "Wharf Reconstruction", "province": "Newfoundland and Labrador"}
    b = {"key": "iaac:2", "source": "iaac", "title": "Wharf Reconstruction", "province": "Nova Scotia"}
    assert find_duplicates([a, b], SETTINGS["duplicates"]) == {}


def test_bad_links_are_skipped():
    good = Item(source="iaac", item_id="1", url="u1", title="Good link", extra={"link_ok": True})
    bad = Item(source="iaac", item_id="2", url="u2", title="Bad link", extra={"link_ok": False})
    records, skipped, html = _publish(_db_with([(good, {"score": 50}), (bad, {"score": 50})]))
    assert [r["title"] for r in records] == ["Good link"] and skipped["bad_link"] == 1


# --- content rules (missing fields, dates, French, quotes) -----------------------

def test_missing_fields_show_unknown():
    item = Item(source="iaac", item_id="1", url="https://example.org/1")      # almost everything missing
    records, _, html = _publish(_db_with([(item, {"score": 40})]))
    rec = records[0]
    assert rec["proponent"] == "unknown" and rec["water_body"] == "unknown"
    assert "Water body: unknown" in html
    assert ">unknown<" in html                      # comment deadline
    assert rec["stage_kind"] == "unknown"


def test_closed_comment_period_is_likely_too_late():
    item = Item(source="nl_ea", item_id="2", url="https://example.org/2", title="Closed one",
                comment_deadline="2026-10-01")
    records, _, html = _publish(_db_with([(item, {"score": 50, "stage": "comment closed 2026-10-01, decision pending"})]))
    assert records[0]["stage_kind"] == "closed" and records[0]["too_late"]
    assert 'class="badge late">Likely too late' in html      # visible (no hidden attribute)
    assert "2026-10-01 (closed)" in html


def test_open_items_first_within_a_tier():
    decided = Item(source="nl_ea", item_id="3", url="u3", title="Decided", comment_deadline="2026-01-01")
    opened = Item(source="iaac", item_id="4", url="u4", title="Open", comment_deadline="2026-11-10")
    records, _, _ = _publish(_db_with([(decided, {"score": 55, "stage": "decision made"}),
                                       (opened, {"score": 40, "stage": "public comment open until 2026-11-10"})]))
    assert [r["title"] for r in records] == ["Open", "Decided"]     # both Possible; open first


def test_french_text_is_kept_and_escaped():
    item = Item(source="drop", item_id="5", url="https://example.org/5",
                title="Réfection du quai <b>Baie-Comeau</b> & dragage", province="Quebec", language="fr",
                full_text="Un plan de compensation de l'habitat du poisson sera déposé auprès de Pêches et Océans.")
    reasons = [_reason("Un plan de compensation de l'habitat du poisson sera déposé auprès de Pêches et Océans.",
                       "compensation de l'habitat", rule="habitat:offsetting", points=25)]
    _, _, html = _publish(_db_with([(item, {"score": 45, "reasons": reasons})]))
    assert "Réfection du quai &lt;b&gt;Baie-Comeau&lt;/b&gt; &amp; dragage" in html
    assert "<b>Baie-Comeau</b>" not in html
    assert "Pêches et Océans" in html


def test_sentences_split_whole_and_keep_abbreviations():
    text = "Work at St. John's Harbour will start soon. It includes approx. 20 m of wharf! Done?\nNew line here."
    assert site.split_sentences(text) == ["Work at St. John's Harbour will start soon.",
                                          "It includes approx. 20 m of wharf!", "Done?", "New line here."]


QCONF = {"max_quotes": 3, "max_quote_chars": 300,
         "quote_project_words": ["project", "proposed", "will", "would", "works?"],
         "quote_habitat_words": ["fish habitat", "offset", "compensat", "eelgrass", "shellfish", "intertidal"]}


def test_quotes_are_whole_sentences_about_the_project_with_habitat_first():
    text = ("Example Harbour Upgrade\n"
            "The harbour supports fish, shellfish and seabirds, with a rocky shoreline.\n"       # background only
            "The project will extend the breakwater by 120 m into the outer harbour.\n"
            "Rock from the work would be placed to create new fish habitat and offset losses.\n"
            "Written comments must be submitted to: Jane Doe, Regional Engineer, 1 Main St.\n"  # contact line
            "The proposed works will also replace the existing ramp " + "and its approaches " * 20 + "next year.")
    reasons = [{"matched": "breakwater", "points": 20}, {"matched": "fish habitat", "points": 10}]
    quotes = site.pick_quotes(text, reasons, QCONF, title="Example Harbour Upgrade")
    assert quotes == ["Rock from the work would be placed to create new fish habitat and offset losses.",
                      "The project will extend the breakwater by 120 m into the outer harbour."]
    for q in quotes:
        assert q in text and q.endswith(".") and "..." not in q        # exact and never cut


def test_quotes_skip_acronym_lists_and_pdf_headers():
    text = ("NL Newfoundland and Labrador NLDECCC Department NL ESA NL Endangered Species Act PRMT project Ltd.\n"
            "Point Example Ltd. Page 6 Project Summary JBC-23-C-192 2.3.2 The project will expand the wharf.\n"
            "The project will expand the wharf into the harbour.")
    quotes = site.pick_quotes(text, [{"matched": "wharf", "points": 15}], QCONF)
    assert quotes == ["The project will expand the wharf into the harbour."]


def test_written_site_files(tmp_path):
    conn = _db_with([(Item(source="iaac", item_id="1", url="u1", title="One"), {"score": 70})])
    records, _ = site.build_public_records(conn, {}, SETTINGS, TODAY)
    site.write_site(tmp_path, records, SETTINGS, TODAY)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    about = (tmp_path / "about.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert "contact@example.org" in html
    assert "Automatic, not yet reviewed" in about and "Strong match" in about
    assert json.loads((tmp_path / "items.json").read_text(encoding="utf-8"))[0]["tier"] == "strong"


def test_no_em_dashes_or_required_claims_in_site_text():
    html = site.render_index([], SETTINGS["site"], SETTINGS["tiers"], TODAY) + \
        site.render_about(SETTINGS["site"], SETTINGS["tiers"], TODAY)
    assert "\u2014" not in html
    assert "will be required" not in html.lower()


def test_prefilled_automatic_summaries_are_not_treated_as_yours():
    auto = {"summary": "Project by Example Ltd. in Example Bay, Nova Scotia. The notice mentions dredging work."}
    assert review.own_summary(auto) == ""
    assert review.own_summary({"summary": "Project in Manche Est - mer du Nord. The page mentions offshore wind work."}) == ""
    mine = {"summary": "Checked: new 300 m wharf with fish habitat offsetting planned."}
    assert review.own_summary(mine) == mine["summary"]


def test_notice_from_for_laws_regulations_and_funding():
    from mpf.review import auto_summary
    facts = '{"types": ["shoreline_protection"], "sizes": []}'
    reg = {"title": "Miljøskadebekendtgørelse efter kystbeskyttelsesloven", "proponent": "Naturstyrelsen", "facts": facts}
    assert auto_summary(reg).startswith("Notice from Naturstyrelsen.")
    fund = {"title": "Coastal Resilience Funding Program 2027", "proponent": "unknown", "facts": facts}
    assert auto_summary(fund).startswith("Notice from an unknown authority.")
    conv = {"title": "Resolución por la que se publica el Convenio entre la Agencia y la Autoridad Portuaria", "facts": facts}
    assert auto_summary(conv).startswith("Notice")
    proj = {"title": "Example Harbour Breakwater Extension", "proponent": "Town of Example", "facts": facts}
    assert auto_summary(proj).startswith("Project by Town of Example")
    assert review.own_summary({"summary": auto_summary(reg)}) == ""       # still recognised as automatic text


def test_facts_only_source_gets_no_quotes_and_credit_line():
    item = Item(source="scot_marine", item_id="1", url="https://marine.gov.scot/?q=node/1", country="GB",
                title="Marine Licence - Seagrass Planting - Example Sound", province="Scotland",
                full_text="The project will plant seagrass to create new fish habitat in the sound.")
    conn = _db_with([(item, {"score": 30})])
    rec = site.build_public_records(conn, {}, SETTINGS, TODAY)[0][0]
    assert rec["quotes"] == [] and rec["attribution"] == "© Crown Copyright"
    assert "© Crown Copyright" in site.render_index([rec], SETTINGS["site"], SETTINGS["tiers"], TODAY)


def test_mostly_canada_note_and_empty_country_note():
    recs = [{"country": "CA", "tier": "weak"}] * 3 + [{"country": "GB", "tier": "weak"}]
    assert site.balance_note(recs).startswith("For now the site is mostly Canada: 3 of the 4 listed notices")
    from mpf.coverage import load_coverage
    rows = site.coverage_rows(load_coverage(), {"CA": 3, "GB": 1})
    assert "Source read; no marine project hearings found in this run." in rows      # Denmark, 0 items
    rows = site.coverage_rows(load_coverage(), {"CA": 3, "DK": 2})
    assert "no marine project hearings found" not in rows


def test_every_card_has_an_agency_attribution_line():
    items = [Item(source="iaac", item_id="1", url="u1", title="A wharf", country="CA"),
             Item(source="es_boe", item_id="2", url="u2", title="Un muelle", country="ES", language="es"),
             Item(source="dk_hoering", item_id="3", url="u3", title="En havn", country="DK", language="da",
                  proponent="Kystdirektoratet")]
    conn = _db_with([(i, {"score": 50}) for i in items])
    records, _ = site.build_public_records(conn, {}, SETTINGS, TODAY)
    html = site.render_index(records, SETTINGS["site"], SETTINGS["tiers"], TODAY)
    assert html.count('<p class="attr">Source: ') == 3
    assert "Source: Impact Assessment Agency of Canada" in html
    assert "Source: Agencia Estatal Boletín Oficial del Estado (www.boe.es)." in html
    assert "Hearing by Kystdirektoratet." in html
