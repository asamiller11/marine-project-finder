# How to review and publish

Nothing appears on the public website until you approve it. This guide shows how.

## 1. Get the latest candidates

Either:

- **On your computer:** open `start.py` in VS Code, press Run, and choose
  "Fetch new notices", then "Score", then "Make the review file".
- **From GitHub (after setup):** go to your repository on github.com, click **Actions**,
  click the latest **Refresh and publish** run, scroll to **Artifacts**, and download
  **review-file**. Unzip it to get `review.csv`.

Either way you get `review/review.csv` and, on your computer, a draft preview at
`review/draft/index.html` (double-click it to open in your browser). The draft shows every
candidate with an "Approved" or "Not approved" badge. It is not public.

## 2. Open the review file

Open `review/review.csv` in Numbers, Excel or Google Sheets. Each row is one project:

| Column | What to do |
|---|---|
| id | Leave it alone. |
| title | The project name. |
| score | The tool's score (0 to 100). Higher means more habitat-work signals. |
| confidence | High, Medium or Low. Change it if you disagree. |
| **publish** | Type **yes** to publish, or leave **no**. |
| summary | One or two sentences shown on the site. Edit freely. Keep it factual. |
| stage, comment_deadline, province, url | For reference. Click the url to read the notice. |

Before typing yes, open the official notice and check that:
- the summary only says things the notice says,
- it does not claim mitigation "will be required",
- the project really is marine construction.

## 3. Save it

Save as **CSV** (not .numbers or .xlsx), with the same name, in the `review` folder.
In Numbers: File > Export To > CSV. In Excel: File > Save As > CSV UTF-8.

## 4. Publish

- **On your computer:** in the `start.py` menu choose **Publish the website**. This builds
  the `site/` folder from the approved rows only. Choose **Open the published site** to look
  at it.
- **To put it online with GitHub:** choose **Upload to GitHub** in the menu (or run
  `git add review/review.csv site && git commit -m "Update approved items" && git push`
  in the project folder). GitHub publishes it within a few minutes.
- **Without GitHub:** choose **Zip the site for Netlify Drop**, then drag `site.zip` onto
  https://app.netlify.com/drop to get a link.

## Your decisions are kept

When the tool refreshes, it keeps your yes/no choices, confidence labels and summaries.
New projects arrive with publish set to no.
