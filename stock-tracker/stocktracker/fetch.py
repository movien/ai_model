"""HTTP fetching with per-site politeness delays and bot-wall detection.

This module deliberately does NOT try to get past bot protection (Cloudflare challenges,
CAPTCHAs, etc.). When a site blocks automated requests the check is reported as "blocked"
and the tracker backs off. Use the site's official API, or another retailer, instead.
"""

from __future__ import annotations

import email.utils
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests

# Strong signs of a bot wall, checked on any HTML response.
_CHALLENGE_MARKERS = (
    "<title>just a moment...</title>",
    "cf-chl-",
    "challenges.cloudflare.com/turnstile",
    "attention required! | cloudflare",
    "px-captcha",
    "/_incapsula_resource",
    "<title>robot check</title>",
    "pardon our interruption",
    "are you a robot",
    "please verify you are a human",
)
_SECRET_RE = re.compile(r"(?i)((?:api[_-]?key|apikey|key|token|secret|password|access_token)=)[^&\s'\"]+")


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def redact(text: str) -> str:
    """Hide API keys / tokens that appear in URLs inside error messages."""
    return _SECRET_RE.sub(r"\1***", text)


def _retry_after(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    if value.strip().isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def challenge_reason(resp: requests.Response) -> str | None:
    """Return a description if the response is a bot wall / block page, else None."""
    ctype = resp.headers.get("Content-Type", "").lower()
    is_html = "html" in ctype or (not ctype and resp.text.lstrip()[:1] == "<")
    head = resp.text[:50000].lower() if is_html else ""
    marker = next((m for m in _CHALLENGE_MARKERS if m in head), None)
    server = resp.headers.get("Server", "").lower()
    if marker:
        vendor = "Cloudflare" if ("cloudflare" in server or "cf-" in marker or "cloudflare" in marker) else "the site"
        return f"bot check by {vendor} (HTTP {resp.status_code}); this site blocks automated checks"
    if is_html and resp.status_code in (403, 503) and ("cloudflare" in server or "akamai" in server):
        return f"blocked by {server.split('/')[0].title()} (HTTP {resp.status_code})"
    if is_html and resp.status_code == 403:
        return "access denied (HTTP 403); this site blocks automated checks"
    return None


class Fetcher:
    def __init__(self, settings: dict[str, Any], session: requests.Session | None = None, sleep=time.sleep):
        self.session = session or requests.Session()
        self._last_hit: dict[str, float] = {}
        self.sleep = sleep
        self.configure(settings)

    def configure(self, settings: dict[str, Any]) -> None:
        self.timeout = float(settings["timeout"])
        self.default_delay = float(settings["per_host_delay"])
        self.session.headers.update(
            {
                "User-Agent": str(settings["user_agent"]),
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        min_delay: float | None = None,
    ) -> requests.Response:
        host = (urlparse(url).hostname or "").lower()
        delay = self.default_delay if min_delay is None else float(min_delay)
        last = self._last_hit.get(host)
        if last is not None:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        try:
            resp = self.session.get(url, headers=headers or None, timeout=self.timeout)
        except requests.Timeout:
            raise FetchError(f"timed out after {self.timeout:g}s") from None
        except requests.RequestException as e:
            raise FetchError(redact(f"network error: {e.__class__.__name__}: {e}")) from None
        finally:
            self._last_hit[host] = time.monotonic()

        if resp.status_code == 429:
            raise BlockedError("rate limited by the site (HTTP 429)", retry_after=_retry_after(resp))
        reason = challenge_reason(resp)
        if reason:
            raise BlockedError(reason, retry_after=_retry_after(resp))
        if resp.status_code >= 400:
            snippet = " ".join(resp.text[:200].split())
            hint = " (check the API key)" if resp.status_code in (401, 403) else ""
            raise FetchError(redact(f"HTTP {resp.status_code}{hint}: {snippet}" if snippet else f"HTTP {resp.status_code}{hint}"))
        return resp
