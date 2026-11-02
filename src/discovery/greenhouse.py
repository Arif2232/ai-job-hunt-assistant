import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.config import Board
from src.discovery.common import HTTP, date, plain
from src.models import Job, Salary

log = logging.getLogger(__name__)


async def discover(
    http: HTTP,
    board: Board,
    limit: int = 500,
    *,
    job_id: str | None = None,
    cache_dir: Path | None = None,
    enrich: bool = True,
) -> list[Job]:
    base = f"https://boards-api.greenhouse.io/v1/boards/{board.board}/jobs"
    payload = (
        {"jobs": [await http.json(f"{base}/{job_id}", pay_transparency="true")]}
        if job_id
        else await http.json(base, content="true")
    )

    cache_path = (
        cache_dir / f"{board.board}.json" if cache_dir is not None and job_id is None else None
    )
    cached, refreshed = {}, {}
    if cache_path and cache_path.is_file():
        try:
            if cache_path.stat().st_size <= 20_000_000:
                cached = json.loads(cache_path.read_text()).get("jobs", {})
                if not isinstance(cached, dict):
                    cached = {}
        except (OSError, ValueError, AttributeError):
            cached = {}

    async def normalize(raw):
        try:
            details = raw
            if enrich and not raw.get("first_published") and job_id is None:
                key = str(raw["id"])
                fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
                entry = cached.get(key, {})
                reusable = False
                try:
                    age = datetime.now(UTC) - datetime.fromisoformat(entry["fetched_at"])
                    reusable = entry["fingerprint"] == fingerprint and timedelta(
                        0
                    ) <= age < timedelta(hours=24)
                    reusable = reusable and isinstance(entry["details"], dict)
                except (KeyError, ValueError, TypeError):
                    pass
                if reusable:
                    details = {**raw, **entry["details"]}
                    refreshed[key] = entry
                else:
                    details = {
                        **raw,
                        **await http.json(f"{base}/{raw['id']}", pay_transparency="true"),
                    }
                    refreshed[key] = {
                        "fingerprint": fingerprint,
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "details": details,
                    }
            published = date(details.get("first_published"))
            return Job(
                source="greenhouse",
                source_id=str(raw["id"]),
                board=board.board,
                company=board.name,
                title=details["title"],
                description=plain(details.get("content")),
                location=details.get("location", {}).get("name", ""),
                url=details["absolute_url"],
                apply_url=f"https://job-boards.greenhouse.io/{board.board}/jobs/{raw['id']}",
                published_at=published,
                date_basis="first_published" if published else "unknown",
                salaries=[
                    Salary(
                        currency=s["currency_type"],
                        minimum=s.get("min_cents", 0) / 100,
                        maximum=s["max_cents"] / 100 if s.get("max_cents") is not None else None,
                        scope=s.get("title", ""),
                    )
                    for s in details.get("pay_input_ranges", [])
                ],
                provenance=base,
            )
        except Exception as exc:
            log.warning("Greenhouse %s job %s: %s", board.board, raw.get("id"), type(exc).__name__)
            return None

    if len(payload["jobs"]) > limit:
        log.warning("Greenhouse %s truncated at configured %d jobs", board.board, limit)
    jobs = await asyncio.gather(*(normalize(r) for r in payload["jobs"][:limit]))
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = cache_path.with_suffix(".json.tmp")
            with temp.open("w") as handle:
                os.chmod(temp, 0o600)
                json.dump({"version": 1, "jobs": refreshed}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(cache_path)
        except OSError:
            log.warning("Greenhouse detail cache could not be saved for %s", board.board)
    return [j for j in jobs if j is not None]
