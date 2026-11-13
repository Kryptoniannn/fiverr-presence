"""
fiverr_presence.core — fetch a Fiverr seller's *publicly displayed* online status.

How it works
------------
Fiverr's own website calls an unauthenticated, public endpoint to light up the
green "Online" badge on every seller profile page that any visitor opens:

    GET https://www.fiverr.com/users/<username>/is_online
    -> {"is_online": true}   (HTTP 200, application/json)

This module does exactly what the visitor's browser does: one plain GET with
normal browser headers. No login, no cookies, no private data — only the
online/offline flag that Fiverr displays publicly on the profile.

Design rules
------------
* stdlib only (urllib + json) — zero pip dependencies, runs anywhere Python 3.9+ runs
* polite by default: rate limiting, retries with backoff, disk cache with TTL
* structured results (never raises on network/business errors)
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

BASE_HOST = "https://www.fiverr.com"
IS_ONLINE_PATH = "/users/{username}/is_online"

# Headers a normal visitor's browser would send for this XHR call.
# (No cookies, no tokens — the endpoint requires none.)
# NOTE: do NOT send Accept-Language — Fiverr's bot manager replies 403
# ("Ignore ABR") to the is_online endpoint when it is present.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Encoding": "gzip",
}

# Fiverr usernames: letters, digits, underscore, dot, hyphen (2..30 chars).
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,30}$")

# Markers inside Fiverr's HTML error/maintenance pages, used to detect
# "this is not real JSON" responses.
_ERROR_PAGE_MARKERS = ("We're fixing this page", "It'll be back up in no time")

STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"
STATUS_UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #
def parse_target(target: str) -> str:
    """Accept a full profile URL (any junk tolerated) or a bare username.

    Examples that all resolve to username "valeriiaty":
      * valeriiaty
      * https://www.fiverr.com/valeriiaty
      * https://www.fiverr.com/valeriiaty?source=search
      * www.fiverr.com/valeriiaty/
      * fiverr.com/valeriiaty/logo-design  (first path segment wins)

    Raises ValueError when no plausible username can be extracted.
    """
    target = (target or "").strip()
    if not target:
        raise ValueError("empty target")

    # Bare username
    if "/" not in target and "." not in target[:-1] and USERNAME_RE.match(target):
        return target

    # URL form: take the first non-empty path segment of the host part
    parsed = urllib.parse.urlsplit(target if "://" in target else "https://" + target)
    segments = [s for s in parsed.path.split("/") if s]
    if segments:
        candidate = urllib.parse.unquote(segments[0])
        # strip a trailing junk like ".html" if someone pasted a cached page
        candidate = candidate.split("?")[0].strip("/.")
        if USERNAME_RE.match(candidate):
            return candidate

    raise ValueError(f"could not extract a Fiverr username from: {target!r}")


def profile_url(username: str) -> str:
    return f"{BASE_HOST}/{urllib.parse.quote(username, safe='')}"


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #
@dataclass
class PresenceResult:
    username: str
    status: str  # STATUS_* constant
    is_online: bool | None  # raw flag from Fiverr (None = unknown)
    profile_url: str
    checked_at: str  # UTC ISO-8601
    http_code: int | None = None
    error: str | None = None
    source: str = "fiverr.com/users/<username>/is_online"
    cached: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
class _RateLimiter:
    """Global minimum interval between outbound requests."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


def _http_get_json(url: str, headers: dict, timeout: float, referer: str) -> tuple[int, object | None, str | None]:
    """GET url, return (http_code, parsed_json_or_None, error_or_None).

    Raises nothing for network/HTTP problems — surfaces them via the tuple.
    """
    req_headers = dict(headers)
    req_headers["Referer"] = referer
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            code = resp.status
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            ctype = resp.headers.get("Content-Type", "")
            text = raw.decode("utf-8", errors="replace")
            if "json" not in ctype.lower() and "json" not in "text/plain":
                # Still try to parse; Fiverr usually answers application/json.
                pass
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                if any(m in text for m in _ERROR_PAGE_MARKERS):
                    return code, None, "fiverr_error_page (site maintenance/error page)"
                return code, None, f"non-JSON response (Content-Type: {ctype or 'n/a'})"
            return code, data, None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(400).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - best effort only
            pass
        if any(m in detail for m in _ERROR_PAGE_MARKERS):
            return exc.code, None, "fiverr_error_page (site maintenance/error page)"
        return exc.code, None, f"HTTP {exc.code} {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, None, f"network error: {exc}"


def _curl_get_json(url: str, headers: dict, timeout: float, referer: str) -> tuple[int | None, object | None, str | None]:
    """Fallback transport using the system curl binary.

    Python's TLS fingerprint is sometimes rejected by Fiverr's edge (HTTP 403)
    while curl's is accepted. This is the same request with the same headers —
    only the client differs. Uses --compressed; no external Python deps.
    """
    import subprocess

    cmd = ["curl", "-sS", "-m", str(int(timeout)), "--compressed",
           "-w", "\n%{http_code}", "-H", f"Referer: {referer}"]
    for key, value in headers.items():
        cmd += ["-H", f"{key}: {value}"]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
    except Exception as exc:  # noqa: BLE001 - missing curl or spawn failure
        return None, None, f"curl transport error: {exc}"

    out = proc.stdout.decode("utf-8", errors="replace")
    code_str = ""
    body = out
    if "\n" in out:
        body, _, code_str = out.rpartition("\n")
    try:
        code = int(code_str.strip())
    except ValueError:
        code = None
        error = (proc.stderr.decode("utf-8", errors="replace").strip() or "curl produced no status")
        return code, None, error

    if code != 200:
        if any(m in body for m in _ERROR_PAGE_MARKERS):
            return code, None, "fiverr_error_page (site maintenance/error page)"
        return code, None, f"HTTP {code}"

    try:
        return code, json.loads(body), None
    except json.JSONDecodeError:
        if any(m in body for m in _ERROR_PAGE_MARKERS):
            return code, None, "fiverr_error_page (site maintenance/error page)"
        return code, None, "non-JSON response via curl transport"


def _fetch(url: str, headers: dict, timeout: float, referer: str) -> tuple[int | None, object | None, str | None]:
    """Fetch preferring the curl transport (its TLS fingerprint is accepted by
    Fiverr's edge); fall back to urllib when curl is unavailable."""
    code, data, error = _curl_get_json(url, headers, timeout, referer)
    if code is None and "curl transport error" in (error or ""):
        return _http_get_json(url, headers, timeout, referer)
    return code, data, error


# --------------------------------------------------------------------------- #
# Disk cache
# --------------------------------------------------------------------------- #
class _DiskCache:
    def __init__(self, path: str | None, ttl_seconds: float) -> None:
        self.ttl = max(0.0, ttl_seconds)
        self.enabled = self.ttl > 0 and path is not None
        self.path = Path(path) if path else None
        self._data: dict[str, dict] = {}
        if self.enabled:
            try:
                if self.path.exists():
                    self._data = json.loads(self.path.read_text("utf-8"))
            except Exception:  # noqa: BLE001 - corrupt cache is not fatal
                self._data = {}

    def get(self, username: str) -> PresenceResult | None:
        if not self.enabled:
            return None
        entry = self._data.get(username)
        if not entry:
            return None
        if entry.get("expires", 0) < time.time():
            return None
        result = PresenceResult(**entry["result"])
        result.cached = True
        return result

    def put(self, result: PresenceResult) -> None:
        if not self.enabled:
            return
        self._data[result.username] = {
            "expires": time.time() + self.ttl,
            "result": result.to_dict(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), "utf-8")
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001 - cache write failure is not fatal
            pass


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
@dataclass
class CheckerConfig:
    timeout: float = 15.0
    retries: int = 3
    backoff_base: float = 1.5  # seconds; sleeps backoff_base**attempt between retries
    rate_limit: float = 1.0  # min seconds between outbound requests
    cache_ttl: float = 60.0  # seconds; 0 disables caching
    cache_path: str = field(
        default_factory=lambda: os.environ.get(
            "FIVERR_PRESENCE_CACHE",
            str(Path.home() / ".cache" / "fiverr_presence" / "cache.json"),
        )
    )
    user_agent: str | None = None  # override DEFAULT_HEADERS UA if set
    retry_on: tuple[int, ...] = (403, 429, 500, 502, 503, 504)


class FiverrPresenceChecker:
    """Check publicly displayed online/offline status of Fiverr sellers."""

    def __init__(self, config: CheckerConfig | None = None) -> None:
        self.config = config or CheckerConfig()
        self._limiter = _RateLimiter(self.config.rate_limit)
        self._cache = _DiskCache(self.config.cache_path, self.config.cache_ttl)

    # -- single ------------------------------------------------------------- #
    def check(self, target: str, use_cache: bool = True) -> PresenceResult:
        """Check one seller. `target` = username or any profile URL form."""
        username = parse_target(target)
        headers = dict(DEFAULT_HEADERS)
        if self.config.user_agent:
            headers["User-Agent"] = self.config.user_agent

        if use_cache:
            cached = self._cache.get(username)
            if cached is not None:
                return cached

        url = BASE_HOST + IS_ONLINE_PATH.format(username=urllib.parse.quote(username, safe=""))
        last_error = None
        last_code = None

        for attempt in range(self.config.retries):
            self._limiter.wait()
            code, data, error = _fetch(url, headers, self.config.timeout, profile_url(username))
            last_code, last_error = code, error

            if data is not None and isinstance(data, dict) and "is_online" in data:
                is_online = bool(data["is_online"])
                result = PresenceResult(
                    username=username,
                    status=STATUS_ONLINE if is_online else STATUS_OFFLINE,
                    is_online=is_online,
                    profile_url=profile_url(username),
                    checked_at=_utcnow_iso(),
                    http_code=code,
                    error=None,
                )
                self._cache.put(result)
                return result

            # Business-level "not found" style answers: do not retry.
            if code == 404:
                break

            # Retryable failures (rate limit, transient 5xx, network glitch)
            if code is None or code in self.config.retry_on:
                if attempt < self.config.retries - 1:
                    if code == 429:
                        # be extra polite after an explicit rate limit
                        sleep_for = 3.0
                    elif code == 403:
                        # Fiverr's bot manager rate-window; wait it out
                        sleep_for = 6.0
                    else:
                        sleep_for = self.config.backoff_base ** (attempt + 1)
                    time.sleep(sleep_for)
                continue

            break  # non-retryable HTTP error

        result = PresenceResult(
            username=username,
            status=STATUS_UNKNOWN,
            is_online=None,
            profile_url=profile_url(username),
            checked_at=_utcnow_iso(),
            http_code=last_code,
            error=last_error or "no data returned",
        )
        return result

    # -- batch -------------------------------------------------------------- #
    def check_many(
        self,
        targets: list[str],
        delay: float | None = None,
        use_cache: bool = True,
        progress=None,
    ) -> list[PresenceResult]:
        """Check many sellers sequentially.

        `delay` overrides config.rate_limit for this run (politeness knob).
        `progress(done, total, result)` is an optional callback.
        """
        original_limit = self._limiter.min_interval
        if delay is not None:
            self._limiter.min_interval = max(0.0, delay)
        try:
            results = []
            total = len(targets)
            for i, target in enumerate(targets, 1):
                try:
                    result = self.check(target, use_cache=use_cache)
                except ValueError as exc:
                    username_guess = (target or "").strip()[:60]
                    result = PresenceResult(
                        username=username_guess,
                        status=STATUS_UNKNOWN,
                        is_online=None,
                        profile_url=target.strip()[:200],
                        checked_at=_utcnow_iso(),
                        error=str(exc),
                    )
                results.append(result)
                if progress:
                    progress(i, total, result)
            return results
        finally:
            self._limiter.min_interval = original_limit
