"""A polite web client.

Rules it follows:
- Reads robots.txt before the first request to any host, and obeys it.
- Waits between requests to the same host (robots.txt Crawl-delay, or our default).
- Caches every response on disk. Detail pages are never fetched twice.
- If a site answers 401, 403 or 429, it stops and raises Blocked. It never retries with
  different headers or tools, and every later request to that site in the same run is refused too.

The source rule (also in README.md and docs/source_rules.md), word for word:
  A source whose robots.txt disallows automated reading may be read only if (a) the site owner has
  given written permission, or (b) its official terms or open-data licence explicitly invite automated
  access (for example a documented API or bulk download page). Never work around a 401, 403 or 429 or
  any block.

How the code enforces it: a URL that robots.txt disallows is refused, unless the source being read has a
`robots_exception` block in config/settings.yaml with basis "a" or "b", the host listed, and the evidence
written down (see valid_exception). Each use is counted per source and printed in the run log. A 401, 403
or 429 always stops the source, with or without an exception.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests


class Blocked(Exception):
    """The site refused us, or robots.txt / our config forbids this URL."""


EXCEPTION_BASES = {
    "a": "written permission from the site owner",
    "b": "official terms or open-data licence invite automated access",
}


def valid_exception(conf: dict | None) -> dict | None:
    """A source's robots_exception block, if it is complete: basis a or b, at least one host, and the
    evidence (where the permission or the terms are). Anything less counts as no exception."""
    if not isinstance(conf, dict):
        return None
    basis = str(conf.get("basis") or "").strip().lower()
    hosts = [h.strip().lower() for h in (conf.get("hosts") or []) if str(h).strip()]
    evidence = str(conf.get("evidence") or "").strip()
    if basis not in EXCEPTION_BASES or not hosts or not evidence:
        return None
    return {"basis": basis, "hosts": hosts, "evidence": evidence}


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------
# Python's built-in robotparser ignores "*" and "$" wildcards, which some of our
# sources use, so we use this small parser instead.

class Robots:
    def __init__(self, text: str, agent_token: str):
        self.rules: list[tuple[bool, str]] = []   # (allow?, path pattern)
        self.crawl_delay: float | None = None
        groups = self._parse_groups(text)
        agent = agent_token.lower()
        chosen = None
        for agents, rules, delay in groups:     # a group naming us wins over "*"
            if any(a != "*" and a in agent for a in agents):
                chosen = (rules, delay)
                break
        if chosen is None:
            for agents, rules, delay in groups:
                if "*" in agents:
                    chosen = (rules, delay)
                    break
        if chosen:
            self.rules, self.crawl_delay = chosen

    @staticmethod
    def _parse_groups(text: str):
        groups = []
        agents, rules, delay = [], [], None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = [p.strip() for p in line.split(":", 1)]
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and agents:
                    groups.append((agents, rules, delay))
                    agents, rules, delay = [], [], None
                agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if key in ("allow", "disallow"):
                if value or key == "allow":
                    rules.append((key == "allow", value))
            elif key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    pass
        if agents:
            groups.append((agents, rules, delay))
        return groups

    @staticmethod
    def _to_regex(pattern: str) -> re.Pattern:
        anchored = pattern.endswith("$")
        body = re.escape(pattern.rstrip("$")).replace(r"\*", ".*")
        return re.compile("^" + body + ("$" if anchored else ""))

    def allowed(self, path_and_query: str) -> bool:
        # The longest matching rule wins; on a tie, Allow wins (Google's convention).
        best_len, best_allow = -1, True
        for allow, pattern in self.rules:
            if not pattern:
                continue
            if self._to_regex(pattern).match(path_and_query):
                length = len(pattern)
                if length > best_len or (length == best_len and allow):
                    best_len, best_allow = length, allow
        return best_allow


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PoliteClient:
    def __init__(self, settings: dict, cache_dir: Path):
        http = settings["http"]
        self.user_agent = http["user_agent"]
        self.agent_token = self.user_agent.split("/")[0]
        self.default_delay = float(http.get("default_delay_seconds", 3))
        self.list_max_age = float(http.get("list_page_max_age_hours", 20))
        self.timeout = float(http.get("timeout_seconds", 60))
        self.never_fetch = http.get("never_fetch", [])
        # Minimum seconds between requests for hosts that publish a rate limit outside robots.txt.
        self.host_delays = {h: float(d) for h, d in (http.get("host_delays") or {}).items()}
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.user_agent
        self._robots: dict[str, Robots] = {}
        self._last_request: dict[str, float] = {}
        self.network_requests = 0           # how many real requests we made (for reports)
        # Project pages to re-read when the saved copy is older than recheck_hours (listed projects that are
        # not decided yet). Everything else keeps the rule: a detail page is fetched once.
        self.refresh_urls: set[str] = set()
        self.recheck_hours = float((settings.get("update") or {}).get("recheck_open_pages_hours", 144))
        self.rechecked = 0                  # real requests made for those re-reads
        # The source rule. current_source is set by the caller before each source runs.
        self.current_source: str | None = None
        self.exceptions: dict[str, dict] = {}       # source name -> valid robots_exception
        self.exceptions_used: dict[str, int] = {}   # source name -> requests robots.txt alone would have refused
        self.refused_hosts: dict[str, str] = {}     # host -> why it refused us (401, 403, 429) this run

    def set_exceptions(self, sources_conf: dict) -> list[str]:
        """Read every source's robots_exception from config. Returns notes about incomplete blocks."""
        notes = []
        for name, conf in (sources_conf or {}).items():
            raw = (conf or {}).get("robots_exception")
            if raw is None:
                continue
            ok = valid_exception(raw)
            if ok:
                self.exceptions[name] = ok
            else:
                notes.append(f"{name}: robots_exception is incomplete (needs basis a or b, hosts and evidence), "
                             f"so robots.txt applies in full")
        return notes

    def _exception_for(self, host: str) -> dict | None:
        exc = self.exceptions.get(self.current_source or "")
        if exc and host.lower() in exc["hosts"]:
            return exc
        return None

    # -- cache ---------------------------------------------------------------
    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        host = urlparse(url).netloc or "local"
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        folder = self.cache_dir / host
        folder.mkdir(parents=True, exist_ok=True)
        return folder / (key + ".body"), folder / (key + ".json")

    def cached_path(self, url: str) -> Path | None:
        body, meta = self._cache_paths(url)
        return body if body.exists() and meta.exists() else None

    def fetched_at(self, url: str) -> str | None:
        """When the cached copy of url was fetched (YYYY-MM-DD), or None."""
        body, meta = self._cache_paths(url)
        if not meta.exists():
            return None
        try:
            return json.loads(meta.read_text())["fetched_at"][:10]
        except (OSError, ValueError, KeyError):
            return None

    # -- rules -----------------------------------------------------------------
    def _check_never_fetch(self, url: str) -> None:
        for pattern in self.never_fetch:
            if pattern in url:
                raise Blocked(f"Config says never fetch URLs containing '{pattern}': {url}")

    def _robots_for(self, url: str) -> Robots:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base in self._robots:
            return self._robots[base]
        robots_url = base + "/robots.txt"
        # robots.txt itself is re-read at most once a day.
        status, body = self._get_raw(robots_url, max_age_hours=24, check_robots=False)
        if status in (401, 403, 429):
            self.refused_hosts[p.netloc] = f"{robots_url} answered HTTP {status}"
            raise Blocked(f"{robots_url} answered {status}. Treating the whole site as off limits.")
        if status >= 500:
            raise Blocked(f"{robots_url} answered {status}. Not crawling until it works.")
        text = body.decode("utf-8", errors="replace") if status == 200 else ""   # 404 = no rules
        robots = Robots(text, self.agent_token)
        self._robots[base] = robots
        return robots

    def _wait(self, host: str, delay: float) -> None:
        last = self._last_request.get(host)
        if last is not None:
            remaining = delay - (time.time() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[host] = time.time()

    # -- fetching ------------------------------------------------------------
    def _get_raw(self, url: str, max_age_hours: float | None, check_robots: bool = True,
                 max_bytes: int | None = None, headers: dict | None = None) -> tuple[int, bytes]:
        self._check_never_fetch(url)
        body_path, meta_path = self._cache_paths(url)
        if body_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text())
            fetched = datetime.fromisoformat(meta["fetched_at"])
            fresh = max_age_hours is None or datetime.now() - fetched < timedelta(hours=max_age_hours)
            if fresh:
                return meta["status"], body_path.read_bytes()

        host = urlparse(url).netloc
        if host in self.refused_hosts:
            # This site already said no (401, 403 or 429) during this run: never ask again.
            raise Blocked(f"{host} refused us earlier in this run ({self.refused_hosts[host]}). Not asking again.")
        delay = max(self.default_delay, self.host_delays.get(host, 0))
        used_exception = False
        if check_robots:
            robots = self._robots_for(url)
            p = urlparse(url)
            path = p.path + ("?" + p.query if p.query else "")
            if not robots.allowed(path or "/"):
                if not self._exception_for(host):
                    raise Blocked(f"robots.txt disallows {url}")
                used_exception = True
            if robots.crawl_delay is not None:
                delay = max(delay, robots.crawl_delay)
        self._wait(host, delay)

        resp = self.session.get(url, timeout=self.timeout, stream=True, headers=headers)
        self.network_requests += 1
        # A redirect must not lead somewhere we may not go: check the final address too.
        if check_robots and resp.url != url:
            resp.close()
            self._check_never_fetch(resp.url)
            fp = urlparse(resp.url)
            final_path = fp.path + ("?" + fp.query if fp.query else "")
            if not self._robots_for(resp.url).allowed(final_path or "/") and not self._exception_for(fp.netloc):
                raise Blocked(f"{url} redirects to {resp.url}, which robots.txt disallows")
            resp = self.session.get(url, timeout=self.timeout, stream=True, headers=headers)
            self.network_requests += 1
        chunks, size = [], 0
        for chunk in resp.iter_content(64 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if max_bytes and size > max_bytes:
                resp.close()
                raise Blocked(f"File larger than {max_bytes} bytes, skipped: {url}")
        body = b"".join(chunks)
        if used_exception:
            name = self.current_source or "unknown"
            self.exceptions_used[name] = self.exceptions_used.get(name, 0) + 1
        if resp.status_code in (401, 403, 429) and check_robots:
            # Always stops the source, exception or not. Later requests to this host are refused too.
            self.refused_hosts[host] = f"HTTP {resp.status_code} for {url}"
            raise Blocked(f"{url} answered HTTP {resp.status_code}. Stopping this source, not retrying.")
        meta = {
            "url": url, "final_url": resp.url, "status": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
        body_path.write_bytes(body)
        meta_path.write_text(json.dumps(meta, indent=1))
        return resp.status_code, body

    def get(self, url: str, list_page: bool = False, max_bytes: int | None = None,
            headers: dict | None = None) -> bytes:
        """Fetch a URL (or return the cached copy). Raises Blocked or requests errors.
        list_page=True lets the cache expire, so we see new items on index pages."""
        max_age = self.list_max_age if list_page else None
        recheck = not list_page and url in self.refresh_urls
        if recheck:
            max_age = self.recheck_hours
        before = self.network_requests
        # headers: only content negotiation an API documents (for example BOE's Accept header).
        # Never used to change who we say we are.
        status, body = self._get_raw(url, max_age, max_bytes=max_bytes, headers=headers)
        if recheck and self.network_requests > before:
            self.rechecked += 1
        if status != 200:
            raise requests.HTTPError(f"HTTP {status} for {url}")
        return body
