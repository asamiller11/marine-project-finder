---
description: Refresh every source, rebuild and publish the Marine Project Finder site, then report
---
Run the one-button update for this project and report the result.

1. From the project folder, run: `./.venv/bin/python -W ignore -m mpf update`
   (it fetches every live source politely, scores, updates review/review.csv, builds site/,
   uploads review/review.csv and site/ to GitHub, and checks the live site).
   It can take 15 to 30 minutes because of crawl delays; run it in the background and wait.
   If the user asked for a dry run, add `--no-upload`.
2. Report, briefly:
   - the items published per country and tier (from the "Update finished" block),
   - every source that was blocked or failed, with the reason from the output,
   - whether the site is live at https://asamiller11.github.io/marine-project-finder/ and whether
     its item count matches.
3. If the command printed "UPDATE STOPPED", show that message and the likely fix.
   Never work around a blocked source (no other headers, browsers or proxies).
