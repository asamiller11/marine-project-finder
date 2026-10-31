"""News signals: early warning from news feeds and open news indexes. Unverified by design.

Sources (each checked first: robots.txt, then terms; see config/settings.yaml news.feeds):
- GDELT DOC 2.0 API (api.gdeltproject.org): an open index of world news. No robots.txt rules.
  Terms: "available for unlimited and unrestricted use ... any use or redistribution of the data must
  include a citation to the GDELT Project and a link". GDELT asks for at most one request every
  5 seconds; we wait longer.
- Canada.ca news API (api.io.canada.ca): official Atom feeds of federal news releases. No robots.txt
  rules. Canada.ca terms allow non-commercial reproduction with the title, author and a link.
- GOV.UK Atom feeds (www.gov.uk/search/news-and-communications.atom): robots.txt allows them
  (only /search/all* is disallowed). Content is under the Open Government Licence v3.0.

What is stored: the headline, the source name, the date and the link. Never article text, never
summaries, and nothing behind a paywall is opened: we only link to the article.

How it is used: each headline is scored with the same rules as notices (config/scoring.yaml), but
a news item is never Strong match or Possible on its own. It is listed only in the collapsed
"News signal, unverified" section. If it matches an official notice for the same project (place
and project name), it appears as an "Also in the news" link on that project's page instead.
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import re
import sqlite3
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urlencode, urlparse

from mpf.http import Blocked


def news_on(conf: dict | None) -> bool:
    """Feeds are read only when news.enabled is true."""
    return bool((conf or {}).get("enabled"))


def news_on_site(conf: dict | None) -> bool:
    """The news section appears only when news.enabled AND news.show_on_site are both true."""
    return news_on(conf) and bool((conf or {}).get("show_on_site"))
from mpf.score import score_text
from mpf.textutil import clean

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_CREDIT = "GDELT Project"
GDELT_URL = "https://www.gdeltproject.org/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    url TEXT PRIMARY KEY, title TEXT, source_name TEXT, date TEXT, feed TEXT, via TEXT, first_seen TEXT
);
"""


# ---------------------------------------------------------------------------
# Reading feeds (only headline, source name, date and link are kept)
# ---------------------------------------------------------------------------

def gdelt_url(query: str, timespan: str = "14d", maxrecords: int = 75) -> str:
    return GDELT_API + "?" + urlencode({"query": query, "mode": "ArtList", "format": "json",
                                        "maxrecords": maxrecords, "timespan": timespan, "sort": "DateDesc"})


def parse_gdelt(body: str, feed: str) -> list[dict]:
    try:
        data = json.loads(body or "{}")
    except ValueError:
        return []
    out = []
    for a in data.get("articles", []) or []:
        url, title = a.get("url"), clean(a.get("title"))
        if not url or not title:
            continue
        d = re.match(r"(\d{4})(\d{2})(\d{2})", a.get("seendate") or "")
        out.append({"url": url, "title": title, "source_name": a.get("domain") or urlparse(url).netloc,
                    "date": f"{d.group(1)}-{d.group(2)}-{d.group(3)}" if d else None, "feed": feed,
                    "via": GDELT_CREDIT})
    return out


def parse_atom(xml: str, feed: str, source_name: str) -> list[dict]:
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml or "", re.S):
        title = re.search(r"<title[^>]*>(.*?)</title>", entry, re.S)
        link = re.search(r'<link[^>]*href="([^"]+)"', entry)
        updated = re.search(r"<(?:updated|published)>(\d{4}-\d{2}-\d{2})", entry)
        if not (title and link):
            continue
        out.append({"url": htmlmod.unescape(link.group(1)), "title": clean(htmlmod.unescape(title.group(1))),
                    "source_name": source_name, "date": updated.group(1) if updated else None,
                    "feed": feed, "via": ""})
    return out


def fetch_feed(client, feed: dict) -> list[dict]:
    if feed["type"] == "gdelt":
        body = client.get(gdelt_url(feed["query"], feed.get("timespan", "14d")), list_page=True)
        return parse_gdelt(body.decode("utf-8", "replace"), feed["name"])
    body = client.get(feed["url"], list_page=True)
    return parse_atom(body.decode("utf-8", "replace"), feed["name"], feed["source_name"])


def ingest(conn: sqlite3.Connection, client, conf: dict) -> list[str]:
    """Read every news feed. One blocked feed stops only that feed (and the rest of its host).
    Does nothing unless news.enabled is true."""
    if not news_on(conf):
        return ["News is switched off (news.enabled: false). No feed was read."]
    conn.executescript(SCHEMA)
    log, blocked_hosts = [], set()
    now = datetime.now().isoformat(timespec="seconds")
    for feed in conf.get("feeds", []):
        host = urlparse(feed.get("url") or GDELT_API).netloc
        if host in blocked_hosts:
            log.append(f"{feed['name']}: skipped, {host} stopped us earlier in this run")
            continue
        try:
            items = fetch_feed(client, feed)
        except Blocked as e:
            blocked_hosts.add(host)
            log.append(f"{feed['name']}: STOPPED: {e}")
            continue
        except Exception as e:        # one broken feed must not stop the others
            log.append(f"{feed['name']}: failed: {str(e)[:160]}")
            continue
        new = 0
        for it in items:
            cur = conn.execute("INSERT OR IGNORE INTO news (url, title, source_name, date, feed, via, first_seen) "
                               "VALUES (?,?,?,?,?,?,?)", (it["url"], it["title"], it["source_name"], it["date"],
                                                          it["feed"], it["via"], now))
            new += cur.rowcount
        log.append(f"{feed['name']}: {len(items)} headlines ({new} new)")
    conn.commit()
    return log


# ---------------------------------------------------------------------------
# Scoring, duplicates and matching to official notices
# ---------------------------------------------------------------------------

# Words that say nothing about which project it is.
_GENERIC = set("""a an and the of at in on for to from with by new project projects phase construction works work
repair repairs replacement reconstruction rehabilitation upgrade upgrades expansion extension installation proposed
port harbour harbor wharf wharves dock docks pier jetty breakwater marina terminal facility facilities dredging
seawall shoreline protection coastal marine sch nl ns nb bc pei qc canada canadian federal government small craft
infrastructure existing associated relocation floating plan approval approved begins start starts update public
notice comment comments determination authority authorities city town county island islands bay river""".split())


_PROVINCES = ("Newfoundland Labrador Nova Scotia New Brunswick Prince Edward Island Quebec Québec Ontario Manitoba "
              "Saskatchewan Alberta British Columbia Yukon Northwest Territories Nunavut Scotland England Wales "
              "Northern Ireland United Kingdom Denmark Danmark Spain España")


_KIND_WORDS = {"wharf", "dock", "pier", "jetty", "breakwater", "marina", "terminal", "dredging", "seawall",
               "launchway", "slipway", "causeway", "outfall", "harbour", "harbor", "port"}


def name_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zà-ÿ0-9][a-zà-ÿ0-9'\-]+", (text or "").lower())
    return {w.strip("'-") for w in words if w not in _GENERIC and len(w) > 2 and not w.isdigit()}


def matches_notice(headline: str, rec: dict) -> bool:
    """Same project: the headline names the place (province, water body or a place word in the title)
    and at least two distinctive words of the official project name."""
    # Province and country names are too broad to identify a project ("Nova Scotia" is not a name).
    broad = name_tokens(" ".join(x for x in (rec.get("province"), _PROVINCES) if x and x != "unknown"))
    distinct = name_tokens(rec.get("title", "")) - broad
    shared = distinct & name_tokens(headline)
    if len(shared) < 2:
        # A title with a single distinctive word (usually the place): also require the same kind of work.
        kinds = _KIND_WORDS & set(re.findall(r"[a-z]+", rec.get("title", "").lower())) & \
            set(re.findall(r"[a-z]+", headline.lower()))
        if not (len(distinct) == 1 and shared and kinds):
            return False
    # Place: a capitalized name in the official title ("Belleoram", "Esquimalt") or the water body.
    place_words = name_tokens(" ".join(re.findall(r"\b[A-ZÀ-Ý][\w'\-]+", rec.get("title", ""))))
    if rec.get("water_body") not in (None, "", "unknown"):
        place_words |= name_tokens(rec["water_body"])
    return bool((shared & place_words) - broad)


def score_news(rows: list[dict], rules: dict) -> list[dict]:
    """Score each headline with the notice rules. Only headlines naming a marine construction type are kept."""
    out = []
    for r in rows:
        s = score_text(r["title"], rules)
        if not s["project_types"]:
            continue
        out.append({**r, "score": s["score"], "types": s["project_types"]})
    return out


def dedupe(rows: list[dict], threshold: float = 0.85) -> list[dict]:
    """Drop repeats: the same link, or nearly the same headline (syndicated copies). Official feeds win."""
    rows = sorted(rows, key=lambda r: (r.get("via") == GDELT_CREDIT, r.get("date") or ""), reverse=False)
    kept: list[dict] = []
    for r in rows:
        t = re.sub(r"\W+", " ", r["title"].lower()).strip()
        if any(k["url"] == r["url"] or SequenceMatcher(None, t, k["_t"]).ratio() >= threshold for k in kept):
            continue
        kept.append({**r, "_t": t})
    for k in kept:
        k.pop("_t", None)
    return kept


def news_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def build_news(conn: sqlite3.Connection, records: list[dict], rules: dict, max_age_days: int = 60,
               today: str | None = None) -> list[dict]:
    """News items for the site, newest first, each marked with the official project it matches (if any)."""
    conn.executescript(SCHEMA)
    rows = [dict(r) for r in conn.execute("SELECT url, title, source_name, date, feed, via FROM news")]
    if today:
        from datetime import date as _d
        limit = _d.fromisoformat(today).toordinal() - max_age_days
        rows = [r for r in rows if not r["date"] or _d.fromisoformat(r["date"]).toordinal() >= limit]
    items = dedupe(score_news(rows, rules))
    for it in items:
        it["id"] = news_id(it["url"])
        it["tier"] = "news"                     # never Strong match or Possible on its own
        it["matches"] = [r["id"] for r in records if r.get("tier") != "watch" and matches_notice(it["title"], r)]
    items.sort(key=lambda r: (r.get("date") or "", r["score"]), reverse=True)
    return items
