import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app import runner
from app.matching import Decision
from app.storage import ApplicationLog
from src.config import Board
from src.models import Job


def item(identifier):
    return Job(source='greenhouse', source_id=identifier, board='example', company='Example',
               title='Operations Manager', description='Operations work', location='Remote - Worldwide',
               remote=True, published_at=datetime.now(UTC) - timedelta(hours=int(identifier)),
               url=f'https://job-boards.greenhouse.io/example/jobs/{identifier}',
               apply_url=f'https://job-boards.greenhouse.io/example/jobs/{identifier}')


@pytest.mark.parametrize('apply_status', ['submitted', 'uncertain'])
@pytest.mark.parametrize('limit', [1, 10])
async def test_run_skips_unsuitable_jobs_and_stops_after_application_limit(tmp_path, monkeypatch, apply_status, limit):
    monkeypatch.setattr(runner, 'SOURCES', {'greenhouse': [Board(name='Example', board='example')]})
    calls = []

    class FakeHTTP:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    class FakePage:
        async def close(self): pass

    class FakeBrowser:
        async def new_page(self): return FakePage()
        async def close(self): pass

    class FakeChromium:
        async def launch(self, **kwargs): return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    async def discover(*args, **kwargs): return [item(str(i)) for i in range(1, 13)]
    async def match(client, job, *args, **kwargs): return Decision(suitable=job.source_id != '1', reason='Test')
    async def apply(page, job, *args):
        calls.append(job.source_id)
        return apply_status, 'Test result'

    monkeypatch.setattr(runner, 'HTTP', FakeHTTP)
    monkeypatch.setattr(runner, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(runner, 'async_playwright', FakePlaywright)
    monkeypatch.setattr(runner.greenhouse, 'discover', discover)
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    candidate = {'email': 'candidate@example.com', 'role': 'Operations Manager', 'location': 'Remote', 'max_applications': limit}
    events = []
    await runner.run_hunt(candidate, tmp_path / 'resume.pdf', 'Experience', 'test-key', ApplicationLog(tmp_path / 'data'),
                          lambda *args: events.append(args), asyncio.Event())
    assert calls == [str(i) for i in range(2, 2 + limit)]
    assert any(event[0] == 'skipped' for event in events)
    assert events[-1][0] == 'done'


async def test_stop_prevents_any_application(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'SOURCES', {'greenhouse': [Board(name='Example', board='example')]})
    class FakeHTTP:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class FakePlaywright:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def discover(*args, **kwargs): return [item('1')]
    async def forbidden(*args, **kwargs): raise AssertionError('Stopped searches must not apply')
    monkeypatch.setattr(runner, 'HTTP', FakeHTTP)
    monkeypatch.setattr(runner, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(runner, 'async_playwright', FakePlaywright)
    monkeypatch.setattr(runner.greenhouse, 'discover', discover)
    monkeypatch.setattr(runner, 'apply_to_job', forbidden)
    stop = asyncio.Event()
    stop.set()
    events = []
    candidate = {'email': 'candidate@example.com', 'role': 'Manager', 'location': '', 'max_applications': 1}
    await runner.run_hunt(candidate, tmp_path / 'resume.pdf', 'Experience', 'test-key', ApplicationLog(tmp_path / 'data'),
                          lambda *args: events.append(args), stop)
    assert events[-1][0] == 'stopped'


def test_newest_verified_jobs_are_first_and_old_or_undated_jobs_are_skipped(tmp_path):
    now = datetime.now(UTC)
    recent = item('2').model_copy(update={'published_at': now - timedelta(hours=2)})
    newest = item('1').model_copy(update={'published_at': now - timedelta(minutes=20)})
    old = item('3').model_copy(update={'published_at': now - timedelta(days=31)})
    undated = item('4').model_copy(update={'published_at': None})
    wrong_region = item('5').model_copy(update={'published_at': now - timedelta(minutes=10), 'location': 'Remote - United States'})
    candidate = {'email': 'candidate@example.com', 'role': 'Manager', 'location': '', 'region': 'india'}
    result = runner.newest_jobs([recent, old, undated, wrong_region, newest], candidate, ApplicationLog(tmp_path), now=now)
    assert [job.source_id for job in result] == ['1', '2']


def test_location_filter_handles_india_worldwide_and_other_remote_restrictions():
    assert runner.location_matches(item('1').model_copy(update={'location': 'Bengaluru'}), '', 'india')
    assert runner.location_matches(item('1').model_copy(update={'location': 'Remote - Worldwide'}), '', 'india')
    assert not runner.location_matches(item('1').model_copy(update={'location': 'Remote anywhere in the US'}), '', 'india')
    assert not runner.location_matches(item('1').model_copy(update={'location': 'Remote'}), '', 'india')
    assert runner.location_matches(item('1').model_copy(update={'location': 'Remote US'}), 'Remote', 'anywhere')
    assert not runner.location_matches(item('1').model_copy(update={'country_restrictions': ['US']}), '', 'india')


def test_latest_sort_combines_supported_sources(tmp_path):
    now = datetime.now(UTC)
    gh = item('1').model_copy(update={'published_at': now - timedelta(hours=2)})
    lever = item('2').model_copy(update={'source': 'lever', 'board': 'example', 'source_id': '2',
        'url': 'https://jobs.lever.co/example/2', 'apply_url': 'https://jobs.lever.co/example/2/apply',
        'published_at': now - timedelta(hours=3)})
    ashby = item('3').model_copy(update={'source': 'ashby', 'board': 'example', 'source_id': '3',
        'url': 'https://jobs.ashbyhq.com/example/3', 'apply_url': 'https://jobs.ashbyhq.com/example/3/application',
        'published_at': now - timedelta(hours=1)})
    candidate = {'email': 'candidate@example.com', 'role': 'Manager', 'location': '', 'region': 'india'}
    result = runner.newest_jobs([gh, lever, ashby], candidate, ApplicationLog(tmp_path), now=now)
    assert [job.source for job in result] == ['ashby', 'greenhouse', 'lever']


def test_short_job_titles_can_be_searched():
    assert runner.title_matches('Senior HR Manager', 'HR')
    assert runner.title_matches('QA Analyst', 'QA')
    assert not runner.title_matches('Account Manager', 'HR')


def test_seniority_alone_does_not_match_unrelated_roles():
    assert runner.title_matches('Senior Software Engineer', 'Senior Software Engineer')
    assert runner.title_matches('Staff Software Engineer', 'Senior Software Engineer')
    assert not runner.title_matches('Senior Internal Auditor', 'Senior Software Engineer')
    assert not runner.title_matches('Senior Program Manager', 'Senior Software Engineer')
    assert runner.title_matches('Senior Manager', 'Senior')


@pytest.mark.parametrize('status,fragment', [(401, 'rejected'), (403, 'permission'), (404, 'unavailable'), (429, 'usage limit'), (500, 'matching failed')])
def test_ai_errors_explain_the_problem_without_exposing_the_key(status, fragment):
    import httpx
    request = httpx.Request('POST', 'https://example.invalid', headers={'Authorization': 'Bearer dummy-secret-key'})
    error = httpx.HTTPStatusError('dummy-secret-key', request=request, response=httpx.Response(status, request=request))
    message = runner.ai_error_message(error)
    if status == 403:
        assert 'cannot access' in message
    else:
        assert fragment in message
    assert 'dummy-secret-key' not in message
