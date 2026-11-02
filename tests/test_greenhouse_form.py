from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from app.apply import apply_to_job
from app.memory import AnswerMemory
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


async def test_unsupported_form_is_queued_for_manual_review(tmp_path):
    memory = AnswerMemory(tmp_path / 'state')
    job = sample_job().model_copy(update={'apply_url': 'https://employer.example/careers/123'})
    candidate = {'email': 'candidate@example.com'}
    status, _ = await apply_to_job(None, job, candidate, tmp_path / 'resume.pdf', None, memory=memory)
    assert status == 'needs_attention'
    questions = memory.questions(candidate['email'])
    assert len(questions) == 1 and questions[0]['kind'] == 'manual'
    assert questions[0]['url'] == job.apply_url

    candidate = {'email': 'other@example.com', 'remember_questions': False}
    await apply_to_job(None, job, candidate, tmp_path / 'resume.pdf', None, memory=memory)
    assert memory.questions(candidate['email']) == []


async def test_unanswered_questions_are_saved_then_reused_on_the_same_form(page, tmp_path):
    extra = '''<label for="country">What is your current country of residence? *</label>
      <select id="country" required><option value="-1">Choose an answer...</option><option value="IN">India</option><option value="CA">Canada</option></select>
      <label for="notice">Notice period in days *</label><input id="notice" type="number" required>'''
    confirm = "window.answers={country:document.querySelector('#country').value,notice:document.querySelector('#notice').value};document.body.textContent='Thank you for applying';"
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', confirm)
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert await page.locator('#resume').evaluate('el => el.files.length') == 0
    questions = memory.questions(candidate['email'])
    assert {item['label'].rstrip(' *') for item in questions} == {'What is your current country of residence?', 'Notice period in days'}
    assert next(item['options'] for item in questions if 'country' in item['label'].lower()) == ['India', 'Canada']
    for question in questions:
        answer = 'India' if 'country' in question['label'].lower() else '30'
        memory.save_answer(candidate['email'], question['id'], answer, reuse_globally=False)
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'submitted' and await page.evaluate('window.submits') == 1
    assert await page.evaluate('window.answers') == {'country': 'IN', 'notice': '30'}


async def test_explicit_profile_details_fill_matching_personal_fields(page, tmp_path):
    extra = '''<label for="preferred_name">Preferred First Name *</label><input id="preferred_name" required>
      <label for="candidate-location">Location (City) *</label><input id="candidate-location" required>
      <label for="residence">What is your current country of residence? *</label>
      <select id="residence" required><option value="">Choose</option><option value="IN">India</option></select>
      <div class="phone-input__country"><label for="dial">Country *</label>
      <select id="dial" required><option value="">Choose</option><option value="91">India +91</option></select></div>'''
    confirm = "window.answers={preferred:document.querySelector('#preferred_name').value,city:document.querySelector('#candidate-location').value,residence:document.querySelector('#residence').value,dial:document.querySelector('#dial').value};document.body.textContent='Thank you for applying';"
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', confirm)
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890',
                 'preferred_first_name': 'Avery', 'current_city': 'Example City', 'current_country': 'India', 'phone_country': 'India +91'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'submitted' and memory.questions(candidate['email']) == []
    assert await page.evaluate('window.answers') == {'preferred': 'Avery', 'city': 'Example City', 'residence': 'IN', 'dial': '91'}


async def test_explicitly_saved_optional_answer_can_be_used(page, tmp_path):
    extra = '<label for="linkedin">LinkedIn profile</label><input id="linkedin" type="url">'
    confirm = "window.answer=document.querySelector('#linkedin').value;document.body.textContent='Thank you for applying';"
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', confirm)
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    memory.add_answer(candidate['email'], 'LinkedIn profile', 'text', 'https://example.com/profile')
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'submitted' and await page.evaluate('window.answer') == 'https://example.com/profile'


@pytest.mark.parametrize('reuse_globally,expected', [(False, 'needs_attention'), (True, 'submitted')])
async def test_answer_crosses_employers_only_when_the_user_chooses(page, tmp_path, reuse_globally, expected):
    extra = '<label for="notice">Notice period? *</label><input id="notice" required>'
    confirm = "window.answer=document.querySelector('#notice').value;document.body.textContent='Thank you for applying';"
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', confirm)
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    memory.record_questions(candidate['email'], sample_job(), [{'label': 'Notice period?', 'kind': 'text'}])
    memory.save_answer(candidate['email'], memory.questions(candidate['email'])[0]['id'], '30 days', reuse_globally=reuse_globally)
    other = sample_job().model_copy(update={'source_id': '456', 'board': 'other', 'company': 'Other',
        'url': 'https://job-boards.greenhouse.io/other/jobs/456', 'apply_url': 'https://job-boards.greenhouse.io/other/jobs/456'})
    status, _ = await apply_to_job(page, other, candidate, pdf, ledger, memory=memory)
    assert status == expected
    if reuse_globally:
        assert await page.evaluate('window.answer') == '30 days'
    else:
        assert await page.evaluate('window.submits') == 0


@pytest.mark.parametrize('option_label', ['India', 'India +91'])
async def test_saved_answer_fills_a_react_style_required_dropdown(page, tmp_path, option_label):
    extra = '''<label for="resident">Country of residence? *</label>
      <div class="select-shell"><div class="select__value-container">
      <input id="resident" class="select__input" type="text" role="combobox" aria-required="true" aria-expanded="false">
      </div><div id="resident-list" role="listbox" hidden>
      <div role="option" data-search="India">OPTION_LABEL</div><div role="option">Canada</div></div>
      <input required aria-hidden="true" tabindex="-1" style="position:absolute;opacity:0;width:1px;height:1px;pointer-events:none"></div>
      <script>
      const field=document.querySelector('#resident'), list=document.querySelector('#resident-list');
      field.addEventListener('click',()=>{list.hidden=false;field.setAttribute('aria-controls','resident-list');});
      field.addEventListener('input',()=>{for(const item of list.children) item.hidden=!(item.dataset.search||item.textContent).toLowerCase().includes(field.value.toLowerCase());});
      field.addEventListener('keydown',e=>{if(e.key==='Escape')list.hidden=true;});
      for(const option of list.children) option.addEventListener('click',()=>{
        let selected=document.querySelector('.select__single-value');
        if(!selected){selected=document.createElement('span');selected.className='select__single-value';field.parentElement.prepend(selected);}
        selected.textContent=option.textContent; document.querySelector('[aria-hidden="true"]').value='selected';
        field.value=''; list.hidden=true;
      });
      </script>'''.replace('OPTION_LABEL', option_label)
    confirm = "window.answer=document.querySelector('.select__single-value').textContent;document.body.textContent='Thank you for applying';"
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', confirm)
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, message = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and 'Unrecognized' not in message
    questions = memory.questions(candidate['email'])
    assert len(questions) == 1 and questions[0]['options'] == [option_label, 'Canada']
    memory.save_answer(candidate['email'], questions[0]['id'], option_label, reuse_globally=False)
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'submitted' and await page.evaluate('window.answer') == option_label


async def test_radio_question_is_saved_as_one_question_and_reused(page, tmp_path):
    extra = '''<fieldset><legend>Are you authorized to work here? *</legend>
      <label><input type="radio" name="authorized" value="yes" required>Yes</label>
      <label><input type="radio" name="authorized" value="no">No</label></fieldset>'''
    confirm = "window.answer=document.querySelector('input[name=authorized]:checked').value;document.body.textContent='Thank you for applying';"
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', confirm)
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    questions = memory.questions(candidate['email'])
    assert len(questions) == 1 and questions[0]['label'] == 'Are you authorized to work here?'
    assert questions[0]['options'] == ['Yes', 'No']
    memory.save_answer(candidate['email'], questions[0]['id'], 'Yes', reuse_globally=False)
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'submitted' and await page.evaluate('window.answer') == 'yes'


async def test_accessible_custom_radio_uses_only_the_saved_choice(page, tmp_path):
    extra = '''<div role="radiogroup" id="work-group" aria-label="Work eligibility?" aria-required="true">
      <div role="radio" tabindex="0" aria-label="Yes" aria-checked="false">Yes</div>
      <div role="radio" tabindex="0" aria-label="No" aria-checked="false">No</div></div>
      <script>for(const el of document.querySelectorAll('[role=radio]')) el.addEventListener('click',()=>{
        for(const other of document.querySelectorAll('[role=radio]')) other.setAttribute('aria-checked','false');
        el.setAttribute('aria-checked','true');
      });</script>'''
    confirm = "window.answer=document.querySelector('[role=radio][aria-checked=true]').textContent;document.body.textContent='Thank you for applying';"
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', confirm)
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention'
    question = memory.questions(candidate['email'])[0]
    assert question['label'] == 'Work eligibility?' and question['options'] == ['Yes', 'No']
    memory.save_answer(candidate['email'], question['id'], 'Yes', reuse_globally=False)
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'submitted' and await page.evaluate('window.answer') == 'Yes'


async def test_required_agreement_is_left_for_manual_review(page, tmp_path):
    extra = '<label><input type="checkbox" required> I agree to the terms and conditions *</label>'
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    questions = memory.questions(candidate['email'])
    assert len(questions) == 1 and questions[0]['kind'] == 'manual'
    with pytest.raises(ValueError):
        memory.save_answer(candidate['email'], questions[0]['id'], 'Yes', reuse_globally=True)


async def test_radio_agreement_is_also_left_for_manual_review(page, tmp_path):
    extra = '''<fieldset><legend>Do you agree to the terms and conditions? *</legend>
      <label><input type="radio" name="terms" value="yes" required>Yes</label>
      <label><input type="radio" name="terms" value="no">No</label></fieldset>'''
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert all(question['kind'] == 'manual' for question in memory.questions(candidate['email']))


async def test_resume_attach_label_is_recognized_without_guessing_other_files(page, tmp_path):
    html = HTML.replace('Resume *</label><input id="resume"', 'Attach</label><input id="resume" aria-hidden="true"')
    html = html.replace('EXTRA', '').replace('CONFIRM', "document.body.textContent='Thank you for applying';")
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'submitted' and await page.evaluate('window.submits') == 1


async def test_new_required_question_after_upload_is_remembered(page, tmp_path):
    script = '''<script>document.querySelector('#resume').addEventListener('change',()=>{
      const label=document.createElement('label'); label.textContent='Availability date *';
      const field=document.createElement('input'); field.required=true; label.append(field);
      document.querySelector('form').append(label);
    });</script>'''
    html = HTML.replace('EXTRA', '').replace('CONFIRM', '').replace('</body>', script + '</body>')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert any(question['label'] == 'Availability date' for question in memory.questions(candidate['email']))


async def test_site_changing_a_saved_answer_never_submits_it(page, tmp_path):
    extra = '<label for="notice">Notice period *</label><input id="notice" required>'
    script = '''<script>document.querySelector('#resume').addEventListener('change',()=>{
      const field=document.querySelector('#notice'); field.value='wrong';
      field.addEventListener('input',()=>{field.value='wrong';});
    });</script>'''
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '').replace('</body>', script + '</body>')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    memory.record_questions(candidate['email'], sample_job(), [{'label': 'Notice period', 'kind': 'text', 'options': []}])
    memory.save_answer(candidate['email'], memory.questions(candidate['email'])[0]['id'], '30 days', reuse_globally=False)
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert not ledger.has_attempt(candidate['email'], sample_job().key)


async def test_invalid_saved_value_is_asked_again_without_submitting(page, tmp_path):
    extra = '<label for="notice">Notice period in days *</label><input id="notice" type="number" required>'
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    memory.record_questions(candidate['email'], sample_job(), [{'label': 'Notice period in days', 'kind': 'text'}])
    question_id = memory.questions(candidate['email'])[0]['id']
    memory.save_answer(candidate['email'], question_id, 'thirty days', reuse_globally=False)
    status, message = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and 'saved answer' in message.lower()
    assert memory.questions(candidate['email'])[0]['pending']
    assert await page.evaluate('window.submits') == 0


async def test_aria_invalid_saved_answer_is_asked_again(page, tmp_path):
    extra = '<label for="notice">Notice period *</label><input id="notice" required aria-invalid="false">'
    script = '''<script>document.querySelector('#notice').addEventListener('input',event=>{
      event.target.setAttribute('aria-invalid','true');
    });</script>'''
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '').replace('</body>', script + '</body>')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    memory.record_questions(candidate['email'], sample_job(), [{'label': 'Notice period', 'kind': 'text'}])
    question_id = memory.questions(candidate['email'])[0]['id']
    memory.save_answer(candidate['email'], question_id, '30 days', reuse_globally=False)
    status, message = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and 'invalid' in message.lower()
    assert memory.questions(candidate['email'])[0]['pending']
    assert await page.evaluate('window.submits') == 0


async def test_question_revealed_by_a_saved_answer_is_remembered_before_upload(page, tmp_path):
    extra = '''<label for="eligible">Eligible? *</label><select id="eligible" required>
      <option value="">Choose</option><option value="yes">Yes</option></select>'''
    script = '''<script>document.querySelector('#eligible').addEventListener('change',()=>{
      if(document.querySelector('#detail'))return;
      const label=document.createElement('label'); label.htmlFor='detail'; label.textContent='Additional detail *';
      const field=document.createElement('input'); field.id='detail'; field.required=true;
      const resume=document.querySelector('#resume'); resume.before(label,field);
    });</script>'''
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '').replace('</body>', script + '</body>')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    memory.record_questions(candidate['email'], sample_job(), [{'label': 'Eligible?', 'kind': 'choice', 'options': ['Yes']}])
    memory.save_answer(candidate['email'], memory.questions(candidate['email'])[0]['id'], 'Yes', reuse_globally=False)
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert await page.locator('#resume').evaluate('el => el.files.length') == 0
    assert any(question['label'] == 'Additional detail' and question['pending'] for question in memory.questions(candidate['email']))


async def test_delayed_follow_up_is_collected_before_upload(page, tmp_path):
    extra = '''<label for="eligible">Eligible? *</label><select id="eligible" required>
      <option value="">Choose</option><option value="yes">Yes</option></select>'''
    script = '''<script>document.querySelector('#eligible').addEventListener('change',()=>setTimeout(()=>{
      if(document.querySelector('#detail'))return;
      const label=document.createElement('label'); label.htmlFor='detail'; label.textContent='Delayed detail *';
      const field=document.createElement('input'); field.id='detail'; field.required=true;
      document.querySelector('#resume').before(label,field);
    },200));</script>'''
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '').replace('</body>', script + '</body>')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    memory.record_questions(candidate['email'], sample_job(), [{'label': 'Eligible?', 'kind': 'choice', 'options': ['Yes']}])
    memory.save_answer(candidate['email'], memory.questions(candidate['email'])[0]['id'], 'Yes', reuse_globally=False)
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert await page.locator('#resume').evaluate('el => el.files.length') == 0
    assert any(question['label'] == 'Delayed detail' for question in memory.questions(candidate['email']))


async def test_memory_storage_failure_is_reported_without_submitting(page, tmp_path):
    extra = '<label for="notice">Notice period *</label><input id="notice" required>'
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    class BrokenMemory:
        def lookup(self, *args): raise OSError('Synthetic storage failure')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, message = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=BrokenMemory())
    assert status == 'needs_attention' and 'saved answers' in message.lower()
    assert await page.evaluate('window.submits') == 0


async def test_user_can_turn_off_saving_new_questions(page, tmp_path):
    extra = '<label for="notice">Notice period *</label><input id="notice" required>'
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com',
                 'phone': '1234567890', 'remember_questions': False}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and memory.questions(candidate['email']) == []
    assert await page.evaluate('window.submits') == 0


async def test_duplicate_question_labels_are_left_for_manual_review(page, tmp_path):
    extra = '''<label for="city1">Current city *</label><input id="city1" required>
      <label for="city2">Current city *</label><input id="city2" required>'''
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com',
                 'phone': '1234567890', 'current_city': 'Example City'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    questions = memory.questions(candidate['email'])
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert len(questions) == 1 and questions[0]['kind'] == 'manual'


async def test_duplicate_choice_labels_are_left_for_manual_review(page, tmp_path):
    extra = '''<label for="choice">Eligibility? *</label><select id="choice" required>
      <option value="">Choose</option><option value="a">Yes</option><option value="b">YES</option></select>'''
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    questions = memory.questions(candidate['email'])
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert len(questions) == 1 and questions[0]['kind'] == 'manual'


async def test_hidden_unrecognized_required_field_is_kept_for_manual_review(page, tmp_path):
    extra = '<input required aria-hidden="true" tabindex="-1" style="position:absolute;opacity:0;width:1px;height:1px;pointer-events:none">'
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert any(question['kind'] == 'manual' and 'required field' in question['label'].lower() for question in memory.questions(candidate['email']))


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


async def test_questions_are_collected_even_when_verification_is_required(page, tmp_path):
    extra = '<div class="g-recaptcha"></div><label for="city">Current city *</label><input id="city" required>'
    html = HTML.replace('EXTRA', extra).replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert any(question['label'] == 'Current city' for question in memory.questions(candidate['email']))
    assert any(question['label'] == 'Employer verification required' and question['kind'] == 'manual' for question in memory.questions(candidate['email']))
    assert await page.locator('#resume').evaluate('el => el.files.length') == 0


async def test_unreadable_form_is_kept_as_a_manual_action(page, tmp_path, monkeypatch):
    from app import apply as application

    html = HTML.replace('EXTRA', '').replace('CONFIRM', '')
    await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    async def no_form(*args): return None
    monkeypatch.setattr(application, 'find_application_form', no_form)
    pdf = tmp_path / 'resume.pdf'
    pdf.write_bytes(b'%PDF-fake')
    ledger = ApplicationLog(tmp_path / 'state')
    memory = AnswerMemory(tmp_path / 'state')
    candidate = {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com', 'phone': '1234567890'}
    status, _ = await apply_to_job(page, sample_job(), candidate, pdf, ledger, memory=memory)
    assert status == 'needs_attention' and await page.evaluate('window.submits') == 0
    assert any(question['label'] == 'Could not read the application form' and question['kind'] == 'manual' for question in memory.questions(candidate['email']))


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
