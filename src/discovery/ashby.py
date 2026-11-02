import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit

from src.config import Board
from src.discovery.common import HTTP, date, plain
from src.models import Job, Salary

log = logging.getLogger(__name__)


async def discover(http: HTTP, board: Board, limit: int = 500) -> list[Job]:
    base = f"https://api.ashbyhq.com/posting-api/job-board/{board.board}"
    payload = await http.json(base, includeCompensation="true")
    jobs = []
    if len(payload["jobs"]) > limit:
        log.warning("Ashby %s truncated at %d jobs", board.board, limit)
    for r in payload["jobs"][:limit]:
        if r.get("isListed") is False:
            continue
        try:
            compensation = r.get("compensation") or {}
            components = compensation.get("summaryComponents", [])
            if not components:
                components = [
                    c | {"scope": t.get("title", "")}
                    for t in compensation.get("compensationTiers", [])
                    for c in t.get("components", [])
                ]
            jobs.append(
                Job(
                    source="ashby",
                    source_id=r.get("id") or urlsplit(r["jobUrl"]).path.rstrip("/").split("/")[-1],
                    board=board.board,
                    company=board.name,
                    title=r["title"],
                    description=plain(r.get("descriptionPlain") or r.get("descriptionHtml", "")),
                    location="; ".join(
                        [
                            r.get("location", ""),
                            *(x.get("location", "") for x in r.get("secondaryLocations", [])),
                        ]
                    ),
                    remote=r.get("isRemote"),
                    workplace_type=r.get("workplaceType") or "",
                    employment_type=r.get("employmentType") or "",
                    url=r["jobUrl"],
                    apply_url=r["applyUrl"],
                    published_at=date(r.get("publishedAt")),
                    verified_at=datetime.now(UTC),
                    date_basis="last published (Ashby publishedAt)",
                    salaries=[
                        Salary(
                            currency=c["currencyCode"],
                            minimum=c.get("minValue"),
                            maximum=c.get("maxValue"),
                            interval=c.get("interval", "unknown"),
                            scope=c.get("scope", ""),
                        )
                        for c in components
                        if c.get("compensationType") == "Salary" and c.get("currencyCode")
                    ],
                    provenance=base,
                )
            )
        except Exception as exc:
            log.warning("Ashby job %s: %s", r.get("jobUrl"), type(exc).__name__)
    return jobs
