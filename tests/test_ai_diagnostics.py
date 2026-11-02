import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from app import runner
from app.matching import Decision
from app.storage import ApplicationLog
from src.config import Board
from src.models import Job


def job(identifier):
    return Job(source='greenhouse', source_id=identifier, board='example', company='Example',
               title='Operations Manager', description='Operations work', location='Remote - Worldwide', remote=True,
               published_at=datetime.now(UTC) - timedelta(hours=int(identifier)),
               url=f'https://job-boards.greenhouse.io/example/jobs/{identifier}',
               apply_url=f'https://job-boards.greenhouse.io/example/jobs/{identifier}')


@pytest.mark.parametrize('data,expected', [
    ('{"suitable":', 'incomplete or malformed'),
    ('{"suitable":"yes","reason":"example"}', 'unexpected format'),
])
def test_response_format_errors_are_distinguished_without_exposing_contents(data, expected):
    with pytest.raises(ValidationError) as raised:
        Decision.model_validate_json(data)
    message = runner.ai_error_message(raised.value)
    assert expected in message
    assert data not in message


@pytest.fixture
def isolated_hunt(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, 'SOURCES', {'greenhouse': [Board(name='Example', board='example')]})
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
    async def discover(*args, **kwargs): return [job('1'), job('2')]
    monkeypatch.setattr(runner, 'HTTP', FakeHTTP)
    monkeypatch.setattr(runner, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(runner, 'async_playwright', FakePlaywright)
    monkeypatch.setattr(runner.greenhouse, 'discover', discover)
    return {'candidate': {'first_name': 'Avery', 'last_name': 'Example', 'phone': '1234567890',
                          'email': 'candidate@example.com', 'role': 'Operations Manager', 'location': '',
                          'region': 'india', 'max_applications': 10},
            'resume': tmp_path / 'resume.pdf', 'ledger': ApplicationLog(tmp_path / 'state')}


@pytest.mark.parametrize('code,keyword', [(401, 'rejected'), (403, 'access'), (404, 'model'), (429, 'limit')])
async def test_provider_failure_explains_why_search_stopped_without_leaking_secrets(isolated_hunt, monkeypatch, code, keyword):
    calls = []
    async def fail(*args, **kwargs):
        request = httpx.Request('POST', 'https://example.invalid', headers={'Authorization': 'Bearer dummy-secret-key'})
        raise httpx.HTTPStatusError('dummy-secret-key', request=request, response=httpx.Response(code, request=request))
    async def forbidden(*args, **kwargs):
        calls.append('applied')
        raise AssertionError('Cannot apply after a provider failure')
    monkeypatch.setattr(runner, 'ai_match', fail)
    monkeypatch.setattr(runner, 'apply_to_job', forbidden)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    errors = [event[1] for event in events if event[0] == 'error']
    assert len(errors) == 1 and keyword in errors[0].lower()
    assert 'dummy-secret-key' not in errors[0]
    assert not calls
    assert events[-1][0] == 'error'


async def test_one_unusable_ai_response_does_not_stop_other_jobs(isolated_hunt, monkeypatch):
    applied = []
    async def match(client, item, *args, **kwargs):
        if item.source_id == '1':
            raise ValueError('AI response was incomplete')
        return Decision(suitable=True, reason='Relevant experience')
    async def apply(page, item, *args):
        applied.append(item.source_id)
        return 'submitted', 'Confirmed'
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert applied == ['2']
    assert any(event[0] == 'needs_attention' for event in events)
    assert '1 application(s) confirmed; 1 need review' in events[-1][1]


@pytest.mark.parametrize('first_status', ['needs_attention', 'uncertain'])
async def test_summary_counts_every_job_needing_review(isolated_hunt, monkeypatch, first_status):
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Relevant experience')
    async def apply(page, item, *args):
        return (first_status, 'Check this job') if item.source_id == '1' else ('submitted', 'Confirmed')
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert '1 application(s) confirmed; 1 need review' in events[-1][1]


async def test_repeated_unusable_ai_responses_stop_before_wasting_more_requests(isolated_hunt, monkeypatch):
    calls = []
    async def discover(*args, **kwargs): return [job(str(i)) for i in range(1, 5)]
    async def match(*args, **kwargs):
        calls.append('match')
        raise ValueError('AI response was incomplete')
    monkeypatch.setattr(runner.greenhouse, 'discover', discover)
    monkeypatch.setattr(runner, 'ai_match', match)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert len(calls) == 3
    assert events[-1][0] == 'error'


async def test_search_keeps_checking_after_twenty_jobs_until_it_finds_a_match(isolated_hunt, monkeypatch):
    applied = []
    async def discover(*args, **kwargs): return [job(str(i)) for i in range(1, 23)]
    async def match(client, item, *args, **kwargs):
        return Decision(suitable=item.source_id == '21', reason='Example reason')
    async def apply(page, item, *args):
        applied.append(item.source_id)
        return 'submitted', 'Confirmed'
    isolated_hunt['candidate']['max_applications'] = 1
    monkeypatch.setattr(runner.greenhouse, 'discover', discover)
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert applied == ['21']
    assert any(event[0] == 'skipped' and 'Example reason' in event[1] for event in events)


async def test_search_checks_all_jobs_if_the_goal_has_not_been_reached(isolated_hunt, monkeypatch):
    checked = []
    async def discover(*args, **kwargs): return [job(str(i)) for i in range(1, 102)]
    async def match(client, item, *args, **kwargs):
        checked.append(item.source_id)
        return Decision(suitable=False, reason='Example reason')
    monkeypatch.setattr(runner.greenhouse, 'discover', discover)
    monkeypatch.setattr(runner, 'ai_match', match)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert len(checked) == 101
    assert any(event[0] == 'searching' and '101' in event[1] for event in events)


async def test_five_possible_submissions_stop_the_search_even_if_one_is_uncertain(isolated_hunt, monkeypatch):
    applied = []
    async def discover(*args, **kwargs): return [job(str(i)) for i in range(1, 8)]
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def apply(page, item, *args):
        applied.append(item.source_id)
        return ('uncertain', 'Check submission') if item.source_id == '3' else ('submitted', 'Confirmed')
    isolated_hunt['candidate']['max_applications'] = 5
    monkeypatch.setattr(runner.greenhouse, 'discover', discover)
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert applied == ['1', '2', '3', '4', '5']
    assert '4 application(s) confirmed; 1 need review' in events[-1][1]


async def test_no_applications_is_reported_clearly(isolated_hunt, monkeypatch):
    async def match(*args, **kwargs): return Decision(suitable=False, reason='Example')
    monkeypatch.setattr(runner, 'ai_match', match)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert events[-1][0] == 'done' and 'No applications were sent' in events[-1][1]


@pytest.mark.parametrize('region,location', [('india', 'India and worldwide remote'), ('anywhere', 'Anywhere')])
async def test_ai_receives_the_selected_search_area_when_no_location_is_entered(isolated_hunt, monkeypatch, region, location):
    seen = []
    async def match(client, item, resume, role, requested_location, **kwargs):
        seen.append(requested_location)
        return Decision(suitable=False, reason='Example reason')
    isolated_hunt['candidate']['region'] = region
    monkeypatch.setattr(runner, 'ai_match', match)
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: None, asyncio.Event())
    assert seen and all(value == location for value in seen)


async def test_ai_receives_the_users_application_preferences(isolated_hunt, monkeypatch):
    seen = []
    async def match(*args, **kwargs):
        seen.append(kwargs)
        return Decision(suitable=False, reason='Example reason')
    isolated_hunt['candidate'].update(relocation='india', experience_flexibility='one_year')
    monkeypatch.setattr(runner, 'ai_match', match)
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: None, asyncio.Event())
    assert seen and all(item['relocation'] == 'india' and item['experience_flexibility'] == 'one_year' for item in seen)


async def test_automatic_tailoring_attaches_a_temporary_pdf_and_cleans_it_up(isolated_hunt, monkeypatch):
    from pypdf import PdfReader, PdfWriter

    applied = []
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def highlights(*args, **kwargs): return ['Example resume']
    async def apply(page, item, candidate, resume, ledger):
        pages = PdfReader(resume).pages
        text = pages[0].extract_text()
        assert len(pages) == 2 and 'Example resume' in text and 'Avery Example' in text
        applied.append(resume)
        return 'submitted', 'Confirmed'
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with isolated_hunt['resume'].open('wb') as handle: writer.write(handle)
    original = isolated_hunt['resume'].read_bytes()
    isolated_hunt['candidate'].update(resume_mode='tailored', max_applications=1)
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'resume_highlights', highlights, raising=False)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert len(applied) == 1 and not applied[0].exists()
    assert isolated_hunt['resume'].read_bytes() == original
    assert events[-1][0] == 'done'


async def test_original_mode_applies_with_the_uploaded_pdf_without_tailoring_or_review(isolated_hunt, monkeypatch):
    applied = []
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def forbidden(*args, **kwargs): raise AssertionError('Original mode must not tailor or request review')
    async def apply(page, item, candidate, resume, ledger):
        applied.append(resume)
        return 'submitted', 'Confirmed'
    isolated_hunt['candidate'].update(resume_mode='original', max_applications=1)
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'resume_highlights', forbidden)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: None, asyncio.Event(), request_review=forbidden)
    assert applied == [isolated_hunt['resume']]


async def test_automatic_tailoring_failure_never_submits_an_unverified_resume(isolated_hunt, monkeypatch):
    calls = []
    async def discover(*args, **kwargs): return [job(str(i)) for i in range(1, 4)]
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def bad_highlights(*args, **kwargs): raise ValueError('Invented line')
    async def apply(*args, **kwargs): calls.append('applied')
    isolated_hunt['candidate']['resume_mode'] = 'tailored'
    monkeypatch.setattr(runner.greenhouse, 'discover', discover)
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'resume_highlights', bad_highlights, raising=False)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert not calls and events[-1][0] == 'error'


async def test_one_tailoring_failure_does_not_stop_other_jobs(isolated_hunt, monkeypatch):
    from pypdf import PdfWriter

    attempts = []
    applied = []
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def highlights(*args, **kwargs):
        attempts.append('tailor')
        if len(attempts) == 1:
            raise ValueError('Unusable result')
        return ['Example resume']
    async def apply(page, item, candidate, resume, ledger):
        applied.append(item.source_id)
        return 'submitted', 'Confirmed'
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with isolated_hunt['resume'].open('wb') as handle: writer.write(handle)
    isolated_hunt['candidate'].update(resume_mode='tailored', max_applications=1)
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'resume_highlights', highlights)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert applied == ['2']
    assert any(event[0] == 'needs_attention' and event[2].source_id == '1' for event in events)


async def test_review_mode_waits_for_each_job_and_only_applies_after_approval(isolated_hunt, monkeypatch):
    from pypdf import PdfReader, PdfWriter

    pending = asyncio.Queue()
    answers = asyncio.Queue()
    applied = []
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def highlights(*args, **kwargs): return ['Example resume']
    async def review(item, pdf, lines, resume, stop, **kwargs):
        assert len(PdfReader(pdf).pages) == 2
        await pending.put((item.source_id, pdf, lines))
        return await answers.get()
    async def apply(page, item, candidate, resume, ledger):
        applied.append(item.source_id)
        assert 'Example resume' in PdfReader(resume).pages[0].extract_text()
        return 'submitted', 'Confirmed'
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with isolated_hunt['resume'].open('wb') as handle: writer.write(handle)
    isolated_hunt['candidate'].update(resume_mode='review', max_applications=1)
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'resume_highlights', highlights)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    events = []
    task = asyncio.create_task(runner.run_hunt(
        isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
        isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event(), request_review=review))
    first = await asyncio.wait_for(pending.get(), timeout=2)
    assert first[0] == '1' and first[2] == ['Example resume'] and not applied
    await answers.put('skip')
    second = await asyncio.wait_for(pending.get(), timeout=2)
    assert second[0] == '2' and not applied and not first[1].exists()
    await answers.put('approve')
    await asyncio.wait_for(task, timeout=2)
    assert applied == ['2'] and not second[1].exists()
    assert any(event[0] == 'skipped' and 'you' in event[1] for event in events)


async def test_review_mode_does_not_apply_if_review_is_unavailable(isolated_hunt, monkeypatch):
    calls = []
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def apply(*args, **kwargs): calls.append('applied')
    isolated_hunt['candidate']['resume_mode'] = 'review'
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    events = []
    await runner.run_hunt(isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
                          isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event())
    assert not calls and events[-1][0] == 'error'


async def test_approval_for_one_job_is_not_reused_for_the_next(isolated_hunt, monkeypatch):
    from pypdf import PdfWriter

    pending = asyncio.Queue()
    answers = asyncio.Queue()
    applied = []
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def highlights(*args, **kwargs): return ['Example resume']
    async def review(item, pdf, lines, resume, stop, **kwargs):
        await pending.put(item.source_id)
        return await answers.get()
    async def apply(page, item, candidate, resume, ledger):
        applied.append(item.source_id)
        return 'submitted', 'Confirmed'
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with isolated_hunt['resume'].open('wb') as handle: writer.write(handle)
    isolated_hunt['candidate']['resume_mode'] = 'review'
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'resume_highlights', highlights)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    events = []
    task = asyncio.create_task(runner.run_hunt(
        isolated_hunt['candidate'], isolated_hunt['resume'], 'Example resume', 'dummy-secret-key',
        isolated_hunt['ledger'], lambda *args: events.append(args), asyncio.Event(), request_review=review))
    assert await asyncio.wait_for(pending.get(), timeout=2) == '1'
    await answers.put('approve')
    assert await asyncio.wait_for(pending.get(), timeout=2) == '2'
    assert applied == ['1']
    await answers.put('stop')
    await asyncio.wait_for(task, timeout=2)
    assert applied == ['1'] and events[-1][0] == 'stopped'


async def test_review_mode_from_ui_to_mock_submission(isolated_hunt, monkeypatch, tmp_path):
    import io
    import socket

    import uvicorn
    from playwright.async_api import async_playwright, expect
    from pypdf import PdfReader, PdfWriter

    from app import server

    applied = []
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def highlights(*args, **kwargs): return ['Example resume']
    async def apply(page, item, candidate, resume, ledger):
        applied.append((item.source_id, PdfReader(resume).pages[0].extract_text()))
        return 'submitted', 'Confirmed'
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'resume_highlights', highlights)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    monkeypatch.setattr(server, 'ai_match', match)
    monkeypatch.setattr(server, 'AsyncOpenAI', runner.AsyncOpenAI)
    monkeypatch.setattr(server, 'resume_text', lambda content: 'Example resume')
    monkeypatch.setattr(server, 'DATA', tmp_path / 'app-data')
    for name, value in {'task': None, 'events': [], 'review': None, 'stop': asyncio.Event(), 'resume_mode': None}.items():
        monkeypatch.setitem(server.state, name, value)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    original = io.BytesIO()
    writer.write(original)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(32)
    port = listener.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(server.app, log_level='error', lifespan='off'))
    service_task = asyncio.create_task(service.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if service.started:
                break
            await asyncio.sleep(0.02)
        assert service.started
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(f'http://127.0.0.1:{port}/')
            await page.wait_for_selector('#provider option[value="openai"]', state='attached')
            for name, value in {'role': 'Operations Manager', 'first_name': 'Avery', 'last_name': 'Example',
                                'email': 'candidate@example.com', 'phone': '1234567890',
                                'api_key': 'dummy-test-key-only'}.items():
                await page.locator(f'input[name="{name}"]').fill(value)
            await page.locator('input[name="resume"]').set_input_files({
                'name': 'resume.pdf', 'mimeType': 'application/pdf', 'buffer': original.getvalue(),
            })
            await page.locator('input[name="resume_mode"][value="review"]').check()
            await page.locator('input[name="consent"]').check()
            await page.locator('#test-ai').click()
            await expect(page.locator('#start')).to_be_enabled()
            await page.locator('#start').click()
            await expect(page.locator('#review-job-link')).to_have_attribute('href', 'https://job-boards.greenhouse.io/example/jobs/1', timeout=10000)
            await expect(page.locator('#review-approve')).to_be_enabled()
            assert not applied
            await page.locator('#review-highlights').fill('Example resume\nVerified detail')
            await expect(page.locator('#review-approve')).to_be_disabled()
            await page.locator('#review-retry').click()
            await expect(page.locator('#review-approve')).to_be_enabled()
            await page.locator('#review-approve').click()
            await expect(page.locator('#review-job-link')).to_have_attribute('href', 'https://job-boards.greenhouse.io/example/jobs/2', timeout=10000)
            assert len(applied) == 1 and applied[0][0] == '1' and 'Verified detail' in applied[0][1]
            await page.locator('#review-skip').click()
            await expect(page.locator('#review-card')).to_be_hidden()
            await asyncio.wait_for(server.state['task'], timeout=2)
            assert len(applied) == 1 and not list((tmp_path / 'app-data').rglob('*.pdf'))
            assert await page.locator('input[name="api_key"]').input_value() == 'dummy-test-key-only'
            await browser.close()
    finally:
        service.should_exit = True
        await service_task
        listener.close()
