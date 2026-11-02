from datetime import UTC, datetime, timedelta

import pytest

from src import __main__ as cli
from src.models import Job


@pytest.mark.asyncio
async def test_discovery_returns_public_results_and_optional_match(monkeypatch):
    class FakeHTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def fake_discover(http, board, limit):
        assert board.board == "example" and limit == 5
        return [Job(
            source="greenhouse", source_id="1", board="example", company="Example",
            title="Python Backend Engineer", description="Python services", location="Remote India",
            remote=True, url="https://example.com/job", apply_url="https://example.com/apply",
            published_at=datetime.now(UTC) - timedelta(hours=1),
        )]

    monkeypatch.setattr(cli, "HTTP", FakeHTTP)
    monkeypatch.setattr(cli.greenhouse, "discover", fake_discover)
    result = await cli.discover("greenhouse", "example", "Example", 5, ["Python"])
    assert result[0]["job"]["source_id"] == "1"
    assert result[0]["match"]["eligible"] is True
    without_skills = await cli.discover("greenhouse", "example", "Example", 5, [])
    assert "match" not in without_skills[0]


def test_cli_rejects_invalid_limits_without_network():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "src", "greenhouse", "example", "--limit", "0"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "--limit must be between 1 and 500" in result.stderr
