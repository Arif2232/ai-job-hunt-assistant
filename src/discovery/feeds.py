import hashlib

import feedparser

from src.config import Feed
from src.discovery.common import HTTP, date, plain
from src.models import Job


async def discover(http: HTTP, feed: Feed, limit: int = 500) -> list[Job]:
    response = await http.get(feed.url)
    if "<html" in response.text[:500].lower() and any(
        s in response.text.lower()
        for s in ("just a moment", "cloudflare", "captcha", "verify you are human")
    ):
        raise ValueError("RSS source returned an anti-bot challenge instead of a feed")
    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError("Invalid RSS/Atom feed")
    jobs = []
    for r in parsed.entries[:limit]:
        title = plain(r.get("title", ""))
        company, sep, role = title.partition(": ")
        url = r.get("link", "")
        jobs.append(
            Job(
                source="feed",
                source_id=hashlib.sha256(url.encode()).hexdigest()[:24],
                board=feed.name,
                company=company if sep else feed.name,
                title=role if sep else title,
                description=plain(r.get("summary", "")),
                location="; ".join(t.get("term", "") for t in r.get("tags", [])),
                remote=True,
                url=url,
                apply_url=url,
                published_at=date(r.get("published")),
                date_basis="RSS published" if r.get("published") else "unknown",
                provenance=feed.url,
            )
        )
    return jobs
