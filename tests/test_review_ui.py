import asyncio
import io
import json
import socket
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright, expect
from pypdf import PdfWriter

from app.server import app


async def test_each_review_requires_an_individual_decision():
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
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        pdf = io.BytesIO()
        writer.write(pdf)
        reviews = [
            {'id': f'example-review-id-{index}', 'title': f'Engineer {index}', 'company': 'Example',
             'url': f'https://job-boards.greenhouse.io/example/jobs/{index}',
             'description': 'Build reliable services.', 'highlights': ['Built reliable services.'],
             'original_resume': 'Example original resume.', 'resume_truncated': False}
            for index in (1, 2)
        ]
        decisions = []
        previews = []
        edited_previews = []
        status_calls = 0
        preview_started = asyncio.Event()
        release_preview = asyncio.Event()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            async def fake_status(route):
                nonlocal status_calls
                status_calls += 1
                if status_calls == 1:
                    await asyncio.sleep(3.2)
                    await route.fulfill(status=200, content_type='application/json', body='{"running":false,"events":[],"review":null}')
                    return
                index = len(decisions)
                body = {'running': index < len(reviews), 'events': [], 'resume_mode': 'review', 'review': reviews[index] if index < len(reviews) else None}
                await route.fulfill(status=200, content_type='application/json', body=json.dumps(body))
            async def fake_preview(route):
                previews.append(route.request.url)
                await route.fulfill(status=200, content_type='application/pdf', body=pdf.getvalue())
            async def fake_decision(route):
                decisions.append(route.request.post_data or '')
                await route.fulfill(status=200, content_type='application/json', body='{"message":"Saved."}')
            async def fake_updated_preview(route):
                edited_previews.append(route.request.post_data or '')
                if len(edited_previews) == 1:
                    preview_started.set()
                    await release_preview.wait()
                await route.fulfill(status=200, content_type='application/pdf', body=pdf.getvalue())
            await page.route('**/api/status', fake_status)
            await page.route('**/api/review-resume?*', fake_preview)
            await page.route('**/api/review-preview', fake_updated_preview)
            await page.route('**/api/review', fake_decision)
            await page.goto(f'http://127.0.0.1:{port}/')
            await expect(page.locator('#review-card')).to_be_visible()
            await page.wait_for_timeout(1000)
            await expect(page.locator('#review-card')).to_be_visible()
            await expect(page.locator('#status')).to_have_text('Waiting for approval')
            await expect(page.locator('#approval-prompt')).to_be_visible()
            await expect(page.locator('input[name="resume_mode"][value="review"]')).to_be_checked()
            await expect(page.locator('input[name="resume_mode"][value="review"]')).to_be_disabled()
            await expect(page.locator('#review-job')).to_have_text('Engineer 1 · Example')
            await expect(page.locator('#review-highlights')).to_have_value('Built reliable services.')
            await page.get_by_text('View original resume text').click()
            await expect(page.locator('#review-original')).to_have_text('Example original resume.')
            await expect(page.locator('#review-download')).to_be_visible()
            await expect(page.locator('#review-approve')).to_be_enabled()
            async with page.expect_download() as download_info:
                await page.locator('#review-download').click()
            download = await download_info.value
            assert download.suggested_filename == 'tailored-resume.pdf'
            assert Path(await download.path()).read_bytes() == pdf.getvalue()
            await page.locator('#review-skip').click()
            await expect(page.locator('#review-job')).to_have_text('Engineer 2 · Example')
            await expect(page.locator('#review-approve')).to_be_enabled()
            await page.locator('#review-highlights').fill('')
            await page.locator('#review-retry').click()
            await expect(page.locator('#review-message')).to_contain_text('Use 1 to 6 highlights')
            await expect(page.locator('#review-approve')).to_be_disabled()
            await page.locator('#review-highlights').fill('Built reliable services.\nAdded a truthful detail')
            await expect(page.locator('#review-approve')).to_be_disabled()
            await expect(page.locator('#review-download')).to_be_hidden()
            await page.locator('#review-retry').click()
            await asyncio.wait_for(preview_started.wait(), timeout=2)
            await page.locator('#review-highlights').fill('Built reliable services.\nFinal truthful detail')
            release_preview.set()
            await expect(page.locator('#review-retry')).to_be_enabled()
            await expect(page.locator('#review-approve')).to_be_disabled()
            await page.locator('#review-retry').click()
            await expect(page.locator('#review-approve')).to_be_enabled()
            assert len(edited_previews) == 2 and 'Added a truthful detail' in edited_previews[0]
            assert 'Final truthful detail' in edited_previews[1]
            await page.locator('#review-approve').click()
            await expect(page.locator('#review-card')).to_be_hidden()
            await expect(page.locator('#approval-prompt')).to_be_hidden()
            await expect(page.locator('#review-original')).to_have_text('')
            await expect(page.locator('#review-highlights')).to_have_value('')
            assert len(previews) == 2 and len(decisions) == 2
            assert 'example-review-id-1' in decisions[0] and 'skip' in decisions[0]
            assert 'example-review-id-2' in decisions[1] and 'approve' in decisions[1]
            assert 'Final truthful detail' in decisions[1]
            assert all('api_key' not in body for body in decisions)
            await browser.close()
    finally:
        server.should_exit = True
        await task
        listener.close()
