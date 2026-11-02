import asyncio
import io
import socket
from datetime import UTC, datetime

import uvicorn
import pytest
from playwright.async_api import async_playwright, expect
from pypdf import PdfWriter

from app import runner, server
from app.memory import AnswerMemory
from app.matching import Decision
from src.config import Board
from src.models import Job


async def test_memory_can_be_loaded_edited_and_cleared_without_saving_the_ai_key(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DATA', tmp_path)
    memory = AnswerMemory(tmp_path)
    email = 'candidate@example.com'
    memory.save_profile(email, {'first_name': 'Avery', 'last_name': 'Example', 'phone': '1234567890',
                                'current_city': 'Example City', 'current_country': 'India'})
    memory.save_profile('other@example.com', {'first_name': 'Other', 'current_city': 'Other City'})
    item = Job(source='greenhouse', source_id='123', board='example', company='Example',
               title='Engineer', description='Example', location='Remote',
               url='https://job-boards.greenhouse.io/example/jobs/123',
               apply_url='https://job-boards.greenhouse.io/example/jobs/123')
    memory.record_questions(email, item, [{'label': 'Current country?', 'kind': 'choice', 'options': ['India', 'Canada']}])
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(32)
    port = listener.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(server.app, log_level='error', lifespan='off'))
    task = asyncio.create_task(service.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if service.started:
                break
            await asyncio.sleep(0.02)
        assert service.started
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            requests = []
            page.on('request', lambda request: requests.append(request.post_data or '') if '/api/memory/' in request.url else None)
            await page.goto(f'http://127.0.0.1:{port}/')
            await page.wait_for_selector('#provider option[value="openai"]', state='attached')
            await page.locator('input[name="email"]').fill(email)
            await page.locator('input[name="api_key"]').fill('dummy-ai-key-not-real')
            await expect(page.locator('input[name="first_name"]')).to_have_value('Avery')
            await expect(page.locator('input[name="current_city"]')).to_have_value('Example City')
            await expect(page.locator('#memory-prompt')).to_be_visible()
            row = page.locator('.memory-question').filter(has_text='Current country?')
            await expect(row).to_be_visible()
            load_started = asyncio.Event()
            release_load = asyncio.Event()
            async def delayed_load(route):
                response = await route.fetch()
                load_started.set()
                await release_load.wait()
                await route.fulfill(response=response)
            await page.route('**/api/memory/list', delayed_load)
            await page.locator('#memory-load').click()
            await asyncio.wait_for(load_started.wait(), timeout=2)
            await row.locator('select').select_option('India')
            release_load.set()
            await expect(page.locator('#memory-message')).to_contain_text('Save your edited answers')
            await expect(row.locator('select')).to_have_value('India')
            await page.unroute('**/api/memory/list', delayed_load)
            await row.locator('input[type="checkbox"]').check()
            await row.get_by_role('button', name='Save answer').click()
            await expect(row.get_by_role('status')).to_have_text('Answer saved.')
            assert memory.questions(email)[0]['answer'] == 'India' and memory.questions(email)[0]['reuse_globally']
            await page.locator('input[name="current_city"]').fill('New Example City')
            profile_started = asyncio.Event()
            release_profile = asyncio.Event()
            async def delayed_profile(route):
                response = await route.fetch()
                profile_started.set()
                await release_profile.wait()
                await route.fulfill(response=response)
            await page.route('**/api/memory/profile', delayed_profile)
            await page.locator('#memory-save-profile').click()
            await asyncio.wait_for(profile_started.wait(), timeout=2)
            await page.locator('input[name="current_city"]').fill('Latest Example City')
            release_profile.set()
            await expect(page.locator('#memory-message')).to_contain_text('Save again')
            assert memory.profile(email)['current_city'] == 'New Example City'
            await page.unroute('**/api/memory/profile', delayed_profile)
            await page.locator('#memory-save-profile').click()
            await expect(page.locator('#memory-message')).to_contain_text('saved on this computer')
            assert memory.profile(email)['current_city'] == 'Latest Example City'
            await page.locator('.memory-add summary').click()
            await page.locator('#memory-new-label').fill('LinkedIn profile')
            await page.locator('#memory-new-answer').fill('https://example.com/profile')
            await page.locator('#memory-add').click()
            await expect(page.locator('#memory-questions summary')).to_contain_text('0 need attention')
            assert len(memory.questions(email)) == 2
            assert await page.locator('input[name="api_key"]').input_value() == 'dummy-ai-key-not-real'
            assert all('dummy-ai-key-not-real' not in request for request in requests)
            second = await browser.new_page()
            await second.goto(f'http://127.0.0.1:{port}/')
            await second.locator('input[name="email"]').fill(email)
            await second.locator('input[name="phone"]').click()
            await expect(second.locator('input[name="current_city"]')).to_have_value('Latest Example City')
            assert len(memory.questions(email)) == 2
            await second.locator('#memory-load').click()
            await expect(second.locator('#memory-message')).to_contain_text('ready')
            await second.locator('input[name="email"]').fill('other@example.com')
            await expect(second.locator('input[name="first_name"]')).to_have_value('')
            await expect(second.locator('input[name="current_city"]')).to_have_value('')
            await second.locator('#memory-save-profile').click()
            await expect(second.locator('#memory-message')).to_contain_text('Select “Save my details”')
            assert memory.profile('other@example.com')['current_city'] == 'Other City'
            page.on('dialog', lambda dialog: asyncio.create_task(dialog.accept()))
            await page.locator('#memory-clear').click()
            await expect(page.locator('#memory-message')).to_contain_text('removed')
            assert memory.profile(email) == {} and memory.questions(email) == []
            assert await page.locator('input[name="api_key"]').input_value() == 'dummy-ai-key-not-real'
            await browser.close()
    finally:
        service.should_exit = True
        await task
        listener.close()


async def test_new_questions_are_shown_when_a_search_finishes(tmp_path, monkeypatch):
    item = Job(source='greenhouse', source_id='123', board='example', company='Example',
               title='Engineer', description='Example', location='Remote',
               url='https://job-boards.greenhouse.io/example/jobs/123',
               apply_url='https://job-boards.greenhouse.io/example/jobs/123')
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def hunt(candidate, pdf, resume, key, ledger, report, stop, *, memory, **kwargs):
        assert candidate['max_applications'] == 5
        memory.record_questions(candidate['email'], item, [{'label': 'Notice period?', 'kind': 'text', 'options': []}])
        report('done', 'No application sent.', item)
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(server, 'ai_match', match)
    monkeypatch.setattr(server, 'run_hunt', hunt)
    monkeypatch.setattr(server, 'resume_text', lambda content: 'Example resume')
    for name, value in {'task': None, 'events': [], 'review': None, 'stop': asyncio.Event(), 'resume_mode': None}.items():
        monkeypatch.setitem(server.state, name, value)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(32)
    port = listener.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(server.app, log_level='error', lifespan='off'))
    task = asyncio.create_task(service.serve(sockets=[listener]))
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
            for name, value in {'role': 'Engineer', 'first_name': 'Avery', 'last_name': 'Example',
                                'email': 'candidate@example.com', 'phone': '1234567890',
                                'api_key': 'dummy-ai-key-not-real'}.items():
                await page.locator(f'input[name="{name}"]').fill(value)
            await page.locator('input[name="resume"]').set_input_files({
                'name': 'resume.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-test',
            })
            await page.locator('input[name="consent"]').check()
            await page.locator('#test-ai').click()
            await expect(page.locator('#start')).to_be_enabled()
            await page.locator('#start').click()
            await expect(page.locator('#memory-prompt')).to_be_visible(timeout=10000)
            await expect(page.locator('.memory-question').filter(has_text='Notice period?')).to_be_visible()
            assert await page.locator('input[name="api_key"]').input_value() == 'dummy-ai-key-not-real'
            await browser.close()
    finally:
        service.should_exit = True
        await task
        listener.close()


@pytest.mark.parametrize('mode', ['original', 'review'])
async def test_saved_answer_is_used_on_the_next_search_end_to_end(tmp_path, monkeypatch, mode):
    item = Job(source='greenhouse', source_id='123', board='example', company='Example',
               title='Engineer', description='Example', location='Remote - Worldwide', remote=True,
               published_at=datetime.now(UTC), url='https://job-boards.greenhouse.io/example/jobs/123',
               apply_url='https://job-boards.greenhouse.io/example/jobs/123')
    html = '''<html><body><form>
      <label for="first_name">First name *</label><input id="first_name" required>
      <label for="last_name">Last name *</label><input id="last_name" required>
      <label for="email">Email *</label><input id="email" type="email" required>
      <label for="phone">Phone *</label><input id="phone" type="tel" required>
      <label for="resume">Resume *</label><input id="resume" type="file" required>
      <label for="country">Country of residence? *</label>
      <select id="country" required><option value="">Choose</option><option value="IN">India</option></select>
      <button type="submit">Submit Application</button></form>
      <script>document.querySelector('form').addEventListener('submit',e=>{
        e.preventDefault(); document.body.textContent='Thank you for applying';
      });</script></body></html>'''
    class FakeHTTP:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def discover(*args, **kwargs): return [item]
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def highlights(*args, **kwargs): return ['Example resume']
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(server, 'ai_match', match)
    monkeypatch.setattr(server, 'resume_text', lambda content: 'Example resume')
    monkeypatch.setattr(runner, 'SOURCES', {'greenhouse': [Board(name='Example', board='example')]})
    monkeypatch.setattr(runner, 'HTTP', FakeHTTP)
    monkeypatch.setattr(runner, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(runner, 'ai_match', match)
    monkeypatch.setattr(runner, 'resume_highlights', highlights)
    monkeypatch.setattr(runner.greenhouse, 'discover', discover)
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
    task = asyncio.create_task(service.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if service.started:
                break
            await asyncio.sleep(0.02)
        assert service.started
        async with async_playwright() as playwright:
            class FakeChromium:
                async def launch(self, **kwargs):
                    browser = await playwright.chromium.launch(headless=True)
                    context = await browser.new_context()
                    await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
                    class Browser:
                        async def new_page(self): return await context.new_page()
                        async def close(self): await browser.close()
                    return Browser()
            class FakePlaywright:
                chromium = FakeChromium()
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
            monkeypatch.setattr(runner, 'async_playwright', FakePlaywright)
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(f'http://127.0.0.1:{port}/')
            await page.wait_for_selector('#provider option[value="openai"]', state='attached')
            for name, value in {'role': 'Engineer', 'first_name': 'Avery', 'last_name': 'Example',
                                'email': 'candidate@example.com', 'phone': '1234567890',
                                'api_key': 'dummy-ai-key-not-real'}.items():
                await page.locator(f'input[name="{name}"]').fill(value)
            await page.locator('input[name="resume"]').set_input_files({
                'name': 'resume.pdf', 'mimeType': 'application/pdf', 'buffer': original.getvalue(),
            })
            if mode == 'review':
                await page.locator('input[name="resume_mode"][value="review"]').check()
            await page.locator('input[name="consent"]').check()
            await page.locator('#test-ai').click()
            await expect(page.locator('#start')).to_be_enabled()
            await page.locator('#start').click()
            if mode == 'review':
                await expect(page.locator('#review-approve')).to_be_enabled(timeout=10000)
                await page.locator('#review-approve').click()
            await expect(page.locator('#memory-prompt')).to_be_visible(timeout=15000)
            row = page.locator('.memory-question').filter(has_text='Country of residence?')
            await row.locator('select').select_option('India')
            await row.get_by_role('button', name='Save answer').click()
            await expect(row.get_by_role('status')).to_have_text('Answer saved.')
            await expect(page.locator('#start')).to_be_enabled()
            await page.locator('#start').click()
            if mode == 'review':
                await expect(page.locator('#review-approve')).to_be_enabled(timeout=10000)
                await page.locator('#review-approve').click()
            await expect(page.locator('#events')).to_contain_text('The employer confirmed the application.', timeout=15000)
            await asyncio.wait_for(server.state['task'], timeout=3)
            assert AnswerMemory(tmp_path).questions('candidate@example.com')[0]['answer'] == 'India'
            assert b'dummy-ai-key-not-real' not in AnswerMemory(tmp_path).path.read_bytes()
            await browser.close()
    finally:
        service.should_exit = True
        await task
        listener.close()
