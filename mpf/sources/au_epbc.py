"""Australia, EPBC Act Public Portal (epbcpublicportal.environment.gov.au).

Used with written permission from the National Environmental Protection Agency, which runs the EPBC Act
(reply of 2026-11-24, filed in config/settings.yaml). robots.txt sets no rules for the portal.

The portal is a Power Pages site: its tables are empty until the page's own script fills them, so this source uses
the hidden browser in mpf/browser.py. Every page open and every "Next page" click goes through the polite checks.

What we read, about once a week:
- /open-for-comments/ : every page of the list of notices open for public comment (EPBC number, notice name,
  start date, due date). This gives the public comment deadlines.
- /all-referrals/ : the list of all EPBC projects, newest first, page by page until the "Valid Date" is older than
  lookback_days (or max_pages is reached).
- the summary page of each of those projects (proposer, industry type, state or territory, location and the
  project description). A project page is read again only when its status in the list changes, or after
  recheck_days. Decision notices and attached documents are not downloaded.

Most EPBC referrals are inland (mines, housing, roads, battery farms), and every one is written around threatened
species and offsets, so the general scoring would list many of them. This source therefore keeps a project only when
it is clearly marine (owner's OK, 2026-11-24): its title names marine work or a sea place, or its description names
at least two different ones (see MARINE and is_marine). Place names such as "Sunshine Coast" or "Moreton Bay" do not
count on their own.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Iterator

from bs4 import BeautifulSoup

from mpf.browser import Browser, NoBrowser
from mpf.extract import extract_water_body
from mpf.http import Blocked
from mpf.models import UNKNOWN, Item
from mpf.sources.base import Source
from mpf.textutil import clean, parse_date, today

BASE = "https://epbcpublicportal.environment.gov.au"
REFERRALS_URL = BASE + "/all-referrals/"
COMMENTS_URL = BASE + "/open-for-comments/"

# Every data row of the page's table, as {header: cell text} plus the first "?id=" link in the row.
ROWS_JS = r"""JSON.stringify((() => {
  const t = document.querySelector('table'); if (!t) return [];
  const heads = [...t.querySelectorAll('thead th')].map(th => th.innerText.replace(/\.\s*sort (a|de)scending/i, '').trim());
  return [...t.querySelectorAll('tbody tr')].filter(tr => tr.querySelectorAll('td').length > 3).map(tr => {
    const o = {}; [...tr.querySelectorAll('td')].forEach((td, i) => { o[heads[i] || ('col' + i)] = td.innerText.trim(); });
    const a = tr.querySelector('a[href*="?id="]'); o._link = a ? a.href : ''; return o; });
})())"""
FIRST_JS = ("(() => { const tr = [...document.querySelectorAll('table tbody tr')].find(r => r.querySelectorAll('td')"
            ".length > 3); return tr ? tr.querySelector('td').innerText.trim() : ''; })()")
NEXT_JS = ("(() => { const a = [...document.querySelectorAll('.pagination a')].find(x => "
           "x.getAttribute('aria-label') === 'Next page' && !x.closest('li.disabled')); if (!a) return false; "
           "a.click(); return true; })()")
# The summary form: field id -> value. The description field holds HTML.
DETAIL_JS = r"""JSON.stringify((() => { const o = {};
  document.querySelectorAll('input[id], textarea[id]').forEach(e => { if (e.value) o[e.id] = e.value; });
  const m = document.body.innerText.match(/Project Status:\s*(.+)/); o._status = m ? m[1].trim() : '';
  return o; })())"""
DETAIL_READY_JS = "(() => { const t = document.getElementById('ticketnumber'); return !!(t && t.value); })()"

# Statuses that mean the final EPBC decision has been made. "Referral Decision Made" is not one of them: it is the
# early decision on whether the project needs a full assessment.
_FINAL = re.compile(r"^(approved|not approved|approval decision|final decision|post approval|completed|"
                    r"clearly unacceptable|not controlled action|lapsed)", re.I)


# Marine works and sea places. Broad words that also turn up in inland projects ("shoreline" of a dam, "tidal",
# "outfall", "decommissioning", "estuary", any creek bridge) are left out on purpose.
MARINE = re.compile(r"""\b(
  wharf|wharves|jetty|jetties|pier|breakwater|seawall|sea\ wall|groyne|revetment|boat\ ramp|marina|berth\w*|
  pontoon|mooring|slipway|ferry\ terminal|ferry|harbour\ (works|infrastructure|facility)|
  port\ (expansion|development|works|berth)|dredg\w*|reclamation|nourishment|sand\ (bypass|pumping)|foreshore|
  intertidal|subtidal|mangrove\w*|seagrass|seabed|sea\ floor|seafloor|offshore|submarine|subsea|
  marine\ (park|area|environment|infrastructure|survey|works|facility)|commonwealth\ marine\ area|
  great\ barrier\ reef|ocean\ outfall|ocean\w*|in\ the\ sea|coastal\ waters|aquaculture|sea\ pens?|fish\ farm|
  desalination|underbridge|cable\ (route|survey|landing)|carbon\ capture|ccs|handfish|offshore\ decommissioning
)\b""", re.I | re.X)


def is_marine(title: str, text: str) -> bool:
    """True when the title names marine work, or the text names at least two different marine things."""
    if MARINE.search(title or ""):
        return True
    return len({m.group(0).lower() for m in MARINE.finditer(text or "")}) >= 2


def item_id(epbc_number: str) -> str:
    return epbc_number.strip().replace("/", "-")


def parse_referral_row(row: dict) -> dict | None:
    num = clean(row.get("EPBC Number", ""))
    if not re.fullmatch(r"\d{4}/\d{3,5}", num) or not row.get("_link"):
        return None
    return {"epbc": num, "url": row["_link"], "title": clean(row.get("Project", "")) or UNKNOWN,
            "proponent": clean(row.get("Proposer/Approval Holder", "")) or UNKNOWN,
            "location": clean(row.get("Location", "")) or UNKNOWN,
            "industry": clean(row.get("Industry Type", "")) or UNKNOWN,
            "valid": parse_date(row.get("Valid Date", "")),
            "status": clean(row.get("Project Status", "")) or UNKNOWN,
            "jurisdiction": clean(row.get("Primary Jurisdiction", "")) or UNKNOWN}


def parse_comment_rows(rows: list[dict]) -> dict[str, dict]:
    """EPBC number -> the open public comment notice that closes first."""
    out: dict[str, dict] = {}
    for row in rows:
        num = clean(row.get("EPBC Number", ""))
        due = parse_date(row.get("Due Date", ""))
        if not num or not due:
            continue
        notice = {"notice": clean(row.get("Notice Name", "")), "start": parse_date(row.get("Start Date", "")),
                  "due": due, "url": row.get("_link", "")}
        if num not in out or due < out[num]["due"]:
            out[num] = notice
    return out


def parse_detail(fields: dict) -> dict:
    desc_html = fields.get("mara_proposedactionoverview", "")
    desc = clean(BeautifulSoup(desc_html, "html.parser").get_text(" ")) if desc_html else ""
    proponent = (fields.get("mara_designatedproponent_name") or fields.get("mara_personproposingaction_name")
                 or fields.get("mara_approvalholder_name") or "")
    return {"epbc": clean(fields.get("ticketnumber", "")), "proponent": clean(proponent) or UNKNOWN,
            "industry": clean(fields.get("mara_industrytype_name", "")) or UNKNOWN,
            "jurisdiction": clean(fields.get("mara_primarymarajurisdiction_name", "")) or UNKNOWN,
            "location": clean(fields.get("mara_location", "")) or UNKNOWN,
            "description": desc, "status": clean(fields.get("_status", "")) or UNKNOWN}


class AUEPBCSource(Source):
    name = "au_epbc"
    country = "AU"

    def fetch(self) -> Iterator[Item]:
        cache_file = self.client.cache_dir / "au_epbc_details.json"
        try:
            cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
        except ValueError:
            cache = {}
        try:
            with Browser(self.client) as b:
                comments = parse_comment_rows(self._read_list(b, COMMENTS_URL, stop_before=None))
                cutoff = (today() - timedelta(days=int(self.conf.get("lookback_days", 365)))).isoformat()
                rows = [r for r in (parse_referral_row(x) for x in self._read_list(b, REFERRALS_URL, cutoff)) if r]
                recent = [r for r in rows if r["valid"] and r["valid"] >= cutoff]
                self.log.append(f"{len(comments)} projects open for public comment; {len(recent)} projects valid since {cutoff}")
                missing = sorted(set(comments) - {r["epbc"] for r in recent})
                if missing:
                    self.log.append(f"{len(missing)} open comment notices are for older projects, not read: "
                                    + ", ".join(missing[:10]))
                recheck = int(self.conf.get("recheck_days", 60))
                inland = 0
                for row in recent:
                    hit = cache.get(row["epbc"])
                    fresh = hit and hit.get("list_status") == row["status"] and hit.get("read", "") >= (
                        today() - timedelta(days=recheck)).isoformat()
                    if not fresh:
                        fields = self._read_detail(b, row["url"])
                        if fields is None:
                            continue
                        hit = {"list_status": row["status"], "read": today().isoformat(), "fields": fields}
                        cache[row["epbc"]] = hit
                        cache_file.write_text(json.dumps(cache, indent=1))
                    detail = parse_detail(hit["fields"])
                    if not is_marine(row["title"], f"{detail['location']}. {detail['description']}"):
                        inland += 1
                        continue
                    yield self.make_item(row, detail, comments.get(row["epbc"]))
                self.log.append(f"{inland} projects left out because nothing in them is clearly marine")
        except NoBrowser as e:
            self.log.append(f"skipped: {e}")
        except Blocked as e:
            self.log.append(f"STOPPED: {e}")
        finally:
            if cache:
                cache_file.write_text(json.dumps(cache, indent=1))

    def _read_list(self, b: Browser, url: str, stop_before: str | None) -> list[dict]:
        """Every row of a paged list. For the referrals list, stop once a page's last Valid Date is before
        stop_before."""
        b.open(url)
        if not b.wait_for(FIRST_JS + " !== ''"):
            self.log.append(f"{url}: the table did not load")
            return []
        out, first = [], None
        for page in range(int(self.conf.get("max_pages", 60))):
            rows = json.loads(b.js(ROWS_JS) or "[]")
            out += rows
            dates = [parse_date(r.get("Valid Date", "")) for r in rows]
            if stop_before and dates and dates[-1] and dates[-1] < stop_before:
                break
            first = rows[0].get("EPBC Number") if rows else None
            if not b.click(url, NEXT_JS):
                break                                   # last page
            changed = f"{FIRST_JS} !== '' && {FIRST_JS} !== {json.dumps(first)}"
            if not b.wait_for(changed) and not b.wait_for(changed, 60):
                self.log.append(f"{url}: page {page + 2} did not load; kept {len(out)} rows")
                break
        return out

    def _read_detail(self, b: Browser, url: str) -> dict | None:
        b.open(url)
        if not b.wait_for(DETAIL_READY_JS):
            self.log.append(f"skipped {url}: the page did not load")
            return None
        b.wait_for("!!document.getElementById('mara_proposedactionoverview')", 5)
        return json.loads(b.js(DETAIL_JS) or "{}")

    def make_item(self, row: dict, d: dict, comment: dict | None) -> Item:
        status = row["status"] if row["status"] != UNKNOWN else d["status"]
        location = d["location"] if d["location"] != UNKNOWN else row["location"]
        text = clean(f"{row['title']}. {location}. {d['description']}")
        deadline = comment["due"] if comment else None
        return Item(
            source=self.name, item_id=item_id(row["epbc"]), url=row["url"], title=row["title"], language="en",
            country=self.country, province=d["jurisdiction"] if d["jurisdiction"] != UNKNOWN else row["jurisdiction"],
            water_body=extract_water_body(text),
            proponent=d["proponent"] if d["proponent"] != UNKNOWN else row["proponent"],
            project_type_raw=d["industry"] if d["industry"] != UNKNOWN else row["industry"],
            date_posted=row["valid"], comment_deadline=deadline, status_raw=status, full_text=text,
            extra={"epbc_number": row["epbc"], "location": location, "decision_made": bool(_FINAL.search(status)),
                   "comment_notice": comment["notice"] if comment else "",
                   "comment_url": comment["url"] if comment else ""},
        )
