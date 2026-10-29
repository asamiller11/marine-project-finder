"""One-button update and the automatic Mac check, without network or real commands."""
import json
import plistlib
from datetime import datetime

import pytest

from mpf import autoupdate, update


def _settings(tmp_path):
    return {"data_dir": str(tmp_path)}     # an absolute path, so data stays in the test folder


def test_due_for_update_every_14_days():
    now = datetime(2026, 11, 15, 9, 0)
    assert autoupdate.due_for_update(None, now)                                  # never updated
    assert autoupdate.due_for_update("2026-11-01T09:00:00", now)                 # exactly 14 days
    assert not autoupdate.due_for_update("2026-11-02T09:00:00", now)             # 13 days
    assert autoupdate.due_for_update("not a date", now)


def test_never_two_updates_at_once(tmp_path):
    lock_file = tmp_path / "update.lock"
    with update.UpdateLock(lock_file):
        with pytest.raises(update.UpdateError, match="already running"):
            with update.UpdateLock(lock_file):
                pass
    with update.UpdateLock(lock_file):     # released after the first one finished
        pass


def test_counts_and_report_text():
    recs = [{"country": "CA", "tier": "strong"}, {"country": "CA", "tier": "weak"}, {"country": "ES", "tier": "weak"}]
    counts = update.counts_by_country(recs)
    assert counts == {"CA": {"strong": 1, "weak": 1}, "ES": {"weak": 1}}
    assert "CA: Strong 1, Weak 1 (total 2)" in update.format_counts(counts)


class FakeRun:
    def __init__(self, code):
        self.code, self.calls = code, []

    def __call__(self, args, **kw):
        self.calls.append(args)
        return type("P", (), {"returncode": self.code})()


def test_auto_check_paused_not_due_offline(tmp_path):
    s = _settings(tmp_path)
    now = datetime(2026, 11, 20)
    update.write_state(s, {"last_success": "2026-11-10T08:00:00"})
    run = FakeRun(0)
    assert autoupdate.auto_check(s, now, run=run, online=lambda: True) == "not due"
    update.write_state(s, {"last_success": "2026-10-01T08:00:00"})
    assert autoupdate.auto_check(s, now, run=run, online=lambda: False) == "offline"
    autoupdate.pause(s)
    assert autoupdate.auto_check(s, now, run=run, online=lambda: True) == "paused"
    autoupdate.resume(s)
    assert run.calls == []                                   # nothing ran in any of these cases
    assert "offline" in (tmp_path / "logs" / "auto_update.log").read_text()


def test_auto_check_runs_low_priority_and_awake(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    notes = []
    monkeypatch.setattr(autoupdate, "notify", lambda title, msg: notes.append(msg))
    run = FakeRun(0)
    assert autoupdate.auto_check(s, datetime(2026, 11, 20), run=run, online=lambda: True) == "updated"
    cmd = run.calls[0]
    assert cmd[:5] == ["/usr/bin/caffeinate", "-i", "/usr/bin/nice", "-n", "10"]
    assert cmd[-2:] == ["mpf", "update"]
    assert notes == []                                       # no notification when it worked


def test_auto_check_failure_is_logged_and_notified(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    notes = []
    monkeypatch.setattr(autoupdate, "notify", lambda title, msg: notes.append(msg))
    assert autoupdate.auto_check(s, datetime(2026, 11, 20), run=FakeRun(1), online=lambda: True) == "failed"
    assert notes and "failed" in notes[0]
    assert "failed" in (tmp_path / "logs" / "auto_update.log").read_text()
    update.write_state(s, {"problems": ["scot_marine"]})
    notes.clear()
    assert autoupdate.auto_check(s, datetime(2026, 11, 20), run=FakeRun(2), online=lambda: True) == "updated with problems"
    assert "scot_marine" in notes[0]


def test_launch_agent_file(tmp_path):
    plist = plistlib.loads(autoupdate.plist_text(_settings(tmp_path)).encode())
    assert plist["Label"] == autoupdate.LABEL
    assert plist["RunAtLoad"] is True and plist["StartInterval"] == 3600
    assert plist["Nice"] == 10 and plist["LowPriorityIO"] is True and plist["ProcessType"] == "Background"
    assert plist["ProgramArguments"][-3:] == ["-m", "mpf", "auto-check"]
    assert plist["EnvironmentVariables"]["GH_CONFIG_DIR"].endswith(".gh-config")
    assert "sudo" not in json.dumps(plist)


def test_update_stops_when_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "is_online", lambda: False)
    with pytest.raises(update.UpdateError, match="No internet connection"):
        update.run_update(_settings(tmp_path), upload=False)


def test_dry_run_does_not_reset_the_14_day_clock(tmp_path, monkeypatch):
    """A --no-upload run must not count as the last successful update."""
    s = _settings(tmp_path)
    monkeypatch.setattr(update, "is_online", lambda: True)
    monkeypatch.setattr(update.subprocess, "run",
                        lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "items.json").write_text("[]")
    monkeypatch.setattr(update, "ROOT", tmp_path)
    assert update.run_update(s, upload=False) == 0
    state = update.read_state(s)
    assert "last_success" not in state and state["last_dry_run"]
    assert autoupdate.due_for_update(state.get("last_success"), datetime(2026, 11, 1))


def test_one_time_instruction_forces_a_dry_run_once(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    update.write_state(s, {"last_success": "2026-11-19T08:00:00"})      # not due
    autoupdate.write_once(s, force=True, upload=False)
    run = FakeRun(0)
    assert autoupdate.auto_check(s, datetime(2026, 11, 20), run=run, online=lambda: True) == "updated"
    assert run.calls[0][-1] == "--no-upload"
    assert not autoupdate.once_path(s).exists()                         # used once, then deleted
    assert autoupdate.auto_check(s, datetime(2026, 11, 20), run=run, online=lambda: True) == "not due"


def test_plist_pins_absolute_tools_and_gh_login(tmp_path):
    plist = plistlib.loads(autoupdate.plist_text(_settings(tmp_path)).encode())
    env = plist["EnvironmentVariables"]
    assert env["MPF_GIT"] == "/usr/bin/git" and env["MPF_GH"] == "/usr/local/bin/gh"
    assert env["PATH"].startswith("/usr/local/bin:/usr/bin:/bin")
    assert plist["ProgramArguments"][0] == str(autoupdate.VENV_PYTHON)
    assert str(autoupdate.VENV_PYTHON).endswith("/.venv/bin/python")
    assert update.GIT.startswith("/") and update.GH.startswith("/")
