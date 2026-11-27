"""A hidden browser for sites whose lists only appear after the page's own script runs.

Some government portals (for example the Australian EPBC Act Public Portal, a Microsoft Power Pages site) send a
page with an empty table, then fill it in the browser. To read them we open the page in the headless Chromium that
Playwright installs (~/Library/Caches/ms-playwright, the same one mpf/icons.py uses), wait for the table, and read
it. Nothing is added to requirements.txt: we talk to Chromium over its built-in DevTools pipe
(--remote-debugging-pipe) with the standard library only.

The polite rules still apply. Every page we open, and every "next page" click, goes through
PoliteClient.browser_visit first: never_fetch patterns, robots.txt (no exceptions for the browser), the per-host
wait, and a hard stop on HTTP 401, 403 or 429 from the site. The browser says who we are with the same User-Agent.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time
from urllib.parse import urlparse

from mpf.http import Blocked, PoliteClient

_PATTERNS = ["~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
             "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"]


class NoBrowser(Exception):
    """Playwright's Chromium is not installed on this machine."""


def find_chromium() -> str:
    found = sorted(p for pat in _PATTERNS for p in glob.glob(os.path.expanduser(pat)))
    if not found:
        raise NoBrowser("Playwright's Chromium was not found (install it with "
                        "`npx playwright install chromium-headless-shell`)")
    return found[-1]


class Browser:
    """One headless Chromium tab. Use as a context manager."""

    def __init__(self, client: PoliteClient, timeout: float = 45):
        self.client = client
        self.timeout = timeout
        self._n = 0
        self._buf = b""
        self._refusal: str | None = None      # set when the site answers 401, 403 or 429
        self._proc = None

    def __enter__(self) -> "Browser":
        exe = find_chromium()
        to_r, to_w = os.pipe()        # we write, Chromium reads on fd 3
        from_r, from_w = os.pipe()    # Chromium writes on fd 4, we read

        def child_fds():
            # Move both ends above 4 first, so placing one on fd 3 cannot overwrite the other (the pipe numbers
            # depend on which fds the parent already has open, e.g. under launchd or a background shell).
            import fcntl
            r = fcntl.fcntl(to_r, fcntl.F_DUPFD, 10)
            w = fcntl.fcntl(from_w, fcntl.F_DUPFD, 10)
            os.dup2(r, 3)
            os.dup2(w, 4)

        self._proc = subprocess.Popen(
            [exe, "--headless", "--disable-gpu", "--remote-debugging-pipe", "--no-first-run",
             f"--user-agent={self.client.user_agent}", "about:blank"],
            close_fds=False, preexec_fn=child_fds, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(to_r)
        os.close(from_w)
        self._w, self._r = to_w, from_r
        target = self._call("Target.createTarget", url="about:blank")["targetId"]
        self._sid = self._call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        for domain in ("Page.enable", "Runtime.enable", "Network.enable"):
            self._call(domain, session=True)
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._call("Browser.close")
        except Exception:
            pass
        if self._proc:
            try:
                self._proc.wait(10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        for fd in (self._w, self._r):
            try:
                os.close(fd)
            except OSError:
                pass

    # -- DevTools pipe -------------------------------------------------------
    def _read(self) -> dict:
        while b"\0" not in self._buf:
            chunk = os.read(self._r, 1 << 16)
            if not chunk:
                raise RuntimeError("the browser closed unexpectedly")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\0", 1)
        return json.loads(raw)

    def _event(self, msg: dict) -> None:
        if msg.get("method") == "Network.responseReceived":
            resp = msg["params"].get("response", {})
            status, url = int(resp.get("status", 0)), resp.get("url", "")
            if status in (401, 403, 429) and urlparse(url).netloc == self._host:
                self._refusal = f"HTTP {status} for {url}"

    def _call(self, method: str, session: bool = False, **params) -> dict:
        self._n += 1
        msg = {"id": self._n, "method": method, "params": params}
        if session:
            msg["sessionId"] = self._sid
        os.write(self._w, json.dumps(msg).encode() + b"\0")
        while True:
            reply = self._read()
            if reply.get("id") == self._n:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})
            self._event(reply)

    _host = ""

    def _check_refusal(self) -> None:
        if self._refusal:
            self.client.refused_hosts[self._host] = self._refusal
            raise Blocked(f"{self._refusal}. Stopping this source, not retrying.")

    # -- what sources use ----------------------------------------------------
    def js(self, expression: str):
        """Run an expression in the page and return its value (JSON-like types only)."""
        r = self._call("Runtime.evaluate", session=True, expression=expression, returnByValue=True, awaitPromise=True)
        self._check_refusal()
        return r.get("result", {}).get("value")

    def open(self, url: str) -> None:
        """Open url after the polite checks (robots.txt, never_fetch, the per-host wait)."""
        self._host = urlparse(url).netloc
        self.client.browser_visit(url)
        self._call("Page.navigate", session=True, url=url)

    def click(self, url_for_rules: str, expression: str):
        """Run a click in the page that makes the site load more data (for example "Next page").
        url_for_rules is the page's address: the click is paced and counted like a request to it."""
        self.client.browser_visit(url_for_rules)
        return self.js(expression)

    def wait_for(self, expression: str, timeout: float | None = None) -> bool:
        end = time.time() + (timeout or self.timeout)
        while time.time() < end:
            if self.js(expression):
                return True
            time.sleep(0.5)
        return False
