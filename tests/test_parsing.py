"""Parsers, run against made-up pages that copy the real sites' HTML structure (tests/samples)."""
from mpf.sources import dfo_far, iaac, nl_ea


def test_iaac_comment_list(samples):
    items = iaac.parse_comment_list((samples / "iaac_comment_list.html").read_text())
    assert set(items) == {"99001", "99002", "99003"}
    first = items["99001"]
    assert first["title"] == "Example Harbour Shoreline Repair – Public comments invited"
    assert first["notices"][0]["ends"] == "2026-11-01"
    assert first["provinces"] == ["British Columbia"]
    assert items["99003"]["provinces"] == []        # only listed under "All"


def test_iaac_project_page(samples):
    p = iaac.parse_project_page((samples / "iaac_project_99001.html").read_text())
    assert p["proponent"] == "Example Port Authority"
    assert p["province"] == "British Columbia"
    assert p["status"] == "In progress"
    assert p["start_date"] == "2026-10-02"
    assert "riprap revetment" in p["summary"]
    assert "Ports and Harbours" in p["nature"]
    # The "nearby assessments" list must NOT leak into the text we score.
    assert "Habitat Offsetting" not in p["summary"] + p["latest_update"]


def test_iaac_deadline_from_notice_text():
    text = "Written comments to be received by November 1, 2026."
    assert iaac._deadline_from_text(text) == "2026-11-01"


def test_nl_project_list(samples):
    rows = nl_ea.parse_project_list((samples / "nl_project_list.html").read_text())
    assert len(rows) == 4
    row = next(r for r in rows if r["reg"] == "9002")
    assert row["title"] == "Sample Cove Navigation Improvement"
    assert row["proponent"] == "Example Ferry Corp."
    assert row["registered"] == "2026-09-15"
    assert row["status"] == "Active"
    released = next(r for r in rows if r["reg"] == "9001")
    assert released["status"] == "Released" and released["release_date"] == "2025-04-10"


def test_nl_project_page(samples):
    p = nl_ea.parse_project_page((samples / "nl_project_9002.html").read_text())
    assert p["proponent"] == "Example Ferry Corp."
    assert p["comment_deadline"] == "2026-10-20"
    assert p["decision_due"] == "2026-10-30"
    assert p["decision_made"] is None
    assert "new fish habitat" in p["description"]
    assert p["summary_pdf"].endswith("example-summary.pdf")


def test_dfo_csv(samples):
    rows = dfo_far.parse_csv((samples / "dfo_far_sample.csv").read_bytes())
    item = dfo_far.row_to_item(rows[0], "https://example.org/x.csv")
    assert item.is_test_set
    assert item.title == "Example Breakwater Extension, Sample Bay"
    assert item.lat == 44.5
    french = dfo_far.row_to_item(rows[1], "https://example.org/x.csv")
    assert french.language == "fr" or french.language == "unknown"
