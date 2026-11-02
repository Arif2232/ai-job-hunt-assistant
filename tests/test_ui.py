import asyncio
import socket

import uvicorn
from playwright.async_api import async_playwright, expect

from app.server import app


async def test_key_stays_in_the_tab_after_start_and_clears_on_provider_switch():
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
            assert await page.locator('#model').input_value() == 'gpt-6-luna'
            assert await page.locator('input[name="max_applications"]').count() == 0
            assert await page.get_by_text('Each search attempts up to 10 applications', exact=False).is_visible()
            assert await page.get_by_text('AI checks up to 100 jobs', exact=False).is_visible()
            assert await page.get_by_text('More checks can increase API charges', exact=False).is_visible()
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
            await page.locator('input[name="resume"]').set_input_files({
                'name': 'resume.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-test',
            })
            await page.locator('input[name="consent"]').check()
            tested = []
            async def fake_test(route):
                tested.append(route.request.post_data or '')
                await route.fulfill(status=200, content_type='application/json', body='{"message":"AI connection works. No application was sent."}')
            await page.route('**/api/test-ai', fake_test)
            async with page.expect_response('**/api/test-ai'):
                await page.locator('#test-ai').click()
            await expect(page.locator('#test-ai-result')).to_contain_text('AI connection works')
            assert 'dummy-test-key-only' in tested[0] and 'claude-haiku-5-5' in tested[0]
            assert all(value not in tested[0] for value in ['candidate@example.com', '1234567890', 'resume.pdf'])
            assert await page.locator('input[name="api_key"]').input_value() == 'dummy-test-key-only'
            sent = []
            async def fake_start(route):
                sent.append(route.request.post_data or '')
                await route.fulfill(status=200, content_type='application/json', body='{"message":"started"}')
            await page.route('**/api/start', fake_start)
            async with page.expect_response('**/api/start'):
                await page.locator('#start').click()
            await expect(page.locator('#start')).to_be_enabled()
            assert len(sent) == 1 and 'dummy-test-key-only' in sent[0]
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
            await browser.close()
    finally:
        server.should_exit = True
        await task
        listener.close()
