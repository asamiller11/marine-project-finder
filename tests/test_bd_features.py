"""Comment-period alerts, stakeholders and winnability scoring (owner's pilot feedback, 2026-11-19)."""
from datetime import date

from mpf import health, site, winnability
from mpf.config import load_scoring
from mpf.stakeholders import NOT_NAMED, find_stakeholders, parse_iaac_contacts, summary_line

TODAY = date(2026, 11, 19)
TIERS = {"strong_min": 60, "possible_min": 35, "weak_min": 20}


# --- B. Stakeholders --------------------------------------------------------------------------------------

def test_consultant_and_owner_from_prepared_by():
    row = {"source": "scot_marine", "proponent": "unknown", "full_text":
           "This report has been prepared by Cura Terrae Land and Nature Ltd (previously EnviroCentre Limited) on behalf "
           "of Peel Ports Group Ltd (PPG). PPG are proposing to deepen the berth."}
    st = find_stakeholders(row)
    assert [e["name"] for e in st["consultant"]] == ["Cura Terrae Land and Nature Ltd (previously EnviroCentre Limited)"]
    assert st["owner"][0]["name"] == "Peel Ports Group Ltd (PPG)"
    assert st["contractor"] == [] and summary_line(st["contractor"]) == NOT_NAMED


def test_licence_agent_is_the_consultant_but_as_per_licensee_is_not():
    a = find_stakeholders({"source": "scot_marine", "full_text": "PART 2 – PARTICULARS 2.1 Agent AECOM Limited 2nd Floor 177 Bothwell Street"})
    assert a["consultant"][0]["name"] == "AECOM Limited"
    b = find_stakeholders({"source": "scot_marine", "full_text": "PART 2 – PARTICULARS 2.1 Agent As per Licensee 2.2 Location"})
    assert b["consultant"] == []


def test_nothing_is_guessed_from_vague_words():
    st = find_stakeholders({"source": "iaac", "proponent": "Parks Canada", "extra": {}, "full_text":
                            "The work will be completed by a civilian contractor. On behalf of the Dee District Salmon "
                            "Fishery Board we welcome the opportunity to comment. Work with Consultant to obtain samples."},
                           {"contacts": []})
    assert st["contractor"] == [] and st["consultant"] == []
    assert [e["name"] for e in st["owner"]] == ["Parks Canada"]


def test_named_contractor_is_recorded():
    st = find_stakeholders({"source": "nl_ea", "full_text": "The contract was awarded to Pennecon Heavy Civil Limited in March."})
    assert st["contractor"][0]["name"] == "Pennecon Heavy Civil Limited"


def test_agency_contact_keeps_name_and_office_but_no_phone_or_email():
    text = ("FOR FURTHER INFORMATION CONTACT: Kate Fleming, Office of Protected Resources, NMFS, (301) 427-8401. "
            "Written comments must be submitted to: Dion Upward Acting Regional Engineer, Small Craft Harbours, "
            "Newfoundland and Labrador Region 80 East White Hills Road Email: dion.upward@dfo-mpo.gc.ca")
    st = find_stakeholders({"source": "fed_register", "full_text": text})
    names = [e["name"] for e in st["agency_contact"]]
    assert names == ["Kate Fleming", "Dion Upward"]
    for e in st["agency_contact"]:
        assert "@" not in e["quote"] and "427-8401" not in e["quote"] and "@" not in e["detail"]


def test_iaac_contacts_box():
    html = ('<address id="contact-info">Public Services and Procurement Canada<br />Carolle Gosselin, Senior '
            'Environmental Specialist<br />Telephone: 418-931-5384<br />Email: <a href="mailto:x@y.ca">x@y.ca</a><br /></address>')
    assert parse_iaac_contacts(html) == [{"organization": "Public Services and Procurement Canada",
                                          "name": "Carolle Gosselin", "title": "Senior Environmental Specialist"}]
    office = ('<address id="contact-info">Department of National Defence<br />CFB Halifax, Public Affairs<br />'
              'PO Box 99000, Station Forces<br />Halifax NS B3K 5X5</address>')
    c = parse_iaac_contacts(office)[0]
    assert c["name"] == "" and c["organization"] == "Department of National Defence, CFB Halifax, Public Affairs"


# --- C. Winnability ---------------------------------------------------------------------------------------

def _assess(text, kind, stakeholders=None, sizes=None, extra=None, source="iaac", deadline=None, title="X"):
    row = {"source": source, "full_text": text, "title": title, "comment_deadline": deadline, "language": "en"}
    return winnability.assess(row, kind, stakeholders or {}, load_scoring(), TODAY, extra or {}, sizes)


def test_earlier_stages_score_higher():
    pts = {k: _assess("A wharf.", k)["points"] for k in ("open", "assessment", "closed", "decided")}
    assert pts["open"] > pts["assessment"] > pts["closed"] > 0 > pts["decided"]
    assert _assess("A wharf. Construction has begun.", "assessment")["stage"] == "under_construction"
    assert _assess("Screening opinion", "assessment", title="Screening Opinion- Berth Reconstruction")["stage"] == "pre_application"


def test_offsetting_firm_start_and_size_add_points_with_reasons():
    w = _assess("A fish habitat offsetting plan is proposed. Construction is expected to begin in early 2027.",
                "assessment", {"consultant": [{"name": "AECOM Limited"}]},
                sizes=[{"kind": "length_m", "value": 185, "matched": "185 m", "quote": "The breakwater is 185 m long."}])
    texts = " | ".join(t for _, t in w["reasons"])
    assert "offsetting" in texts.lower() and "AECOM Limited" in texts and "early 2027" in texts and "185 m" in texts
    assert w["points"] == 25           # 8 + 8 + 6 + 6 + 4 = 32, capped at max_bonus
    assert "engineering firm (AECOM Limited)" in w["entry_point"]


def test_sizes_about_roads_pipes_or_distances_do_not_count():
    for q in ("Approximately 1.4 kilometres of new access road will be required.",
              "The minimum distance between the dredge areas and protected areas is 2 km.",
              "A 230-metre extension to the effluent pipeline, seaward of the breakwater."):
        w = _assess("A wharf.", "assessment", sizes=[{"kind": "length_m", "value": 2000, "matched": "x", "quote": q}])
        assert not any("Large" in t for _, t in w["reasons"])


def test_noaa_window_started_means_under_construction():
    w = _assess("Pile driving.", "decided", source="noaa_ita", extra={"effective": "9/1/2026 - 8/31/2027"})
    assert w["stage"] == "under_construction" and w["points"] < 0


def test_open_entry_point_names_the_date():
    w = _assess("A wharf.", "open", {"owner": [{"name": "Parks Canada"}]}, deadline="2026-11-28")
    assert w["entry_point"].startswith("Comment period open until November 28, 2026: submit a comment or contact "
                                       "the proponent (Parks Canada)")


def test_winnability_never_changes_who_is_listed():
    assert winnability.adjusted(25, -15, TIERS) == 20           # listed stays at least Weak
    assert winnability.adjusted(15, 25, TIERS) == 15            # not lifted onto the list
    assert winnability.adjusted(50, 25, TIERS) == 75
    assert winnability.adjusted(50, 25, TIERS, strong_cap=True) == 59


def test_plain_reasons_are_two_or_three_without_repeats():
    w = {"reasons": [(8, "Habitat offsetting or compensation mentioned (habitat work is likely needed)"),
                     (10, "Early: public comment period open"), (0, "")]}
    r = winnability.plain_reasons("Habitat offsetting or compensation mentioned", w)
    assert r == ["Habitat offsetting or compensation mentioned", "Early: public comment period open (+10)"]


# --- A. Comment-period alerts -----------------------------------------------------------------------------

def _rec(i, deadline, kind="open", tier="possible", **kw):
    r = {"id": f"iaac:{i}", "title": f"Project {i}", "url": f"https://iaac-aeic.gc.ca/{i}", "page": f"p/iaac-{i}.html",
         "source": "iaac", "source_name": "Registry", "agency": "", "attribution": "", "issued_by": "",
         "comment_deadline": deadline, "stage_kind": kind, "tier": tier, "tier_label": tier.title(),
         "profiles": {}, "win": {"entry_point": "Comment period open: contact the proponent."}, "province": "NS",
         "country": "CA", "types": [], "summary": ""}
    r.update(kw)
    return r


def test_open_for_comment_label_only_while_open():
    assert "Open for comment until November 27, 2026" in site._open_until_badge(_rec(1, "2026-11-27"))
    assert site._open_until_badge(_rec(1, "2026-11-01", kind="closed")) == ""
    assert site._open_until_badge(_rec(1, None)) == ""


def test_closing_soon_is_14_days_soonest_first():
    recs = [_rec(1, "2026-12-01"), _rec(2, "2026-11-20"), _rec(3, "2026-12-20"), _rec(4, "2026-11-25", tier="notrel"),
            _rec(5, "2026-11-10", kind="closed")]
    assert [r["id"] for r in site.closing_soon(recs, 14, TODAY)] == ["iaac:2", "iaac:1"]


def test_feeds_list_closing_comment_periods_first(tmp_path):
    recs = [_rec(1, "2026-11-25", changes=["new"], first_listed="2026-11-19")]
    site.write_feeds(tmp_path, recs, [], {"title": "MitiMark", "url": "https://example.org/", "closing_soon_days": 14}, TODAY)
    xml = (tmp_path / "feeds" / "comment-periods.xml").read_text()
    assert "Comments close November 25, 2026: Project 1" in xml and "iaac:1#comments-2026-11-25" in xml
    allxml = (tmp_path / "feeds" / "all.xml").read_text()
    assert allxml.index("Comments close") < allxml.index("<title>Project 1</title>")


def test_csv_has_stakeholder_and_entry_point_columns():
    rec = _rec(1, "2026-11-25", province="NS", proponent="Parks Canada", types=["Wharf"], stage="x", score=40,
               reviewed=False, comments={"closes": "2026-11-25"}, stakeholders={"owner": [], "consultant": [{"name": "AECOM Limited", "detail": ""}],
                                                                                 "contractor": [], "agency_contact": []})
    rec["win"]["reasons"] = ["Wharf", "Early: public comment period open (+10)"]
    row = dict(zip(site.CSV_COLUMNS, site.csv_row(rec, {"CA": "Canada"}, {})))
    assert row["owner_or_proponent"] == "Parks Canada" and row["consultant"] == "AECOM Limited"
    assert row["contractor"] == NOT_NAMED and row["open_for_comment_until"] == "2026-11-25"
    assert row["entry_point"].startswith("Comment period open") and "Wharf" in row["score_reasons"]


# --- Health check -----------------------------------------------------------------------------------------

def test_health_checks_new_fields():
    good = [{"title": "A", "stage_kind": "open", "comment_deadline": "2026-11-25", "win": {"entry_point": "x"},
             "stakeholders": {"owner": [{"name": "P", "how": "Proponent named by the registry"}], "consultant": [],
                              "contractor": [], "agency_contact": []}}]
    res = health.check_new_fields(good, {"last_successful_update": "2026-11-19"})
    assert all(ok for ok, _ in res)
    bad = [dict(good[0], comment_deadline="2026-11-01", stakeholders={"owner": []}, win={})]
    res = health.check_new_fields(bad, {"last_successful_update": "2026-11-19"})
    assert sum(not ok for ok, _ in res) == 3


# --- Round 2 (2026-11-19): offshore wind, approval caps, research ------------------------------------------

def _hs(text, title="X"):
    from mpf.profiles import ProfileScorer
    return ProfileScorer(load_scoring()).score(text, title, "hard-structure", TIERS, "en")


def test_offshore_wind_counts_under_hard_structure_fixed_more_than_floating():
    plain = _hs("Morven North Offshore Wind Array, 61km off Aberdeenshire coast.")["score"]
    fixed = _hs("Offshore wind farm with monopile foundations on the seabed off the coast.")["score"]
    floating = _hs("Floating offshore wind farm with semi-submersible platforms off the coast.")["score"]
    assert plain > 0 and fixed > plain > floating > 0


def test_approved_capped_at_possible_and_under_construction_at_weak():
    assert winnability.adjusted(79, -4, TIERS, cap_tier="possible") == 59
    assert winnability.adjusted(70, 0, TIERS, cap_tier="weak") == 34
    w = _assess("A wharf.", "decided")
    assert w["cap_tier"] == "possible"


def test_scotland_approval_letters_and_decision_notices_are_decided():
    from mpf.sources.scot_marine import stage_words
    assert stage_words("", "Approval Letter - Green Volt Offshore Windfarm") == "decision made"
    assert stage_words("", "Electricity Works Decision Notice - Muir Mhor") == "decision made"


def test_research_files_follow_the_rules():
    from mpf import research
    p, o = research.load_research()
    assert research.problems(p, o) == []
    bad = {"x": {"owner": [{"name": "A Ltd", "source": "https://www.linkedin.com/in/someone"}],
                 "facts": [{"kind": "procurement", "text": "t"}], "people": [{"name": "B", "source": "https://a.gov"}]}}
    msgs = research.problems(bad, {})
    assert any("personal-data" in m for m in msgs) and any("no source" in m for m in msgs) and any("no role" in m for m in msgs)


def test_find_contacts_is_a_linkedin_people_search_only():
    from mpf import research
    url = research.find_contacts_url("Thistle Wind Partners")
    assert url.startswith("https://www.linkedin.com/search/results/people/?keywords=")
    assert "Thistle+Wind+Partners" in url and "procurement" in url


def test_research_merges_into_card_with_procurement_first():
    rec = {"stakeholders": {"owner": [], "consultant": [], "contractor": [], "agency_contact": []}}
    proj = {"owner": [{"name": "Marine Atlantic Inc.", "source": "https://www.marineatlantic.ca/x", "checked": "read"}],
            "facts": [{"kind": "biodiversity", "text": "Habitat creation.", "source": "https://m.ca/a", "checked": "read"},
                      {"kind": "procurement", "text": "RFP on CanadaBuys.", "source": "https://c.ca/t", "checked": "search"}]}
    site.add_research(rec, proj, {})
    assert rec["stakeholders"]["owner"][0]["source"] == "https://www.marineatlantic.ca/x"
    assert rec["research"]["facts"][0]["kind"] == "procurement"
    html = site._research_html(rec)
    assert html.index("How to get in") < html.index("Habitat creation") and "open the link to confirm" in html
    assert "Find contacts" in site._stakeholder_rows(rec)


def test_developer_only_projects_are_scored_and_labelled():
    rows = site.developer_rows({"developer:x": {"developer_only": True, "title": "New breakwater, Test Harbour",
                                                "country": "GB", "url": "https://dev.example/x", "stage": "pre-application",
                                                "full_text": "A new 200 m rock breakwater at Test Harbour on the coast."}},
                               load_scoring(), TODAY)
    assert rows and rows[0]["score"] >= 20 and rows[0]["source"] == "developer"


def test_weekly_check_queues_new_strong_and_possible_projects(tmp_path, monkeypatch):
    from mpf import research
    monkeypatch.setattr(research, "QUEUE_FILE", tmp_path / "queue.md")
    monkeypatch.setattr(research, "load_research", lambda: ({}, {}))
    monkeypatch.setattr(research, "_state_path", lambda s: tmp_path / "state.json")
    recs = [{"id": "iaac:1", "title": "New wharf", "url": "https://x", "tier": "possible", "tier_label": "Possible"},
            {"id": "iaac:2", "title": "Weak one", "url": "https://y", "tier": "weak", "profiles": {}}]
    r = research.weekly_check({}, recs, client=object(), today=TODAY, log=lambda *a: None)
    q = (tmp_path / "queue.md").read_text()
    assert r["queued"] == 1 and "New wharf" in q and "Weak one" not in q
