import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from src.models import Job

GREENHOUSE_HOSTS = {'boards.greenhouse.io', 'job-boards.greenhouse.io',
                    'boards.eu.greenhouse.io', 'job-boards.eu.greenhouse.io'}
LEVER_HOSTS = {'jobs.lever.co', 'jobs.eu.lever.co'}
ASHBY_HOSTS = {'jobs.ashbyhq.com'}
SUBMIT = re.compile(r'^(submit application|submit your application|send application|apply now|submit)$', re.I)
SUCCESS = re.compile(r'(application (?:has been |was )?(?:successfully )?(?:submitted|received)|thank you for applying|thanks for applying|we have received your application)', re.I)


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
        'resume': {'resume', 'cv', 'resumecv', 'attachresume', 'uploadresume', 'attachcv', 'uploadcv'},
    }
    keys = {known[clean(value)] for value in (field.get('id'), field.get('name')) if clean(value) in known}
    label = clean(field.get('label'))
    if len(keys) > 1:
        return None
    key = next(iter(keys), known.get(label))
    return key if key and (not label or label in labels[key]) else None


def preflight(fields: list[dict]) -> tuple[dict[str, int], list[str]]:
    found = {}
    missing = []
    for index, field in enumerate(fields):
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
  const legend = box?.querySelector('legend,.application-label,[data-qa="question-label"]');
  const label = labels || legend?.textContent?.trim() || el.getAttribute('aria-label') || el.name || el.id || '';
  return {id: el.id || '', name: el.name || '', label, type: el.getAttribute('role') || el.type || el.tagName.toLowerCase(),
    required: !!el.required || el.getAttribute('aria-required') === 'true' ||
      !!box?.querySelector('[data-required="true"],[aria-required="true"]') || /[*✱✳＊]/.test(label)};
}'''


async def describe_controls(form):
    controls = []
    for control in await form.locator('input:not([type="hidden"]),textarea,select,[role="combobox"],[role="checkbox"],[role="radio"]').all():
        kind = (await control.get_attribute('type') or '').lower()
        if kind == 'file' or (await control.is_visible() and await control.is_enabled()):
            controls.append(control)
    if len(controls) > 100:
        return controls, {}, ['The form has too many fields to check safely.']
    found, missing = preflight([await control.evaluate(DESCRIBE) for control in controls])
    return controls, found, missing


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


async def apply_to_job(page, job: Job, candidate: dict, resume: Path, ledger) -> tuple[str, str]:
    if not matches_job(job.apply_url, job):
        return 'needs_attention', 'This application link is not a supported form.'
    if ledger.has_attempt(candidate['email'], job.key):
        return 'already_attempted', 'Already attempted; it will not be submitted twice.'
    reserved = False
    try:
        await page.goto(job.apply_url, wait_until='domcontentloaded', timeout=30000)
        found_form = await find_application_form(page, job)
        if not found_form:
            return 'needs_attention', 'Could not find one supported application form.'
        frame, form = found_form
        challenges = frame.locator('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], .g-recaptcha, [data-sitekey]')
        if await challenges.count():
            return 'needs_attention', 'This form requires verification.'
        controls, found, missing = await describe_controls(form)
        if missing:
            return 'needs_attention', 'Additional or unsupported fields: ' + ', '.join(missing[:5])
        values = {**candidate, 'full_name': f"{candidate['first_name']} {candidate['last_name']}"}
        for key in ('first_name', 'last_name', 'full_name', 'email', 'phone'):
            if key not in found:
                continue
            control = controls[found[key]]
            await control.fill(values[key], timeout=5000)
            if await control.input_value() != values[key]:
                return 'needs_attention', 'The form did not accept a contact field.'
        upload = controls[found['resume']]
        await upload.set_input_files(str(resume), timeout=10000)
        if not await upload.evaluate('(el, name) => el.files?.length === 1 && el.files[0].name === name', resume.name):
            return 'needs_attention', 'The resume could not be attached.'
        if await frame.evaluate('() => !!window.__remixContext'):
            try:
                await frame.get_by_text(resume.name, exact=True).first.wait_for(state='visible', timeout=10000)
            except Exception:
                return 'needs_attention', 'The site did not confirm the resume upload.'
        controls, found, missing = await describe_controls(form)
        if missing or await challenges.count():
            return 'needs_attention', 'The form revealed additional questions or verification.'
        for key in ('first_name', 'last_name', 'full_name', 'email', 'phone'):
            if key in found and await controls[found[key]].input_value() != values[key]:
                return 'needs_attention', 'The site changed a contact field after the resume upload.'
        if not await controls[found['resume']].evaluate('(el, name) => el.files?.length === 1 && el.files[0].name === name', resume.name):
            return 'needs_attention', 'The site removed the resume attachment.'
        if not matches_job(frame.url, job) or not await form.evaluate('el => el.checkValidity()'):
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
    except Exception:
        if reserved:
            return 'uncertain', 'Submission could not be confirmed; check the employer page before retrying.'
        return 'needs_attention', 'The form could not be completed safely.'
