# How publishing works

Every scored project is published automatically, grouped into tiers. You do not have to
approve anything. You can mark projects you have checked, and hide ones that are wrong.

## What visitors see

| Tier | Score | Shown |
|---|---|---|
| Strong match | 60 or more | Always |
| Possible | 35 to 59 | Always |
| Weak | 20 to 34 | When "Show weaker matches" is ticked |
| Probably not relevant | under 20, or a quarry, pit or withdrawn project | When "Show probably not relevant" is ticked |

You can change the score thresholds and the quarry/pit rules in `config/settings.yaml`
under `tiers:`.

Each project has one of two badges:
- **Reviewed by a person**: you set it to yes in the review file.
- **Automatic, not yet reviewed**: everything else.

Also:
- The same project listed by two registries is shown once (the Newfoundland and
  Labrador entry is kept over the federal one), with an "Also listed" link.
- Projects whose official page did not load, or showed a different project, are left out.

Handy links: add `?show=weak` (or `?show=weak,notrel`) to the web address to open the page
with those toggles already ticked.

## Marking projects in the review file

1. Get the latest `review/review.csv`:
   - **On your computer:** in the `start.py` menu, choose "Fetch new notices", "Score", then
     "Make the review file".
   - **From GitHub:** Actions tab > latest **Refresh and publish** run > **Artifacts** >
     download **review-file** and unzip it.
2. Open it in Numbers, Excel or Google Sheets. Every scored project is a row, with its
   country in the `country` column (CA, DK, ES, FR, GB) so you can sort or filter by country.
3. In the **publish** column type:
   - **yes** if you read the official notice and the listing is right,
   - **no** to hide a project everywhere (wrong, irrelevant or misleading),
   - or leave it **blank** to keep it as an automatic listing.
4. Optionally improve the **summary** column. Keep it factual, and never claim mitigation
   "will be required".
5. Save as **CSV** with the same name in the `review` folder.
   Numbers: File > Export To > CSV. Excel: File > Save As > CSV UTF-8.

Your yes, no and summary edits are kept every time the tool refreshes.

## Putting your changes online

- **GitHub:** in the `start.py` menu choose **Upload to GitHub**. GitHub rebuilds and publishes
  the site within a few minutes.
- **On your computer only:** choose **Publish the website**, then **Open the published site**.
- **Without GitHub:** choose **Zip the site for Netlify Drop** and drag `site.zip` onto
  https://app.netlify.com/drop.

## Automatic refresh

GitHub refreshes the data on the 1st and 15th of each month (about every 2 weeks), so new
notices appear on their own. You can also start it any time: Actions tab >
**Refresh and publish** > **Run workflow**.
