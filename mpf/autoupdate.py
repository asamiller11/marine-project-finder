"""Automatic updates on this Mac, through a LaunchAgent for your user only (no sudo).

    python -m mpf auto-check        # what the LaunchAgent runs: update only if 14+ days have passed
    python -m mpf auto-install      # install the LaunchAgent
    python -m mpf auto-uninstall    # remove it completely
    python -m mpf auto-status       # is it installed, paused, when was the last update
    python -m mpf auto-pause / auto-resume

When the check runs: at login, and then every hour. macOS has no "run on wake" trigger for a
user LaunchAgent, but an hourly job that was missed during sleep runs once right after the Mac
wakes, so a check happens soon after every wake. Each check is quick: it reads the date of the
last successful update and exits unless 14 or more days have passed, you are online, and
automatic updates are not paused. The update itself runs at low priority (nice), with
"caffeinate -i" keeping the Mac awake only until it finishes. Failures are written to the log
and shown as a macOS notification.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from mpf.config import ROOT, data_dir
from mpf.update import is_online, read_state

LABEL = "com.marineprojectfinder.autoupdate"
# Always the project's own Python (the venv with its packages), named by absolute path.
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DAYS_BETWEEN_UPDATES = 14
CHECK_EVERY_SECONDS = 3600


def paths(settings: dict) -> dict:
    d = data_dir(settings)
    logs = d / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return {"paused": d / "auto_update_paused", "log": logs / "auto_update.log",
            "launchd_out": logs / "launchd.out.log", "launchd_err": logs / "launchd.err.log"}


def due_for_update(last_success: str | None, now: datetime, days: int = DAYS_BETWEEN_UPDATES) -> bool:
    """True when there has never been an update, or the last one is `days` or more days old."""
    if not last_success:
        return True
    try:
        last = datetime.fromisoformat(last_success)
    except ValueError:
        return True
    return (now - last).days >= days


def log(settings: dict, message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}  {message}\n"
    with open(paths(settings)["log"], "a", encoding="utf-8") as f:
        f.write(line)


def notify(title: str, message: str) -> None:
    """A macOS notification (Notification Center). Silently does nothing if it cannot."""
    safe = message.replace('"', "'")[:200]
    try:
        subprocess.run(["/usr/bin/osascript", "-e", f'display notification "{safe}" with title "{title}"'],
                       check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def update_command(upload: bool = True) -> list[str]:
    """The update, at low priority, keeping the Mac awake only while it runs."""
    cmd = ["/usr/bin/caffeinate", "-i", "/usr/bin/nice", "-n", "10",
           str(VENV_PYTHON), "-W", "ignore", "-m", "mpf", "update"]
    return cmd if upload else cmd + ["--no-upload"]


def once_path(settings: dict) -> Path:
    """A one-time instruction for the next check (used to test the agent under launchd):
    {"force": true, "upload": false}. The check reads it, deletes it, and obeys it once."""
    return data_dir(settings) / "auto_check_once.json"


def write_once(settings: dict, force: bool, upload: bool) -> None:
    once_path(settings).write_text(json.dumps({"force": force, "upload": upload}), encoding="utf-8")


def take_once(settings: dict) -> dict:
    p = once_path(settings)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        data = {}
    p.unlink()
    return data


def auto_check(settings: dict, now: datetime | None = None, run=subprocess.run, online=is_online) -> str:
    """Decide whether to update, and do it. Returns a short word for logs and tests."""
    now = now or datetime.now()
    p = paths(settings)
    once = take_once(settings)
    if once:
        log(settings, f"One-time instruction: force={once.get('force')}, upload={once.get('upload', True)}")
    upload = bool(once.get("upload", True))
    if p["paused"].exists() and not once.get("force"):
        return "paused"
    if not once.get("force") and not due_for_update(read_state(settings).get("last_success"), now):
        log(settings, "Check: not due yet.")
        return "not due"
    if not online():
        log(settings, "Update due, but offline. Will try again at the next check.")
        return "offline"
    log(settings, f"Update due. Starting ({'with' if upload else 'without'} upload). "
                  f"Environment: GH_CONFIG_DIR={os.environ.get('GH_CONFIG_DIR')} PATH={os.environ.get('PATH')}")
    with open(p["log"], "a", encoding="utf-8") as out:
        proc = run(update_command(upload), cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT)
    code = getattr(proc, "returncode", 1)
    if code == 0:
        log(settings, "Update finished successfully.")
        return "updated"
    if code == 2:
        state = read_state(settings)
        msg = "Published, but these sources were blocked or failed: " + ", ".join(state.get("problems", []) or ["unknown"])
        log(settings, msg)
        notify("Marine Project Finder", msg)
        return "updated with problems"
    msg = f"Automatic update failed (exit code {code}). Details: {p['log']}"
    log(settings, msg)
    notify("Marine Project Finder", msg)
    return "failed"


def kick(settings: dict, upload: bool) -> str:
    """Start the installed agent now under launchd, forced due, with upload on or off."""
    if not PLIST.exists():
        return "The LaunchAgent is not installed."
    write_once(settings, force=True, upload=upload)
    proc = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        if once_path(settings).exists():
            once_path(settings).unlink()
        return f"launchctl kickstart failed: {proc.stderr.strip()}"
    return f"Started under launchd (forced due, upload {'on' if upload else 'off'}). Watch {paths(settings)['log']}"


def plist_text(settings: dict) -> str:
    p = paths(settings)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{VENV_PYTHON}</string>
    <string>-W</string><string>ignore</string>
    <string>-m</string><string>mpf</string><string>auto-check</string>
  </array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>{CHECK_EVERY_SECONDS}</integer>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>{Path.home()}</string>
    <key>GH_CONFIG_DIR</key><string>{Path.home() / '.gh-config'}</string>
    <key>MPF_GIT</key><string>/usr/bin/git</string>
    <key>MPF_GH</key><string>/usr/local/bin/gh</string>
  </dict>
  <key>StandardOutPath</key><string>{p['launchd_out']}</string>
  <key>StandardErrorPath</key><string>{p['launchd_err']}</string>
</dict>
</plist>
"""


def install(settings: dict) -> str:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(plist_text(settings), encoding="utf-8")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)   # if already loaded
    proc = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], capture_output=True, text=True)
    if proc.returncode != 0:
        return f"Wrote {PLIST}, but launchctl could not load it: {proc.stderr.strip()}"
    return f"Installed and loaded {PLIST}."


def uninstall() -> str:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    if PLIST.exists():
        PLIST.unlink()
        return f"Unloaded and deleted {PLIST}. Automatic updates are fully removed."
    return "The LaunchAgent was not installed. Nothing to remove."


def status(settings: dict) -> str:
    p = paths(settings)
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0
    state = read_state(settings)
    last = state.get("last_success") or "never"
    return "\n".join([
        f"LaunchAgent file: {'present' if PLIST.exists() else 'not installed'} ({PLIST})",
        f"Loaded in launchd: {'yes' if loaded else 'no'}",
        f"Paused: {'yes' if p['paused'].exists() else 'no'}",
        f"Last successful (uploaded) update: {last}",
        f"Last dry run (not uploaded): {state.get('last_dry_run') or 'never'}",
        f"Log: {p['log']}",
    ])


def pause(settings: dict) -> str:
    paths(settings)["paused"].write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    return "Automatic updates paused. The hourly check will do nothing until you resume."


def resume(settings: dict) -> str:
    f = paths(settings)["paused"]
    if f.exists():
        f.unlink()
    return "Automatic updates resumed."
