"""Scoring fixes after the 2026-11-11 audit: Spanish structure words only with building work, sizes alone cannot
list a project, wharf rebuilds, berth/pontoon/mooring words, SARA only as the Canadian act, federal decision dates,
and the Scotland reader's optional PDF text."""
from pathlib import Path

import pytest

from mpf.config import load_scoring, load_settings
from mpf.http import Blocked
from mpf.score import score_text
from mpf.sources import iaac, scot_marine

R = load_scoring()


def _rules(text):
    return {r["rule"] for r in score_text(text, R)["reasons"]}


# Fix 1: Spanish "muelle", "espigón", "dique", "terminal" as an address or a company name do not count.
@pytest.mark.parametrize("text", [
    "Concesión para la ocupación de los locales situados en el Espigón Central de Fomento del puerto de Gijón.",
    "Sistema OPS para buques atracados en la séptima alineación del muelle de la Osa del puerto de El Musel.",
    "Concesión en la Zona de la Ampliación del Puerto de Bilbao, en el término municipal de Santurtzi.",
    "Licencia a la entidad Terminal de Contenedores de Tenerife, S.A.U. para la prestación del servicio.",
    "Centro de distribución de cemento junto al dique de abrigo del puerto de Palma.",
])
def test_spanish_structure_word_as_address_does_not_count(text):
    assert not _rules(text) & {"type:wharf_dock", "type:breakwater_jetty", "type:port_terminal"}


@pytest.mark.parametrize("text, rule", [
    ("Se construirá un nuevo muelle de 150 metros en el puerto.", "type:wharf_dock"),
    ("Obras de ampliación del muelle de Levante en el puerto de Málaga.", "type:wharf_dock"),
    ("Se prolongará el dique de abrigo existente.", "type:breakwater_jetty"),
    ("Proyecto de construcción de una nueva escollera en la playa.", "type:breakwater_jetty"),
    ("Se proyecta la ampliación del puerto hacia el este.", "type:port_terminal"),
    ("Construcción de una terminal de contenedores en la dársena sur.", "type:port_terminal"),
])
def test_spanish_building_work_still_counts(text, rule):
    assert rule in _rules(text)


# Fix 2: sizes alone cannot list a project.
def test_sizes_alone_are_capped_below_the_list():
    trail = ("The project includes the construction of a 400m trail, benches and a pavilion. Clearing and grubbing of "
             "16,600 m2 of land on the reserve along the English River.")
    s = score_text(trail, R)
    assert s["score"] <= 15 and "cap:size_only" in {r["rule"] for r in s["reasons"]}


def test_sizes_with_a_marine_type_are_not_capped():
    s = score_text("A new 250 m breakwater will be built in the harbour, with 12,000 m2 of seabed footprint.", R)
    assert s["score"] >= 35 and "cap:size_only" not in {r["rule"] for r in s["reasons"]}


def test_french_beach_recharge_with_article_counts():
    assert "type:shoreline_protection" in _rules("Recharge de la plage de la Grave, baie de Plaisance.")


# Fix 3: wharf rebuilds count a little more, and the wharf is not counted twice.
@pytest.mark.parametrize("text", [
    "Reconstruction of Wharf 402 - Mont-Louis Harbour. The wharf has exceeded its useful service life.",
    "Wharf Reconstruction at Hermitage SCH. The new wharf will be constructed of timber piles.",
    "Screening Opinion- Berth Reconstruction- Montrose Port. Type of application: Construction of works.",
    "Réfection du quai, fleuve Saint-Laurent, Sainte-Anne-de-Beaupré.",
])
def test_wharf_rebuild(text):
    s = score_text(text, R)
    rules = {r["rule"] for r in s["reasons"]}
    assert "type:wharf_rebuild" in rules and "type:wharf_dock" not in rules
    assert s["project_types"].count("Wharf, pier or large dock") == 1
    assert s["score"] >= 20


def test_plain_wharf_mention_is_unchanged():
    s = score_text("Floating dock relocation at the wharf in Belleoram harbour.", R)
    rules = {r["rule"] for r in s["reasons"]}
    assert "type:wharf_dock" in rules and "type:wharf_rebuild" not in rules


# Fix 4: pontoon and moorings are scoring words; moorings alone never list a project.
def test_pontoon_and_moorings():
    assert "type:wharf_dock" in _rules("Pontoons - Easdale Island Community Harbour, Oban.")
    s = score_text("Moorings - Achnacree Bay, Loch Etive. Marine licence application.", R)
    assert "type:moorings" in {r["rule"] for r in s["reasons"]} and s["score"] < 20


# Fix 11: SARA only as Canada's Species at Risk Act.
def test_sara_only_as_the_act():
    assert "habitat:species_at_risk" in _rules("Listed under the Species at Risk Act (SARA) as threatened.")
    assert "habitat:species_at_risk" in _rules("Two SARA-listed fish use the harbour.")
    assert "habitat:species_at_risk" not in _rules(
        "Convenio con la Autoridad Portuaria de Baleares para la aplicación SARA de registro electrónico.")
    assert "habitat:species_at_risk" not in _rules("Contact Sara Smith at the port office.")


# Fix 8: federal decision dates from the Key documents table.
KEY_DOCS = """<main><h1>Breakwater Rehabilitation at Fortune SCH, NL</h1>
<table id="keyDocumentsTable"><tbody>
<tr><td>2</td><td><a href="/d/2">Notice of Determination</a></td><td>PDF</td><td>2026-10-20</td></tr>
<tr><td>1</td><td><a href="/d/1">Notice of Intent to Make a Determination - Start of Public Comment Period</a></td><td>PDF</td><td>2026-09-18</td></tr>
</tbody></table></main>"""


def test_federal_decision_date_from_key_documents():
    assert iaac.parse_project_page(KEY_DOCS)["decision_date"] == "2026-10-20"


def test_no_decision_date_while_in_progress():
    html = KEY_DOCS.replace('<tr><td>2</td><td><a href="/d/2">Notice of Determination</a></td><td>PDF</td><td>2026-10-20</td></tr>', "")
    assert iaac.parse_project_page(html)["decision_date"] is None


def test_parks_canada_wording_counts():
    html = KEY_DOCS.replace("Notice of Determination", "Parks Canada has issued a notice of determination and decided "
                            "that the project is not likely to cause significant adverse environmental effects")
    assert iaac.parse_project_page(html)["decision_date"] == "2026-10-20"


# Scotland: the first attached PDF, only when switched on; stop on a refusal.
DOC_PAGE = """<h1>Marine Licence Application - Capital Dredge - Helmsdale harbour - 00011257</h1>
<table><tr><td><a href="https://marine.gov.scot/sites/default/files/printed_application_doc_redacted.pdf">Marine Licence Application</a></td></tr>
<tr><td><a href="/sites/default/files/gis_chart_224.pdf">Chart</a></td></tr></table>"""


def test_document_page_lists_attached_pdfs_in_order():
    pdfs = scot_marine.parse_document(DOC_PAGE)["pdfs"]
    assert pdfs == ["https://marine.gov.scot/sites/default/files/printed_application_doc_redacted.pdf",
                    "https://marine.gov.scot/sites/default/files/gis_chart_224.pdf"]


def test_scotland_pdf_reading_is_on_in_config():
    assert load_settings()["sources"]["scot_marine"]["fetch_pdf"] is True


class _Client:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def get(self, url, **kw):
        self.calls.append(url)
        raise self.error


def test_scotland_pdf_refusal_stops_and_too_large_skips():
    src = scot_marine.ScotMarineSource(_Client(Blocked("answered HTTP 403")), {}, {})
    assert src._pdf_text("https://marine.gov.scot/sites/default/files/a.pdf") is None
    assert src.log[-1].startswith("STOPPED")
    src = scot_marine.ScotMarineSource(_Client(Blocked("File larger than 5242880 bytes, skipped")), {}, {})
    assert src._pdf_text("https://marine.gov.scot/sites/default/files/b.pdf") == ""


def test_river_lock_quay_wall_is_not_listed():
    text = ("St. Andrews Lock & Dam Quay Wall Rehabilitation Project. Rehabilitation of the quay wall on the north side "
            "of the St. Andrews Lock and Dam, along the Red River in Lockport, Manitoba.")
    assert score_text(text, R)["score"] < 20
