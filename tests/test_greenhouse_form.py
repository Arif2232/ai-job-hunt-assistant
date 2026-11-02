from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from app.apply import apply_to_job
from app.storage import ApplicationLog
from src.models import Job


HTML = '''<!doctype html><html><body><form>
<label for="first_name">First name *</label><input id="first_name" required>
<label for="last_name">Last name *</label><input id="last_name" required>
<label for="email">Email *</label><input id="email" type="email" required>
<label for="phone">Phone *</label><input id="phone" type="tel" required>
<label for="resume">Resume *</label><input id="resume" type="file" required>
EXTRA
<button type="submit">Submit Application</button></form>
<script>window.submits=0;document.querySelector('form').addEventListener('submit',e=>{
  e.preventDefault();window.submits++; CONFIRM
});</script></body></html>'''


def sample_job():
    return Job(source='greenhouse', source_id='123', board='example', company='Example',
               title='Manager', description='A job', location='Remote',
               url='https://job-boards.greenhouse.io/example/jobs/123',
               apply_url='https://job-boards.greenhouse.io/example/jobs/123')


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        yield page
        await browser.close()


async def test_success_is_confirmed_and_cannot_be_submitted_twice(page, tmp_path):
    html = HTML.replace('EXTRA', '').replace('CONFIRM', "document.body.textContent='Thank you for applying';")
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger)
    assert status == 'submitted'
    assert await page.evaluate('window.submits') == 1
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger)
    assert status == 'already_attempted'
    assert await page.evaluate('window.submits') == 1


async def test_unknown_required_field_stops_before_upload_or_submission(page, tmp_path):
    html = HTML.replace('EXTRA', '<label for="visa">Work authorization *</label><select id="visa" required><option value="">Choose</option></select>').replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, message = await apply_to_job(page, sample_job(), candidate, pdf, ledger)
    assert status == 'needs_attention' and 'Work authorization' in message
    assert await page.evaluate('window.submits') == 0
    assert await page.locator('#resume').evaluate('el => el.files.length') == 0
    assert not ledger.has_attempt(candidate['email'], sample_job().key)


async def test_uncertain_submission_is_never_retried(page, tmp_path):
    html = HTML.replace('EXTRA', '').replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    original_wait = page.wait_for_timeout

    async def fast_wait(_milliseconds):
        await original_wait(50)

    page.wait_for_timeout = fast_wait
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger)
    assert status == 'uncertain'
    assert await page.evaluate('window.submits') == 1
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger)
    assert status == 'already_attempted'
    assert await page.evaluate('window.submits') == 1


async def test_verification_prompt_is_skipped_before_submission(page, tmp_path):
    html = HTML.replace('EXTRA', '<div class="g-recaptcha"></div>').replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger)
    assert status == 'needs_attention'
    assert await page.evaluate('window.submits') == 0
    assert not ledger.has_attempt(candidate['email'], sample_job().key)


async def test_new_required_question_after_upload_prevents_submission(page, tmp_path):
    script = '''<script>document.querySelector('#resume').addEventListener('change',()=>{
      const label=document.createElement('label'); label.textContent='Additional question *';
      const field=document.createElement('input'); field.required=true; label.append(field);
      document.querySelector('form').append(label);
    });</script>'''
    html = HTML.replace('EXTRA', '').replace('CONFIRM', '').replace('</body>', script + '</body>')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger)
    assert status == 'needs_attention'
    assert await page.evaluate('window.submits') == 0
    assert not ledger.has_attempt(candidate['email'], sample_job().key)


async def test_waits_for_hydrated_form_before_filling(page, tmp_path):
    hydration = '''<script>window.__remixContext = {}; setTimeout(() => {
      document.querySelector('#email')['__reactProps$test'] = {};
    }, 400); document.querySelector('#resume').addEventListener('change', event => {
      const preview = document.createElement('span'); preview.textContent = event.target.files[0].name;
      document.querySelector('form').append(preview);
    });</script>'''
    html = HTML.replace('EXTRA', '').replace('CONFIRM', "document.body.textContent='Thank you for applying';").replace('</body>', hydration + '</body>')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger)
    assert status == 'submitted'
    assert ledger.has_attempt(candidate['email'], sample_job().key)


@pytest.mark.parametrize('source,url', [
    ('lever', 'https://jobs.lever.co/example/123/apply'),
    ('ashby', 'https://jobs.ashbyhq.com/example/123/application'),
])
async def test_other_supported_forms_submit_only_user_supplied_details(page, tmp_path, source, url):
    name_fields = '<label for="name">Name *</label><input id="name" required>'
    html = HTML.replace('<label for="first_name">First name *</label><input id="first_name" required>\n<label for="last_name">Last name *</label><input id="last_name" required>', name_fields)
    html = html.replace('EXTRA', '').replace('CONFIRM', "document.body.textContent='Thank you for applying';")
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    item = sample_job().model_copy(update={'source': source, 'apply_url': url, 'url': url})
    status, _ = await apply_to_job(page, item, candidate, pdf, ledger)
    assert status == 'submitted'
    assert await page.evaluate('window.submits') == 1


async def test_ledger_error_after_submission_is_reported_as_uncertain(page, tmp_path, monkeypatch):
    html = HTML.replace('EXTRA', '').replace('CONFIRM', "document.body.textContent='Thank you for applying';")
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    def fail_finish(*args):
        raise OSError('Temporary database failure')
    monkeypatch.setattr(ledger, 'finish', fail_finish)
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger)
    assert status == 'uncertain'
    assert ledger.has_attempt(candidate['email'], sample_job().key)
