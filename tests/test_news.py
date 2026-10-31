"""News signals: only headline, source, date and link; never Strong or Possible; duplicates; matching."""
import json
from pathlib import Path

from mpf import db, news, site
from mpf.http import Blocked

GDELT = json.dumps({"articles": [
    {"url": "https://example.com/a", "title": "Breakwater extension planned at Belleoram harbour",
     "seendate": "20261029T101500Z", "domain": "example.com", "language": "English"},
    {"url": "https://example.com/b", "title": "", "seendate": "20261029T101500Z", "domain": "example.com"}]})
ATOM = """<feed><entry><title>New wharf construction in Glace Bay</title>
<summary>Long article text that must never be stored.</summary><updated>2026-10-14T10:00:00-04:00</updated>
<link href="https://www.canada.ca/x.html"/></entry></feed>"""


def test_feeds_keep_only_headline_source_date_and_link():
    g = news.parse_gdelt(GDELT, "gdelt_ca")
    assert len(g) == 1 and set(g[0]) == {"url", "title", "source_name", "date", "feed", "via"}
    assert g[0]["date"] == "2026-10-29" and g[0]["via"] == "GDELT Project"
    a = news.parse_atom(ATOM, "canada_dfo", "DFO news")
    assert a[0]["title"] == "New wharf construction in Glace Bay" and "never be stored" not in json.dumps(a)


def test_headlines_without_a_marine_construction_type_are_dropped(rules):
    rows = [{"url": "u1", "title": "Two individuals fined in the commercial lobster fishery", "date": "2026-10-30",
             "source_name": "s", "feed": "f", "via": ""},
            {"url": "u2", "title": "Breakwater extension planned at Belleoram harbour", "date": "2026-10-29",
             "source_name": "s", "feed": "f", "via": ""}]
    kept = news.score_news(rows, rules)
    assert [r["url"] for r in kept] == ["u2"]


def test_news_is_never_strong_or_possible_and_duplicates_are_removed(rules, tmp_path):
    conn = db.connect(Path(":memory:"))
    conn.executescript(news.SCHEMA)
    big = "Major port expansion with 500,000 m3 dredging, new breakwater and fish habitat offsetting in the harbour"
    for url, title, via in [("u1", big, ""), ("u2", big + "!", "GDELT Project"), ("u3", big, "GDELT Project")]:
        conn.execute("INSERT INTO news VALUES (?,?,?,?,?,?,?)", (url, title, "s", "2026-10-29", "f", via, "x"))
    items = news.build_news(conn, [], rules)
    assert len(items) == 1 and items[0]["url"] == "u1"          # official feed kept, syndicated copies dropped
    assert items[0]["tier"] == "news"


def test_matching_needs_the_project_name_and_place_not_just_the_province():
    rec = {"id": "iaac:1", "title": "Floating Dock Relocation at Belleoram SCH, NL",
           "province": "Newfoundland and Labrador", "water_body": "unknown", "tier": "weak"}
    assert news.matches_notice("Belleoram floating dock relocation gets go-ahead", rec)
    assert not news.matches_notice("New dock in Newfoundland and Labrador", rec)
    ns = {"id": "iaac:2", "title": "Breakwater Reconstruction at Port Morien, Cape Breton County, Nova Scotia",
          "province": "Nova Scotia", "water_body": "unknown", "tier": "weak"}
    assert not news.matches_notice("Government of Canada advances construction of new marginal wharf in Glace Bay, "
                                   "Nova Scotia", ns)


class _Client:
    def __init__(self):
        self.calls = []

    def get(self, url, list_page=False):
        self.calls.append(url)
        if "gdeltproject" in url:
            raise Blocked("answered HTTP 429")
        return ATOM.encode()


def test_a_blocked_feed_stops_its_host_only():
    conn = db.connect(Path(":memory:"))
    client = _Client()
    conf = {"enabled": True, "feeds": [{"name": "g1", "type": "gdelt", "query": "a"}, {"name": "g2", "type": "gdelt", "query": "b"},
                      {"name": "dfo", "type": "atom", "url": "https://api.io.canada.ca/x", "source_name": "DFO"}]}
    log = news.ingest(conn, client, conf)
    assert sum("gdeltproject" in c for c in client.calls) == 1        # never retried, second query skipped
    assert "STOPPED" in log[0] and "skipped" in log[1] and "1 headlines" in log[2]


def test_news_section_is_collapsed_capped_and_matched_items_go_to_the_project_page():
    items = [{"id": str(i), "url": f"https://e.com/{i}", "title": f"Wharf news {i}", "source_name": "e.com",
              "date": "2026-10-29", "via": "GDELT Project", "matches": []} for i in range(20)]
    items[0]["matches"] = ["iaac:1"]
    html = site.news_section(items, 15)
    assert html.startswith('<details class="panel news"') and "News signal, unverified (19)" in html
    assert "Show all 19" in html and "Wharf news 0" not in html and "via <a" in html
    recs = [{"id": "iaac:1"}]
    site.attach_news(recs, items)
    assert recs[0]["in_the_news"][0]["title"] == "Wharf news 0"
    assert "Also in the news" in site._in_the_news_html(recs[0])


def test_news_is_off_by_default_and_nothing_runs_or_shows():
    from mpf.config import load_settings
    conf = load_settings()["news"]
    assert conf["enabled"] is False and conf["show_on_site"] is False
    assert not news.news_on(conf) and not news.news_on_site(conf)
    assert not news.news_on_site({"enabled": False, "show_on_site": True})     # both switches are needed
    assert not any("gdelt" in f.get("type", "") or "gdelt" in f.get("url", "") for f in conf["feeds"])
    client = _Client()
    log = news.ingest(db.connect(Path(":memory:")), client, conf)
    assert client.calls == [] and "switched off" in log[0]
