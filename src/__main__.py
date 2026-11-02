import argparse
import asyncio
import json

import httpx

from src.config import Board
from src.discovery import ashby, greenhouse, lever
from src.discovery.common import HTTP
from src.matching.evaluator import Evaluator
from src.models import Profile


async def discover(provider: str, board: str, company: str, limit: int, skills: list[str]):
    source = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}[provider]
    async with HTTP() as http:
        jobs = await source.discover(http, Board(name=company, board=board), limit=limit)
    evaluator = Evaluator(Profile(skills=skills)) if skills else None
    results = []
    for job in jobs:
        entry = {"job": job.model_dump(mode="json")}
        if evaluator:
            entry["match"] = (await evaluator.evaluate(job)).model_dump(mode="json")
        results.append(entry)
    return results


def main():
    parser = argparse.ArgumentParser(description="Read public job boards and optionally score skill matches.")
    parser.add_argument("provider", choices=["greenhouse", "lever", "ashby"])
    parser.add_argument("board", help="Public job board identifier")
    parser.add_argument("--company", default="", help="Display name for the employer")
    parser.add_argument("--limit", type=int, default=50, help="Maximum jobs to fetch (1–500)")
    parser.add_argument("--skill", action="append", default=[], help="Known skill; repeat for multiple skills")
    args = parser.parse_args()
    if not 1 <= args.limit <= 500:
        parser.error("--limit must be between 1 and 500")
    try:
        results = asyncio.run(discover(args.provider, args.board, args.company or args.board, args.limit, args.skill))
    except (httpx.HTTPError, ValueError, OSError) as exc:
        parser.exit(1, f"Could not read board: {type(exc).__name__}\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
