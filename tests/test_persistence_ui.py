import asyncio
import socket

import uvicorn
from playwright.async_api import async_playwright, expect

from app import server
from app.matching import Decision
from app.memory import AnswerMemory
from app.settings import LocalSettings


async def serve():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(32)
    instance = uvicorn.Server(uvicorn.Config(server.app, log_level='error', lifespan='off'))
    task = asyncio.create_task(instance.serve(sockets=[listener]))
    for _ in range(100):
        if instance.started:
            break
        await asyncio.sleep(0.02)
    assert instance.started
    return instance, task, listener, f'http://127.0.0.1:{listener.getsockname()[1]}/'


async def test_settings_details_resume_and_verified_keys_survive_a_new_browser_and_port(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    checks = []
    searches = []
    async def match(*args, **kwargs):
        checks.append((kwargs['provider'], kwargs['model'], kwargs['api_key']))
        return Decision(suitable=True, reason='Example')
    async def hunt(candidate, resume, text, key, ledger, report, stop, **kwargs):
        searches.append((candidate, resume.read_bytes(), key))
        report('done', 'Sample search finished.')
    def read_resume(content):
        if content != b'%PDF-sample-resume': raise ValueError('Choose a readable PDF.')
        return 'Example resume text'
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(server.httpx, 'AsyncClient', FakeClient)
    monkeypatch.setattr(server, 'ai_match', match)
    monkeypatch.setattr(server, 'run_hunt', hunt)
    monkeypatch.setattr(server, 'resume_text', read_resume)
    for name, value in {'task': None, 'events': [], 'review': None, 'stop': asyncio.Event(), 'resume_mode': None}.items():
        monkeypatch.setitem(server.state, name, value)
    services = [await serve()]
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            first = await browser.new_page()
            await first.goto(services[0][3])
            await first.wait_for_selector('#provider option[value="openai"]', state='attached')
            await first.locator('input[name="role"]').fill('Senior Engineer')
            await first.locator('input[name="location"]').fill('Remote')
            await first.locator('input[name="first_name"]').fill('Avery')
            await first.locator('input[name="last_name"]').fill('Example')
            await first.locator('input[name="email"]').fill('candidate@example.com')
            await first.locator('input[name="phone"]').fill('1234567890')
            await first.locator('input[name="current_city"]').fill('Example City')
            await first.locator('input[name="current_country"]').fill('India')
            await first.select_option('select[name="relocation"]', 'india')
            await first.locator('input[name="resume_mode"][value="review"]').check()
            await first.locator('input[name="resume"]').set_input_files({
                'name': 'sample.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-sample-resume',
            })
            await expect(first.locator('#saved-resume-name')).to_have_text('sample.pdf')
            await first.select_option('#model', 'gpt-6.1-terra')
            await first.locator('input[name="api_key"]').fill('dummy-openai-test-key')
            await first.locator('#test-ai').click()
            await expect(first.locator('#ai-summary')).to_contain_text('Connected')
            await expect(first.locator('#ai-settings')).not_to_have_attribute('open', '')
            assert await first.locator('input[name="api_key"]').input_value() == ''
            await first.evaluate('Promise.all([window.appPersistence.flush(), window.flushApplicationDetails()])')
            await first.close()

            monkeypatch.setattr(server, 'CONNECTION_SECRET', b'n' * 32)
            services.append(await serve())
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(services[1][3])
            await expect(page.locator('input[name="role"]')).to_have_value('Senior Engineer')
            await expect(page.locator('input[name="location"]')).to_have_value('Remote')
            await expect(page.locator('input[name="email"]')).to_have_value('candidate@example.com')
            await expect(page.locator('input[name="first_name"]')).to_have_value('Avery')
            await expect(page.locator('input[name="current_city"]')).to_have_value('Example City')
            await expect(page.locator('#saved-resume-name')).to_have_text('sample.pdf')
            await expect(page.locator('#ai-summary')).to_contain_text('Previously verified')
            await expect(page.locator('#start')).to_be_enabled()
            assert not await page.locator('input[name="consent"]').is_checked()
            assert await page.locator('input[name="api_key"]').input_value() == ''
            assert await page.locator('input[name="resume"]').is_disabled()
            assert await page.locator('select[name="relocation"]').input_value() == 'india'
            assert await page.locator('input[name="resume_mode"][value="review"]').is_checked()
            assert len(checks) == 1

            await page.locator('#open-settings').click()
            assert await page.locator('#model').input_value() == 'gpt-6.1-terra'
            await page.select_option('#model', 'gpt-6-luna')
            await expect(page.locator('#start')).to_be_disabled()
            await page.evaluate('window.appPersistence.flush()')
            new_page = await browser.new_page()
            await new_page.goto(services[1][3])
            await expect(new_page.locator('#model')).to_have_value('gpt-6-luna')
            await expect(new_page.locator('#ai-summary')).to_contain_text('Needs test')
            await expect(new_page.locator('input[name="api_key"]')).to_be_hidden()
            await expect(new_page.locator('#start')).to_be_disabled()
            await new_page.close()
            await page.select_option('#model', 'gpt-6.1-terra')
            await expect(page.locator('#ai-summary')).to_contain_text('Previously verified')
            await page.select_option('#provider', 'gemini')
            await expect(page.locator('input[name="api_key"]')).to_be_visible()
            await page.locator('input[name="api_key"]').fill('dummy-gemini-test-key')
            await page.locator('#test-ai').click()
            await expect(page.locator('#ai-summary')).to_contain_text('Connected')
            await page.locator('#open-settings').click()
            await page.select_option('#provider', 'openai')
            await expect(page.locator('#ai-summary')).to_contain_text('Previously verified')
            assert await page.locator('#model').input_value() == 'gpt-6.1-terra'
            assert await page.locator('input[name="api_key"]').is_disabled()
            assert len(checks) == 2
            await page.select_option('#model', 'custom')
            await expect(page.locator('#ai-summary')).to_contain_text('Needs test')
            await expect(page.locator('#start')).to_be_disabled()
            await page.select_option('#model', 'gpt-6.1-terra')
            await expect(page.locator('#ai-summary')).to_contain_text('Previously verified')

            await page.locator('#change-resume').click()
            await page.locator('input[name="resume"]').set_input_files({
                'name': 'invalid.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-invalid',
            })
            await expect(page.locator('#resume-status')).to_contain_text('readable PDF')
            await page.locator('#resume-revert').click()
            await expect(page.locator('#saved-resume-name')).to_have_text('sample.pdf')

            sent = []
            page.on('request', lambda req: sent.append(req.post_data or '') if req.url.endswith('/api/start') else None)
            await page.locator('input[name="consent"]').check()
            LocalSettings(tmp_path).save_resume('candidate@example.com', 'changed.pdf', b'%PDF-changed-elsewhere')
            await page.locator('#start').click()
            await expect(page.locator('#error')).to_contain_text('Your saved resume changed')
            await expect(page.locator('#saved-resume-name')).to_have_text('changed.pdf')
            assert not searches
            await page.locator('#change-resume').click()
            await page.locator('input[name="resume"]').set_input_files({
                'name': 'sample.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-sample-resume',
            })
            await expect(page.locator('#saved-resume-name')).to_have_text('sample.pdf')
            await page.locator('#start').click()
            await expect(page.locator('#status')).to_have_text('Finished', timeout=10000)
            assert searches and searches[0][1:] == (b'%PDF-sample-resume', 'dummy-openai-test-key')
            assert searches[0][0]['resume_mode'] == 'review'
            assert sent and 'dummy-openai-test-key' not in sent[0]
            assert LocalSettings(tmp_path).credential('gemini')['api_key'] == 'dummy-gemini-test-key'
            assert AnswerMemory(tmp_path).profile('candidate@example.com')['current_city'] == 'Example City'
            await page.locator('#open-settings').click()
            await page.select_option('#provider', 'gemini')
            await expect(page.locator('#ai-summary')).to_contain_text('Previously verified')
            page.on('dialog', lambda dialog: asyncio.create_task(dialog.accept()))
            await page.locator('#remove-key').click()
            await expect(page.locator('#test-ai-result')).to_contain_text('Saved key removed')
            assert LocalSettings(tmp_path).credential('gemini') is None
            assert LocalSettings(tmp_path).credential('openai')['api_key'] == 'dummy-openai-test-key'
            await context.close()
            await browser.close()
    finally:
        for instance, task, listener, _ in services:
            instance.should_exit = True
            await task
            listener.close()


async def test_detail_autosave_respects_edits_opt_out_and_email_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DATA', tmp_path)
    memory = AnswerMemory(tmp_path)
    memory.save_profile('other@example.com', {'first_name': 'Other', 'phone': '5550001234', 'current_city': 'Other City'})
    instance, task, listener, url = await serve()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url)
            await page.wait_for_selector('#provider option[value="openai"]', state='attached')
            await page.locator('input[name="first_name"]').fill('Avery')
            await page.locator('input[name="last_name"]').fill('Example')
            await page.locator('input[name="email"]').fill('candidate@example.com')
            await page.locator('input[name="phone"]').fill('1234567890')
            await page.locator('input[name="current_city"]').fill('Original City')
            await page.evaluate('Promise.all([window.appPersistence.flush(), window.flushApplicationDetails()])')
            assert memory.profile('candidate@example.com')['first_name'] == 'Avery'
            assert memory.profile('candidate@example.com')['current_city'] == 'Original City'

            started = asyncio.Event()
            release = asyncio.Event()
            seen = []
            async def delay_first(route):
                response = await route.fetch()
                seen.append(True)
                if len(seen) == 1:
                    started.set()
                    await release.wait()
                await route.fulfill(response=response)
            await page.route('**/api/memory/profile-fields', delay_first)
            await page.locator('input[name="current_city"]').fill('Middle City')
            await asyncio.wait_for(started.wait(), timeout=3)
            await page.locator('input[name="current_city"]').fill('Latest City')
            release.set()
            await page.evaluate('window.flushApplicationDetails()')
            await page.unroute('**/api/memory/profile-fields', delay_first)
            assert memory.profile('candidate@example.com')['current_city'] == 'Latest City'

            failed = asyncio.Event()
            release_failure = asyncio.Event()
            requests = []
            async def fail_first(route):
                requests.append(True)
                if len(requests) == 1:
                    failed.set()
                    await release_failure.wait()
                    await route.fulfill(status=500, content_type='application/json', body='{"detail":"Storage unavailable."}')
                else:
                    await route.continue_()
            await page.route('**/api/memory/profile-fields', fail_first)
            await page.locator('input[name="current_city"]').fill('Failed old value')
            await asyncio.wait_for(failed.wait(), timeout=3)
            await page.locator('input[name="current_city"]').fill('Newest value')
            release_failure.set()
            await page.evaluate('window.flushApplicationDetails()')
            await page.evaluate('window.flushApplicationDetails()')
            await page.unroute('**/api/memory/profile-fields', fail_first)
            assert memory.profile('candidate@example.com')['current_city'] == 'Newest value'

            load_started = asyncio.Event()
            release_load = asyncio.Event()
            async def slow_profile_load(route):
                response = await route.fetch()
                load_started.set()
                await release_load.wait()
                await route.fulfill(response=response)
            await page.route('**/api/memory/list', slow_profile_load)
            await page.locator('#memory-manage > summary').click()
            await page.locator('#memory-load').click()
            await asyncio.wait_for(load_started.wait(), timeout=3)
            await page.locator('input[name="current_city"]').fill('')
            await page.evaluate('window.flushApplicationDetails()')
            release_load.set()
            await expect(page.locator('#memory-message')).to_contain_text('up to date')
            await expect(page.locator('input[name="current_city"]')).to_have_value('')
            await page.unroute('**/api/memory/list', slow_profile_load)
            assert memory.profile('candidate@example.com')['current_city'] == ''
            await page.reload()
            await expect(page.locator('input[name="email"]')).to_have_value('candidate@example.com')
            await expect(page.locator('input[name="first_name"]')).to_have_value('Avery')
            await expect(page.locator('input[name="current_city"]')).to_have_value('')

            await page.locator('input[name="email"]').fill('other@example.com')
            await page.locator('input[name="phone"]').click()
            await expect(page.locator('input[name="first_name"]')).to_have_value('Other')
            await expect(page.locator('input[name="current_city"]')).to_have_value('Other City')
            await page.locator('input[name="remember_questions"]').uncheck()
            await page.locator('input[name="phone"]').fill('9999999999')
            await page.evaluate('window.flushApplicationDetails()')
            assert memory.profile('other@example.com')['phone'] == '5550001234'
            await page.locator('input[name="remember_questions"]').check()
            await page.evaluate('window.flushApplicationDetails()')
            assert memory.profile('other@example.com')['phone'] == '9999999999'
            assert memory.profile('candidate@example.com')['phone'] == '1234567890'
            await page.evaluate('window.appPersistence.flush()')
            await page.locator('input[name="role"]').fill('')
            await page.locator('input[name="role"]').press_sequentially('Saved title on close')
            await page.locator('input[name="current_city"]').fill('')
            await page.locator('input[name="current_city"]').press_sequentially('Saved on close')
            dialogs = []
            async def finish_saving_then_leave(dialog):
                dialogs.append(dialog.type)
                for _ in range(100):
                    if (memory.profile('other@example.com')['current_city'] == 'Saved on close' and
                            LocalSettings(tmp_path).preferences().get('role') == 'Saved title on close'):
                        break
                    await asyncio.sleep(0.02)
                await dialog.accept()
            page.on('dialog', lambda dialog: asyncio.create_task(finish_saving_then_leave(dialog)))
            await page.goto('about:blank')
            await page.close()
            for _ in range(100):
                if (memory.profile('other@example.com')['current_city'] == 'Saved on close' and
                        LocalSettings(tmp_path).preferences().get('role') == 'Saved title on close' and page.is_closed()):
                    break
                await asyncio.sleep(0.02)
            assert memory.profile('other@example.com')['current_city'] == 'Saved on close', f'dialogs={dialogs}, closed={page.is_closed()}'
            assert LocalSettings(tmp_path).preferences()['role'] == 'Saved title on close'
            assert page.is_closed()
            await browser.close()
    finally:
        instance.should_exit = True
        await task
        listener.close()


async def test_loading_settings_does_not_overwrite_new_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DATA', tmp_path)
    LocalSettings(tmp_path).update_preferences({'role': 'Old role', 'provider': 'claude', 'model': 'claude-haiku-5-5'})
    instance, task, listener, url = await serve()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            started = asyncio.Event()
            release = asyncio.Event()
            async def slow_load(route):
                response = await route.fetch()
                started.set()
                await release.wait()
                await route.fulfill(response=response)
            await page.route('**/api/settings/preferences', slow_load)
            await page.goto(url)
            await asyncio.wait_for(started.wait(), timeout=3)
            await page.locator('input[name="role"]').fill('New role')
            await page.locator('input[name="api_key"]').fill('dummy-typed-test-key')
            release.set()
            await expect(page.locator('input[name="role"]')).to_have_value('New role')
            await expect(page.locator('input[name="api_key"]')).to_have_value('dummy-typed-test-key')
            await expect(page.locator('#provider')).to_have_value('openai')
            await expect(page.locator('#test-ai')).to_be_enabled()
            await page.unroute('**/api/settings/preferences', slow_load)
            await page.evaluate('window.appPersistence.flush()')
            saved = LocalSettings(tmp_path).preferences()
            assert saved['role'] == 'New role' and saved['provider'] == 'openai'
            assert 'dummy-typed-test-key' not in str(saved)
            failed = asyncio.Event()
            release_failure = asyncio.Event()
            requests = []
            async def fail_first(route):
                requests.append(True)
                if len(requests) == 1:
                    failed.set()
                    await release_failure.wait()
                    await route.fulfill(status=500, content_type='application/json', body='{"detail":"Storage unavailable."}')
                else:
                    await route.continue_()
            await page.route('**/api/settings/preferences', fail_first)
            await page.locator('input[name="role"]').fill('Old failed value')
            await asyncio.wait_for(failed.wait(), timeout=3)
            await page.locator('input[name="role"]').fill('Newest role')
            release_failure.set()
            await page.evaluate('window.appPersistence.flush()')
            await page.evaluate('window.appPersistence.flush()')
            assert LocalSettings(tmp_path).preferences()['role'] == 'Newest role'
            await browser.close()
    finally:
        instance.should_exit = True
        await task
        listener.close()


async def test_resume_chosen_before_email_is_saved_and_not_used_for_another_applicant(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'resume_text', lambda content: 'Example resume text')
    instance, task, listener, url = await serve()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url)
            await page.locator('input[name="resume"]').set_input_files({
                'name': 'sample.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-sample-resume',
            })
            await expect(page.locator('#resume-status')).to_contain_text('Enter your application email')
            await page.locator('input[name="email"]').fill('candidate@example.com')
            await page.locator('input[name="role"]').click()
            await expect(page.locator('#saved-resume-name')).to_have_text('sample.pdf')
            assert LocalSettings(tmp_path).resume('candidate@example.com')['content'] == b'%PDF-sample-resume'
            await page.locator('input[name="email"]').fill('other@example.com')
            await page.locator('input[name="role"]').click()
            await expect(page.locator('#saved-resume')).to_be_hidden()
            await expect(page.locator('input[name="resume"]')).to_be_visible()
            assert LocalSettings(tmp_path).resume('other@example.com') is None
            await browser.close()
    finally:
        instance.should_exit = True
        await task
        listener.close()


async def test_switching_email_keeps_unsaved_details_with_the_right_person(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DATA', tmp_path)
    memory = AnswerMemory(tmp_path)
    memory.save_profile('candidate@example.com', {'first_name': 'Avery', 'phone': '1234567890'})
    memory.save_profile('other@example.com', {'first_name': 'Other', 'phone': '5550001234'})
    instance, task, listener, url = await serve()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url)
            await page.locator('input[name="email"]').fill('candidate@example.com')
            await page.locator('input[name="phone"]').click()
            await expect(page.locator('input[name="first_name"]')).to_have_value('Avery')
            started = asyncio.Event()
            release = asyncio.Event()
            requests = []
            async def fail_first(route):
                requests.append(True)
                if len(requests) == 1:
                    started.set()
                    await release.wait()
                    await route.fulfill(status=500, content_type='application/json', body='{"detail":"Storage unavailable."}')
                else:
                    await route.continue_()
            await page.route('**/api/memory/profile-fields', fail_first)
            await page.locator('input[name="first_name"]').fill('Updated Avery')
            await asyncio.wait_for(started.wait(), timeout=3)
            await page.locator('input[name="email"]').fill('other@example.com')
            await page.locator('input[name="phone"]').click()
            await expect(page.locator('input[name="first_name"]')).to_have_value('Other')
            release.set()
            await page.locator('input[name="email"]').fill('candidate@example.com')
            await page.locator('input[name="phone"]').click()
            await expect(page.locator('input[name="first_name"]')).to_have_value('Updated Avery')
            await page.evaluate('window.flushApplicationDetails()')
            assert memory.profile('candidate@example.com')['first_name'] == 'Updated Avery'
            assert memory.profile('other@example.com')['first_name'] == 'Other'
            await browser.close()
    finally:
        instance.should_exit = True
        await task
        listener.close()
