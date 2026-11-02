import asyncio
import io
import socket

import uvicorn
from playwright.async_api import async_playwright, expect
from pypdf import PdfReader

from app.server import app


async def test_key_stays_in_the_tab_after_start_and_clears_on_provider_switch(tmp_path, monkeypatch):
    from app import server as app_server

    monkeypatch.setattr(app_server, 'DATA', tmp_path)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(32)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(f'http://127.0.0.1:{port}/')
            await page.wait_for_selector('#provider option[value="openai"]', state='attached')
            await expect(page.locator('#start')).to_be_disabled()
            assert await page.locator('#model').input_value() == 'gpt-6-luna'
            assert await page.locator('input[name="max_applications"]').count() == 0
            assert await page.get_by_text('up to 5 applications', exact=False).is_visible()
            assert await page.get_by_text('AI charges may increase', exact=False).is_visible()
            assert await page.locator('select[name="relocation"]').input_value() == 'unknown'
            assert await page.locator('select[name="experience_flexibility"]').input_value() == 'flexible'
            await page.locator('input[name="api_key"]').fill('dummy-key-not-real')
            await page.locator('input[name="consent"]').check()
            await page.select_option('#provider', 'gemini')
            assert await page.locator('input[name="api_key"]').input_value() == ''
            assert not await page.locator('input[name="consent"]').is_checked()
            assert await page.locator('#model').input_value() == 'gemini-3.5-flash-lite'
            assert await page.locator('#privacy-note').is_visible()
            await page.select_option('#provider', 'claude')
            assert await page.locator('#model').input_value() == 'claude-haiku-5-5'
            assert await page.locator('#workspace-id').is_visible()
            await page.select_option('#model', 'custom')
            assert await page.locator('#custom-model').is_visible()
            await page.select_option('#model', 'claude-haiku-5-5')
            for name, value in {'role': 'Manager', 'first_name': 'Avery', 'last_name': 'Example',
                                'email': 'candidate@example.com', 'phone': '1234567890',
                                'api_key': 'dummy-test-key-only'}.items():
                await page.locator(f'input[name="{name}"]').fill(value)
            await page.select_option('select[name="relocation"]', 'india')
            await page.select_option('select[name="experience_flexibility"]', 'one_year')
            assert await page.locator('input[name="resume_mode"]').count() == 3
            await page.locator('input[name="resume_mode"][value="review"]').check()
            assert 'approve each application' in await page.locator('#consent-text').inner_text()
            await page.locator('input[name="consent"]').check()
            await page.locator('input[name="resume_mode"][value="tailored"]').check()
            assert not await page.locator('input[name="consent"]').is_checked()
            await page.locator('input[name="remember_questions"]').uncheck()
            await page.locator('input[name="resume"]').set_input_files({
                'name': 'resume.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-test',
            })
            await page.locator('input[name="consent"]').check()
            tested = []
            async def fake_test(route):
                tested.append(route.request.post_data or '')
                if len(tested) == 1:
                    await route.fulfill(status=400, content_type='application/json', body='{"detail":"Connection failed. Check your API key."}')
                else:
                    await route.fulfill(status=200, content_type='application/json', body='{"message":"Connected. You can start your search.","verification":"example-ticket"}')
            await page.route('**/api/test-ai', fake_test)
            async with page.expect_response('**/api/test-ai'):
                await page.locator('#test-ai').click()
            await expect(page.locator('#test-ai-result')).to_contain_text('Connection failed')
            await expect(page.locator('#start')).to_be_disabled()
            async with page.expect_response('**/api/test-ai'):
                await page.locator('#test-ai').click()
            await expect(page.locator('#test-ai-result')).to_contain_text('Connected')
            await expect(page.locator('#start')).to_be_enabled()
            assert 'dummy-test-key-only' in tested[0] and 'claude-haiku-5-5' in tested[0]
            assert all(value not in tested[0] for value in ['candidate@example.com', '1234567890', 'resume.pdf'])
            assert await page.locator('input[name="api_key"]').input_value() == 'dummy-test-key-only'
            await page.locator('.draft-tool summary').click()
            await page.locator('#job-description').fill('Example job description with enough detail for a test.')
            drafts = []
            async def fake_draft(route):
                drafts.append(route.request.post_data or '')
                await route.fulfill(status=200, content_type='application/json', body='{"resume":"Example draft","missing_skills":["Example skill"]}')
            await page.route('**/api/tailor-resume', fake_draft)
            async with page.expect_response('**/api/tailor-resume'):
                await page.locator('#draft-button').click()
            await expect(page.locator('#draft-resume')).to_have_value('Example draft')
            await expect(page.locator('#missing-skills')).to_contain_text('Example skill')
            await page.locator('#draft-resume').fill('Reviewed draft')
            assert await page.locator('#print-draft').is_visible()
            await page.evaluate('window.print = () => {}')
            await page.locator('#print-draft').click()
            pdf = await page.pdf(format='A4')
            printed = '\n'.join(item.extract_text() for item in PdfReader(io.BytesIO(pdf)).pages)
            assert 'Reviewed draft' in printed and 'API key' not in printed
            assert 'resume.pdf' in drafts[0] and 'candidate@example.com' not in drafts[0]
            await page.locator('input[name="api_key"]').fill('different-dummy-key')
            await expect(page.locator('#start')).to_be_disabled()
            await page.locator('input[name="api_key"]').fill('dummy-test-key-only')
            async with page.expect_response('**/api/test-ai'):
                await page.locator('#test-ai').click()
            await expect(page.locator('#start')).to_be_enabled()
            sent = []
            async def fake_start(route):
                sent.append(route.request.post_data or '')
                await route.fulfill(status=200, content_type='application/json', body='{"message":"started"}')
            await page.route('**/api/start', fake_start)
            async with page.expect_response('**/api/start'):
                await page.locator('#start').click()
            await expect(page.locator('#start')).to_be_enabled()
            assert len(sent) == 1 and 'dummy-test-key-only' in sent[0]
            assert 'connection_ticket' in sent[0] and 'example-ticket' in sent[0]
            assert 'relocation' in sent[0] and 'india' in sent[0] and 'one_year' in sent[0]
            assert 'resume_mode' in sent[0] and 'tailored' in sent[0]
            assert 'remember_questions' in sent[0] and 'false' in sent[0]
            assert await page.locator('input[name="api_key"]').input_value() == 'dummy-test-key-only'
            await page.route('**/api/status', lambda route: route.fulfill(
                status=200, content_type='application/json', body='{"running":false,"events":[{"kind":"skipped","message":"Not a close match","url":"https://job-boards.greenhouse.io/example/jobs/123"},{"kind":"error","message":"AI failed"}]}'))
            async with page.expect_response('**/api/start'):
                await page.locator('#start').click()
            await expect(page.locator('#start')).to_be_enabled()
            await expect(page.locator('#status')).to_have_text('Needs attention')
            await expect(page.get_by_role('link', name='Open job ↗')).to_have_attribute('href', 'https://job-boards.greenhouse.io/example/jobs/123')
            assert len(sent) == 2 and 'dummy-test-key-only' in sent[1]
            assert await page.locator('input[name="api_key"]').input_value() == 'dummy-test-key-only'
            await page.select_option('#provider', 'openai')
            assert await page.locator('input[name="api_key"]').input_value() == ''
            assert await page.locator('#test-ai-result').is_hidden()
            await expect(page.locator('#start')).to_be_disabled()
            await browser.close()
    finally:
        server.should_exit = True
        await task
        listener.close()
