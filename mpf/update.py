"""One-button update: fetch, score, review file, publish, upload, then check the live site.

    python -m mpf update               # the full update
    python -m mpf update --no-upload   # everything except the upload (nothing leaves your Mac)

Safety rules:
- Only one update can run at a time (a lock file in data/).
- No internet: stops before doing anything, with a clear message.
- A blocked or broken source: the other sources still run and are published, and the update
  ends with a clear warning (exit code 2). Set update.stop_on_blocked_source: true in
  config/settings.yaml to stop without publishing instead.
- After a successful run the date is saved in data/last_update.json; the automatic Mac check
  uses it to decide when the next update is due (every 14 days).
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from mpf.config import ROOT, data_dir

# Absolute paths, so the update behaves the same in Terminal and under launchd.
# (This Mac has two gh programs; we always use /usr/local/bin/gh.)
GIT = os.environ.get("MPF_GIT") or "/usr/bin/git"
GH = os.environ.get("MPF_GH") or "/usr/local/bin/gh"
GH_CONFIG_DIR = os.environ.get("GH_CONFIG_DIR") or str(Path.home() / ".gh-config")
SITE_URL = "https://asamiller11.github.io/marine-project-finder/"
REPO = "asamiller11/marine-project-finder"
TIER_ORDER = ("strong", "possible", "weak")


class UpdateError(Exception):
    """A problem that stops the update. The message is shown to the user as is."""


# ---------------------------------------------------------------------------
# Small helpers (kept separate so they can be tested without a network)
# ---------------------------------------------------------------------------

def state_path(settings: dict) -> Path:
    return data_dir(settings) / "last_update.json"


def read_state(settings: dict) -> dict:
    try:
        return json.loads(state_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(settings: dict, state: dict) -> None:
    state_path(settings).write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def is_online(host: str = "github.com", port: int = 443, timeout: float = 5.0) -> bool:
    """True if we can open a connection to GitHub (which the upload needs anyway)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class UpdateLock:
    """Never two updates at once: an exclusive lock on data/update.lock, released on exit."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "w")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise UpdateError("Another update is already running. Not starting a second one.")
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def counts_by_country(records: list[dict]) -> dict:
    c = Counter((r.get("country") or "unknown", r.get("tier")) for r in records)
    out = {}
    for country in sorted({k[0] for k in c}):
        out[country] = {t: c[(country, t)] for t in TIER_ORDER if c[(country, t)]}
    return out


def format_counts(counts: dict) -> str:
    if not counts:
        return "  (no items published)"
    lines = []
    for country, tiers in counts.items():
        parts = ", ".join(f"{t.capitalize()} {n}" for t, n in tiers.items())
        lines.append(f"  {country}: {parts} (total {sum(tiers.values())})")
    return "\n".join(lines)


def _step(n: int, total: int, text: str) -> None:
    print(f"\n[{n}/{total}] {text}", flush=True)


def _run(args: list[str], what: str, env: dict | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=capture)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:] if capture else ""
        raise UpdateError(f"{what} failed (exit code {proc.returncode}). {detail}".strip())
    return proc


def _python() -> str:
    return sys.executable


# ---------------------------------------------------------------------------
# The update
# ---------------------------------------------------------------------------

def run_update(settings: dict, upload: bool = True) -> int:
    """Returns 0 (all good), 2 (published, but some sources failed). Raises UpdateError to stop."""
    total = 6 if upload else 5
    with UpdateLock(data_dir(settings) / "update.lock"):
        _step(1, total, "Checking the internet connection")
        if not is_online():
            raise UpdateError("No internet connection (cannot reach github.com). Nothing was changed. Try again later.")
        print("  Online.")

        _step(2, total, "Fetching new notices from every live source (polite, cached; Scotland waits 10 s per page)")
        proc = subprocess.run([_python(), "-W", "ignore", "-m", "mpf", "ingest", "--live"], cwd=ROOT,
                              text=True, capture_output=True)
        print(proc.stdout.rstrip())
        if proc.returncode != 0:
            raise UpdateError(f"Fetching failed (exit code {proc.returncode}). {proc.stderr.strip()[-400:]}")
        problems = []
        for line in proc.stdout.splitlines():
            if line.startswith("Sources that stopped early or failed:"):
                problems = [p.strip() for p in line.split(":", 1)[1].split(".")[0].split(",") if p.strip()]
        if problems and settings.get("update", {}).get("stop_on_blocked_source"):
            raise UpdateError(f"Stopped before publishing: these sources were blocked or failed: {', '.join(problems)}.")

        _step(3, total, "Scoring")
        _run([_python(), "-W", "ignore", "-m", "mpf", "score"], "Scoring")
        _step(4, total, "Updating the review file (your yes/no decisions are kept)")
        _run([_python(), "-W", "ignore", "-m", "mpf", "review"], "Updating the review file")
        _step(5, total, "Building the website")
        _run([_python(), "-W", "ignore", "-m", "mpf", "publish"], "Building the website")
        records = json.loads((ROOT / "site" / "items.json").read_text(encoding="utf-8"))
        counts = counts_by_country(records)

        live = "not uploaded (--no-upload): the live site was not changed"
        if upload:
            _step(6, total, "Uploading the review file and site to GitHub, then checking the live site")
            live = upload_and_check(len(records))

        state = read_state(settings)
        stamp = datetime.now().isoformat(timespec="seconds")
        # Only a real, uploaded update resets the 14-day clock; a dry run is recorded separately.
        state["last_success" if upload else "last_dry_run"] = stamp
        state.update({"counts": counts, "problems": problems, "live": live})
        write_state(settings, state)

        print("\n=== Update finished ===")
        print("Items published, per country and tier:")
        print(format_counts(counts))
        print("Sources blocked or failed: " + (", ".join(problems) if problems else "none"))
        print("Live site: " + live)
        return 2 if problems else 0


def _git_env() -> dict:
    env = dict(os.environ)
    env["GH_CONFIG_DIR"] = GH_CONFIG_DIR
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def ensure_credential_helper(env: dict) -> None:
    """git asks gh for the GitHub login. Pin the helper to the absolute gh path and the
    GH_CONFIG_DIR where the login lives, so it works under launchd too."""
    helper = f"!GH_CONFIG_DIR={GH_CONFIG_DIR} {GH} auth git-credential"
    key = "credential.https://github.com.helper"
    current = subprocess.run([GIT, "config", "--local", "--get-all", key], cwd=ROOT, env=env,
                             text=True, capture_output=True).stdout.split("\n")
    if helper not in current:
        subprocess.run([GIT, "config", "--local", "--unset-all", key], cwd=ROOT, env=env, capture_output=True)
        subprocess.run([GIT, "config", "--local", "--add", key, ""], cwd=ROOT, env=env, check=True)
        subprocess.run([GIT, "config", "--local", "--add", key, helper], cwd=ROOT, env=env, check=True)


def upload_and_check(expected_items: int, wait_minutes: int = 15) -> str:
    env = _git_env()
    ensure_credential_helper(env)
    _run([GIT, "add", "review/review.csv", "site"], "git add", env=env, capture=True)
    staged = subprocess.run([GIT, "diff", "--cached", "--quiet"], cwd=ROOT, env=env)
    if staged.returncode == 0:
        return "no changes since the last upload, so nothing was pushed (the live site is already current)"
    msg = f"Update site ({datetime.now().strftime('%Y-%m-%d')} refresh)"
    _run([GIT, "commit", "-q", "-m", msg], "git commit", env=env, capture=True)
    _run([GIT, "push", "-q"], "git push", env=env, capture=True)
    sha = subprocess.run([GIT, "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True).stdout.strip()
    print(f"  Pushed {sha[:7]}. Waiting for GitHub to publish (usually 1 to 3 minutes)...", flush=True)
    deadline = time.time() + wait_minutes * 60
    while time.time() < deadline:
        time.sleep(30)
        out = subprocess.run([GH, "run", "list", "-R", REPO, "--workflow", "refresh.yml", "--limit", "5",
                              "--json", "headSha,status,conclusion"], cwd=ROOT, env=env, text=True,
                             capture_output=True)
        try:
            runs = [r for r in json.loads(out.stdout or "[]") if r.get("headSha") == sha]
        except ValueError:
            runs = []
        if runs and runs[0].get("status") == "completed":
            if runs[0].get("conclusion") != "success":
                raise UpdateError(f"GitHub's publish run finished with '{runs[0].get('conclusion')}'. "
                                  f"See the Actions tab on GitHub.")
            break
    else:
        return f"pushed, but GitHub had not finished publishing after {wait_minutes} minutes; check the Actions tab"
    return check_live_site(expected_items)


def check_live_site(expected_items: int) -> str:
    import requests
    try:
        resp = requests.get(SITE_URL + "items.json", timeout=30,
                            headers={"Cache-Control": "no-cache"}, params={"t": int(time.time())})
        live_count = len(resp.json()) if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        live_count = None
    if live_count is None:
        return f"could not read {SITE_URL} just now; check it in a browser"
    if live_count == expected_items:
        return f"live at {SITE_URL} with {live_count} items (matches this update)"
    return (f"live at {SITE_URL}, but it shows {live_count} items instead of {expected_items}; "
            f"GitHub Pages can take a few more minutes to refresh")
