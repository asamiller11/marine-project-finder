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


def test_coastal_protection_and_outfall_are_recognised(rules):
    # Paraphrased from a real IAAC notice that the old rules scored 8.
    text = ("Stormwater Outfall Rehabilitation and Coastal Protection Project. The project stabilizes a major "
            "stormwater outfall along the shoreline of the St. Lawrence River, affected by coastal erosion. "
            "The aim is to protect the riverbank through durable coastal engineering measures. "
            "The overall project footprint is estimated at 1,080 m².")
    s = score_text(text, rules)
    assert {"shoreline_protection", "outfall_intake"} <= set(s["facts"]["types"])
    assert s["score"] >= 20


def test_french_coastal_protection_terms(rules):
    text = ("Travaux de stabilisation des berges et de protection contre l'érosion sur le littoral de la baie, "
            "avec un perré de 150 mètres et la réfection de l'émissaire pluvial.")
    s = score_text(text, rules)
    assert {"shoreline_protection", "outfall_intake"} <= set(s["facts"]["types"])
    assert s["score"] >= 20


def test_land_side_port_paving_stays_low(rules):
    # Paraphrased from a real notice: port terminal expansion with only grading, paving and drainage on land.
    text = ("Terminal Expansion. The Port is planning the expansion of a terminal. Work includes regrading an "
            "embankment, redevelopment of a drainage ditch, and grading and paving of a new storage area for "
            "general cargo and dry bulk materials.")
    assert score_text(text, rules)["score"] < 20


def test_existing_outfalls_mentioned_in_background_do_not_count(rules):
    # The sentence that wrongly tagged a real harbour project as outfall work.
    background = ("The sheltered inner harbour can be affected by land-based and harbour activities, "
                  "including drainage and wastewater outfalls.")
    assert "outfall_intake" not in score_text(background, rules)["facts"]["types"]
    for work in ("The sewer outfall will be relocated to the seaward side of the breakwater.",
                 "The project involves stabilizing and upgrading a major stormwater outfall.",
                 "Intake Structure Replacement at the generating station.",
                 "Réfection de l'émissaire pluvial dans la baie."):
        assert "outfall_intake" in score_text(work, rules)["facts"]["types"], work


def test_redirect_to_a_disallowed_path_is_blocked(tmp_path, monkeypatch):
    """A link that redirects into a path robots.txt disallows must be refused, not followed."""
    import pytest
    from mpf.http import Blocked, PoliteClient

    class FakeResp:
        def __init__(self, url, final, body=b"ok", status=200):
            self.url, self.status_code, self.headers, self._body = final, status, {}, body
        def iter_content(self, n):
            yield self._body
        def close(self):
            pass

    robots = {"https://a.example/robots.txt": b"User-agent: *\nDisallow:\n",
              "https://files.example/robots.txt": b"User-agent: *\nDisallow: /resources\n"}

    def fake_get(url, **kw):
        if url in robots:
            return FakeResp(url, url, robots[url])
        if url == "https://a.example/r/123":
            return FakeResp(url, "https://files.example/resources/data.gpkg")
        return FakeResp(url, url)

    client = PoliteClient({"http": {"user_agent": "MarineProjectFinder/0.1 (test)", "default_delay_seconds": 0,
                                    "never_fetch": []}}, tmp_path)
    monkeypatch.setattr(client.session, "get", fake_get)
    with pytest.raises(Blocked, match="robots.txt disallows"):
        client.get("https://a.example/r/123")
    assert client.get("https://a.example/page") == b"ok"


def test_distance_offshore_is_not_a_size():
    # Real title: "Morven North Offshore Wind Array - 61km off Aberdeenshire Coast"
    assert extract_sizes("Morven North Offshore Wind Array – 61km off Aberdeenshire Coast") == []


def test_habitat_restoration_licences_count_as_habitat_creation(rules):
    s = score_text("Marine Licence - Seagrass Planting - SE of Isle of Berneray, Sound of Harris, Western Isles", rules)
    assert {"habitat_creation", "eelgrass"} <= set(s["facts"]["habitat_clues"])
    assert s["score"] >= 20
