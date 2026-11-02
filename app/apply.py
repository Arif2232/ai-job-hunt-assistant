import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.memory import is_placeholder, normalize_label, options_for, restricted_question
from src.models import Job

GREENHOUSE_HOSTS = {'boards.greenhouse.io', 'job-boards.greenhouse.io',
                    'boards.eu.greenhouse.io', 'job-boards.eu.greenhouse.io'}
LEVER_HOSTS = {'jobs.lever.co', 'jobs.eu.lever.co'}
ASHBY_HOSTS = {'jobs.ashbyhq.com'}
SUBMIT = re.compile(r'^(submit application|submit your application|send application|apply now|submit)$', re.I)
SUCCESS = re.compile(r'(application (?:has been |was )?(?:successfully )?(?:submitted|received)|thank you for applying|thanks for applying|we have received your application)', re.I)


class AnswerMemoryError(Exception):
    pass


def matches_job(url: str, job: Job) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        parts = parsed.path.strip('/').split('/')
        query = parse_qs(parsed.query, keep_blank_values=True)
        if job.source == 'greenhouse' and parsed.hostname in GREENHOUSE_HOSTS:
            if any(query[key] != [job.source_id] for key in ('gh_jid', 'token') if key in query):
                return False
            if parts == ['embed', 'job_app']:
                return query.get('for') == [job.board] and query.get('token') == [job.source_id]
            return len(parts) == 3 and parts[0].casefold() == job.board.casefold() and parts[1:] == ['jobs', job.source_id]
        if job.source == 'lever' and parsed.hostname in LEVER_HOSTS:
            return len(parts) in (2, 3) and parts[0].casefold() == job.board.casefold() and parts[1] == job.source_id and (len(parts) == 2 or parts[2] == 'apply')
        if job.source == 'ashby' and parsed.hostname in ASHBY_HOSTS:
            return len(parts) in (2, 3) and parts[0].casefold() == job.board.casefold() and parts[1] == job.source_id and (len(parts) == 2 or parts[2] == 'application')
        return False
    except ValueError:
        return False


def field_key(field: dict) -> str | None:
    def clean(value):
        return re.sub(r'[^a-z0-9]', '', (value or '').lower()).replace('required', '')

    known = {
        'firstname': 'first_name', 'lastname': 'last_name', 'fullname': 'full_name', 'name': 'full_name', 'email': 'email',
        'emailaddress': 'email', 'phone': 'phone', 'phonenumber': 'phone',
        'resume': 'resume', 'cv': 'resume', 'resumecv': 'resume', 'systemfieldresume': 'resume',
    }
    labels = {
        'first_name': {'firstname', 'yourfirstname', 'legalfirstname', 'givenname'},
        'last_name': {'lastname', 'yourlastname', 'legallastname', 'familyname', 'surname'},
        'full_name': {'name', 'fullname', 'yourname', 'applicantname', 'candidatename'},
        'email': {'email', 'emailaddress', 'youremail', 'youremailaddress'},
        'phone': {'phone', 'phonenumber', 'yourphone', 'yourphonenumber', 'mobilephone', 'mobilenumber'},
        'resume': {'resume', 'cv', 'resumecv', 'attach', 'attachresume', 'uploadresume', 'attachcv', 'uploadcv'},
    }
    keys = {known[clean(value)] for value in (field.get('id'), field.get('name')) if clean(value) in known}
    label = clean(field.get('label'))
    if len(keys) > 1:
        return None
    key = next(iter(keys), known.get(label))
    return key if key and (not label or label in labels[key]) else None


def preflight(fields: list[dict], resolved=None) -> tuple[dict[str, int], list[str]]:
    found = {}
    missing = []
    resolved = resolved or set()
    for index, field in enumerate(fields):
        if index in resolved:
            continue
        kind = field.get('type', '').lower()
        key = field_key(field)
        if key == 'resume' and kind != 'file':
            key = None
        if key and ((key == 'resume' and kind == 'file') or (key != 'resume' and kind in {'text', 'email', 'tel'})):
            if key in found:
                missing.append('Multiple fields match ' + key.replace('_', ' '))
            else:
                found[key] = index
        elif field.get('required'):
            missing.append(field.get('label') or 'Unrecognized required field')
    for key in ('email', 'phone', 'resume'):
        if key not in found:
            missing.append('Missing ' + key.replace('_', ' '))
    if 'full_name' not in found and not {'first_name', 'last_name'} <= found.keys():
        missing.append('Missing first and last name')
    return found, list(dict.fromkeys(missing))


DESCRIBE = r'''el => {
  const box = el.closest('fieldset,[role="group"],.application-question,.field,.application-field');
  const labels = [...(el.labels || [])].map(x => x.textContent.trim()).join(' ');
  const referenced = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent?.trim() || '').filter(Boolean).join(' ');
  const legend = box?.querySelector('legend,.application-label,[data-qa="question-label"]');
  const rawLabel = labels || referenced || el.getAttribute('aria-label') || legend?.textContent?.trim() || el.name || el.id || '';
  let label = rawLabel;
  if (el.closest('.phone-input__country')) label = 'Phone country (calling code)';
  const type = el.tagName === 'SELECT' ? (el.multiple ? 'select-multiple' : 'select-one') : (el.getAttribute('role') || el.type || el.tagName.toLowerCase());
  const radioGroup = type === 'radio' ? el.closest('fieldset,[role="radiogroup"]') : null;
  const groupIds = (radioGroup?.getAttribute('aria-labelledby') || '').split(/\s+/);
  const groupLabel = radioGroup?.querySelector('legend')?.textContent?.trim() || radioGroup?.getAttribute('aria-label') ||
    groupIds.map(id => document.getElementById(id)?.textContent?.trim() || '').filter(Boolean).join(' ');
  const optionLabel = type === 'radio' ? (labels || el.getAttribute('aria-label') || el.value || '') : '';
  if (type === 'radio') label = groupLabel || '';
  const options = el.tagName === 'SELECT' ? [...el.options].filter(x => !x.disabled && x.value !== '').map(x => x.textContent.trim()).filter(Boolean) : [];
  return {id: el.id || '', name: el.name || '', label, type, options, option_label: optionLabel,
    group: type === 'radio' ? (el.name || radioGroup?.id || '') : '',
    react_select: !!el.closest('.select-shell') && el.classList.contains('select__input'),
    required: !!el.required || el.getAttribute('aria-required') === 'true' || radioGroup?.getAttribute('aria-required') === 'true' || /[*✱✳＊]/.test(label + rawLabel)};
}'''


async def describe_controls(form):
    controls = []
    for locator in await form.locator('input:not([type="hidden"]),textarea,select,[role="combobox"],[role="checkbox"],[role="radio"]').all():
        control = await locator.element_handle(timeout=1000)
        if control is None:
            continue
        kind = (await control.get_attribute('type') or '').lower()
        if kind != 'file' and await control.get_attribute('aria-hidden') == 'true':
            continue
        if kind == 'file' or (await control.is_visible() and await control.is_enabled()):
            controls.append(control)
    if len(controls) > 100:
        return controls, []
    return controls, [await control.evaluate(DESCRIBE) for control in controls]


def question_for(field):
    label = re.sub(r'\s*(?:\(required\)|[*✱✳＊]+)\s*$', '', (field.get('label') or '').strip(), flags=re.I).strip()
    kind = (field.get('type') or '').lower()
    if not label or re.fullmatch(r'(?:question[_-]?\d+|search)', label, re.I):
        return {'label': 'Unrecognized required field', 'kind': 'manual', 'options': []}
    if kind in {'password', 'file', 'checkbox', 'select-multiple'} or restricted_question(label):
        return {'label': label, 'kind': 'manual', 'options': []}
    if kind in {'text', 'email', 'tel', 'url', 'number', 'textarea'}:
        return {'label': label, 'kind': 'text', 'options': []}
    if kind == 'select-one' or (kind == 'combobox' and field.get('react_select')):
        options = options_for({'options': field.get('options') or []})
        if len({normalize_label(option) for option in options}) != len(options):
            return {'label': label, 'kind': 'manual', 'options': []}
        return {'label': label, 'kind': 'choice', 'options': options}
    return {'label': label, 'kind': 'manual', 'options': []}


def profile_answer(question, candidate, field):
    label = normalize_label(question['label'])
    if label == 'location (city)':
        identifiers = {re.sub(r'[^a-z0-9]', '', (field.get(key) or '').lower()) for key in ('id', 'name')}
        if not identifiers.intersection({'candidatelocation', 'applicantlocation', 'currentcity', 'cityofresidence'}):
            return ''
    fields = {
        'what is your current country of residence?': 'current_country',
        'current country of residence': 'current_country', 'country of residence': 'current_country',
        'current city': 'current_city', 'city of residence': 'current_city', 'location (city)': 'current_city',
        'preferred first name': 'preferred_first_name', 'phone country (calling code)': 'phone_country',
    }
    return candidate.get(fields.get(label, ''), '').strip()


def answer_plan(fields, candidate, job, memory):
    resolved, plan, unanswered = set(), [], []
    candidates = []
    radio_groups = {}
    for index, field in enumerate(fields):
        if field.get('type') == 'radio':
            key = (field.get('group') or '', normalize_label(field.get('label') or ''))
            radio_groups.setdefault(key, []).append((index, field))
            continue
        if field_key(field) or (not field.get('required') and not memory):
            continue
        question = question_for(field)
        if not field.get('required') and question['kind'] == 'manual':
            continue
        candidates.append(([index], field, question, None, bool(field.get('required'))))
    for (group, label), members in radio_groups.items():
        required = any(field.get('required') for _, field in members)
        if not required and not memory:
            continue
        indexes = [index for index, _ in members]
        field = members[0][1]
        options = [item.get('option_label', '').strip() for _, item in members]
        question = question_for(field)
        if (group and label and not restricted_question(field['label']) and all(options)
                and len({normalize_label(value) for value in options}) == len(options)):
            question = {'label': re.sub(r'\s*[*✱✳＊]+\s*$', '', field['label']).strip(), 'kind': 'choice', 'options': options}
        if not required and question['kind'] == 'manual':
            continue
        candidates.append((indexes, field, question, options, required))
    counts = {}
    for _, _, question, _, _ in candidates:
        key = (normalize_label(question['label']), question['kind'])
        counts[key] = counts.get(key, 0) + 1
    for indexes, field, question, radio_options, required in candidates:
        key = (normalize_label(question['label']), question['kind'])
        if counts[key] > 1:
            if required:
                unanswered.append({'label': question['label'] + ' (multiple fields)', 'original_label': question['label'],
                                   'kind': 'manual', 'options': []})
            continue
        provided = profile_answer(question, candidate, field)
        try:
            saved = memory.lookup(candidate['email'], job, question) if memory else None
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise AnswerMemoryError from exc
        if provided and saved and normalize_label(provided) != normalize_label(saved):
            label = normalize_label(question['label'])
            is_city = label in {'current city', 'city of residence', 'location (city)'}
            city_matches = is_city and normalize_label(saved).startswith(normalize_label(provided) + ',')
            phone_matches = label == 'phone country (calling code)' and bool(re.fullmatch(re.escape(normalize_label(provided)) + r' \+\d{1,4}', normalize_label(saved)))
            answer = saved if city_matches or phone_matches else None
        else:
            answer = saved or provided
        if answer and question['kind'] == 'choice' and question['options']:
            matches = [option for option in question['options'] if normalize_label(option) == normalize_label(answer)]
            answer = matches[0] if len(matches) == 1 else None
        if answer and normalize_label(question['label']) == 'phone country (calling code)':
            code = re.search(r'\+(\d{1,4})\s*$', answer)
            phone = candidate.get('phone', '').strip()
            if code and phone.startswith('+') and not re.sub(r'\D', '', phone).startswith(code.group(1)):
                answer = None
        if answer and question['kind'] in {'text', 'choice'}:
            resolved.update(indexes)
            selected = indexes[radio_options.index(answer)] if radio_options else indexes[0]
            plan.append((selected, fields[selected], question, answer))
        elif required:
            unanswered.append(question | {'index': indexes[0]})
    return resolved, plan, unanswered


async def combo_options(control, frame):
    try:
        await control.click(timeout=2000)
        if await control.input_value():
            await control.fill('', timeout=2000)
        list_id = await control.get_attribute('aria-controls')
        if not list_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', list_id):
            return []
        options = frame.locator(f'[id="{list_id}"] [role="option"]')
        await options.first.wait_for(state='visible', timeout=1500)
        values = await options.evaluate_all('els => els.filter(e => e.getAttribute("aria-disabled") !== "true").map(e => e.innerText.trim()).filter(Boolean).slice(0, 301)')
        return [value for value in values if not is_placeholder(value)] if len(values) <= 300 else []
    except Exception:
        return []
    finally:
        try:
            await control.press('Escape', timeout=1000)
        except Exception:
            pass


async def collect_options(controls, fields, questions, frame, candidate, memory):
    if not memory or not candidate.get('remember_questions', True):
        return
    for question in questions:
        index = question.get('index')
        if (index is not None and question['kind'] == 'choice' and fields[index]['type'] == 'combobox'
                and fields[index].get('react_select')):
            question['options'] = await combo_options(controls[index], frame)
            if len({normalize_label(option) for option in question['options']}) != len(question['options']):
                question['kind'] = 'manual'
                question['options'] = []


async def combo_value(control):
    return await control.evaluate("el => el.closest('.select-shell')?.querySelector('.select__single-value')?.textContent?.trim() || ''")


async def choose_combo_option(control, frame, answer):
    try:
        list_id = await control.get_attribute('aria-controls')
        if not list_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', list_id):
            return 'missing'
        box = frame.locator(f'[id="{list_id}"]')
        if await box.get_attribute('aria-multiselectable', timeout=1000) == 'true':
            return 'ambiguous'
        options = box.locator('[role="option"]')
        values = await options.evaluate_all('els => els.map((e, i) => ({index: i, text: e.innerText.trim(), disabled: e.getAttribute("aria-disabled") === "true", visible: !!e.getClientRects().length && getComputedStyle(e).visibility !== "hidden"}))')
        matching = [item['index'] for item in values if item['visible'] and not item['disabled'] and normalize_label(item['text']) == normalize_label(answer)]
        if len(matching) > 1:
            return 'ambiguous'
        if not matching:
            return 'missing'
        await options.nth(matching[0]).click(timeout=2000)
        return 'selected' if normalize_label(await combo_value(control)) == normalize_label(answer) else 'ambiguous'
    except Exception:
        return 'missing'


async def fill_answer(control, field, question, answer, frame):
    try:
        if question['kind'] == 'choice' and is_placeholder(answer):
            return False
        if question['kind'] == 'text':
            await control.fill(answer, timeout=5000)
            return await control.input_value() == answer
        if field['type'] == 'select-one':
            matches = [option for option in field.get('options', []) if not is_placeholder(option) and normalize_label(option) == normalize_label(answer)]
            if len(matches) != 1:
                return False
            await control.select_option(label=matches[0], timeout=5000)
            selected = await control.evaluate("el => [...el.selectedOptions].map(x => x.textContent.trim())")
            return len(selected) == 1 and normalize_label(selected[0]) == normalize_label(answer)
        if field['type'] == 'radio':
            await control.check(timeout=5000)
            return await control.is_checked()
        if field['type'] == 'combobox' and field.get('react_select'):
            await control.click(timeout=2000)
            outcome = await choose_combo_option(control, frame, answer)
            if outcome != 'missing':
                return outcome == 'selected'
            await control.fill(answer, timeout=2000)
            for _ in range(10):
                outcome = await choose_combo_option(control, frame, answer)
                if outcome != 'missing':
                    return outcome == 'selected'
                await frame.page.wait_for_timeout(200)
    except Exception:
        return False
    finally:
        if field['type'] == 'combobox' and field.get('react_select'):
            try:
                await control.press('Escape', timeout=1000)
            except Exception:
                pass
    return False


async def answer_matches(control, field, question, answer):
    if question['kind'] == 'text':
        return await control.input_value() == answer
    if field['type'] == 'select-one':
        selected = await control.evaluate("el => [...el.selectedOptions].map(x => x.textContent.trim())")
        return len(selected) == 1 and normalize_label(selected[0]) == normalize_label(answer)
    if field['type'] == 'radio':
        return await control.is_checked()
    if field['type'] == 'combobox' and field.get('react_select'):
        return normalize_label(await combo_value(control)) == normalize_label(answer)
    return False


async def same_field(control, expected):
    current = await control.evaluate(DESCRIBE)
    return all(current.get(key) == expected.get(key) for key in ('id', 'name', 'label', 'type', 'option_label', 'group'))


def remember_missing(memory, candidate, job, questions, missing):
    if not memory or not candidate.get('remember_questions', True):
        return
    known = {normalize_label(label) for item in questions for label in (item['label'], item.get('original_label', '')) if label}
    questions += [{'label': item, 'kind': 'manual', 'options': []} for item in missing
                  if normalize_label(item) not in known]
    try:
        memory.record_questions(candidate['email'], job, questions)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise AnswerMemoryError from exc


async def check_before_upload(form, frame, candidate, job, memory, values):
    # shortcut: after three changing-form passes, ask for manual review instead of retrying indefinitely.
    for _ in range(3):
        controls, fields = await describe_controls(form)
        if not fields and len(controls) > 100:
            remember_missing(memory, candidate, job, [{'label': 'The form has too many fields to check safely', 'kind': 'manual', 'options': []}], [])
            return None, 'The form has too many fields to check safely.'
        resolved, answers, questions = answer_plan(fields, candidate, job, memory) if memory else (set(), [], [])
        found, missing = preflight(fields, resolved)
        if missing:
            await collect_options(controls, fields, questions, frame, candidate, memory)
            remember_missing(memory, candidate, job, questions, missing)
            return None, 'Additional or unsupported fields: ' + ', '.join(missing[:5])
        for key in ('first_name', 'last_name', 'full_name', 'email', 'phone'):
            if key in found and (not await same_field(controls[found[key]], fields[found[key]]) or
                                 await controls[found[key]].input_value() != values[key]):
                return None, 'The form changed a contact field. Review it manually.'
        changed = False
        for index, field, question, answer in answers:
            if not await same_field(controls[index], field):
                changed = True
                break
            if not await answer_matches(controls[index], field, question, answer):
                if not await fill_answer(controls[index], field, question, answer, frame):
                    question['index'] = index
                    await collect_options(controls, fields, [question], frame, candidate, memory)
                    remember_missing(memory, candidate, job, [question], [])
                    return None, 'The form did not accept a saved answer. Review it manually.'
                changed = True
        if not changed:
            return (controls, fields, found), ''
        await frame.page.wait_for_timeout(350)
    controls, fields = await describe_controls(form)
    resolved, _, questions = answer_plan(fields, candidate, job, memory) if memory else (set(), [], [])
    _, missing = preflight(fields, resolved)
    await collect_options(controls, fields, questions, frame, candidate, memory)
    remember_missing(memory, candidate, job, questions, missing)
    return None, 'The form kept changing. Review it manually.'


async def find_application_form(page, job):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        forms = []
        for frame in page.frames:
            if not matches_job(frame.url, job):
                continue
            try:
                ready = await frame.evaluate("""() => {
                    if (!window.__remixContext) return true;
                    const email = document.querySelector('input#email,input[name="email"],input[type="email"]');
                    return !!email && Object.keys(email).some(key => key.startsWith('__reactProps$'));
                }""")
                if not ready:
                    continue
                for form in await frame.locator('form').all():
                    if await form.locator('input[type="email"], input#email, input[name="email"]').count() and await form.locator('input[type="file"]').count():
                        forms.append((frame, form))
            except Exception:
                continue
        if len(forms) == 1:
            return forms[0]
        await page.wait_for_timeout(200)
    return None


async def apply_to_job(page, job: Job, candidate: dict, resume: Path, ledger, *, memory=None) -> tuple[str, str]:
    reserved = False
    try:
        if not matches_job(job.apply_url, job):
            remember_missing(memory, candidate, job, [{'label': 'This application needs manual review', 'kind': 'manual', 'options': []}], [])
            return 'needs_attention', 'This application link is not a supported form.'
        if ledger.has_attempt(candidate['email'], job.key):
            return 'already_attempted', 'Already attempted; it will not be submitted twice.'
        await page.goto(job.apply_url, wait_until='domcontentloaded', timeout=30000)
        found_form = await find_application_form(page, job)
        if not found_form:
            remember_missing(memory, candidate, job, [{'label': 'Could not read the application form', 'kind': 'manual', 'options': []}], [])
            return 'needs_attention', 'Could not find one supported application form.'
        frame, form = found_form
        challenges = frame.locator('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], .g-recaptcha, [data-sitekey]')
        controls, fields = await describe_controls(form)
        if not fields and len(controls) > 100:
            remember_missing(memory, candidate, job, [{'label': 'The form has too many fields to check safely', 'kind': 'manual', 'options': []}], [])
            return 'needs_attention', 'The form has too many fields to check safely.'
        resolved, answers, questions = answer_plan(fields, candidate, job, memory) if memory else (set(), [], [])
        found, missing = preflight(fields, resolved)
        if missing:
            if await challenges.count():
                questions.append({'label': 'Employer verification required', 'kind': 'manual', 'options': []})
            await collect_options(controls, fields, questions, frame, candidate, memory)
            remember_missing(memory, candidate, job, questions, missing)
            return 'needs_attention', 'Additional or unsupported fields: ' + ', '.join(missing[:5])
        if await challenges.count():
            remember_missing(memory, candidate, job, [{'label': 'Employer verification required', 'kind': 'manual', 'options': []}], [])
            return 'needs_attention', 'This form requires verification.'
        values = {**candidate, 'full_name': f"{candidate['first_name']} {candidate['last_name']}"}
        for key in ('first_name', 'last_name', 'full_name', 'email', 'phone'):
            if key not in found:
                continue
            control = controls[found[key]]
            if not await same_field(control, fields[found[key]]):
                return 'needs_attention', 'The form changed while it was being checked.'
            await control.fill(values[key], timeout=5000)
            if await control.input_value() != values[key]:
                return 'needs_attention', 'The form did not accept a contact field.'
        for index, field, question, answer in answers:
            if not await same_field(controls[index], field) or not await fill_answer(controls[index], field, question, answer, frame):
                question['index'] = index
                await collect_options(controls, fields, [question], frame, candidate, memory)
                remember_missing(memory, candidate, job, [question], [])
                return 'needs_attention', 'The form did not accept a saved answer. Review it manually.'
        if answers:
            # shortcut: wait briefly for follow-up fields; slower changing forms may still need manual review.
            await page.wait_for_timeout(350)
        checked, message = await check_before_upload(form, frame, candidate, job, memory, values)
        if not checked:
            return 'needs_attention', message
        controls, fields, found = checked
        if await challenges.count():
            remember_missing(memory, candidate, job, [{'label': 'Employer verification required', 'kind': 'manual', 'options': []}], [])
            return 'needs_attention', 'This form requires verification.'
        upload = controls[found['resume']]
        if not await same_field(upload, fields[found['resume']]):
            return 'needs_attention', 'The form changed while it was being checked.'
        await upload.set_input_files(str(resume), timeout=10000)
        if not await upload.evaluate('(el, name) => el.files?.length === 1 && el.files[0].name === name', resume.name):
            return 'needs_attention', 'The resume could not be attached.'
        await page.wait_for_timeout(350)
        if await frame.evaluate('() => !!window.__remixContext'):
            try:
                await frame.get_by_text(resume.name, exact=True).first.wait_for(state='visible', timeout=10000)
            except Exception:
                return 'needs_attention', 'The site did not confirm the resume upload.'
        controls, fields = await describe_controls(form)
        if not fields and len(controls) > 100:
            remember_missing(memory, candidate, job, [{'label': 'The form has too many fields to check safely', 'kind': 'manual', 'options': []}], [])
            return 'needs_attention', 'The form has too many fields to check safely.'
        resolved, answers, questions = answer_plan(fields, candidate, job, memory) if memory else (set(), [], [])
        found, missing = preflight(fields, resolved)
        verification = bool(await challenges.count())
        if missing or verification:
            if verification:
                questions.append({'label': 'Employer verification required', 'kind': 'manual', 'options': []})
            await collect_options(controls, fields, questions, frame, candidate, memory)
            remember_missing(memory, candidate, job, questions, missing)
            return 'needs_attention', 'The form revealed additional questions or verification.'
        for key in ('first_name', 'last_name', 'full_name', 'email', 'phone'):
            if key in found and await controls[found[key]].input_value() != values[key]:
                return 'needs_attention', 'The site changed a contact field after the resume upload.'
        filled_after_upload = False
        for index, field, question, answer in answers:
            if not await same_field(controls[index], field):
                return 'needs_attention', 'The form changed while it was being checked.'
            if not await answer_matches(controls[index], field, question, answer):
                if not await fill_answer(controls[index], field, question, answer, frame):
                    question['index'] = index
                    await collect_options(controls, fields, [question], frame, candidate, memory)
                    remember_missing(memory, candidate, job, [question], [])
                    return 'needs_attention', 'The form did not accept a saved answer. Review it manually.'
                filled_after_upload = True
        if filled_after_upload:
            await page.wait_for_timeout(350)
        final_controls, final_fields = await describe_controls(form)
        final_resolved, final_answers, final_questions = answer_plan(final_fields, candidate, job, memory) if memory else (set(), [], [])
        final_found, final_missing = preflight(final_fields, final_resolved)
        if final_missing:
            await collect_options(final_controls, final_fields, final_questions, frame, candidate, memory)
            remember_missing(memory, candidate, job, final_questions, final_missing)
            return 'needs_attention', 'The form revealed additional questions.'
        for key in ('first_name', 'last_name', 'full_name', 'email', 'phone'):
            if key in final_found and (not await same_field(final_controls[final_found[key]], final_fields[final_found[key]]) or
                                       await final_controls[final_found[key]].input_value() != values[key]):
                return 'needs_attention', 'The site changed a contact field after the resume upload.'
        for index, field, question, answer in final_answers:
            if not await same_field(final_controls[index], field) or not await answer_matches(final_controls[index], field, question, answer):
                return 'needs_attention', 'The form changed a saved answer. Review it manually.'
        invalid = []
        for index, control in enumerate(final_controls):
            if await control.get_attribute('aria-invalid') == 'true':
                field = final_fields[index]
                question = ({'label': 'Check ' + field_key(field).replace('_', ' '), 'kind': 'manual', 'options': []}
                            if field_key(field) else question_for(field))
                invalid.append(question | {'index': index})
        if invalid:
            await collect_options(final_controls, final_fields, invalid, frame, candidate, memory)
            remember_missing(memory, candidate, job, invalid, [])
            return 'needs_attention', 'The site marked an answer invalid. Review it manually.'
        controls, found = final_controls, final_found
        if not await controls[found['resume']].evaluate('(el, name) => el.files?.length === 1 && el.files[0].name === name', resume.name):
            return 'needs_attention', 'The site removed the resume attachment.'
        if await challenges.count():
            remember_missing(memory, candidate, job, [{'label': 'Employer verification required', 'kind': 'manual', 'options': []}], [])
            return 'needs_attention', 'This form requires verification.'
        if not matches_job(frame.url, job):
            return 'needs_attention', 'The application changed to an unexpected page.'
        if not await form.evaluate('el => el.checkValidity()'):
            remember_missing(memory, candidate, job, [{'label': 'An unrecognized required field needs manual review', 'kind': 'manual', 'options': []}], [])
            return 'needs_attention', 'The application has unresolved required fields.'
        buttons = form.get_by_role('button', name=SUBMIT)
        if await buttons.count() != 1 or not await buttons.first.is_visible() or not await buttons.first.is_enabled():
            return 'needs_attention', 'Could not identify a single submit button.'
        before = await frame.locator('body').inner_text(timeout=5000)
        if not ledger.reserve(candidate['email'], job.key, job.company, job.title, job.apply_url):
            return 'already_attempted', 'Already attempted; it will not be submitted twice.'
        reserved = True
        try:
            await buttons.first.click(timeout=10000)
            for _ in range(20):
                await page.wait_for_timeout(1000)
                for current in page.frames:
                    if not matches_job(current.url, job):
                        continue
                    try:
                        text = await current.locator('body').inner_text(timeout=1000)
                    except Exception:
                        continue
                    if SUCCESS.search(text) and not SUCCESS.search(before):
                        ledger.finish(candidate['email'], job.key, 'submitted')
                        return 'submitted', 'The employer confirmed the application.'
        except Exception:
            pass
        ledger.finish(candidate['email'], job.key, 'uncertain')
        return 'uncertain', 'Submission could not be confirmed; check the employer page before retrying.'
    except AnswerMemoryError:
        if reserved:
            return 'uncertain', 'Submission could not be confirmed; check the employer page before retrying.'
        return 'needs_attention', 'Could not access saved answers. Check storage and review this job manually.'
    except Exception:
        if reserved:
            return 'uncertain', 'Submission could not be confirmed; check the employer page before retrying.'
        return 'needs_attention', 'The form could not be completed safely.'
