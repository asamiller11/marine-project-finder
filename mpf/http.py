"""A polite web client.

Rules it follows:
- Reads robots.txt before the first request to any host, and obeys it.
- Waits between requests to the same host (robots.txt Crawl-delay, or our default).
- Caches every response on disk. Detail pages are never fetched twice.
- If a site answers 401, 403 or 429, it stops and raises Blocked. It never retries with
  different headers or tools.
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
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.user_agent
        self._robots: dict[str, Robots] = {}
        self._last_request: dict[str, float] = {}
        self.network_requests = 0           # how many real requests we made (for reports)

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
        if status in (401, 403):
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
        delay = self.default_delay
        if check_robots:
            robots = self._robots_for(url)
            p = urlparse(url)
            path = p.path + ("?" + p.query if p.query else "")
            if not robots.allowed(path or "/"):
                raise Blocked(f"robots.txt disallows {url}")
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
            if not self._robots_for(resp.url).allowed(final_path or "/"):
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
        if resp.status_code in (401, 403, 429) and check_robots:
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
        # headers: only content negotiation an API documents (for example BOE's Accept header).
        # Never used to change who we say we are.
        status, body = self._get_raw(url, max_age, max_bytes=max_bytes, headers=headers)
        if status != 200:
            raise requests.HTTPError(f"HTTP {status} for {url}")
        return body
