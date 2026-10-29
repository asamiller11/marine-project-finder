# One-time GitHub setup

You do steps 1 to 3 yourself. After that, Claude can do the rest from your computer.

## 1. Create a GitHub account

Go to https://github.com/signup and create a free account. Your username becomes part of
the web address: `https://YOUR-USERNAME.github.io/marine-project-finder/`.

## 2. Install the GitHub command line tool (gh)

1. Go to https://cli.github.com and click **Download for macOS**.
2. Open the downloaded file and follow the installer.
3. Open Terminal (or the terminal in VS Code) and type `gh --version`. You should see a
   version number.

## 3. Log in

In the terminal, type:

```
gh auth login
```

Choose: **GitHub.com**, **HTTPS**, **Yes** (authenticate Git), **Login with a web browser**.
Copy the code it shows, press Enter, paste the code in the browser, and approve.
Check with `gh auth status`.

## 4. What happens next (Claude does this after your OK)

1. Create a public repository called `marine-project-finder` and push the code.
2. Run the **Check source access** workflow to see if GitHub's servers can reach the
   Impact Assessment Registry and the Newfoundland and Labrador pages.
   - If both are reachable: the **Refresh and publish** workflow runs every 2 weeks on its own.
   - If either blocks GitHub: nothing is worked around. Instead the repository is set to
     fallback mode (`CI_FETCH=false`): you fetch on your computer and the menu's
     **Upload to GitHub** option publishes your `site/` folder.
3. Turn on GitHub Pages (Settings > Pages > Source: **GitHub Actions**).
4. Run the workflow once and check the web address loads.

## Checking the site

Open `https://YOUR-USERNAME.github.io/marine-project-finder/` on your laptop and phone.
If you see "404", wait two minutes and refresh. You can see each run's progress under the
**Actions** tab; a green tick means it worked.

## Good to know

- Search engines are asked not to list the site (`noindex`), but anyone with the link can
  open it.
- GitHub may pause scheduled runs in a public repository after about 60 days with no new
  commits. If the "Last updated" date stops changing, open Actions and click
  **Enable workflow**, or push any change.
- If you ever add an Anthropic API key, add it in Settings > Secrets and variables >
  Actions as `ANTHROPIC_API_KEY`, and set the variable `USE_LLM` to `true`. Never put a
  key in a file.
