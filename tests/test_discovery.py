from datetime import UTC, datetime

import httpx
import pytest

from src.config import Board, Feed
from src.discovery import ashby, feeds, greenhouse, lever
from src.discovery.common import HTTP, date, safe_url


def test_dates_do_not_invent_timezones():
    assert date(None) is None
    assert date("2026-09-23") is None
    assert date("2026-09-23T12:30:00") is None
    assert date("2026-09-23T12:30:00+05:30").hour == 7
    assert date("not a date") is None
    assert date(1700000000000).tzinfo is UTC


@pytest.mark.parametrize(
    "url",
    [
        "http://jobs.lever.co/example/123",
        "https://localhost/jobs",
        "https://127.0.0.1/jobs",
        "https://jobs.lever.co.evil.example/123",
        "https://user:pass@jobs.lever.co/123",
        "https://jobs.lever.co:444/123",
    ],
)
def test_untrusted_destinations(url):
    assert not safe_url(url)


async def test_greenhouse_uses_first_published_not_updated():
    def handler(request):
        raw = {
            "id": 12,
            "title": "Backend Engineer",
            "content": "&lt;p&gt;Python &amp;amp; SQL&lt;/p&gt;",
            "location": {"name": "Remote India"},
            "absolute_url": "https://example.com/job?gh_jid=12",
            "updated_at": "2026-09-23T00:00:00Z",
        }
        if request.url.path.endswith("/12"):
            return httpx.Response(200, json=raw | {"first_published": "2020-01-01T00:00:00Z"})
        return httpx.Response(200, json={"jobs": [raw]})

    async with HTTP(transport=httpx.MockTransport(handler)) as http:
        jobs = await greenhouse.discover(http, Board(name="Example", board="example"))
    assert len(jobs) == 1
    assert jobs[0].published_at.year == 2020
    assert jobs[0].description == "Python & SQL"
    assert jobs[0].apply_url == "https://job-boards.greenhouse.io/example/jobs/12"


async def test_lever_pagination_and_jsonld():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "jobs.lever.co":
            return httpx.Response(
                200,
                text='<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-09-23T01:00:00Z"}</script>',
            )
        offset = int(request.url.params.get("skip", "0"))
        count = min(int(request.url.params["limit"]), 101 - offset)
        return httpx.Response(
            200,
            json=[
                {
                    "id": str(i),
                    "text": "Backend Engineer",
                    "descriptionPlain": "Python",
                    "hostedUrl": f"https://jobs.lever.co/example/{i}",
                    "applyUrl": f"https://jobs.lever.co/example/{i}/apply",
                    "publishedAt": "2026-09-23T01:00:00Z" if i else None,
                    "createdAt": 1700000000000,
                }
                for i in range(offset, offset + count)
            ],
        )

    async with HTTP(transport=httpx.MockTransport(handler)) as http:
        jobs = await lever.discover(http, Board(name="Example", board="example"), limit=200)
    assert len(jobs) == 101
    assert any("skip=100" in c for c in calls)
    assert jobs[0].date_basis == "JSON-LD datePosted"
    assert jobs[0].published_at.year == 2026


async def test_ashby_nullable_fields_and_compensation():
    raw = {
        "id": "abc",
        "title": "Backend Engineer",
        "location": "Remote India",
        "workplaceType": None,
        "isRemote": None,
        "descriptionPlain": "Python",
        "isListed": True,
        "jobUrl": "https://jobs.ashbyhq.com/example/abc",
        "applyUrl": "https://jobs.ashbyhq.com/example/abc/application",
        "publishedAt": "2026-09-23T01:00:00Z",
        "compensation": {
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "currencyCode": "USD",
                    "minValue": 30000,
                    "maxValue": 80000,
                    "interval": "1 YEAR",
                },
                {"compensationType": "EquityPercentage", "currencyCode": None, "maxValue": 1},
            ]
        },
    }
    async with HTTP(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"jobs": [raw, raw | {"isListed": False}]})
        )
    ) as http:
        jobs = await ashby.discover(http, Board(name="Example", board="example"))
    assert len(jobs) == 1
    assert jobs[0].workplace_type == ""
    assert len(jobs[0].salaries) == 1
    assert jobs[0].salaries[0].maximum == 80000


async def test_rss_published_not_updated():
    xml = '<rss version="2.0"><channel><title>Remote</title><item><title>Example: Backend Engineer</title><link>https://weworkremotely.com/jobs/example</link><pubDate>Wed, 23 Sep 2026 01:00:00 GMT</pubDate><description>Python remote worldwide</description></item></channel></rss>'
    async with HTTP(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=xml))) as http:
        jobs = await feeds.discover(
            http, Feed(name="WWR", url="https://weworkremotely.com/remote-job-rss-feed")
        )
    assert jobs[0].company == "Example"
    assert jobs[0].published_at == datetime(2026, 9, 23, 1, tzinfo=UTC)


async def test_redirect_to_private_host_is_rejected():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    async with HTTP(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="Unregistered"):
            await http.get("https://api.ashbyhq.com/test")
    assert len(calls) == 1


async def test_rate_limit_retry_is_bounded(monkeypatch):
    calls = []

    async def no_sleep(_):
        pass

    monkeypatch.setattr("src.discovery.common.asyncio.sleep", no_sleep)

    def handler(request):
        calls.append(request)
        return httpx.Response(
            429 if len(calls) < 3 else 200, json={"ok": True}, headers={"Retry-After": "0"}
        )

    async with HTTP(transport=httpx.MockTransport(handler)) as http:
        assert await http.json("https://api.ashbyhq.com/test") == {"ok": True}
    assert len(calls) == 3


async def test_greenhouse_can_skip_detail_requests_for_fast_search():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'jobs': [{
            'id': 123, 'title': 'Operations Manager', 'content': '<p>Operations</p>',
            'location': {'name': 'Remote'}, 'absolute_url': 'https://example.com/jobs/123',
        }]})

    async with HTTP(transport=httpx.MockTransport(handler)) as http:
        jobs = await greenhouse.discover(http, Board(name='Example', board='example'), enrich=False)
    assert len(jobs) == 1 and jobs[0].published_at is None
    assert calls == ['/v1/boards/example/jobs']


async def test_lever_checks_posting_dates_only_for_matching_titles():
    calls = []
    rows = [
        {'id': '1', 'text': 'Operations Manager', 'hostedUrl': 'https://jobs.lever.co/example/1', 'applyUrl': 'https://jobs.lever.co/example/1/apply'},
        {'id': '2', 'text': 'Software Engineer', 'hostedUrl': 'https://jobs.lever.co/example/2', 'applyUrl': 'https://jobs.lever.co/example/2/apply'},
    ]

    def handler(request):
        calls.append((request.url.host, request.url.path))
        if request.url.host == 'api.lever.co':
            return httpx.Response(200, json=rows)
        return httpx.Response(200, text='<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-10-30T01:00:00Z"}</script>')

    async with HTTP(transport=httpx.MockTransport(handler)) as http:
        jobs = await lever.discover(http, Board(name='Example', board='example'), title_filter=lambda title: 'Manager' in title)
    assert [job.source_id for job in jobs] == ['1']
    assert ('jobs.lever.co', '/example/1') in calls
    assert ('jobs.lever.co', '/example/2') not in calls
