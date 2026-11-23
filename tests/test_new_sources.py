"""Sources added after the written permissions granted in reply to the 2026-11-17 requests (owner, 2026-11-23)."""
from pathlib import Path

from mpf.config import load_settings
from mpf.sources import CHECK_URLS, LIVE_SOURCES, SOURCES
from mpf.sources.ns_ea import parse_project_list, parse_project_page

SAMPLES = Path(__file__).parent / "samples"


def test_ns_ea_list_reads_both_tables():
    rows = parse_project_list((SAMPLES / "ns_ea_projects.html").read_text(encoding="utf-8"))
    assert len(rows) > 200
    first = rows[0]
    assert first["title"] == "West Bay Wind Project" and first["proponent"] == "West Bay Wind Inc"
    assert first["registered"] == "2026-11-12" and first["under_review"] is True
    assert first["url"] == "https://novascotia.ca/nse/ea/west-bay-wind/"
    assert any(not r["under_review"] for r in rows)                  # completed reviews table too
    assert all(r["url"] != "https://novascotia.ca/nse/ea/PAGE" for r in rows)   # commented-out template row


def test_ns_ea_project_page_dates_and_facts():
    p = parse_project_page((SAMPLES / "ns_ea_west_bay.html").read_text(encoding="utf-8"))
    assert p["proponent"] == "West Bay Wind Inc" and p["location"] == "Cumberland County, Nova Scotia"
    assert p["status"] == "Under Review" and p["class"] == "I"
    assert p["comment_deadline"] == "2026-12-22" and p["decision_due"] == "2027-01-11" and p["decided"] is None
    assert p["description"].startswith("This is to advise that on 12 November 2026")


def test_permission_sources_are_registered_with_a_permission_record():
    settings = load_settings()
    for name in ("ns_ea", "ceqanet", "qc_ree", "bc_eao", "iaac"):
        assert name in SOURCES and name in LIVE_SOURCES and name in CHECK_URLS
        assert settings["sources"][name]["enabled"] is True
        perm = settings["sources"][name]["permission"]
        assert perm["basis"] == "a" and perm["from"]


def test_ceqanet_search_rows_and_water_titles():
    from mpf.sources.ceqanet import WATER_WORDS, parse_results
    rows = parse_results((SAMPLES / "ceqanet_coastal_search.html").read_text(encoding="utf-8"))
    berth = [r for r in rows if r["sch"] == "2021110263"][0]
    assert berth["url"] == "https://ceqanet.lci.ca.gov/2021110263/5" and berth["received"] == "2026-11-18"
    assert berth["agency"] == "Port of Los Angeles" and berth["type"] == "EIR"
    recent = parse_results((SAMPLES / "ceqanet_recent.html").read_text(encoding="utf-8"))
    picked = [r["title"] for r in recent if WATER_WORDS.search(r["title"])]
    assert len(recent) == 100 and "Buck's Landing Dock Replacement Project" in picked
    assert "Twin Lakes Food Bank Facility" not in picked


def test_ceqanet_document_never_keeps_contact_people():
    from mpf.sources.ceqanet import parse_document
    d = parse_document((SAMPLES / "ceqanet_doc_berths.html").read_text(encoding="utf-8"))
    f = d["fields"]
    assert f["Lead Agency"] == "Port of Los Angeles" and f["Waterways"] == "Port of Los Angeles Main Channel"
    assert d["review_end"] == "2027-01-04" and d["lat"] == 33.75594 and d["lon"] == -118.27242
    assert not {"Email", "Phone", "Name", "Job Title"} & set(f)
    assert "@" not in " ".join(f.values())


def test_qc_ree_results_and_project_page():
    from mpf.sources.qc_ree import parse_project, parse_results
    rows = parse_results((SAMPLES / "qc_ree_ports.html").read_bytes().decode("utf-8", "replace"))
    bc = [r for r in rows if r["id"] == "3211-04-071"][0]
    assert bc["title"] == "Projet de réaménagement du terminal n°5 au port de Baie-Comeau"
    assert bc["initiator"].startswith("Corporation de gestion du port de Baie-Comeau") and bc["changed"] == "2026-11-10"
    assert bc["url"] == "https://www.ree.environnement.gouv.qc.ca/projet.asp?no_dossier=3211-04-071"
    p = parse_project((SAMPLES / "qc_ree_projet.html").read_bytes().decode("utf-8", "replace"))
    assert p["opened"] == "2023-05-09" and p["region"] == "Côte-Nord" and p["municipality"] == "Baie-Comeau"
    assert p["activity"] == "Port, quai et terminal portuaire" and p["procedure"] == "Québec méridional"
    assert p["documents"][0].startswith("PR1.1") and p["decided"] is False and p["hearing"] is True


def test_post_form_keeps_the_polite_rules(tmp_path, monkeypatch):
    """A search form sent by POST goes through robots.txt, the cache and the stop on 403 like any page."""
    from mpf.http import Blocked, PoliteClient
    import pytest
    c = PoliteClient(load_settings(), tmp_path)
    calls = []

    class R:
        def __init__(self, status, body=b"ok"):
            self.status_code, self._b, self.url, self.headers = status, body, "https://x.example/s", {}
        def iter_content(self, n):
            yield self._b
        def close(self):
            pass

    monkeypatch.setattr(c, "_robots_for", lambda url: type("Rb", (), {"allowed": lambda self, p: True, "crawl_delay": None})())
    monkeypatch.setattr(c, "_wait", lambda host, delay: None)
    monkeypatch.setattr(c.session, "post", lambda url, data=None, **k: calls.append(data) or R(200, b"results"))
    assert c.post_form("https://x.example/s", {"a": "1"}) == b"results"
    assert c.post_form("https://x.example/s", {"a": "1"}) == b"results" and len(calls) == 1     # cached
    c.post_form("https://x.example/s", {"a": "2"})
    assert len(calls) == 2                                                                  # other fields, other cache
    monkeypatch.setattr(c.session, "post", lambda url, data=None, **k: R(403))
    with pytest.raises(Blocked):
        c.post_form("https://x.example/s", {"a": "3"})


def test_health_accepts_owner_reported_permission_with_a_reminder(tmp_path):
    from mpf.health import check_permissions_and_site
    base = {"basis": "a", "from": "An office"}
    conf = {"sources": {
        "filed": {"enabled": True, "permission": dict(base, date="2026-11-19", quote="Yes.")},
        "reported": {"enabled": True, "permission": dict(base, date="", quote="", reported_by_owner="2026-11-23")},
        "missing": {"enabled": True, "permission": dict(base, date="", quote="")}}}
    res = {m.split(":")[0]: (ok, m) for ok, m in check_permissions_and_site(conf, tmp_path) if ":" in m}
    assert res["filed"][0] and "complete" in res["filed"][1]
    assert res["reported"][0] and "reminder" in res["reported"][1]
    assert not res["missing"][0]
    settings = load_settings()
    for name in ("iaac", "bc_eao", "ns_ea", "ceqanet", "qc_ree"):
        assert settings["sources"][name]["permission"]["reported_by_owner"] == "2026-11-23"
