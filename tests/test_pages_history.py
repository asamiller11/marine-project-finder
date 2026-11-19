"""Project pages, snapshot history, comment tracking, CSV export and the watch list."""
import csv
import json
from datetime import date
from pathlib import Path

from mpf import comments, history, site
from mpf.models import Item

from tests.test_site import SETTINGS, TODAY, _db_with, _reason


def _rec(id_, deadline=None, stage="open", tier="weak", title="A wharf"):
    return {"id": id_, "comment_deadline": deadline, "stage_kind": stage, "tier": tier, "title": title}


# --- history -------------------------------------------------------------------

def test_first_snapshot_marks_nothing_new_then_changes_are_found():
    h = history.load(Path("/nonexistent/history.json"))
    h = history.record_snapshot(h, [_rec("a", "2026-11-01"), _rec("b")], "2026-10-15")
    assert h["events"] == {}
    recs = [_rec("a", "2026-11-20"), _rec("b", stage="decided"), _rec("c")]
    h = history.record_snapshot(h, recs, "2026-10-29")
    history.annotate(recs, h)
    by = {r["id"]: r for r in recs}
    assert by["a"]["changes"] == ["deadline"]
    assert "changed from 2026-11-01 to 2026-11-20" in by["a"]["timeline"][0]["text"]
    assert by["b"]["changes"] == ["decided"]
    assert by["c"]["changes"] == ["new"] and by["c"]["first_listed"] == "2026-10-29"
    assert by["a"]["first_listed"] == "2026-10-15"


def test_open_to_closed_is_not_an_event():
    h = history.record_snapshot(history.load(Path("/x")), [_rec("a", "2026-10-20", "open")], "2026-10-15")
    h = history.record_snapshot(h, [_rec("a", "2026-10-20", "closed")], "2026-10-29")
    assert h["events"] == {}


def test_publishing_twice_on_one_day_replaces_that_snapshot():
    h = history.record_snapshot(history.load(Path("/x")), [_rec("a", "2026-11-01")], "2026-10-15")
    h = history.record_snapshot(h, [_rec("a", "2026-11-05")], "2026-10-29")
    h = history.record_snapshot(h, [_rec("a", "2026-11-09")], "2026-10-29")
    assert h["snapshots"] == ["2026-10-15", "2026-10-29"]
    evs = h["events"]["a"]
    assert len(evs) == 1 and "2026-11-01 to 2026-11-09" in evs[0]["text"]


# --- comment tracking -------------------------------------------------------------

def test_comment_opening_date_stated_or_from_the_notice_date():
    stated = "Notice of Intent – Start of Public Comment Period October 27, 2026 – The authorities intend ..."
    assert comments.comment_opens(stated, "2026-11-27") == ("2026-10-27", "stated")
    notice = ("Notice of intent Raising of a wharf September 22, 2026 – The authorities intend to make a "
              "determination. To support it, they invite the public to provide comments on this determination.")
    assert comments.comment_opens(notice, "2026-11-01") == ("2026-09-22", "notice date")
    assert comments.comment_opens("October 1, 2026 – The project was registered.", "2026-11-01") == (None, "")
    assert comments.comment_opens(stated, None) == (None, "")          # no closing date: do not guess
    assert comments.comment_opens(stated, "2026-10-01") == (None, "")  # an opening after the close is wrong


def test_comment_count_parsed_only_from_the_total_line():
    html = '<span class="task">View comments</span><span id="commentCount" class="found">Total: 1,463</span>'
    assert comments.parse_comment_count(html) == 1463
    assert comments.parse_comment_count("<p>463 comments were sent by Jane Doe</p>") is None


def test_comment_info_shows_not_published_and_never_counts_for_other_sources():
    row = {"source": "nl_ea", "comment_deadline": "2026-11-10", "full_text": ""}
    info = comments.comment_info(row, {"comment_count": 5}, date(2026, 10, 31))
    assert info["count"] is None and info["closes"] == "2026-11-10" and info["days_left"] == 10
    assert "not the number" in info["count_note"]
    iaac = comments.comment_info({"source": "iaac", "comment_deadline": None, "full_text": ""},
                                 {"comment_count": 12, "comment_count_as_of": "2026-10-28",
                                  "comments_url": "https://iaac-aeic.gc.ca/050/evaluations/proj/1/contributions"},
                                 date(2026, 10, 31))
    assert iaac["count"] == 12 and iaac["count_note"] == "" and iaac["comments_url"].endswith("/contributions")


# --- written site -------------------------------------------------------------

def _written(tmp_path, extra=None):
    item = Item(source="iaac", item_id="77", url="https://example.org/77", title="Harbour Wharf Extension",
                proponent="Port Authority", comment_deadline="2026-11-20",
                full_text="October 20, 2026 – The authority is inviting public comments until November 20, 2026.\n"
                          "The project will place 4,000 m2 of fill in fish habitat and create new eelgrass habitat.",
                extra=extra or {"comment_count": 9, "comment_count_as_of": "2026-10-27",
                                "comments_url": "https://example.org/77/contributions"})
    conn = _db_with([(item, {"score": 45, "types": ["Wharf, pier or large dock"],
                             "reasons": [_reason("fish habitat", "fish habitat")]})])
    records, _ = site.build_public_records(conn, {}, SETTINGS, TODAY)
    site.write_site(tmp_path, records, SETTINGS, TODAY)
    return records


def test_every_project_gets_a_stable_page_with_source_badges_comments_and_timeline(tmp_path):
    records = _written(tmp_path)
    assert records[0]["page"] == "p/iaac-77.html"
    page = (tmp_path / "p" / "iaac-77.html").read_text()
    assert "Harbour Wharf Extension" in page and 'class="attr">Source:' in page
    assert "tier-badge" in page and "Listed automatically; not yet checked by a person." in page
    assert "2026-10-20 (date of the notice)" in page
    assert "9 (as of 2026-10-27)" in page and 'href="https://example.org/77/contributions"' in page
    assert "<h2>Timeline</h2>" in page and "Comment period closes (official notice)" in page
    assert '<meta name="robots" content="noindex, nofollow">' in page
    index = (tmp_path / "index.html").read_text()
    assert 'href="p/iaac-77.html"' in index


def test_missing_comment_data_says_not_published(tmp_path):
    _written(tmp_path, extra={"link_ok": True})
    page = (tmp_path / "p" / "iaac-77.html").read_text()
    assert "<dt>Comments received</dt><dd>Not published</dd>" in page


def test_unlisted_project_page_keeps_its_address_with_a_note(tmp_path):
    _written(tmp_path)
    site.write_site(tmp_path, [], SETTINGS, date(2026, 11, 12))
    page = (tmp_path / "p" / "iaac-77.html").read_text()
    assert "no longer listed on this site (as of 2026-11-12)" in page
    site.write_site(tmp_path, [], SETTINGS, date(2026, 11, 26))
    assert (tmp_path / "p" / "iaac-77.html").read_text().count("no longer listed") == 1


def test_csv_export_has_facts_but_no_quotes(tmp_path):
    _written(tmp_path)
    rows = list(csv.reader(open(tmp_path / "projects.csv", encoding="utf-8-sig")))
    # The fixed columns come first, then one column per buyer profile.
    assert rows[0][:len(site.CSV_COLUMNS)] == site.CSV_COLUMNS
    assert rows[0][len(site.CSV_COLUMNS):] == ["profile_hard_structure", "profile_living_shoreline", "profile_seagrass",
                                               "profile_kelp", "profile_coral", "profile_banking"]
    row = dict(zip(rows[0], rows[1]))
    assert row["comments_close"] == "2026-11-20" and row["comments_received"] == "9"
    assert row["source"] and row["official_notice"] == "https://example.org/77"
    assert "eelgrass" not in open(tmp_path / "projects.csv", encoding="utf-8-sig").read()
    data = (tmp_path / "index.html").read_text()
    assert 'id="csv-data"' in data and json.loads(data.split('id="csv-data">')[1].split("</script>")[0])["rows"]


def test_history_file_is_written_and_second_update_marks_changes(tmp_path):
    _written(tmp_path)
    h = json.loads((tmp_path / "history.json").read_text())
    assert h["snapshots"] == [TODAY.isoformat()]
    records = [dict(r) for r in json.loads((tmp_path / "items.json").read_text())]
    records[0]["comment_deadline"] = "2026-12-01"
    site.write_site(tmp_path, records, SETTINGS, date(2026, 11, 12))
    index = (tmp_path / "index.html").read_text()
    assert "What changed since the last update (2026-10-28)" in index and "1 deadline changed" in index
    assert "Deadline changed" in (tmp_path / "p" / "iaac-77.html").read_text()


def test_watch_list_stays_in_the_browser():
    js = site.JS_COMMON
    assert "localStorage" in js and "fetch(" not in js and "XMLHttpRequest" not in js


def test_only_listed_projects_with_a_count_are_re_read(tmp_path):
    from mpf.sources.iaac import listed_with_count
    p = tmp_path / "items.json"
    p.write_text(json.dumps([
        {"id": "iaac:1", "source": "iaac", "comments": {"count": 4}},
        {"id": "iaac:2", "source": "iaac", "comments": {"count": None, "closes": "2026-11-20"}},
        {"id": "nl_ea:3", "source": "nl_ea", "comments": {"count": None}}]))
    assert listed_with_count(p) == {"1"}
    assert listed_with_count(tmp_path / "missing.json") == set()


def test_site_name_is_mitimark_in_titles_and_footer(tmp_path):
    from mpf.config import load_settings
    conf = load_settings()["site"]
    assert conf["title"] == "MitiMark"
    settings = dict(SETTINGS, site=dict(SETTINGS["site"], title="MitiMark"))
    _written(tmp_path)
    records = json.loads((tmp_path / "items.json").read_text())
    site.write_site(tmp_path, records, settings, TODAY)
    for name in ("index.html", "about.html", "p/iaac-77.html"):
        html = (tmp_path / name).read_text()
        assert "MitiMark" in html.split("</title>")[0] and "MitiMark is a prototype" in html
        assert "Marine Project Finder" not in html
