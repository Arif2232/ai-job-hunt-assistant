from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)
ATS_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.eu.greenhouse.io": "greenhouse",
    "job-boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}
FETCH_HOSTS = set(ATS_HOSTS) | {
    "boards-api.greenhouse.io",
    "api.lever.co",
    "api.eu.lever.co",
    "api.ashbyhq.com",
    "weworkremotely.com",
    "www.weworkremotely.com",
}


def safe_url(url: str, hosts: set[str] | dict[str, str] = FETCH_HOSTS) -> bool:
    try:
        u = urlsplit(url)
        return (
            u.scheme == "https"
            and u.hostname in hosts
            and u.port in (None, 443)
            and not u.username
            and not u.password
        )
    except ValueError:
        return False


def plain(value: str | None) -> str:
    return BeautifulSoup(html.unescape(value or ""), "html.parser").get_text(" ", strip=True)


def date(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            result = parsedate_to_datetime(str(value))
        # A day or naive timestamp cannot establish the configured freshness boundary.
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


class HTTP:
    def __init__(self, concurrency: int = 4, transport=None):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30),
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": "JobBoardDiscovery/1.0 (read-only)",
                "Accept": "application/json,text/html,application/rss+xml",
            },
        )
        self.semaphore = asyncio.Semaphore(concurrency)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.client.aclose()

    async def get(self, url: str, *, _attempts: int = 4, **params) -> httpx.Response:
        for _redirect in range(6):
            if not safe_url(url):
                raise ValueError(f"Unregistered public source URL: {url}")
            response = None
            for attempt in range(_attempts):
                try:
                    async with self.semaphore:
                        response = await self.client.get(url, params=params or None)
                    if response.status_code not in (429, 500, 502, 503, 504):
                        break
                    response.raise_for_status()
                except (httpx.TransportError, httpx.HTTPStatusError):
                    if attempt == _attempts - 1:
                        raise
                    retry = response.headers.get("Retry-After", "") if response else ""
                    delay = float(retry) if retry.isdigit() else 2**attempt + random.random()
                    await asyncio.sleep(min(delay, 30))
            assert response is not None
            if response.is_redirect:
                url = str(response.url.join(response.headers["location"]))
                params = {}
                continue
            response.raise_for_status()
            if len(response.content) > 15_000_000:
                raise ValueError("Source response exceeds 15 MB")
            return response
        raise ValueError("Too many redirects")

    async def json(self, url: str, **params):
        return (await self.get(url, **params)).json()


async def jsonld_date(http: HTTP, url: str, job_id: str) -> datetime | None:
    """Use a hosted job's own JSON-LD; never reinterpret updated_at as publication."""
    if not safe_url(url, ATS_HOSTS) or job_id not in urlsplit(url).path:
        return None
    try:
        soup = BeautifulSoup((await http.get(url)).text, "html.parser")
        for script in soup.select('script[type="application/ld+json"]'):
            data = json.loads(script.string or script.get_text())
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                nodes.extend(node.get("@graph", []))
                if node.get("@type") == "JobPosting":
                    return date(node.get("datePosted"))
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.warning("Publication date unavailable for %s: %s", url, type(exc).__name__)
    return None
