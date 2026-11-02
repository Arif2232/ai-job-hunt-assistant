import asyncio
import io
from datetime import UTC, datetime

import httpx
import pytest
from pypdf import PdfReader, PdfWriter

from app import runner, server, tailoring
from app.matching import Decision
from src.config import Board
from src.models import Job


@pytest.fixture
def pending_job(tmp_path, monkeypatch):
    item = Job(source='greenhouse', source_id='123', board='example', company='Example',
               title='Software Engineer', description='Build reliable services.', location='Remote',
               url='https://job-boards.greenhouse.io/example/jobs/123',
               apply_url='https://job-boards.greenhouse.io/example/jobs/123')
    pdf = tmp_path / 'review.pdf'
    pdf.write_bytes(b'%PDF-synthetic-review')
    stop = asyncio.Event()
    monkeypatch.setitem(server.state, 'review', None)
    monkeypatch.setitem(server.state, 'events', [])
    monkeypatch.setitem(server.state, 'stop', stop)
    monkeypatch.setitem(server.state, 'task', None)
    return item, pdf, stop


@pytest.mark.parametrize('action', ['approve', 'skip'])
async def test_review_is_job_bound_and_requires_an_explicit_decision(pending_job, monkeypatch, action):
    item, pdf, stop = pending_job
    task = asyncio.create_task(server.wait_for_review(item, pdf, ['Built reliable services.'], 'Example original resume.', stop))
    monkeypatch.setitem(server.state, 'task', task)
    await asyncio.sleep(0)
    headers = {'X-App-Token': server.TOKEN}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://127.0.0.1') as client:
        state = (await client.get('/api/status', headers=headers)).json()
        review = state['review']
        assert state['running'] and review['title'] == item.title
        assert review['highlights'] == ['Built reliable services.']
        assert review['original_resume'] == 'Example original resume.'
        assert not task.done()
        assert (await client.get('/api/review-resume', params={'review_id': review['id']})).status_code == 403
        assert (await client.get('/api/review-resume', headers=headers, params={'review_id': 'wrong-id'})).status_code == 409
        preview = await client.get('/api/review-resume', headers=headers, params={'review_id': review['id']})
        assert preview.status_code == 200 and preview.content == pdf.read_bytes()
        form = {'review_id': review['id'], 'action': action}
        assert (await client.post('/api/review', data=form)).status_code == 403
        assert (await client.post('/api/review', headers=headers, data=form | {'review_id': 'wrong-id'})).status_code == 409
        assert not task.done()
        assert (await client.post('/api/review', headers=headers, data=form)).status_code == 200
        assert await asyncio.wait_for(task, timeout=2) == action
        assert (await client.post('/api/review', headers=headers, data=form)).status_code == 409
    assert server.state['review'] is None


async def test_stop_while_waiting_for_review_never_approves_the_job(pending_job, monkeypatch):
    item, pdf, stop = pending_job
    task = asyncio.create_task(server.wait_for_review(item, pdf, ['Example'], 'Example original resume.', stop))
    monkeypatch.setitem(server.state, 'task', task)
    await asyncio.sleep(0)
    review_id = server.state['review']['id']
    headers = {'X-App-Token': server.TOKEN}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://127.0.0.1') as client:
        assert (await client.post('/api/stop', headers=headers)).status_code == 200
        assert await asyncio.wait_for(task, timeout=2) == 'stop'
        assert (await client.post('/api/review', headers=headers, data={'review_id': review_id, 'action': 'approve'})).status_code == 409
    assert server.state['review'] is None


async def test_two_tabs_cannot_decide_the_same_job_twice(pending_job, monkeypatch):
    item, pdf, stop = pending_job
    task = asyncio.create_task(server.wait_for_review(item, pdf, ['Example'], 'Example original resume.', stop))
    monkeypatch.setitem(server.state, 'task', task)
    await asyncio.sleep(0)
    review_id = server.state['review']['id']
    headers = {'X-App-Token': server.TOKEN}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://127.0.0.1') as client:
        responses = await asyncio.gather(*(
            client.post('/api/review', headers=headers, data={'review_id': review_id, 'action': action})
            for action in ('approve', 'skip')
        ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert await asyncio.wait_for(task, timeout=2) in ('approve', 'skip')


async def test_review_can_edit_and_preview_the_resume_before_approval(pending_job, monkeypatch, tmp_path):
    item, pdf, stop = pending_job
    original = tmp_path / 'original.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with original.open('wb') as handle: writer.write(handle)
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    tailoring.make_tailored_pdf(original, ['Example experience'], pdf, name='Avery Example',
                                email=candidate['email'], phone=candidate['phone'], job_title=item.title)
    task = asyncio.create_task(server.wait_for_review(item, pdf, ['Example experience'], 'Example original resume.', stop,
                                                     original_pdf=original, candidate=candidate))
    monkeypatch.setitem(server.state, 'task', task)
    await asyncio.sleep(0)
    review_id = server.state['review']['id']
    headers = {'X-App-Token': server.TOKEN}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://127.0.0.1') as client:
        before = pdf.read_bytes()
        invalid = {'review_id': review_id, 'highlights': 'x' * 201}
        assert (await client.post('/api/review-preview', headers=headers, data=invalid)).status_code == 400
        assert not task.done() and pdf.read_bytes() == before
        def fail_after_writing(original_pdf, lines, output, **kwargs):
            output.write_bytes(b'partial')
            raise OSError('Synthetic write failure')
        with monkeypatch.context() as patch:
            patch.setattr(server, 'make_tailored_pdf', fail_after_writing)
            failed = await client.post('/api/review-preview', headers=headers,
                                       data={'review_id': review_id, 'highlights': 'Another valid highlight'})
            assert failed.status_code == 500
        assert not task.done() and pdf.read_bytes() == before
        data = {'review_id': review_id, 'highlights': 'Example experience\nAdded\u00a0a truthful detail'}
        preview = await client.post('/api/review-preview', headers=headers, data=data)
        assert preview.status_code == 200
        assert 'Added a truthful detail' in PdfReader(io.BytesIO(preview.content)).pages[0].extract_text().replace('\u00a0', ' ')
        assert not task.done()
        approved = await client.post('/api/review', headers=headers,
                                     data=data | {'action': 'approve', 'highlights': 'Example experience\nFinal approved detail'})
        assert approved.status_code == 200 and await asyncio.wait_for(task, timeout=2) == 'approve'
    final_text = PdfReader(pdf).pages[0].extract_text()
    assert 'Final approved detail' in final_text and 'Added a truthful detail' not in final_text


async def test_started_search_pauses_until_the_user_approves(pending_job, monkeypatch, tmp_path):
    item, _, _ = pending_job
    applied = []
    async def hunt(candidate, pdf, resume, key, ledger, report, stop, *, request_review, **kwargs):
        decision = await request_review(item, pdf, ['Example experience'], resume, stop)
        if decision == 'approve':
            applied.append(item.source_id)
            report('submitted', 'Confirmed', item)
    monkeypatch.setattr(server, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(server, 'resume_text', lambda content: 'Example resume text')
    monkeypatch.setattr(server, 'run_hunt', hunt)
    form = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com',
            'phone': '1234567890', 'role': 'Engineer', 'api_key': 'dummy-test-key-only',
            'consent': 'true', 'provider': 'openai', 'model': 'gpt-6-luna', 'resume_mode': 'review'}
    form['connection_ticket'] = server.make_connection_ticket('openai', 'gpt-6-luna', '', form['api_key'])
    headers = {'X-App-Token': server.TOKEN}
    files = {'resume': ('resume.pdf', b'%PDF-test', 'application/pdf')}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://127.0.0.1') as client:
        assert (await client.post('/api/start', headers=headers, data=form, files=files)).status_code == 200
        for _ in range(100):
            current = (await client.get('/api/status', headers=headers)).json()
            if current['review']:
                break
            await asyncio.sleep(0.01)
        assert current['review'] and not applied
        result = await client.post('/api/review', headers=headers, data={'review_id': current['review']['id'], 'action': 'approve'})
        assert result.status_code == 200
        await asyncio.wait_for(server.state['task'], timeout=2)
        assert applied == ['123']
        assert not list((tmp_path / 'data').glob('*.pdf'))


async def test_real_runner_and_api_wait_for_approval_before_opening_a_form(pending_job, monkeypatch, tmp_path):
    item, _, _ = pending_job
    item = item.model_copy(update={'published_at': datetime.now(UTC), 'location': 'Remote - Worldwide', 'remote': True})
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
        async def launch(self, **kwargs):
            calls.append('browser opened')
            return FakeBrowser()
    class FakePlaywright:
        chromium = FakeChromium()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def discover(*args, **kwargs): return [item]
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def highlights(*args, **kwargs): return ['Example experience']
    async def apply(page, job, candidate, resume, ledger):
        assert 'Example experience' in PdfReader(resume).pages[0].extract_text()
        calls.append('submitted')
        return 'submitted', 'Confirmed'
    monkeypatch.setattr(server, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(server, 'resume_text', lambda content: 'Example experience')
    monkeypatch.setattr(runner, 'SOURCES', {'greenhouse': [Board(name='Example', board='example')]})
    monkeypatch.setattr(runner, 'HTTP', FakeHTTP)
    monkeypatch.setattr(runner, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(runner, 'async_playwright', FakePlaywright)
    monkeypatch.setattr(runner.greenhouse, 'discover', discover)
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'resume_highlights', highlights)
    monkeypatch.setattr(runner, 'apply_to_job', apply)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    original = io.BytesIO()
    writer.write(original)
    form = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com',
            'phone': '1234567890', 'role': 'Engineer', 'region': 'anywhere',
            'api_key': 'dummy-test-key-only', 'consent': 'true', 'provider': 'openai',
            'model': 'gpt-6-luna', 'resume_mode': 'review'}
    form['connection_ticket'] = server.make_connection_ticket('openai', 'gpt-6-luna', '', form['api_key'])
    headers = {'X-App-Token': server.TOKEN}
    files = {'resume': ('resume.pdf', original.getvalue(), 'application/pdf')}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://127.0.0.1') as client:
        assert (await client.post('/api/start', headers=headers, data=form, files=files)).status_code == 200
        for _ in range(100):
            current = (await client.get('/api/status', headers=headers)).json()
            if current['review']:
                break
            await asyncio.sleep(0.01)
        assert current['review'] and current['resume_mode'] == 'review' and not calls
        assert 'dummy-test-key-only' not in str(current)
        review_id = current['review']['id']
        preview = await client.get('/api/review-resume', headers=headers, params={'review_id': review_id})
        assert len(PdfReader(io.BytesIO(preview.content)).pages) == 2
        assert (await client.post('/api/review', headers=headers, data={'review_id': review_id, 'action': 'approve'})).status_code == 200
        await asyncio.wait_for(server.state['task'], timeout=2)
        assert calls == ['browser opened', 'submitted']
        assert not list((tmp_path / 'data').rglob('*.pdf'))
        assert all(b'dummy-test-key-only' not in path.read_bytes() for path in (tmp_path / 'data').rglob('*') if path.is_file())
