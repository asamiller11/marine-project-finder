"""Scoring, size extraction, timing and robots.txt rules."""
from datetime import date

from mpf.extract import extract_coordinates, extract_sizes, extract_water_body, largest_sizes
from mpf.http import Robots
from mpf.llm import check_result, verify_quotes
from mpf.score import score_text
from mpf.sources import iaac, nl_ea
from mpf.textutil import guess_language, parse_date
from mpf.timing import compute


def _text_nl(samples):
    p = nl_ea.parse_project_page((samples / "nl_project_9002.html").read_text())
    return p["title"] + "\n" + p["description"]


def _text_iaac(samples):
    p = iaac.parse_project_page((samples / "iaac_project_99001.html").read_text())
    return "\n".join([p["title"], p["summary"], p["latest_update"]])


def test_harbour_with_new_fish_habitat_is_flagged(samples, rules):
    s = score_text(_text_nl(samples), rules)
    assert s["flagged"], s["score"]
    assert "habitat_creation" in s["facts"]["habitat_clues"]
    # Every reason carries a quote taken from the text.
    text = _text_nl(samples)
    assert all(r["quote"] in text for r in s["reasons"] if r["quote"])


def test_small_shoreline_repair_scores_low(samples, rules):
    s = score_text(_text_iaac(samples), rules)
    assert s["score"] < 25
    assert any(r["rule"] == "size:small" for r in s["reasons"])
    # The proponent "Port Authority" must not count as a port project.
    assert "type:port_terminal" not in [r["rule"] for r in s["reasons"]]


def test_french_keywords(rules):
    text = ("Agrandissement du quai et dragage de 60 000 m³ dans la baie de Gaspé. "
            "Un plan de compensation de l'habitat du poisson sera déposé; présence de zostère.")
    s = score_text(text, rules)
    assert s["flagged"]
    assert {"offsetting", "fish_habitat", "eelgrass"} <= set(s["facts"]["habitat_clues"])
    assert largest_sizes(s["facts"]["sizes"])["volume_m3"] == 60000


def test_inland_project_is_capped(rules):
    text = "Construction of a 300 m dock and fish habitat offsetting plan at the gravel pit pond."
    s = score_text(text, rules)
    assert s["score"] <= rules["caps"]["no_marine_context_max"]


def test_sizes():
    sizes = largest_sizes(extract_sizes(
        "Dredging of 45,000 m3 and a 250 m breakwater, 2 km north of town, 12 m deep, 1,200 ft wharf, 400 MW."))
    assert sizes["volume_m3"] == 45000
    assert sizes["power_mw"] == 400
    assert sizes["length_m"] == 365.8          # 1,200 ft beats 250 m; '2 km north' and '12 m deep' ignored
    assert extract_sizes("at approximately 49.130668, -123.067900") == []


def test_size_unit_variants():
    # Real phrasings seen on IAAC pages.
    sizes = largest_sizes(extract_sizes("a footprint expansion below high normal tide of approximately 440 m 2 ."))
    assert sizes == {"area_m2": 440}
    sizes = largest_sizes(extract_sizes("Approximately, 800 meters cubed of dredge material will be removed."))
    assert sizes == {"volume_m3": 800}


def test_negated_keywords_do_not_count(rules):
    s = score_text("Breakwater in the harbour. No dredging is required.", rules)
    assert "dredging" not in s["facts"]["types"]
    s = score_text("Breakwater in the harbour. Dredging of the basin is planned.", rules)
    assert "dredging" in s["facts"]["types"]


def test_quarry_and_small_floats_are_not_marine_works(rules):
    quarry = score_text("Musgrave Harbour quarry producing armour stone and pit run material.", rules)
    assert "shoreline_protection" not in quarry["facts"]["types"]
    floats = score_text("12 m finger floats on either side to accommodate stern berthing in the harbour.", rules)
    assert "port_terminal" not in floats["facts"]["types"]


def test_coordinates_and_water_body():
    assert extract_coordinates("located at 49.130668, -123.067900.") == (49.130668, -123.0679)
    assert extract_water_body("works in Port aux Basques Harbour near the ferry") == "Port aux Basques Harbour"
    assert extract_water_body("no water named here") == "unknown"


def test_dates_and_language():
    assert parse_date("November 9, 2026") == "2026-11-09"
    assert parse_date("(Ends on\nNovember 1, 2026)") == "2026-11-01"
    assert parse_date("9 novembre 2026") == "2026-11-09"
    assert parse_date("sometime soon") is None
    assert guess_language("Le projet vise la réfection du quai et des travaux dans la baie pour les pêcheurs") == "fr"


def test_timing_labels():
    today = date(2026, 10, 28)
    open_row = {"source": "iaac", "status_raw": "In progress", "comment_deadline": "2026-11-01"}
    t = compute(open_row, today)
    assert t["stage"] == "public comment open until 2026-11-01"
    assert t["days_to_deadline"] == 4 and "closing soon" in t["timing"] and not t["too_late"]
    late = {"source": "nl_ea", "status_raw": "Active", "comment_deadline": "2026-10-20",
            "decision_date": "2026-10-30"}
    t = compute(late, today)
    assert t["too_late"] and "likely too late" in t["timing"]
    assert compute({"status_raw": "Released"}, today)["stage"] == "decision made"


def test_robots_rules():
    text = """User-agent: Googlebot
Disallow:

User-agent: *
Crawl-delay: 5
Disallow: /050/*SrchPg=1*
Disallow: /arcgis/
"""
    r = Robots(text, "MarineProjectFinder")
    assert r.crawl_delay == 5
    assert r.allowed("/050/evaluations/proj/90821?culture=en-CA")
    assert not r.allowed("/050/evaluations/exploration?SrchPg=1&x=2")
    assert not r.allowed("/arcgis/rest/services/x")


def test_llm_quotes_are_verified():
    source = "Dredged rock would be reused to create an infill area which may support new fish habitat."
    kept, dropped = verify_quotes(["may support new fish habitat", "will require compensation"], source)
    assert kept == ["may support new fish habitat"] and dropped == 1
    # No verifiable quote: the summary is dropped, not shown.
    r = check_result({"summary_en": "Made-up claim.", "confidence": "high",
                      "evidence_quotes": ["not in the text at all"]}, source)
    assert r["summary_en"] is None and r["confidence"] == "unverified"
