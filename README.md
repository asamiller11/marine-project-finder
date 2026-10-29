# Marine Project Finder

A small research prototype that reads public project registries and flags marine
construction projects (ports, wharves, breakwaters, dredging, shoreline works, small craft
harbours, offshore wind) that are **likely to need marine habitat work**: avoidance and
reduction measures, habitat enhancement, fish habitat offsetting or compensation,
nature-inclusive design, or eco-engineered structures.

It never claims mitigation will be required. Every flag shows the exact sentences that
triggered it, so a person can judge. Most projects score low, and that is expected.

Status: ingest, scoring, a review step and a static website are built. The monthly
digest is not built yet.

## Website and publishing (short version)

```bash
./.venv/bin/python -m mpf review     # writes review/review.csv and a draft preview
# mark rows publish=yes in review/review.csv (see docs/how_to_publish.md)
./.venv/bin/python -m mpf publish    # builds site/ from approved rows only
./.venv/bin/python -m mpf zip-site   # optional: site.zip for Netlify Drop
```

GitHub Actions (`.github/workflows/refresh.yml`) refreshes and publishes to GitHub Pages
on the 1st and 15th of each month. One-time setup: `docs/github_setup.md`.

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

| Source | What we read | Access check |
|---|---|---|
| Canadian Impact Assessment Registry (`iaac`) | Open comment periods page, project sitemap, project pages | robots.txt allows it; 5 second crawl delay honoured |
| Newfoundland and Labrador EA (`nl_ea`) | Project list, project pages, "Summary of Registration" PDF | robots.txt allows it |
| DFO Fisheries Act Registry CSV (`dfo_far`) | One open-data CSV, **scorer test set only** (data ends June 2023) | Open Government Licence - Canada |
| Drop folder (`drop_folder`) | Files you save by hand into `drop/` | Local files only |

Not used: DFO PATH live service (its robots.txt disallows `/arcgis/`; not used
until DFO confirms scripts may use it), Common Project Search (results
load from disallowed paths), US Army Corps of Engineers (returns HTTP 403 to scripts).
Quebec can be added by hand through the drop folder.

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
./.venv/bin/python -m mpf ingest                 # all enabled sources (first run takes about 20 minutes)
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

## Adding a new source later (Norway, Denmark, France)

1. Copy `mpf/sources/nl_ea.py` as a starting point and write a class with `name`,
   `country` and a `fetch()` that yields `Item`s (see `mpf/sources/base.py`).
2. Register it in `mpf/sources/__init__.py` and add a block under `sources:` in
   `config/settings.yaml`.
3. Add Norwegian, Danish or French keywords to `config/scoring.yaml`.
4. Add a small made-up page to `tests/samples/` that copies the real site's HTML
   structure (not its content) and add a parser test. Real third-party pages are never
   committed.

## Project layout

```
config/settings.yaml   sources, politeness, timing, LLM settings
config/scoring.yaml    keywords, points, size thresholds
mpf/http.py            polite client: robots.txt, delays, cache, stop on block
mpf/sources/           one file per source, plus the drop folder
mpf/extract.py         sizes, coordinates, water body, keyword hits
mpf/score.py           the 0 to 100 score and its reasons
mpf/timing.py          stage, days to deadline, "likely too late"
mpf/llm.py             optional summaries with quote checking
mpf/evaluate.py        precision and recall
mpf/review.py          review/review.csv: nothing is published without your yes
mpf/site.py            builds the static website in site/
review/review.csv      your publish decisions (committed, so GitHub can publish)
site/                  the built website (approved items only)
tests/                 unit tests with made-up sample pages
docs/                  how to publish, one-time GitHub setup
.github/workflows/     scheduled refresh and publish; source access check
```
