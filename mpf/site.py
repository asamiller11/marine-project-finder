"""Builds the static website from the items you approved in review/review.csv.

Output (folder site/):
  index.html   the project list (works without JavaScript; JavaScript adds live
               countdowns, the "closing soon" list, filters and search)
  about.html   what the tool does, sources, limits
  items.json   the published records (also used as a fallback if a source drops an item)

Content rules built in: short exact quotes only, every item links to its official
notice, missing facts show "unknown", nothing says mitigation "will be required".
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from html import escape
from pathlib import Path

from mpf.models import UNKNOWN
from mpf.review import auto_summary, confidence_for

SOURCE_NAMES = {
    "iaac": "Canadian Impact Assessment Registry",
    "nl_ea": "Newfoundland and Labrador environmental assessment",
    "drop": "Added by hand from a public notice",
}

# Which reasons make the best evidence, in order.
_QUOTE_ORDER = ("habitat:", "size:", "type:")


def trim_quote(quote: str, matched: str, max_chars: int) -> str:
    """Shorten a quote around its keyword. The text kept is an exact piece of the original;
    '...' marks where we cut."""
    quote = quote.strip()
    if len(quote) <= max_chars:
        return quote
    core = quote.strip(".")
    i = core.lower().find((matched or "").lower()) if matched else -1
    if i < 0:
        i = 0
    start = max(0, i - max_chars // 3)
    end = min(len(core), start + max_chars)
    start = max(0, end - max_chars)
    # Cut at word boundaries so no word is broken.
    if start > 0:
        nxt = core.find(" ", start)
        start = nxt + 1 if 0 <= nxt < i else start
    if end < len(core):
        prv = core.rfind(" ", start, end)
        end = prv if prv > i + len(matched or "") else end
    piece = core[start:end].strip(" ,;:")
    return ("..." if start > 0 else "") + piece + ("..." if end < len(core) else "")


# Contact lines are not evidence, and they often name individual officials. Never quote them.
_CONTACT = re.compile(r"submitted to|send (your )?comments|comments? (should|must|may) be|contact|e-?mail|@|"
                      r"\bphone\b|\btel\b|\bfax\b|P\.?O\.? Box|courriel|téléphone|adress(e|ed) to", re.IGNORECASE)


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def pick_quotes(reasons: list[dict], max_quotes: int, max_chars: int, title: str = "") -> list[str]:
    """Up to max_quotes short evidence quotes: habitat clues first, then size, then type.
    Skips repeats of the same sentence and quotes that are just the project title."""
    usable = [r for r in reasons if r.get("quote") and r.get("points", 0) > 0 and not _CONTACT.search(r["quote"])]
    usable.sort(key=lambda r: (next((n for n, p in enumerate(_QUOTE_ORDER) if r["rule"].startswith(p)), 9),
                               -r["points"]))
    out, used_sentences = [], []
    t = _norm(title)
    for r in usable:
        sentence = _norm(r["quote"])
        if t and (sentence == t or sentence in t):
            continue
        if any(sentence in u or u in sentence for u in used_sentences):
            continue
        q = trim_quote(r["quote"], r.get("matched", ""), max_chars)
        if len(q) < 25:
            continue
        used_sentences.append(sentence)
        out.append(q)
        if len(out) >= max_quotes:
            break
    return out


def stage_kind(row: dict, today: date) -> str:
    stage = (row.get("stage") or "").lower()
    deadline = row.get("comment_deadline")
    if stage.startswith("decision made") or stage.startswith("authorized"):
        return "decided"
    if deadline and deadline != UNKNOWN:
        return "open" if date.fromisoformat(deadline) >= today else "closed"
    if stage in ("under assessment", "registered"):
        return "assessment"
    return "unknown"


STAGE_LABELS = {"open": "Comment period open", "closed": "Comment period closed",
                "decided": "Decision made", "assessment": "Under assessment", "unknown": "Stage unknown"}


def _val(v) -> str:
    return v if v not in (None, "") else UNKNOWN


def make_record(row: dict, review_row: dict, site_conf: dict, review_conf: dict, today: date) -> dict:
    reasons = json.loads(row.get("reasons") or "[]")
    kind = stage_kind(row, today)
    confidence = (review_row.get("confidence") or "").strip().capitalize()
    if confidence not in ("High", "Medium", "Low"):
        confidence = confidence_for(row.get("score") or 0, review_conf)
    summary = (review_row.get("summary") or "").strip() or row.get("summary_en") or auto_summary(row)
    return {
        "id": row["key"],
        "title": _val(row.get("title")),
        "url": row.get("url") or "",
        "source": row.get("source"),
        "source_name": SOURCE_NAMES.get(row.get("source"), "Public notice"),
        "proponent": _val(row.get("proponent")),
        "province": _val(row.get("province")),
        "water_body": _val(row.get("water_body")),
        "types": json.loads(row.get("project_types") or "[]"),
        "stage": _val(row.get("stage")),
        "stage_kind": kind,
        "comment_deadline": row.get("comment_deadline") or None,
        "decision_date": row.get("decision_date") or None,
        "score": row.get("score") or 0,
        "confidence": confidence,
        "summary": summary,
        "quotes": pick_quotes(reasons, site_conf.get("max_quotes", 3), site_conf.get("max_quote_chars", 180),
                              title=row.get("title") or ""),
        "too_late": kind in ("decided", "closed"),
        "review": (review_row.get("publish") or "no").strip().lower(),
    }


def build_records(conn: sqlite3.Connection, review_rows: dict, ids: list[str], settings: dict,
                  today: date, previous: dict | None = None) -> list[dict]:
    """Records for the given ids. If an approved item is no longer in the database (for
    example it dropped out of a source's recent list), reuse its last published record."""
    previous = previous or {}
    records = []
    for key in ids:
        row = conn.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
        if row is None:
            if key in previous:
                rec = dict(previous[key])
                rec["stage_kind"] = stage_kind(rec, today) if rec.get("stage_kind") != "decided" else "decided"
                records.append(rec)
            continue
        records.append(make_record(dict(row), review_rows.get(key, {}), settings.get("site", {}),
                                   settings.get("review", {}), today))
    records.sort(key=lambda r: (r["stage_kind"] != "open", -r["score"]))
    return records


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f4f8fa;--card:#ffffff;--text:#13293a;--muted:#4f6676;--line:#d3e0e8;--accent:#0b6b85;
--accent-soft:#e2f1f5;--high:#0f6b4f;--high-bg:#dcf3ea;--med:#7a5300;--med-bg:#fbefd5;--low:#4b5a66;
--low-bg:#e8edf1;--late:#9a3412;--late-bg:#fdebe2;--open:#0b6b85;--draft:#fff4c2;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#0c1a22;--card:#12252f;--text:#e2edf3;--muted:#9fb4c2;
--line:#24404e;--accent:#6cc7da;--accent-soft:#163846;--high:#7fdcb7;--high-bg:#143a2f;--med:#f2c46b;
--med-bg:#3a2e12;--low:#c2ced6;--low-bg:#25343d;--late:#f6a982;--late-bg:#3d2116;--open:#6cc7da;--draft:#3a3412}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,"Helvetica Neue",Arial,sans-serif}
a{color:var(--accent)}
.wrap{max-width:1040px;margin:0 auto;padding:0 16px}
header.site{background:var(--card);border-bottom:1px solid var(--line);padding:20px 0 16px}
header.site h1{margin:0;font-size:1.6rem;line-height:1.25}
header.site p{margin:6px 0 0;color:var(--muted)}
nav.top{margin-top:10px;font-size:.95rem}
nav.top a{margin-right:16px}
.updated{font-size:.9rem}
.draft{background:var(--draft);border:1px solid var(--line);padding:10px 14px;border-radius:8px;margin:16px 0 0}
.summary-line{margin:18px 0 8px;font-weight:600}
section.soon{background:var(--accent-soft);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}
section.soon h2{margin:0 0 6px;font-size:1.05rem}
section.soon ul{margin:0;padding-left:18px}
section.soon li{margin:4px 0}
.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0;align-items:end}
.filters label{display:flex;flex-direction:column;font-size:.85rem;color:var(--muted);gap:4px}
.filters select,.filters input[type=search]{font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--text);min-width:0}
.filters .check{flex-direction:row;align-items:center;gap:8px;font-size:.95rem;color:var(--text);padding-bottom:8px}
.cards{display:grid;gap:14px;margin:8px 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h3{margin:0 0 4px;font-size:1.1rem;line-height:1.35}
.card h3 a{text-decoration:none}
.card h3 a:hover{text-decoration:underline}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 10px}
.badge{font-size:.8rem;font-weight:600;padding:2px 9px;border-radius:999px;white-space:nowrap}
.conf-high{color:var(--high);background:var(--high-bg)}
.conf-medium{color:var(--med);background:var(--med-bg)}
.conf-low{color:var(--low);background:var(--low-bg)}
.late{color:var(--late);background:var(--late-bg)}
.st-open{color:var(--open);background:var(--accent-soft)}
.st-other{color:var(--low);background:var(--low-bg)}
.rev-yes{color:var(--high);background:var(--high-bg)}
.rev-no{color:var(--late);background:var(--late-bg)}
dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:3px 12px;margin:0 0 10px;font-size:.95rem}
dl.facts dt{color:var(--muted)}
dl.facts dd{margin:0}
.countdown{font-weight:600}
.card p.summary{margin:8px 0}
ul.quotes{margin:8px 0;padding:0;list-style:none}
ul.quotes li{border-left:3px solid var(--line);padding:2px 0 2px 10px;margin:6px 0;color:var(--muted);font-size:.95rem}
p.source{margin:10px 0 0;font-size:.92rem}
.empty{padding:24px;text-align:center;color:var(--muted);background:var(--card);border:1px dashed var(--line);border-radius:12px}
footer{border-top:1px solid var(--line);padding:18px 0 40px;color:var(--muted);font-size:.9rem}
main.about h2{margin-top:28px;font-size:1.2rem}
main.about{padding-bottom:20px}
@media (max-width:560px){header.site h1{font-size:1.3rem}dl.facts{grid-template-columns:1fr}dl.facts dt{margin-top:4px}}
"""

JS = r"""
(function(){
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var today = new Date(); today.setHours(0,0,0,0);
  var SOON = parseInt(document.body.getAttribute('data-soon') || '30', 10);
  function daysLeft(iso){ if(!iso) return null; var p = iso.split('-');
    var d = new Date(+p[0], +p[1]-1, +p[2]); return Math.round((d - today)/86400000); }
  var STAGE = {open:'Comment period open', closed:'Comment period closed', decided:'Decision made',
               assessment:'Under assessment', unknown:'Stage unknown'};
  cards.forEach(function(c){
    var dl = c.getAttribute('data-deadline'), n = daysLeft(dl), kind = c.getAttribute('data-stage');
    if (kind !== 'decided' && n !== null) { kind = n >= 0 ? 'open' : 'closed'; }
    c.setAttribute('data-stage-now', kind); c._days = n;
    var cd = c.querySelector('.countdown');
    if (cd) {
      if (kind === 'decided') cd.textContent = (dl ? dl + ', ' : '') + 'decision made';
      else if (n === null) cd.textContent = 'unknown';
      else if (n > 1) cd.textContent = dl + ' (' + n + ' days left)';
      else if (n === 1) cd.textContent = dl + ' (1 day left)';
      else if (n === 0) cd.textContent = dl + ' (closes today)';
      else cd.textContent = dl + ' (closed)';
    }
    var late = c.querySelector('.late'); if (late) late.hidden = !(kind === 'decided' || kind === 'closed');
    var st = c.querySelector('.stage-badge');
    if (st) { st.textContent = STAGE[kind]; st.className = 'badge stage-badge ' + (kind === 'open' ? 'st-open' : 'st-other'); }
  });
  // Open comment periods first, then by score.
  var list = document.querySelector('.cards');
  cards.sort(function(a,b){
    var ao = a.getAttribute('data-stage-now') === 'open' ? 0 : 1, bo = b.getAttribute('data-stage-now') === 'open' ? 0 : 1;
    return ao - bo || (+b.getAttribute('data-score')) - (+a.getAttribute('data-score'));
  });
  cards.forEach(function(c){ list.appendChild(c); });
  // Closing soon.
  var soon = cards.filter(function(c){ return c.getAttribute('data-stage-now') === 'open' && c._days !== null && c._days <= SOON; })
                  .sort(function(a,b){ return a._days - b._days; });
  var ul = document.getElementById('soon-list'), none = document.getElementById('soon-none');
  soon.forEach(function(c){
    var li = document.createElement('li'), a = document.createElement('a');
    a.href = '#' + c.id; a.textContent = c.querySelector('h3').textContent;
    li.appendChild(a); li.appendChild(document.createTextNode(': ' + c.getAttribute('data-deadline') + ' (' +
      (c._days === 0 ? 'closes today' : c._days === 1 ? '1 day left' : c._days + ' days left') + ')'));
    ul.appendChild(li);
  });
  if (none) none.hidden = soon.length > 0;
  // Filters.
  function fill(id, values){ var s = document.getElementById(id); if (!s) return;
    values.sort().forEach(function(v){ var o = document.createElement('option'); o.value = v; o.textContent = v; s.appendChild(o); }); }
  var provs = {}, types = {};
  cards.forEach(function(c){ provs[c.getAttribute('data-province')] = 1;
    (c.getAttribute('data-types') || '').split('|').forEach(function(t){ if (t) types[t] = 1; }); });
  fill('f-province', Object.keys(provs)); fill('f-type', Object.keys(types));
  var fp = document.getElementById('f-province'), ft = document.getElementById('f-type'), fs = document.getElementById('f-stage'),
      fo = document.getElementById('f-open'), fq = document.getElementById('f-search'), line = document.getElementById('summary-line'),
      empty = document.getElementById('no-match');
  function apply(){
    var q = (fq.value || '').toLowerCase().trim(), shown = 0, open = 0;
    cards.forEach(function(c){
      var k = c.getAttribute('data-stage-now');
      var ok = (!fp.value || c.getAttribute('data-province') === fp.value) &&
               (!ft.value || ('|' + c.getAttribute('data-types') + '|').indexOf('|' + ft.value + '|') >= 0) &&
               (!fs.value || k === fs.value) && (!fo.checked || k === 'open') &&
               (!q || c.getAttribute('data-search').indexOf(q) >= 0);
      c.hidden = !ok; if (ok) { shown++; if (k === 'open') open++; }
    });
    line.textContent = shown + (shown === 1 ? ' project' : ' projects') + ' shown. ' + open +
      (open === 1 ? ' has a' : ' have a') + ' comment period open now.';
    if (empty) empty.hidden = shown > 0 || cards.length === 0;
  }
  [fp, ft, fs, fo].forEach(function(el){ el.addEventListener('change', apply); });
  fq.addEventListener('input', apply);
  apply();
})();
"""


def _head(title: str, description: str) -> str:
    return (f'<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<meta name="robots" content="noindex, nofollow">\n<title>{escape(title)}</title>\n'
            f'<meta name="description" content="{escape(description)}">\n<style>{CSS}</style>')


def _footer(site: dict) -> str:
    email = escape(site.get("contact_email", ""))
    return (
        '<footer><div class="wrap">'
        "<p>Prototype. Flags show projects that may need marine habitat work. They do not state that "
        "mitigation is required. Always check the official notice. Compiled from public sources.</p>"
        f'<p>Contact: {escape(site.get("contact_name", ""))}, <a href="mailto:{email}">{email}</a></p>'
        "</div></footer>")


def _card(rec: dict, draft: bool) -> str:
    types = rec["types"] or []
    location = f'{rec["province"]}. Water body: {rec["water_body"]}'
    search = " ".join([rec["title"], rec["proponent"], rec["province"], rec["water_body"],
                       rec["summary"], " ".join(types)]).lower()
    conf = rec["confidence"]
    deadline = rec.get("comment_deadline")
    deadline_text = deadline if deadline else UNKNOWN
    if rec["stage_kind"] == "decided":
        deadline_text = (deadline + ", " if deadline else "") + "decision made"
    elif rec["stage_kind"] == "closed" and deadline:
        deadline_text = deadline + " (closed)"
    quotes = "".join(f"<li>&ldquo;{escape(q)}&rdquo;</li>" for q in rec["quotes"])
    review_badge = ""
    if draft:
        yes = rec.get("review") in ("yes", "y", "1", "true", "x")
        review_badge = (f'<span class="badge {"rev-yes" if yes else "rev-no"}">'
                        f'{"Approved for publishing" if yes else "Not approved"}</span>')
    anchor = "p-" + re.sub(r"[^A-Za-z0-9_-]", "-", rec["id"])
    link = escape(rec["url"]) if rec["url"] else "#"
    return f"""<article class="card" id="{anchor}" data-province="{escape(rec['province'])}"
 data-types="{escape('|'.join(types))}" data-stage="{rec['stage_kind']}" data-deadline="{escape(deadline or '')}"
 data-score="{int(rec['score'])}" data-search="{escape(search)}">
<h3><a href="{link}" rel="noopener">{escape(rec['title'])}</a></h3>
<div class="badges"><span class="badge conf-{conf.lower()}">{conf} confidence</span>
<span class="badge stage-badge {'st-open' if rec['stage_kind'] == 'open' else 'st-other'}">{STAGE_LABELS[rec['stage_kind']]}</span>
<span class="badge late"{'' if rec['too_late'] else ' hidden'}>Likely too late</span>{review_badge}</div>
<dl class="facts">
<dt>Proponent</dt><dd>{escape(rec['proponent'])}</dd>
<dt>Location</dt><dd>{escape(location)}</dd>
<dt>Project type</dt><dd>{escape(', '.join(types) if types else UNKNOWN)}</dd>
<dt>Stage</dt><dd>{escape(rec['stage'])}</dd>
<dt>Comment deadline</dt><dd><span class="countdown">{escape(deadline_text)}</span></dd>
</dl>
<p class="summary">{escape(rec['summary'])}</p>
{f'<ul class="quotes">{quotes}</ul>' if quotes else ''}
<p class="source"><a href="{link}" rel="noopener">Official notice</a> ({escape(rec['source_name'])})</p>
</article>"""


def render_index(records: list[dict], site: dict, updated: date, draft: bool = False) -> str:
    title = site.get("title", "Marine Project Finder: Canada")
    desc = site.get("description", "")
    n_open = sum(1 for r in records if r["stage_kind"] == "open")
    cards = "\n".join(_card(r, draft) for r in records)
    if not records:
        cards = ('<p class="empty">No projects have been published yet. Items appear here after a person '
                 "reviews and approves them.</p>")
    draft_note = ('<p class="draft"><strong>Draft preview.</strong> This shows every candidate, approved or not. '
                  "It is not public. Approve items in review/review.csv, then publish.</p>") if draft else ""
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(title, desc)}
</head>
<body data-soon="{int(site.get('closing_soon_days', 30))}">
<header class="site"><div class="wrap">
<h1>{escape(title)}</h1>
<p>{escape(desc)}</p>
<p class="updated">Last updated: {updated.strftime('%B %-d, %Y')}</p>
<nav class="top"><a href="index.html">Projects</a><a href="about.html">About</a></nav>
{draft_note}
</div></header>
<main class="wrap">
<p class="summary-line" id="summary-line">{len(records)} projects shown. {n_open} with comment periods open now.</p>
<section class="soon" aria-labelledby="soon-h">
<h2 id="soon-h">Comment deadlines closing soon (next {int(site.get('closing_soon_days', 30))} days)</h2>
<ul id="soon-list"></ul>
<p id="soon-none">No comment deadlines in the next {int(site.get('closing_soon_days', 30))} days.</p>
<noscript><p>Turn on JavaScript to see live countdowns, filters and search.</p></noscript>
</section>
<form class="filters" onsubmit="return false" aria-label="Filter projects">
<label>Province<select id="f-province"><option value="">All provinces</option></select></label>
<label>Project type<select id="f-type"><option value="">All types</option></select></label>
<label>Stage<select id="f-stage"><option value="">All stages</option>
<option value="open">Comment period open</option><option value="closed">Comment period closed</option>
<option value="assessment">Under assessment</option><option value="decided">Decision made</option>
<option value="unknown">Stage unknown</option></select></label>
<label>Search<input type="search" id="f-search" placeholder="Name, place, proponent"></label>
<label class="check"><input type="checkbox" id="f-open"> Comment period open only</label>
</form>
<p class="empty" id="no-match" hidden>No projects match these filters.</p>
<div class="cards">
{cards}
</div>
</main>
{_footer(site)}
<script>{JS}</script>
</body>
</html>
"""


def render_about(site: dict, updated: date) -> str:
    title = "About: " + site.get("title", "Marine Project Finder: Canada")
    sources = "".join(f'<li><a href="{escape(s["url"])}" rel="noopener">{escape(s["name"])}</a></li>'
                      for s in site.get("sources", []))
    freq = escape(site.get("update_frequency", "every 2 weeks"))
    return f"""<!doctype html>
<html lang="en">
<head>
{_head(title, site.get('description', ''))}
</head>
<body>
<header class="site"><div class="wrap">
<h1>About this prototype</h1>
<p>{escape(site.get('description', ''))}</p>
<p class="updated">Last updated: {updated.strftime('%B %-d, %Y')}</p>
<nav class="top"><a href="index.html">Projects</a><a href="about.html">About</a></nav>
</div></header>
<main class="wrap about">
<h2>What it does</h2>
<p>The tool reads public environmental assessment and permit notices for marine construction projects in
Canada, such as ports, wharves, breakwaters, dredging and shoreline works. It flags projects that may need
marine habitat work, for example avoidance and reduction measures, habitat enhancement, fish habitat
offsetting, or nature-inclusive design. Each listing links to the official notice.</p>

<h2>What the score and confidence mean</h2>
<p>Each notice gets a score from 0 to 100 from simple, visible rules: the type of marine work, words about
fish habitat, offsetting, eelgrass, shellfish or species at risk, and the size of the work. The confidence
label (High, Medium or Low) sums this up and is checked by a person. A high score means the notice is worth
a closer look. It does not mean that mitigation or compensation is required. Only the regulator decides that.</p>
<p>The short quotes on each listing are the exact sentences from the notice that led to the flag, so you can
judge for yourself.</p>

<h2>How items are reviewed</h2>
<p>A person reads every candidate before it appears here. Items that are not reviewed and approved stay off
this site. Summaries are kept short and factual, and facts that a notice does not give are shown as
"unknown".</p>

<h2>Public sources used</h2>
<ul>{sources}</ul>
<p>More provinces and countries may be added later.</p>

<h2>How often it updates</h2>
<p>The list is refreshed {freq}. Comment countdowns are calculated in your browser, so they stay current
between updates.</p>

<h2>Known limits</h2>
<ul>
<li>Public notices often appear after a project's design is done, so some listings will be too late to
influence the design.</li>
<li>Some size readings can be wrong, for example when a notice also mentions a road or pipeline length.</li>
<li>Not every province is covered yet.</li>
<li>Notices in French are quoted in French.</li>
</ul>
</main>
{_footer(site)}
</body>
</html>
"""


def write_site(out_dir: Path, records: list[dict], site_conf: dict, updated: date, draft: bool = False) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_index(records, site_conf, updated, draft), encoding="utf-8")
    (out_dir / "about.html").write_text(render_about(site_conf, updated), encoding="utf-8")
    if not draft:
        public = [{k: v for k, v in r.items() if k != "review"} for r in records]
        (out_dir / "items.json").write_text(json.dumps(public, ensure_ascii=False, indent=1), encoding="utf-8")
        (out_dir / ".nojekyll").write_text("", encoding="utf-8")   # tells GitHub Pages to serve files as-is


def load_previous(out_dir: Path) -> dict:
    path = out_dir / "items.json"
    if not path.exists():
        return {}
    try:
        return {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
    except (ValueError, KeyError):
        return {}
