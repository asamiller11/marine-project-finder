"""Fixes after the 2026-11-11 live check: profile banner figures and the closing-soon table leave out freshwater
projects the profile hides, phone view tabs fit, the About page has no gap at the top, and shortlist cards show
the approval date when the notice gives one."""
import re
from datetime import date

from mpf import site
from mpf.models import Item
from tests.test_batch1 import BW, SETTINGS, TODAY, _records


def _items():
    return [
        (Item(source="iaac", item_id="1", url="https://example.org/1", title="Breakwater at Test SCH",
              full_text=BW, country="CA", province="Nova Scotia", comment_deadline="2026-11-20"), 70,
         "comment period open until 2026-11-20"),
        (Item(source="nl_ea", item_id="2", url="https://example.org/2", title="Approved Breakwater",
              full_text=BW, country="CA", province="Newfoundland and Labrador", decision_date="2025-07-11"), 45,
         "decision made"),
        (Item(source="iaac", item_id="3", url="https://example.org/3", title="Lake Breakwater",
              full_text=BW.replace("in the harbour", "on Lake Ontario"), country="CA", province="Ontario",
              comment_deadline="2026-11-25"), 80, "comment period open until 2026-11-25"),
    ]


def _script(html):
    return html[html.find("function freshOk"):]


def test_banner_and_closing_soon_table_use_the_freshwater_filter(tmp_path):
    site.write_site(tmp_path, _records(_items()), SETTINGS, TODAY)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    js = _script(html)
    assert "PFRESH[prof()] && c.getAttribute('data-fresh') && !(withFresh || (tf && tf.checked))" in js
    # Banner figures, the closing-soon table and the project list all apply the same check.
    assert re.search(r"function paintStats\(\)\{\s*var mine = cards\.filter\(function\(c\)\{ return listed\(tierOf\(c\)\) && freshOk\(c\); \}\)", js)
    assert re.search(r"var soon = cards\.filter\(function\(c\)\{[^}]*freshOk\(c\)", js)
    assert "if (!freshOk(c, withFresh)) return false;" in js
    # The fixture's freshwater project is marked so the script can hide it.
    assert 'data-fresh="1"' in html


def test_phone_view_tabs_fit_in_a_grid_and_about_has_no_top_gap(tmp_path):
    site.write_site(tmp_path, _records(_items()), SETTINGS, TODAY)
    css = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert ".views{display:grid;grid-template-columns:1fr 1fr;width:100%;overflow:visible}" in css
    assert "[hidden]{display:none!important}" in css      # the tabs still stay hidden until the script runs
    about = (tmp_path / "about.html").read_text(encoding="utf-8")
    assert ".prose h2:first-child{border-top:0;padding-top:20px;margin-top:0}" in about


def test_approval_date_from_the_notice():
    row = {"decision_date": "2026-07-05", "extra": '{"decision_made": {"date": "2026-07-02", "text": "released"}}'}
    assert site.approval_date(row, "decided", TODAY) == "2026-07-02"
    assert site.approval_date({"decision_date": "2025-07-11"}, "decided", TODAY) == "2025-07-11"
    assert site.approval_date({"decision_date": "2025-07-11"}, "open", TODAY) is None       # not approved
    assert site.approval_date({"decision_date": "2027-01-01"}, "decided", TODAY) is None     # a future due date
    assert site.approval_date({"decision_date": ""}, "decided", TODAY) is None              # notice gives none
    assert site.approved_label("2025-07-11") == "Approved July 2025"
    assert site.approved_label(None) == ""


def test_shortlist_card_shows_approval_date_only_when_given(tmp_path):
    site.write_site(tmp_path, _records(_items()), SETTINGS, TODAY)
    page = (tmp_path / "shortlist.html").read_text(encoding="utf-8")
    assert '<time datetime="2025-07-11">Approved July 2025</time>' in page
    assert page.count("approved-on") == 1               # only the approved project carries a date
    # Source line, quotes and badges stay.
    assert "Source:" in page and "&ldquo;" in page and "Approved, may not be built yet" in page
