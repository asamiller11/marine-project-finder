"""Calm, blue layout (owner, 2026-11-22): a short home page, a list that opens on the cards, project pages in three
sections. Layout only: scores, data and the weekly update are unchanged."""
import re

from mpf import site
from tests.test_batch1 import SETTINGS, TODAY, _records
from tests.test_freshwater_fix import _items


def _built(tmp_path):
    site.write_site(tmp_path, _records(_items()), SETTINGS, TODAY)
    read = lambda n: (tmp_path / n).read_text(encoding="utf-8")
    return read("index.html"), read("projects.html"), read("p/iaac-1.html")


def test_palette_is_defined_once_as_variables():
    css = site.CSS
    for name, hexv in (("navy", "#0B3C5D"), ("sea", "#1B7F9E"), ("mid", "#4FA3BF"), ("tint", "#E8F4F8"),
                       ("soft", "#F3F9FB"), ("coral", "#E2725B"), ("text", "#16313F")):
        assert f"--{name}:{hexv}" in css
    assert "--coral-text:#B4452E" in css          # coral text needs a darker shade to reach 4.5:1 on white


def test_home_page_is_short_and_in_order(tmp_path):
    home, _, _ = _built(tmp_path)
    order = ["Find coastal projects that need marine mitigation, before they&rsquo;re awarded.",
             '<ul class="stats">', 'id="how-h"', 'id="picks-h"', "See all projects", 'id="who-h"', "<footer"]
    pos = [home.index(x) for x in order]
    assert pos == sorted(pos)
    assert ">See projects</a>" in home and ">How it works</a>" in home
    assert home.split('<ul class="stats">')[1].split("</ul>")[0].count("<li>") == 3
    assert home.count('<li><span class="icon">') == 3
    assert home.split("Top picks this week")[1].split("See all projects")[0].count('<article class="card') <= 3
    assert "reef builders, living shoreline firms, mitigation banks" in home
    assert 'class="wave"' in home and "profile-bar" not in home


def test_old_list_links_still_work(tmp_path):
    home, projects, _ = _built(tmp_path)
    assert "location.replace('projects.html'+s+h)" in home and "location.replace('p/'+m[1]+'.html')" in home
    assert "location.replace('p/'+m[1]+'.html')" in projects


def test_projects_page_opens_on_the_list(tmp_path):
    _, html, _ = _built(tmp_path)
    assert 'class="sidebar"' not in html and 'id="tools"' not in html          # no permanent sidebar
    picker = html.split('class="blue picker')[1].split("</section>")[0]
    assert 'id="f-profile"' in picker and 'id="profile-desc"' in picker and 'id="f-search"' not in picker
    assert '<aside class="sheet" id="filters" role="dialog"' in html and 'id="filters-btn"' in html
    for panel in ("soon", "coverage", "how-to-read"):
        assert f'aria-controls="{panel}"' in html and f'id="{panel}" hidden' in html.replace('" aria-labelledby="soon-h" hidden', '" hidden')
    assert "PAGE = 12" in html and 'id="show-more"' in html
    card = html.split('<article class="card')[1].split("</article>")[0]
    assert "<details" not in card and "stage-badge" not in card                 # name, place, fit, deadline only


def test_project_page_has_a_blue_band_and_three_sections(tmp_path):
    _, _, page = _built(tmp_path)
    assert '<section class="blue page-band">' in page
    folds = re.findall(r'<details class="fold"( open)?><summary>([^<]+)</summary>', page)
    assert [t for _, t in folds] == ["Why it matters", "Who to contact", "How to get in"]
    assert [bool(o) for o, _ in folds] == [True, False, False]
    assert "<strong>Approach first:</strong>" in page
    assert "transform:rotate" not in site.CSS                                   # arrows swap, never rotate


def test_approach_first_names_owner_consultant_or_contractor():
    rec = {"win": {"entry_point": "Design stage: contact the proponent (Port); no engineering firm is named yet."},
           "stakeholders": {"owner": [{"name": "Port Authority"}]}}
    assert site.approach_first(rec) == "the owner, Port Authority"
    rec["win"]["entry_point"] = "Approved: contact the engineering firm (Stantec) before tender."
    rec["stakeholders"]["consultant"] = [{"name": "Stantec"}]
    assert site.approach_first(rec) == "the engineering consultant, Stantec"
    rec["win"]["entry_point"] = "Under construction: contact the contractor (Dexter)."
    rec["stakeholders"]["contractor"] = [{"name": "Dexter"}]
    assert site.approach_first(rec) == "the contractor, Dexter"


def test_no_em_dashes_or_emoji_in_the_new_pages(tmp_path):
    for html in _built(tmp_path):
        assert "—" not in html
        assert not re.search("[\U0001F300-\U0001FAFF☀-⛿]", html)


def test_softer_pass_header_blends_and_bands_curve(tmp_path):
    """Owner, 2026-11-22: softer and less boxy. Header on the band, curved band ends, no card outlines."""
    home, projects, page = _built(tmp_path)
    css = site.CSS
    assert ".topbar+main>.blue:first-child,.topbar+.blue{margin-top:-200px" in css      # band reaches under header
    assert ".blue:not(.hero){border-radius:0 0 50% 50%" in css                          # soft arc, not a straight cut
    assert "footer{position:relative" in css and "border-radius:50% 50% 0 0" in css
    assert "--shadow:0 12px 32px rgba(11,60,93,.08)" in css and "--r:22px" in css
    for rule in re.findall(r"\.(?:card|steps li|who-for a|side-card|info-panel|prose)\{[^}]*\}", css):
        assert "border:1px" not in rule
    assert "font-weight:700;color:var(--text)" not in css                              # headings are 600
    assert 'class="band band-white curve-bottom" id="how"' in home and "curve-top" in home
    picker = projects.split('class="blue picker')[1].split("</section>")[0]
    assert '<div class="pick">' in picker and '<div class="pick-about"><p id="profile-desc"' in picker
    assert "flex-wrap:nowrap" in css.split(".toolbar{")[1].split("}")[0]                # scrolls sideways on phones
    assert "repeat(2,minmax(0,1fr))" in css                                            # two-column list on wide screens
    assert "prefers-reduced-motion: reduce" in page and "IntersectionObserver" in page  # fade-in respects motion


def test_filters_in_use_are_always_visible(tmp_path):
    """Owner, 2026-11-23: "open for comment" stayed on unseen and emptied every country. Filters in use now show
    as removable chips, the empty message names them, and the address only keeps filters still switched on."""
    _, projects, _ = _built(tmp_path)
    assert 'id="active-filters"' in projects and "Open for public comment only" in projects
    assert "Remove filter: " in projects and "Clear all filters" in projects
    assert "No projects match these filters: " in projects and "OWN = ['profile', 'open', 'country'" in projects
    assert "fo.checked = false" in projects
