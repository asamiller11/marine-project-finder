"""One test for every language rule in config/scoring.yaml (language_patterns).

Each sample is a short made-up sentence in that language. The test checks that the rule's
group is found, and that every language pattern in the config has a sample here.
"""
import pytest
import yaml

from mpf.config import ROOT
from mpf.extract import find_patterns, has_marine_context
from mpf.score import score_text

SAMPLES = {
    ("marine_context", "da"): "Arbejdet foregår i havnen ved Esbjerg.",
    ("marine_context", "no"): "Tiltaket ligger i fjorden ved Bergen.",
    ("marine_context", "nl"): "Het werk vindt plaats in de haven van Rotterdam.",
    ("marine_context", "es"): "Las obras se realizan en el puerto de Huelva.",
    ("port_terminal", "da"): "Projektet omfatter en havneudvidelse mod nord.",
    ("port_terminal", "no"): "Planen gjelder havneutbygging på østsiden.",
    ("port_terminal", "nl"): "Het plan betreft een havenuitbreiding in het westen.",
    ("port_terminal", "es"): "Se proyecta la ampliación del puerto hacia el este.",
    ("wharf_dock", "da"): "Den eksisterende kaj forlænges med 80 meter.",
    ("wharf_dock", "no"): "Den gamle kaia skal rives og bygges på nytt.",
    ("wharf_dock", "nl"): "De kademuur wordt over 200 meter vernieuwd.",
    ("wharf_dock", "es"): "Se construirá un nuevo muelle de 150 metros.",
    ("breakwater_jetty", "da"): "Molen forlænges for at beskytte indsejlingen.",
    ("breakwater_jetty", "no"): "Moloen skal forlenges med 50 meter.",
    ("breakwater_jetty", "nl"): "Er komt een nieuwe golfbreker bij de ingang.",
    ("breakwater_jetty", "es"): "Se prolongará el dique de abrigo existente.",
    ("dredging", "da"): "Der udføres opmudring af sejlrenden.",
    ("dredging", "no"): "Det planlegges mudring i innseilingen.",
    ("dredging", "nl"): "Het baggeren van de vaargeul start in maart.",
    ("dredging", "es"): "El proyecto incluye el dragado de la dársena.",
    ("shoreline_protection", "da"): "Der etableres kystbeskyttelse langs stranden.",
    ("shoreline_protection", "no"): "Det skal utføres erosjonssikring langs elvebredden.",
    ("shoreline_protection", "nl"): "De zandsuppletie beschermt de kust tegen erosie.",
    ("shoreline_protection", "es"): "Se ejecutará la regeneración de la playa norte.",
    ("marina_small_craft", "da"): "Lystbådehavnen udvides med 120 pladser.",
    ("marina_small_craft", "no"): "Småbåthavna får nye flytebrygger.",
    ("marina_small_craft", "nl"): "De jachthaven krijgt nieuwe steigers.",
    ("marina_small_craft", "es"): "Se amplía el puerto deportivo de la localidad.",
    ("infill_causeway", "da"): "Projektet kræver opfyldning af et havareal.",
    ("infill_causeway", "no"): "Tiltaket omfatter utfylling i sjø ved kaia.",
    ("infill_causeway", "nl"): "Voor de terminal is landaanwinning nodig.",
    ("infill_causeway", "es"): "Se prevé la ganancia de terrenos al mar en la zona sur.",
    ("offshore_wind", "da"): "Høringen gælder en ny havvindmøllepark i Nordsøen.",
    ("offshore_wind", "no"): "Søknaden gjelder havvind utenfor kysten.",
    ("offshore_wind", "nl"): "Het windpark op zee ligt 30 kilometer uit de kust.",
    ("offshore_wind", "es"): "Se tramita un parque eólico marino frente a la costa.",
    ("piles", "da"): "Fundamentet etableres ved nedramning af stålrør.",
    ("piles", "no"): "Kaia bygges på peler av stål.",
    ("piles", "nl"): "De steiger wordt gefundeerd op heipalen.",
    ("piles", "es"): "El muelle se apoya sobre pilotes de hormigón.",
    ("outfall_intake", "da"): "Projektet omfatter etablering af en ny udløbsledning.",
    ("outfall_intake", "no"): "Det planlegges utskifting av utslippsledning i fjorden.",
    ("outfall_intake", "nl"): "Het werk omvat de aanleg van een nieuwe lozingsleiding.",
    ("outfall_intake", "es"): "Se prevé la construcción de un nuevo emisario submarino.",
    ("offsetting", "da"): "Der fastsættes kompenserende foranstaltninger for tabt natur.",
    ("offsetting", "no"): "Det stilles krav om kompenserende tiltak.",
    ("offsetting", "nl"): "De natuurcompensatie vindt plaats op de oever.",
    ("offsetting", "es"): "Se establecen medidas compensatorias para el hábitat afectado.",
    ("habitat_creation", "da"): "Projektet omfatter et nyt stenrev ved kysten.",
    ("habitat_creation", "no"): "Det skal etableres et kunstig rev.",
    ("habitat_creation", "nl"): "Er wordt een natuurvriendelijke oever aangelegd.",
    ("habitat_creation", "es"): "Se instalará un arrecife artificial al sur del dique.",
    ("eelgrass", "da"): "Området rummer ålegræs i lavt vand.",
    ("eelgrass", "no"): "Det er registrert ålegras i bukta.",
    ("eelgrass", "nl"): "In het gebied groeit zeegras.",
    ("eelgrass", "es"): "La zona alberga praderas de posidonia.",
    ("fish_habitat", "da"): "Bugten er et vigtigt gydeområde for sild.",
    ("fish_habitat", "no"): "Fjorden er et gyteområde for torsk.",
    ("fish_habitat", "nl"): "Het estuarium is een paaigebied voor vis.",
    ("fish_habitat", "es"): "El estuario es zona de desove de varias especies.",
    ("shellfish_oyster", "da"): "Der findes en muslingebanke nær kajen.",
    ("shellfish_oyster", "no"): "Det finnes blåskjell langs moloen.",
    ("shellfish_oyster", "nl"): "Vlakbij ligt een mosselbank.",
    ("shellfish_oyster", "es"): "Existen bancos de moluscos en la ría.",
    ("intertidal_marsh", "da"): "Projektet ligger tæt på Vadehavet.",
    ("intertidal_marsh", "no"): "Arbeidet berører fjæresonen.",
    ("intertidal_marsh", "nl"): "De kwelder wordt niet aangetast.",
    ("intertidal_marsh", "es"): "La obra afecta a la zona intermareal.",
    ("species_at_risk", "da"): "Området er udpeget som Natura 2000-område.",
    ("species_at_risk", "no"): "Det er registrert rødlistede arter.",
    ("species_at_risk", "nl"): "Er komen beschermde soorten voor.",
    ("species_at_risk", "es"): "La zona forma parte de la Red Natura 2000.",
    ("marine_footprint", "da"): "Arbejdet berører havbunden i et mindre område.",
    ("marine_footprint", "no"): "Tiltaket berører sjøbunnen.",
    ("marine_footprint", "nl"): "De zeebodem wordt plaatselijk verstoord.",
    ("marine_footprint", "es"): "Las obras afectan al fondo marino.",
    ("scour_protection", "da"): "Der udlægges erosionsbeskyttelse ved fundamenterne.",
    ("scour_protection", "no"): "Det legges erosjonsbeskyttelse rundt fundamentene.",
    ("scour_protection", "nl"): "Rond de funderingen komt bodembescherming.",
    ("scour_protection", "es"): "Se colocará protección antisocavación en las cimentaciones.",
    ("mitigation_general", "da"): "Der beskrives afværgeforanstaltninger i rapporten.",
    ("mitigation_general", "no"): "Rapporten beskriver avbøtende tiltak.",
    ("mitigation_general", "nl"): "Er worden mitigerende maatregelen genomen.",
    ("mitigation_general", "es"): "Se aplicarán medidas correctoras durante las obras.",
}


def _config_rules():
    with open(ROOT / "config" / "scoring.yaml", encoding="utf-8") as f:
        lp = yaml.safe_load(f)["language_patterns"]
    keys = [("marine_context", lang) for lang in lp["marine_context"]]
    for group in ("project_types", "habitat_clues"):
        for name, per in lp[group].items():
            keys += [(name, lang) for lang in per]
    return keys


def test_every_language_rule_has_a_sample():
    assert set(_config_rules()) == set(SAMPLES), set(_config_rules()) ^ set(SAMPLES)


@pytest.mark.parametrize("rule,lang", sorted(SAMPLES))
def test_language_rule_matches_its_sample(rules, rule, lang):
    text = SAMPLES[(rule, lang)]
    if rule == "marine_context":
        assert has_marine_context(text, rules["marine_context"]), text
        return
    group = "project_types" if rule in rules["project_types"] else "habitat_clues"
    assert rule in find_patterns(text, {rule: rules[group][rule]}), text


def test_danish_farm_compensation_is_not_habitat_offsetting(rules):
    # A real Danish feed title about farm subsidies: "kompensation" here is money, not habitat.
    s = score_text("Bekendtgørelse om kompensation for udledningsbaseret markregulering i 2027", rules)
    assert "offsetting" not in s["facts"]["habitat_clues"]


def test_marine_words_inside_compound_words(rules):
    # Danish, Norwegian and Dutch join words together; the marine part must still count.
    for text in ("Ændring af kystbeskyttelsesloven og havneområder.", "Tiltak i havneområdet.",
                 "De kustversterking bij Petten."):
        assert has_marine_context(text, rules["marine_context"]), text


def test_spanish_marina_mercante_is_not_a_marina(rules):
    law = "Conforme al Texto Refundido de la Ley de Puertos del Estado y de la Marina Mercante, se somete a información pública."
    assert "marina_small_craft" not in score_text(law, rules)["facts"]["types"]
    assert "marina_small_craft" not in score_text("La zona forma parte de una reserva marina.", rules)["facts"]["types"]
    for text in ("The proposed marina will have 120 berths.", "Breakwater and Marina Construction at Bedford Bay",
                 "Expansion of the existing marina in the harbour."):
        assert "marina_small_craft" in score_text(text, rules)["facts"]["types"], text
