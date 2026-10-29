# Marine Project Finder

A small research prototype that reads public project registries and flags marine
construction projects (ports, wharves, breakwaters, dredging, shoreline works, small craft
harbours, offshore wind) that are **likely to need marine habitat work**: avoidance and
reduction measures, habitat enhancement, fish habitat offsetting or compensation,
nature-inclusive design, or eco-engineered structures.

It never claims mitigation will be required. Every flag shows the exact sentences that
triggered it, so a person can judge. Most projects score low, and that is expected.

Status: ingest from five live sources in four countries (mostly Canada for now), scoring in five languages, a review step and a
static website are built. The monthly digest is not built yet.

## Website and publishing (short version)

```bash
./.venv/bin/python -m mpf review     # writes review/review.csv (every scored item)
# optional: mark rows yes (checked) or no (hide) (see docs/how_to_publish.md)
./.venv/bin/python -m mpf publish    # builds site/ with every item, grouped into tiers
./.venv/bin/python -m mpf zip-site   # optional: site.zip for Netlify Drop
```

GitHub Actions (`.github/workflows/refresh.yml`) refreshes and publishes to GitHub Pages
on the 1st and 15th of each month. One-time setup: `docs/github_setup.md`.

## How to update

The site is built on this Mac and GitHub only publishes it (fallback mode, `CI_FETCH=false` on the
repository), because marine.gov.scot refuses requests from GitHub's servers but accepts this Mac.

- **One button:** open `start.py`, press Run, choose **Refresh everything and publish**. It checks the
  internet connection, fetches every live source (politely, cached), scores, updates
  `review/review.csv` (your yes/no choices are kept), builds `site/`, uploads it to GitHub, waits for
  GitHub to publish, and checks the live site. It takes 15 to 30 minutes, mostly crawl delays.
  The same command: `./.venv/bin/python -m mpf update` (add `--no-upload` for a dry run).
- **In Claude Code:** type `/update-site`; it runs the same update and reports the counts per country
  and tier, any blocked or failed source, and whether the site is live.
- **Automatically:** a LaunchAgent for your user (no sudo) checks at login and every hour (including
  right after the Mac wakes). If 14 or more days have passed since the last successful update and you
  are online, it runs the update in the background at low priority, keeping the Mac awake only until
  it finishes. Failures go to `data/logs/auto_update.log` and appear as a macOS notification. Two
  updates never run at once.
  - Install: `./.venv/bin/python -m mpf auto-install`
  - Pause or resume: `start.py` menu, or `python -m mpf auto-pause` / `auto-resume`
  - Status: `python -m mpf auto-status`
  - **Remove completely:** `./.venv/bin/python -m mpf auto-uninstall` (unloads it and deletes
    `~/Library/LaunchAgents/com.marineprojectfinder.autoupdate.plist`). Optionally also delete
    `data/logs/`, `data/last_update.json` and `data/auto_update_paused`.
- If a source is blocked, the others are still published and you get a warning. To stop without
  publishing instead, set `update.stop_on_blocked_source: true` in `config/settings.yaml`.

## Known limitations (read these first)

- **Public notices often appear after design is done**, so some leads will be late.
  Each item shows its stage and a "likely too late" mark so you can see how early the
  tool really catches things.
- Scores come from keyword and size rules. They miss projects described in unusual words
  and can over-score projects that only mention habitat in passing. Use the evidence quotes.
- Missing facts are shown as "unknown". The tool never fills gaps with guesses.
- Text is kept in its original language (English or French). The optional LLM step adds
  an English summary and says when it translated.
- Government website content is published under terms that allow non-commercial
  reproduction; commercial redistribution may need permission. The DFO CSV is under the
  Open Government Licence - Canada. Check the terms before selling anything built on this.

## Sources

Countries, regions, statuses and sources are listed in `config/coverage.yaml`, which also
drives the Coverage panel on the site.

| Source | Place | What we read | Access check |
|---|---|---|---|
| `iaac` | Canada (federal) | Open comment list, sitemap, project pages | robots.txt allows it; 5 s crawl delay |
| `nl_ea` | Newfoundland and Labrador | Project list, project pages, summary PDF | robots.txt allows it |
| `dk_hoering` | Denmark | Høringsportalen Atom feeds (all hearings and marine authorities) | Feeds offered for free use ("Brug dem frit") |
| `scot_marine` | Scotland | Licence documentation listings and document pages | robots.txt allows it; 10 s crawl delay |
| `es_boe` | Spain | BOE daily summary API, item HTML pages (Sections III and V-B) | Reuse allowed with source citation; the XML path is disallowed and never used |
| `dfo_far` | Canada | DFO Fisheries Act Registry CSV, **scorer test set only** | Open Government Licence - Canada |
| `drop_folder` | Any | Files you save by hand into `drop/` | Local files only |

Readers exist but are switched off because the sites' terms forbid reproduction without written
permission: `bc_eao` (BC Environmental Assessment Office) and `fr_eolien` (eoliennesenmer.fr).

Checked and not used (reasons are shown in the Coverage panel): Quebec's registry and Ireland's
MARA (their terms forbid reproduction), Norway's NVE and the Netherlands' Platform Participatie
(lists need JavaScript), regjeringen.no, Finland's consultation service, New Zealand's EPA and
the US Army Corps of Engineers (block automated access), French and Dutch official datasets and
the New York bulletin (disallowed by robots.txt), and others listed in the panel.


## How the tool behaves on the web

- Reads `robots.txt` before the first request to each host and obeys it, including
  `*` wildcards and `Crawl-delay`.
- Identifies itself with a clear User-Agent (set in `config/settings.yaml`).
- Waits between requests to the same host (at least 3 seconds).
- Caches every response in `data/cache/`. A project page is fetched once and never again.
  List pages are re-read at most every 20 hours to find new items.
- If a site answers 401, 403 or 429, the tool stops that source and tells you. It never
  retries with other headers, browsers or proxies.
- It only reads public pages and writes local files. It sends no email and posts nothing.

## Setup (macOS, Python 3.9 or newer)

```bash
cd marine-project-finder
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pytest          # should print "passed"
```

Every command below is run from the project folder as `./.venv/bin/python -m mpf <command>`.

## Run

```bash
./.venv/bin/python -m mpf ingest --live          # all live sources (first run takes about 30 minutes)
./.venv/bin/python -m mpf ingest --source nl_ea  # just one source
./.venv/bin/python -m mpf score                  # score everything
./.venv/bin/python -m mpf show --min-score 40    # flagged items with evidence
./.venv/bin/python -m mpf show --test-set        # DFO test-set rows instead
```

### Adding a notice by hand (Quebec, or anything else)

1. Save the notice as PDF, HTML or text into `drop/`.
2. Optionally add `drop/<same file name>.yaml` with the URL, title, province, proponent,
   comment deadline and decision date (see `drop/README.txt`).
3. Run `ingest --source drop_folder`, then `score`.

## Evaluate against your own labels

```bash
./.venv/bin/python -m mpf label-template   # writes labels/to_label.csv (no scores shown)
# Fill the "label" column with 1 (a real lead) or 0 (not a lead) for about 30 rows,
# then save the file as labels/labels.csv
./.venv/bin/python -m mpf eval             # precision, recall, a threshold sweep, misses
```

The DFO CSV is only reported as recall, because every row in it was authorized, so it
has no negative examples. Precision comes only from your hand labels.

## Optional LLM summaries

```bash
./.venv/bin/pip install anthropic
export ANTHROPIC_API_KEY=...               # never put the key in a file in this folder
./.venv/bin/python -m mpf llm --limit 5
```

For items at or above `llm.min_score`, Claude writes a 2 to 3 sentence English summary and
a confidence label (high, medium, low). The tool checks every quote the model returns
against the original text and drops any that do not match; if none match, the summary is
dropped too. The rule score is never changed by this step. The request turns on the
API's server-side fallback, so a declined request is retried on another model.

## Changing what gets flagged

All keywords (English and French), points, size thresholds and the flag threshold are in
`config/scoring.yaml`, with comments. After editing, run `score` again (no re-download
needed), then `eval` to see the effect on precision and recall.

## Adding a new source (another country or region)

1. Copy `mpf/sources/nl_ea.py` as a starting point and write a class with `name`,
   `country` and a `fetch()` that yields `Item`s (see `mpf/sources/base.py`).
2. Register it in `mpf/sources/__init__.py` and add a block under `sources:` in
   `config/settings.yaml`.
3. If the language is new, add its keywords under `language_patterns` in `config/scoring.yaml`,
   with a sample sentence for each rule in `tests/test_languages.py`.
4. Add a small made-up page to `tests/samples/` that copies the real site's HTML
   structure (not its content) and add a parser test. Real third-party pages are never
   committed.
5. Add the source to `config/coverage.yaml` and set the place's status, with a note.

## Project layout

```
config/settings.yaml   sources, politeness, timing, LLM settings
config/scoring.yaml    keywords (five languages), points, size thresholds
config/coverage.yaml   countries, regions, statuses, sources
mpf/http.py            polite client: robots.txt, delays, cache, stop on block
mpf/sources/           one file per source, plus the drop folder
mpf/extract.py         sizes, coordinates, water body, keyword hits
mpf/score.py           the 0 to 100 score and its reasons
mpf/timing.py          stage, days to deadline, "likely too late"
mpf/llm.py             optional summaries with quote checking
mpf/evaluate.py        precision and recall
mpf/review.py          review/review.csv: mark yes (checked) or no (hide)
mpf/tiers.py           tiers (Strong, Possible, Weak, Probably not relevant) and duplicate merging
mpf/coverage.py        countries, regions and coverage statuses (config/coverage.yaml)
mpf/site.py            builds the static website in site/
review/review.csv      your yes/no decisions (committed, so GitHub uses them)
site/                  the built website (Strong, Possible and Weak tiers, plus project watch)
tests/                 unit tests with made-up sample pages
docs/                  how to publish, one-time GitHub setup
.github/workflows/     scheduled refresh and publish; source access check
```

## Notices in other languages

Quotes stay in the original language. Without an API key there is no translation: the English
summary for a non-English notice is built only from the keywords and sizes found in it and is
labelled as such, or the listing says "Not translated". Keyword rules for French, Danish, Spanish,
Norwegian and Dutch are in `config/scoring.yaml` under `language_patterns`, and every rule has a
test in `tests/test_languages.py`.
