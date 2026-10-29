"""Site generator and review step: missing fields, closed periods, French text, long quotes."""
import csv
import json
from datetime import date
from pathlib import Path

from mpf import db, review, site
from mpf.models import Item

TODAY = date(2026, 10, 28)
SETTINGS = {
    "review": {"min_score": 20, "confidence_high": 60, "confidence_medium": 35},
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


def test_missing_fields_show_unknown():
    item = Item(source="iaac", item_id="1", url="https://example.org/1")      # almost everything missing
    conn = _db_with([(item, {"score": 30})])
    rec = site.build_records(conn, {}, ["iaac:1"], SETTINGS, TODAY)[0]
    html = site.render_index([rec], SETTINGS["site"], TODAY)
    assert rec["proponent"] == "unknown" and rec["water_body"] == "unknown"
    assert "Water body: unknown" in html
    assert ">unknown<" in html                      # comment deadline
    assert rec["stage_kind"] == "unknown"


def test_closed_comment_period_is_likely_too_late():
    item = Item(source="nl_ea", item_id="2", url="https://example.org/2", title="Closed one",
                comment_deadline="2026-10-01")
    conn = _db_with([(item, {"score": 50, "stage": "comment closed 2026-10-01, decision pending"})])
    rec = site.build_records(conn, {}, ["nl_ea:2"], SETTINGS, TODAY)[0]
    assert rec["stage_kind"] == "closed" and rec["too_late"]
    html = site.render_index([rec], SETTINGS["site"], TODAY)
    assert 'class="badge late">Likely too late' in html      # visible (no hidden attribute)
    assert "2026-10-01 (closed)" in html


def test_decision_made_and_open_sorting():
    decided = Item(source="nl_ea", item_id="3", url="u3", title="Decided", comment_deadline="2026-01-01")
    opened = Item(source="iaac", item_id="4", url="u4", title="Open", comment_deadline="2026-11-10")
    conn = _db_with([(decided, {"score": 90, "stage": "decision made"}),
                     (opened, {"score": 25, "stage": "public comment open until 2026-11-10"})])
    recs = site.build_records(conn, {}, ["nl_ea:3", "iaac:4"], SETTINGS, TODAY)
    assert [r["title"] for r in recs] == ["Open", "Decided"]     # open first, despite lower score
    assert recs[1]["stage_kind"] == "decided"


def test_french_text_is_kept_and_escaped():
    item = Item(source="drop", item_id="5", url="https://example.org/5",
                title="Réfection du quai <b>Baie-Comeau</b> & dragage", province="Quebec", language="fr")
    reasons = [_reason("Un plan de compensation de l'habitat du poisson sera déposé auprès de Pêches et Océans.",
                       "compensation de l'habitat", rule="habitat:offsetting", points=25)]
    conn = _db_with([(item, {"score": 45, "reasons": reasons})])
    rec = site.build_records(conn, {}, ["drop:5"], SETTINGS, TODAY)[0]
    html = site.render_index([rec], SETTINGS["site"], TODAY)
    assert "Réfection du quai &lt;b&gt;Baie-Comeau&lt;/b&gt; &amp; dragage" in html
    assert "<b>Baie-Comeau</b>" not in html
    assert "Pêches et Océans" in html


def test_long_quotes_are_trimmed_but_exact():
    long = ("The proponent has studied the site for many seasons and " * 4 +
            "the infill area may support the development of new fish habitat in the outer harbour, " +
            "and further studies " * 6 + "will follow.")
    q = site.trim_quote(long, "new fish habitat", 180)
    assert len(q) <= 186                                   # 180 plus the "..." markers
    assert "new fish habitat" in q
    assert q.strip(".") in long                            # the kept text is an exact piece
    assert q.startswith("...") and q.endswith("...")


def test_quotes_skip_titles_contacts_and_repeats():
    reasons = [
        _reason("Example Wharf Repair", "Wharf", rule="type:wharf_dock", points=15),
        _reason("Written comments must be submitted to: Jane Doe, Regional Engineer, 1 Main St.", "Harbour"),
        _reason("Dredging of the harbour basin is planned next to the wharf.", "Dredging", rule="type:dredging"),
        _reason("Dredging of the harbour basin is planned next to the wharf.", "wharf", rule="type:wharf_dock"),
    ]
    quotes = site.pick_quotes(reasons, 3, 180, title="Example Wharf Repair")
    assert quotes == ["Dredging of the harbour basin is planned next to the wharf."]


def test_only_approved_items_are_published(tmp_path):
    a = Item(source="iaac", item_id="10", url="u10", title="Approved one")
    b = Item(source="iaac", item_id="11", url="u11", title="Not approved one")
    conn = _db_with([(a, {"score": 50}), (b, {"score": 50})])
    path = tmp_path / "review.csv"
    review.write_review(conn, path, SETTINGS["review"])
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert {r["publish"] for r in rows} == {"no"}            # default is no
    for r in rows:
        if r["id"] == "iaac:10":
            r["publish"] = "yes"
            r["summary"] = "My own summary."
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=review.COLUMNS)
        w.writeheader()
        w.writerows(rows)
    review.write_review(conn, path, SETTINGS["review"])      # a later run keeps my decisions
    assert review.approved_ids(path) == ["iaac:10"]
    recs = site.build_records(conn, review.read_review(path), review.approved_ids(path), SETTINGS, TODAY)
    site.write_site(tmp_path / "site", recs, SETTINGS["site"], TODAY)
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "Approved one" in html and "Not approved one" not in html
    assert "My own summary." in html
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert "contact@example.org" in html
    assert (tmp_path / "site" / "about.html").exists()


def test_no_em_dashes_or_required_claims_in_site_text():
    html = site.render_index([], SETTINGS["site"], TODAY) + site.render_about(SETTINGS["site"], TODAY)
    assert "\u2014" not in html
    assert "will be required" not in html.lower()
    assert "No projects have been published yet" in html
