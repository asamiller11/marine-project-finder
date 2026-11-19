"""Health check for the readers, run by its own LaunchAgent, separate from the weekly update.

    python -m mpf health              # run the check now (prints the result, notifies on a problem)
    python -m mpf health-install      # install the daily LaunchAgent
    python -m mpf health-uninstall    # remove it
    python -m mpf health-status       # is it installed, when did it last run

What it checks (each is one line in the result):
1. The update ran on schedule: the last uploaded update is not older than update.days_between_updates
   plus health.grace_days.
2. Each source returned a normal number of notices: not less than half its usual number
   (the median of the earlier runs kept in data/ingest_runs.json).
3. No source suddenly returned zero or stopped with an error in the last run.
4. The site data updated: the live site's status.json shows the date of the last uploaded update.
5. The canary projects (health.canaries) are still on the live site.

When something is wrong it shows a macOS notification and writes data/logs/health.log.
It never changes the site, the data or the update. Its only network request is to the project's own
live site (one small file, status.json, and items.json).
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
from datetime import datetime
from pathlib import Path

from mpf.config import ROOT, data_dir

LABEL = "com.marineprojectfinder.healthcheck"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
KEEP_RUNS = 30


def runs_path(settings: dict) -> Path:
    return data_dir(settings) / "ingest_runs.json"


def status_path(settings: dict) -> Path:
    return data_dir(settings) / "health_status.json"


def log_path(settings: dict) -> Path:
    logs = data_dir(settings) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "health.log"


def load_runs(settings: dict) -> list[dict]:
    try:
        return json.loads(runs_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def record_ingest_run(settings: dict, counts: dict, exceptions_used: dict, network_requests: int,
                      now: datetime | None = None) -> None:
    """Called at the end of every live ingest: what each source returned."""
    runs = load_runs(settings)
    runs.append({"at": (now or datetime.now()).isoformat(timespec="seconds"), "sources": counts,
                 "robots_exceptions_used": dict(exceptions_used), "network_requests": network_requests})
    runs_path(settings).write_text(json.dumps(runs[-KEEP_RUNS:], indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# The checks. Each returns (ok, message). Kept free of network calls so they can be tested.
# ---------------------------------------------------------------------------

def check_schedule(state: dict, now: datetime, days_between: int, grace_days: int, paused: bool) -> tuple[bool, str]:
    last = state.get("last_success")
    if not last:
        return False, "No uploaded update has ever finished."
    try:
        age = (now - datetime.fromisoformat(last)).total_seconds() / 86400
    except ValueError:
        return False, f"The last update date cannot be read: {last}"
    limit = days_between + grace_days
    if age <= limit:
        return True, f"Last uploaded update {last[:10]} ({age:.0f} days ago; due every {days_between} days)."
    why = " Automatic updates are paused." if paused else ""
    return False, f"No uploaded update for {age:.0f} days (last {last[:10]}; expected every {days_between} days).{why}"


def check_sources(runs: list[dict], min_share: float = 0.5, min_usual: int = 4) -> list[tuple[bool, str]]:
    """Per source in the last run: an error, zero when there used to be some, or under half the usual number."""
    if not runs:
        return [(False, "No ingest run has been recorded yet (data/ingest_runs.json is empty).")]
    last, earlier = runs[-1], runs[:-1][-5:]
    out = []
    for name, now in sorted(last.get("sources", {}).items()):
        before = [r["sources"][name]["items"] for r in earlier if name in r.get("sources", {})]
        usual = statistics.median(before) if before else None
        n = now.get("items", 0)
        if now.get("problem"):
            out.append((False, f"{name}: stopped early or failed in the last run ({n} notices)."))
        elif n == 0 and usual:
            out.append((False, f"{name}: returned 0 notices; it usually returns {usual:g}."))
        elif usual is not None and usual >= min_usual and n < min_share * usual:
            out.append((False, f"{name}: returned {n} notices; it usually returns {usual:g}."))
        else:
            out.append((True, f"{name}: {n} notices" + (f" (usually {usual:g})." if usual is not None else ".")))
    return out


def check_site_status(live_status: dict | None, state: dict) -> tuple[bool, str]:
    if live_status is None:
        return False, "Could not read the live site's status.json."
    live = (live_status.get("last_successful_update") or "")[:10]
    expected = (state.get("last_success") or "")[:10]
    if live and expected and live >= expected:
        return True, f"Live site data is from {live}, matching the last uploaded update."
    return False, f"Live site data is from {live or 'an unknown date'}, but the last uploaded update was {expected}."


def check_canaries(live_items: list[dict] | None, canaries: list[str]) -> list[tuple[bool, str]]:
    if live_items is None:
        return [(False, "Could not read the live site's items.json, so the canary projects were not checked.")]
    titles = [(r.get("title") or "").lower().replace("’", "'") for r in live_items]
    out = []
    for c in canaries:
        key = c.lower().replace("’", "'")
        found = any(key in t for t in titles)
        out.append((found, f"Canary '{c}': {'on the live site' if found else 'MISSING from the live site'}."))
    return out


ROLE_KEYS = ("owner", "consultant", "contractor", "agency_contact")


def check_new_fields(live_items: list[dict] | None, live_status: dict | None) -> list[tuple[bool, str]]:
    """Comment deadlines, stakeholders and the winnability fields on the live site (added 2026-11-19)."""
    if live_items is None:
        return [(False, "Could not read the live site's items.json, so the comment, stakeholder and score fields were not checked.")]
    built = ((live_status or {}).get("last_successful_update") or "")[:10]
    out = []
    no_roles = [r.get("title", "?") for r in live_items if set(ROLE_KEYS) - set((r.get("stakeholders") or {}))]
    bad_names = [r.get("title", "?") for r in live_items for es in (r.get("stakeholders") or {}).values()
                 for e in es if not (e.get("name") or "").strip() or not e.get("how")]
    out.append((not no_roles and not bad_names,
                "Stakeholder fields: " + ("present on every project, each name with its source." if not no_roles and not bad_names
                                          else f"{len(no_roles)} projects missing roles, {len(bad_names)} names without a source "
                                               f"(first: {(no_roles or bad_names)[0]}).")))
    no_entry = [r.get("title", "?") for r in live_items if r.get("tier") != "watch" and not (r.get("win") or {}).get("entry_point")]
    out.append((not no_entry, "Entry point and score reasons: " + ("on every project." if not no_entry else
                                                                    f"missing on {len(no_entry)} (first: {no_entry[0]}).")))
    open_recs = [r for r in live_items if r.get("stage_kind") == "open"]
    stale = [r.get("title", "?") for r in open_recs if not r.get("comment_deadline")
             or (built and r["comment_deadline"] < built)]
    out.append((not stale, f"Open for comment labels: {len(open_recs)} projects, " +
                ("every one with a deadline on or after the update date." if not stale else
                 f"{len(stale)} without a valid deadline (first: {stale[0]}).")))
    filled = {k: sum(1 for r in live_items if (r.get("stakeholders") or {}).get(k)) for k in ROLE_KEYS}
    out.append((True, f"Names found: owner {filled['owner']}, consultant {filled['consultant']}, contractor "
                      f"{filled['contractor']}, agency contact {filled['agency_contact']} of {len(live_items)} projects."))
    return out


def check_research(live_items: list[dict] | None) -> list[tuple[bool, str]]:
    """The research files follow the rules (every fact and name linked to a source, no personal-data sites), and the
    live site carries the research fields (added 2026-11-19)."""
    from mpf import research
    projects, orgs = research.load_research()
    bad = research.problems(projects, orgs)
    out = [(not bad, "Research files: " + ("every fact and name has a source link." if not bad
                                           else f"{len(bad)} problems (first: {bad[0]})."))]
    if live_items is not None:
        listed = [r for r in live_items if r.get("id") in projects]
        missing = [r["title"] for r in listed if not r.get("research")]
        n_proc = sum(1 for r in live_items if any(f.get("kind") == "procurement" for f in (r.get("research") or {}).get("facts") or []))
        out.append((not missing, f"Research on the live site: {len(listed)} researched projects, {n_proc} with a procurement route"
                    + ("." if not missing else f"; {len(missing)} missing their research (first: {missing[0]}).")))
    return out


def check_comment_feed(site_dir: Path) -> tuple[bool, str]:
    """The comment-periods RSS feed was written and is valid XML."""
    import xml.etree.ElementTree as ET
    path = site_dir / "feeds" / "comment-periods.xml"
    try:
        n = len(ET.parse(path).getroot().findall("./channel/item"))
    except (OSError, ET.ParseError) as e:
        return False, f"Comment-periods RSS feed missing or broken ({e.__class__.__name__})."
    return True, f"Comment-periods RSS feed: {n} entries."


def _fetch_json(url: str):
    import time
    import requests
    try:
        resp = requests.get(url, timeout=30, params={"t": int(time.time())},
                            headers={"Cache-Control": "no-cache", "User-Agent": "MitiMark health check"})
        return resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def run_health(settings: dict, now: datetime | None = None, fetch=_fetch_json, notify_fn=None) -> dict:
    from mpf.autoupdate import notify, paths as auto_paths
    from mpf.update import SITE_URL, read_state
    now = now or datetime.now()
    conf = settings.get("health") or {}
    state = read_state(settings)
    days_between = int((settings.get("update") or {}).get("days_between_updates", 7))
    checks = [check_schedule(state, now, days_between, int(conf.get("grace_days", 1)),
                             auto_paths(settings)["paused"].exists())]
    checks += check_sources(load_runs(settings), float(conf.get("min_share_of_usual", 0.5)))
    site = settings.get("site", {}).get("url") or SITE_URL
    live_status = fetch(site + "status.json")
    checks.append(check_site_status(live_status, state))
    live_items = fetch(site + "items.json")
    checks += check_canaries(live_items, conf.get("canaries") or [])
    checks += check_new_fields(live_items, live_status)
    checks += check_research(live_items)
    checks.append(check_comment_feed(Path(settings.get("site_dir") or ROOT / "site")))
    problems = [m for ok, m in checks if not ok]
    result = {"at": now.isoformat(timespec="seconds"), "ok": not problems, "problems": problems,
              "checks": [m for _, m in checks]}
    status_path(settings).write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    with open(log_path(settings), "a", encoding="utf-8") as f:
        f.write(f"{result['at']}  {'OK' if result['ok'] else 'PROBLEM'}  " + " | ".join(problems or ["all checks passed"]) + "\n")
    if problems:
        (notify_fn or notify)("MitiMark health check", f"{len(problems)} problem(s): " + problems[0])
    return result


def stale_health_warning(settings: dict, now: datetime, max_days: int = 3) -> str | None:
    """Used by the hourly update check: a warning when the health check itself has not run for max_days."""
    if "health" not in settings or not PLIST.exists():
        return None
    try:
        last = json.loads(status_path(settings).read_text(encoding="utf-8"))["at"]
        age = (now - datetime.fromisoformat(last)).days
    except (OSError, ValueError, KeyError):
        return "The MitiMark health check has never run."
    if age >= max_days:
        return f"The MitiMark health check has not run for {age} days."
    return None


# ---------------------------------------------------------------------------
# The LaunchAgent: once a day at health.hour (default 9:00). If the Mac is asleep then, macOS runs it when
# the Mac wakes; if the Mac is off, that day's check is skipped.
# ---------------------------------------------------------------------------

def plist_text(settings: dict) -> str:
    hour = int((settings.get("health") or {}).get("hour", 9))
    logs = data_dir(settings) / "logs"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{VENV_PYTHON}</string>
    <string>-W</string><string>ignore</string>
    <string>-m</string><string>mpf</string><string>health</string>
  </array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>0</integer></dict>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>{Path.home()}</string>
  </dict>
  <key>StandardOutPath</key><string>{logs / 'health.out.log'}</string>
  <key>StandardErrorPath</key><string>{logs / 'health.err.log'}</string>
</dict>
</plist>
"""


def install(settings: dict) -> str:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    (data_dir(settings) / "logs").mkdir(parents=True, exist_ok=True)
    PLIST.write_text(plist_text(settings), encoding="utf-8")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    proc = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], capture_output=True, text=True)
    if proc.returncode != 0:
        return f"Wrote {PLIST}, but launchctl could not load it: {proc.stderr.strip()}"
    return f"Installed and loaded {PLIST} (runs daily)."


def uninstall() -> str:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if PLIST.exists():
        PLIST.unlink()
        return f"Unloaded and deleted {PLIST}."
    return "The health check LaunchAgent was not installed."


def status(settings: dict) -> str:
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0
    try:
        last = json.loads(status_path(settings).read_text(encoding="utf-8"))
        last_line = f"{last['at']} ({'OK' if last['ok'] else 'problems: ' + '; '.join(last['problems'])})"
    except (OSError, ValueError, KeyError):
        last_line = "never"
    return "\n".join([f"LaunchAgent file: {'present' if PLIST.exists() else 'not installed'} ({PLIST})",
                      f"Loaded in launchd: {'yes' if loaded else 'no'}",
                      f"Last health check: {last_line}",
                      f"Log: {log_path(settings)}"])
