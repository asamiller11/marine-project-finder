"""New logo (map pin with a sea fan coral) and the softer design: one logo source, every size built from it,
comment deadlines shown as a date, not a day count."""
import re
import struct
from datetime import date

from mpf import icons, site
from tests.test_batch1 import SETTINGS, TODAY, _records
from tests.test_freshwater_fix import _items


def test_logo_source_is_a_sea_blue_pin_with_a_white_coral():
    svg = (icons.ASSETS / "logo.svg").read_text(encoding="utf-8")
    assert 'fill="#1B7F9E"' in svg and 'stroke="#fff"' in svg
    assert svg.count("<circle") == 6                       # six round tips: three rays, each forking once
    assert 'stroke-linecap="round"' in svg
    fav = (icons.ASSETS / "favicon.svg").read_text(encoding="utf-8")
    assert fav.count("<circle") == 3                       # tab icon: pin and three thick branches


def test_site_uses_the_new_logo_everywhere(tmp_path):
    site.write_site(tmp_path, _records(_items()), SETTINGS, TODAY)
    for name in ("logo.svg", "favicon.svg", "favicon.ico", "favicon-16.png", "favicon-32.png",
                 "apple-touch-icon.png", "logo-144.png"):
        assert (tmp_path / name).read_bytes() == (icons.ASSETS / name).read_bytes(), name
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert '<svg class="mark"' in index and "#1B7F9E" in index
    assert 'href="favicon-32.png"' in index and 'rel="apple-touch-icon"' in index
    page = next((tmp_path / "p").glob("*.html")).read_text(encoding="utf-8")
    assert 'href="../favicon-32.png"' in page              # project pages sit one folder down
    about = (tmp_path / "about.html").read_text(encoding="utf-8")
    assert '<svg class="mark"' in about
    feed = next((tmp_path / "feeds").glob("*.xml")).read_text(encoding="utf-8")
    assert "logo-144.png</url>" in feed                    # RSS readers want a PNG, not an SVG
    old = "M14 6h4v21h-4z"                                 # the old pile, tide line and signal arcs
    assert not any(old in f.read_text(encoding="utf-8", errors="ignore") for f in tmp_path.rglob("*.html"))


def test_ico_holds_both_pngs():
    ico = (icons.ASSETS / "favicon.ico").read_bytes()
    assert struct.unpack("<HHH", ico[:6]) == (0, 1, 2)
    assert {ico[6], ico[22]} == {16, 32}


def test_comment_deadlines_say_the_date_not_a_day_count(tmp_path):
    assert site.short_date("2026-12-03", date(2026, 11, 19)) == "Dec 3"
    assert site.short_date("2027-01-08", date(2026, 11, 19)) == "Jan 8, 2027"
    site.write_site(tmp_path, _records(_items()), SETTINGS, TODAY)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "Public comment deadline <span class=\"countdown\">Nov 20</span>" in html
    assert not re.search(r"days? left|closing in", html)


def test_one_shadow_style_one_accent_and_big_tap_targets(tmp_path):
    css = site.CSS
    assert len(set(re.findall(r"box-shadow:([^;}]+)", css))) == 1     # only var(--shadow)
    # The owner asked for one flat navy-to-sea gradient on the blue bands (2026-11-22), no blur or glass.
    assert len(re.findall(r"gradient\(", css)) == 1 and "backdrop-filter" not in css and "blur" not in css
    assert "min-height:48px" in css                                    # phone tap targets
    assert "—" not in css
