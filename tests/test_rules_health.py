"""Phase A (2026-11-17): the source rule (robots.txt exceptions, hard stop on 401/403/429), decision wording,
NL decision letters, the health check and the RSS feeds."""
import json
import re
from datetime import date, datetime

import pytest

from mpf import health, site
from mpf.cli import robots_exception_line
from mpf.http import Blocked, PoliteClient, valid_exception
from mpf.sources.nl_ea import parse_project_page

RULE = ("A source whose robots.txt disallows automated reading may be read only if (a) the site owner has given "
        "written permission, or (b) its official terms or open-data licence explicitly invite automated access "
        "(for example a documented API or bulk download page). Never work around a 401, 403 or 429 or any block.")


class FakeResp:
    def __init__(self, url, status=200, body=b"ok"):
        self.url, self.status_code, self._body, self.headers = url, status, body, {}

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


def _client(tmp_path, monkeypatch, statuses=None):
    robots = b"User-agent: *\nDisallow: /data/\n"
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return FakeResp(url, 200, robots)
        return FakeResp(url, (statuses or {}).get(url, 200))

    c = PoliteClient({"http": {"user_agent": "MarineProjectFinder/0.1 (test)", "default_delay_seconds": 0,
                               "never_fetch": []}}, tmp_path)
    monkeypatch.setattr(c.session, "get", fake_get)
    return c, calls


EXC = {"basis": "b", "hosts": ["a.example"], "evidence": "https://a.example/api-terms, checked 2026-11-17"}


def test_rule_is_written_word_for_word_in_the_docs():
    from mpf.config import ROOT
    flat = lambda t: re.sub(r"\s+", " ", t)
    for f in ("README.md", "docs/source_rules.md"):
        assert RULE in flat((ROOT / f).read_text(encoding="utf-8")), f
    doc = flat((ROOT / "mpf" / "http.py").read_text(encoding="utf-8"))
    assert RULE.replace("(a) the site", "(a) the site") in doc.replace("  ", " ")


def test_disallowed_page_is_refused_without_an_exception(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    c.current_source = "src"
    with pytest.raises(Blocked, match="robots.txt disallows"):
        c.get("https://a.example/data/1")
    assert c.get("https://a.example/page") == b"ok"


def test_disallowed_page_is_read_with_exception_a_or_b_and_logged(tmp_path, monkeypatch):
    for basis in ("a", "b"):
        c, _ = _client(tmp_path / basis, monkeypatch)
        c.set_exceptions({"src": {"robots_exception": dict(EXC, basis=basis)}})
        c.current_source = "src"
        assert c.get("https://a.example/data/1") == b"ok"
        assert c.exceptions_used == {"src": 1}
        line = robots_exception_line(c)
        assert "src (1 requests; exception (" + basis + ")" in line and "api-terms" in line


def test_exception_only_covers_its_own_source_and_hosts(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    c.set_exceptions({"src": {"robots_exception": EXC}})
    c.current_source = "other"
    with pytest.raises(Blocked):
        c.get("https://a.example/data/1")
    c.current_source = "src"
    with pytest.raises(Blocked):
        c.get("https://b.example/data/1")
    assert robots_exception_line(c) == "Robots.txt exceptions used this run: none."


@pytest.mark.parametrize("bad", [{"basis": "c", "hosts": ["a.example"], "evidence": "x"},
                                 {"basis": "b", "hosts": [], "evidence": "x"},
                                 {"basis": "a", "hosts": ["a.example"], "evidence": ""}, "yes"])
def test_incomplete_exception_counts_as_none(tmp_path, monkeypatch, bad):
    assert valid_exception(bad) is None
    c, _ = _client(tmp_path, monkeypatch)
    notes = c.set_exceptions({"src": {"robots_exception": bad}})
    assert notes and "robots.txt applies in full" in notes[0]
    c.current_source = "src"
    with pytest.raises(Blocked):
        c.get("https://a.example/data/1")


@pytest.mark.parametrize("status", [401, 403, 429])
def test_401_403_429_always_stop_even_with_an_exception(tmp_path, monkeypatch, status):
    c, calls = _client(tmp_path, monkeypatch, {"https://a.example/data/1": status})
    c.set_exceptions({"src": {"robots_exception": EXC}})
    c.current_source = "src"
    with pytest.raises(Blocked, match=f"HTTP {status}"):
        c.get("https://a.example/data/1")
    n = len(calls)
    with pytest.raises(Blocked, match="refused us earlier"):
        c.get("https://a.example/page")            # an allowed page: still not asked
    assert len(calls) == n                         # no request was sent
    assert "a.example" in c.refused_hosts


def test_429_on_robots_txt_stops_the_site(tmp_path, monkeypatch):
    def fake_get(url, **kw):
        return FakeResp(url, 429)
    c = PoliteClient({"http": {"user_agent": "T/1", "default_delay_seconds": 0, "never_fetch": []}}, tmp_path)
    monkeypatch.setattr(c.session, "get", fake_get)
    with pytest.raises(Blocked):
        c.get("https://z.example/page")
    assert "z.example" in c.refused_hosts


def test_only_the_netherlands_source_has_an_exception_and_it_is_off():
    from mpf.config import load_settings
    with_exc = {n: c for n, c in load_settings()["sources"].items() if "robots_exception" in (c or {})}
    assert list(with_exc) == ["nl_sru"] and not with_exc["nl_sru"]["enabled"]
    assert valid_exception(with_exc["nl_sru"]["robots_exception"])["basis"] == "b"


# --- A2: decision wording ---------------------------------------------------------------------------------

def _timeline(rec):
    return site._timeline_html(dict({"timeline": [], "comments": {}}, **rec))


def test_due_date_says_decision_due():
    # Port aux Basques on 2026-11-14: Active, decision due 2026-11-13 (extended), no decision posted.
    html = _timeline({"decision_date": "2026-11-13", "stage_kind": "assessment", "approved_on": None})
    assert "Decision due (official notice)" in html and "Decided" not in html


def test_posted_decision_says_decided():
    html = _timeline({"decision_date": "2026-10-14", "stage_kind": "decided", "approved_on": "2026-10-14"})
    assert "Decided (official notice)" in html and "Decision due" not in html


# --- A3: NL decision letters ------------------------------------------------------------------------------

PAGE = """<article><h1>Crémaillère Harbour Marine Port Development Resubmission</h1><div class="entry-content">
<h3>Proponent: Town of St. Anthony</h3><p>Project Description: An industrial subdivision and deepwater marine port.</p>
<ul><li>October 14, 2026 – Project released.</li></ul>
<p><a href="https://www.gov.nl.ca/eccc/files/x-Registration-Document-.pdf">Registration</a>
<a href="https://www.gov.nl.ca/eccc/files/x-Minister-s-Decision-Letter-Release.pdf">Decision Letter</a>
<a href="https://www.gov.nl.ca/eccc/files/x-Attachment-A-Conditions-of-Release.pdf">Attachment A – Conditions of Release</a>
<a href="https://www.gov.nl.ca/eccc/files/x-EPR-Guidelines.pdf">Decision Letter – EPR Guidelines</a>
<a href="https://www.gov.nl.ca/eccc/files/EA-Bulletin-2026-10-16.pdf">Project Released</a></p></div></article>"""


def test_decision_letter_and_conditions_are_found_but_not_guidelines():
    docs = parse_project_page(PAGE)["decision_docs"]
    assert [d["title"] for d in docs] == ["Decision Letter", "Attachment A – Conditions of Release"]


def test_decision_documents_are_capped_and_only_for_listed_marine_projects(tmp_path, monkeypatch):
    from mpf.config import load_settings
    from mpf.sources import nl_ea
    s = load_settings()
    c = PoliteClient({"http": {"user_agent": "T/1", "default_delay_seconds": 0, "never_fetch": []}}, tmp_path)
    fetched = []

    def fake_get(url, **kw):
        if url not in fetched:
            fetched.append(url)
            c.network_requests += 1
        return b"%PDF"
    monkeypatch.setattr(c, "get", fake_get)
    monkeypatch.setattr(c, "cached_path", lambda url: tmp_path if url in fetched else None)
    monkeypatch.setattr(nl_ea, "pdf_text", lambda data, max_pages=15: "The Proponent shall build the breakwater.")
    quarry = PAGE.replace("Crémaillère Harbour Marine Port Development Resubmission", "Blue Cove Quarry").replace(
        "An industrial subdivision and deepwater marine port.", "A quarry near the cove.")
    pages = [({"reg": str(n), "registered": f"2026-{n % 12 + 1:02d}-01"}, PAGE.replace("files/x-", f"files/{n}-"))
             for n in range(20)]
    pages.append(({"reg": "Q", "registered": "2026-12-31"}, quarry))
    src = nl_ea.NLEASource(c, s, dict(s["sources"]["nl_ea"], max_decision_doc_requests=5))
    out = src._decision_texts(pages)
    assert len(fetched) == 5                        # the cap
    assert "Q" not in out                           # a quarry is not marine construction: no letter read
    assert "left for the next run" in src.log[-1]


# --- A4: health check -------------------------------------------------------------------------------------

def _hs(tmp_path):
    return {"data_dir": str(tmp_path), "update": {"days_between_updates": 7},
            "health": {"canaries": ["Port aux Basques", "Point Rousse", "Kings Bay"]},
            "site": {"url": "https://example.test/"}}


def test_schedule_check():
    now = datetime(2026, 11, 20, 9)
    assert health.check_schedule({"last_success": "2026-11-14T18:30:00"}, now, 7, 1, False)[0]
    ok, msg = health.check_schedule({"last_success": "2026-11-01T18:30:00"}, now, 7, 1, True)
    assert not ok and "paused" in msg
    assert not health.check_schedule({}, now, 7, 1, False)[0]


def test_source_counts_zero_error_and_drop():
    run = lambda **kw: {"sources": {k: {"items": v, "new": 0, "problem": False} for k, v in kw.items()}}
    runs = [run(nl_ea=83, scot_marine=110, dk_hoering=219)] * 3
    last = run(nl_ea=0, scot_marine=40, dk_hoering=219)
    last["sources"]["dk_hoering"]["problem"] = True
    res = dict((m.split(":")[0], ok) for ok, m in health.check_sources(runs + [last]))
    assert res == {"nl_ea": False, "scot_marine": False, "dk_hoering": False}
    assert all(ok for ok, _ in health.check_sources(runs + [run(nl_ea=84, scot_marine=108, dk_hoering=220)]))


def test_run_health_notifies_on_problems_and_passes_when_all_is_well(tmp_path):
    s = _hs(tmp_path)
    from mpf import update
    update.write_state(s, {"last_success": "2026-11-14T18:30:00"})
    for _ in range(3):
        health.record_ingest_run(s, {"nl_ea": {"items": 83, "new": 0, "problem": False}}, {}, 5)
    roles = {"owner": [{"name": "Marine Atlantic Inc.", "how": "Proponent named by the registry"}], "consultant": [],
             "contractor": [], "agency_contact": []}
    win = {"entry_point": "Design stage: contact the proponent.", "reasons": ["Breakwater"]}
    live = [{"title": t, "stakeholders": roles, "win": win} for t in
            ("Port aux Basques Navigation Improvement Project", "Point Rousse Port Expansion",
             "U.S. Navy's TRF Wharf Interface Repairs Project at U.S. Naval Submarine Base Kings Bay")]
    (tmp_path / "site" / "feeds").mkdir(parents=True)
    (tmp_path / "site" / "feeds" / "comment-periods.xml").write_text('<rss version="2.0"><channel></channel></rss>')
    (tmp_path / "site" / "logo.svg").write_text("<svg/>")
    s["site_dir"] = str(tmp_path / "site")
    fetch = lambda url: {"last_successful_update": "2026-11-14"} if url.endswith("status.json") else live
    sent = []
    r = health.run_health(s, datetime(2026, 11, 17, 9), fetch=fetch, notify_fn=lambda t, m: sent.append(m))
    assert r["ok"] and not sent
    r = health.run_health(s, datetime(2026, 11, 17, 9), fetch=lambda url: live[1:] if "items" in url else
                          {"last_successful_update": "2026-11-07"}, notify_fn=lambda t, m: sent.append(m))
    assert not r["ok"] and len(sent) == 1
    assert any("Port aux Basques" in p for p in r["problems"]) and any("2026-11-07" in p for p in r["problems"])
    assert "PROBLEM" in (tmp_path / "logs" / "health.log").read_text()


def test_health_launch_agent_runs_daily():
    import plistlib
    from mpf.config import load_settings
    pl = plistlib.loads(health.plist_text(load_settings()).encode())
    assert pl["ProgramArguments"][-1] == "health" and pl["StartCalendarInterval"]["Hour"] == 9
    assert pl["Label"] != "com.marineprojectfinder.autoupdate"


# --- A4 and A5: site date, status file and RSS feeds ------------------------------------------------------

def _rec(i, tier="strong", changes=("new",), profiles=None, **kw):
    r = {"id": f"nl_ea:{i}", "title": f"Project {i}", "url": f"https://www.gov.nl.ca/p{i}/", "page": f"p/nl_ea-{i}.html",
         "source": "nl_ea", "source_name": "Newfoundland and Labrador environmental assessment", "agency": "",
         "province": "Newfoundland and Labrador", "country": "CA", "types": ["Breakwater"], "tier": tier,
         "tier_label": tier.capitalize(), "summary": "A breakwater.", "changes": list(changes), "first_listed": "2026-11-14",
         "profiles": profiles or {"hard-structure": {"tier": "strong", "tier_label": "Strong", "score": 70, "because": ""}}, "attribution": "", "issued_by": ""}
    r.update(kw)
    return r


def test_feeds_hold_only_new_projects_with_the_source_line():
    recs = [_rec(1), _rec(2, changes=()), _rec(3, tier="notrel", profiles={"hard-structure": {"tier": "notrel", "score": 5}})]
    assert [r["id"] for r in site.feed_items(recs, None)] == ["nl_ea:1"]
    assert [r["id"] for r in site.feed_items(recs, "hard-structure")] == ["nl_ea:1"]
    xml = site.render_feed(site.feed_items(recs, None), "MitiMark", "d", "https://x.test/", "feeds/all.xml",
                           date(2026, 11, 17))
    import xml.etree.ElementTree as ET
    items = ET.fromstring(xml).findall("./channel/item")
    assert len(items) == 1
    desc = items[0].find("description").text
    assert "Source: Newfoundland and Labrador environmental assessment." in desc
    assert "Official notice: https://www.gov.nl.ca/p1/" in desc
    assert items[0].find("link").text == "https://x.test/p/nl_ea-1.html"
    assert "@" not in xml                           # no email addresses anywhere in a feed


def test_freshwater_projects_left_out_of_profiles_that_exclude_them():
    recs = [_rec(1, freshwater=True)]
    assert site.feed_items(recs, "hard-structure", exclude_freshwater=True) == []


def test_built_site_has_feeds_status_and_last_successful_update(tmp_path):
    from mpf.config import load_settings
    s = load_settings()
    recs = [_rec(1, comments={}, timeline=[], stage_kind="assessment", stage="under assessment", quotes=[],
                 also_listed=[], decision_date="2026-11-13", approved_on=None, comment_deadline=None, proponent="Town",
                 language="en", water_body="", score=70, tier_reason="", reviewed=False, summary_note="",
                 licence_types=[], freshwater=False, too_late=False)]
    site.write_site(tmp_path, recs, s, date(2026, 11, 17), coverage={"countries": []})
    assert json.loads((tmp_path / "status.json").read_text()) == {"last_successful_update": "2026-11-17", "items": 1}
    assert (tmp_path / "feeds" / "all.xml").exists()
    assert "Last update" in (tmp_path / "index.html").read_text()        # the home page says when
    assert 'href="feeds/all.xml"' in (tmp_path / "about.html").read_text()
    assert "Decision due (official notice)" in (tmp_path / "p" / "nl_ea-1.html").read_text()


def test_feed_attributes_are_quoted_and_only_web_links_are_kept():
    import xml.etree.ElementTree as ET
    recs = [_rec(1, url='https://x.test/a?b="c"&d=<e>'), _rec(2, url="javascript:alert(1)")]
    xml = site.render_feed(site.feed_items(recs, None), "M", "d", "https://x.test/", "feeds/all.xml", date(2026, 11, 17))
    items = ET.fromstring(xml).findall("./channel/item")      # parses: the quote did not break the XML
    assert items[0].find("source").get("url") == 'https://x.test/a?b="c"&d=<e>'
    assert items[1].find("source") is None and "javascript:" not in xml


# --- Country menu labels (2026-11-17) ---------------------------------------------------------------------

def test_country_menu_says_coming_soon_only_for_countries_not_read():
    meta = [{"key": "hard-structure", "label": "Hard-structure eco-engineering"}]
    us_rec = {"profile_only": True, "country": "US", "tier": "notrel",
              "profiles": {"hard-structure": {"tier": "weak", "score": 23, "min": 20}}}
    us = {"code": "US", "name": "United States", "status": "partial", "region_label": "State"}
    opt = site.country_option(us, [us_rec], meta)
    assert ">United States (only under Hard-structure)<" in opt and "disabled" not in opt
    assert "coming soon" not in opt
    no = {"code": "NO", "name": "Norway", "status": "coming_soon"}
    assert "Norway (coming soon)" in site.country_option(no, [], meta) and "disabled" in site.country_option(no, [], meta)
    dk = {"code": "DK", "name": "Denmark", "status": "partial"}
    assert "Denmark (nothing listed now)" in site.country_option(dk, [], meta)
