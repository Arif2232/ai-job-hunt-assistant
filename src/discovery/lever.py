import asyncio
import logging
from datetime import UTC, datetime
from collections.abc import Callable

from src.config import Board
from src.discovery.common import HTTP, date, jsonld_date, plain
from src.models import Job, Salary

log = logging.getLogger(__name__)


async def discover(
    http: HTTP, board: Board, limit: int = 500, *, job_id: str | None = None,
    title_filter: Callable[[str], bool] | None = None,
) -> list[Job]:
    host = "api.eu.lever.co" if board.region == "eu" else "api.lever.co"
    base = f"https://{host}/v0/postings/{board.board}"
    raw_jobs = [await http.json(f"{base}/{job_id}", mode="json")] if job_id else []
    while job_id is None and len(raw_jobs) < limit:
        count = min(100, limit - len(raw_jobs))
        batch = await http.json(base, mode="json", skip=len(raw_jobs), limit=count)
        if not isinstance(batch, list):
            raise ValueError("Lever did not return a postings list")
        raw_jobs.extend(batch)
        if len(batch) < count:
            break
    if len(raw_jobs) >= limit:
        log.warning("Lever %s reached configured cap %d", board.board, limit)

    if title_filter is not None:
        raw_jobs = [raw for raw in raw_jobs if isinstance(raw, dict) and title_filter(raw.get('text') or '')]

    async def normalize(r):
        try:
            # createdAt is not part of the documented public schema; do not assume it exists.
            published = date(r.get("publishedAt"))
            basis = "publishedAt" if published else "JSON-LD datePosted"
            published = published or await jsonld_date(http, r["hostedUrl"], r["id"])
            salary = r.get("salaryRange") or {}
            return Job(
                source="lever",
                source_id=r["id"],
                board=board.board,
                company=board.name,
                title=r["text"],
                description=plain(
                    " ".join(
                        [
                            r.get("descriptionPlain") or r.get("description", ""),
                            *(
                                x.get("text", "") + " " + x.get("content", "")
                                for x in r.get("lists", [])
                            ),
                            r.get("additionalPlain") or r.get("additional", ""),
                            r.get("salaryDescriptionPlain", ""),
                        ]
                    )
                ),
                location="; ".join(
                    r.get("categories", {}).get("allLocations")
                    or [r.get("categories", {}).get("location", "")]
                ),
                workplace_type=r.get("workplaceType", ""),
                remote=r.get("workplaceType") == "remote",
                employment_type=r.get("categories", {}).get("commitment", ""),
                url=r["hostedUrl"],
                apply_url=r["applyUrl"],
                published_at=published,
                verified_at=datetime.now(UTC),
                date_basis=basis if published else "unknown",
                provenance=base,
                salaries=[
                    Salary(
                        currency=salary["currency"],
                        minimum=salary.get("min"),
                        maximum=salary.get("max"),
                        interval={"per-year-salary": "year", "per-month-salary": "month"}.get(
                            salary.get("interval"), salary.get("interval", "unknown")
                        ),
                    )
                ]
                if salary and (salary.get("max") or salary.get("min") or 0) > 0
                else [],
            )
        except Exception as exc:
            log.warning("Lever job %s: %s", r.get("id"), type(exc).__name__)
            return None

    return [j for j in await asyncio.gather(*(normalize(r) for r in raw_jobs)) if j]
