import asyncio
import hashlib
import hmac
import io
import json
import re
import secrets
import sqlite3
import tempfile
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader
from openai import AsyncOpenAI

from app.matching import PROVIDERS, ai_match, selected_model
from app.memory import AnswerMemory
from app.runner import ai_error_message, run_hunt
from app.storage import ApplicationLog
from app.tailoring import make_tailored_pdf, resume_draft
from src.models import Job

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / '.local'
WEB = ROOT / 'web'
TOKEN = secrets.token_urlsafe(32)
CONNECTION_SECRET = secrets.token_bytes(32)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1'])
app.mount('/assets', StaticFiles(directory=WEB), name='assets')
state = {'task': None, 'events': [], 'stop': asyncio.Event(), 'review': None, 'resume_mode': None}
START_LOCK = asyncio.Lock()


class Candidate(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    phone: str = Field(min_length=5, max_length=40)
    current_city: str = Field(default='', max_length=100)
    current_country: str = Field(default='', max_length=100)
    preferred_first_name: str = Field(default='', max_length=100)
    phone_country: str = Field(default='', max_length=200)
    remember_questions: bool = True
    role: str = Field(min_length=2, max_length=100)
    location: str = Field(default='', max_length=100)
    region: Literal['india', 'anywhere'] = 'india'
    relocation: Literal['unknown', 'india', 'anywhere', 'no'] = 'unknown'
    experience_flexibility: Literal['strict', 'one_year', 'flexible'] = 'strict'
    resume_mode: Literal['original', 'tailored', 'review'] = 'original'
    max_applications: int = Field(default=5, ge=1, le=10)

    @field_validator('first_name', 'last_name', 'email', 'phone', 'role', 'location', 'current_city',
                     'current_country', 'preferred_first_name', 'phone_country', mode='before')
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('email')
    @classmethod
    def check_email(cls, value):
        import re
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('Enter a valid email address.')
        return value


def verify_token(value: str | None):
    if not value or not hmac.compare_digest(value, TOKEN):
        raise HTTPException(status_code=403, detail='Refresh the page and try again.')


def validate_ai_settings(provider, model, workspace_id, api_key):
    try:
        model = selected_model(provider, model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if workspace_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', workspace_id):
        raise HTTPException(status_code=400, detail='Enter a valid workspace ID.')
    if len(api_key.strip()) < 12 or len(api_key) > 500:
        raise HTTPException(status_code=400, detail='Enter your AI provider API key.')
    return model


def make_connection_ticket(provider, model, workspace_id, api_key):
    settings = json.dumps([provider, model, workspace_id, api_key.strip()], separators=(',', ':')).encode()
    return hmac.new(CONNECTION_SECRET, settings, hashlib.sha256).hexdigest()


def verify_connection(provider, model, workspace_id, api_key, ticket):
    expected = make_connection_ticket(provider, model, workspace_id, api_key)
    if not re.fullmatch(r'[0-9a-f]{64}', ticket) or not hmac.compare_digest(ticket, expected):
        raise HTTPException(status_code=400, detail='Test your AI connection before starting.')


def memory_call(method, *args):
    try:
        return getattr(AnswerMemory(DATA), method)(*args)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=500, detail='Could not access saved answers on this computer.') from exc


def resume_text(content: bytes) -> str:
    if not content.startswith(b'%PDF-') or len(content) > 5_000_000:
        raise ValueError('Choose a PDF resume smaller than 5 MB.')
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 12:
            raise ValueError('Choose an unlocked PDF with at most 12 pages.')
        text = '\n'.join(page.extract_text() or '' for page in reader.pages).strip()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('The PDF could not be read. Try another resume.') from exc
    if len(text) < 50:
        raise ValueError('The PDF needs selectable text. Scanned images are not supported yet.')
    return text


def report(kind, message, job=None):
    event = {'kind': kind, 'message': message}
    if job is not None:
        event.update({'company': job.company, 'title': job.title, 'url': job.apply_url,
                      'posted_at': job.published_at.isoformat() if job.published_at else None})
    state['events'].append(event)
    state['events'] = state['events'][-200:]


async def wait_for_review(job, pdf: Path, highlights, resume, stop, *, original_pdf=None, candidate=None):
    future = asyncio.get_running_loop().create_future()
    pending = {'id': secrets.token_urlsafe(18), 'job': job, 'pdf': pdf, 'highlights': highlights,
               'resume': resume, 'future': future, 'original_pdf': original_pdf, 'candidate': candidate}
    state['review'] = pending
    report('awaiting_review', 'Review this job and tailored resume before applying.', job)
    stopped = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((future, stopped), return_when=asyncio.FIRST_COMPLETED)
        return 'stop' if stop.is_set() else future.result()
    finally:
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)
        if not future.done():
            future.cancel()
        if state.get('review') is pending:
            state['review'] = None


def pending_review(review_id):
    pending = state.get('review')
    if (not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', review_id) or not pending
            or pending['future'].done() or state['stop'].is_set()
            or not hmac.compare_digest(review_id, pending['id'])):
        raise HTTPException(status_code=409, detail='This job is no longer waiting for review.')
    return pending


def update_review_pdf(pending, text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not 1 <= len(lines) <= 6 or any(not 3 <= len(line) <= 200 or
                                        any(not char.isprintable() and char not in '\t\u00a0' for char in line) for line in lines):
        raise HTTPException(status_code=400, detail='Use 1 to 6 highlights, each 3 to 200 characters.')
    if lines != pending['highlights']:
        candidate = pending['candidate']
        if not candidate or not pending['original_pdf']:
            raise HTTPException(status_code=400, detail='This resume cannot be edited. Skip the job and try again.')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=pending['pdf'].parent, suffix='.pdf', delete=False) as handle:
                temporary = Path(handle.name)
            make_tailored_pdf(pending['original_pdf'], lines, temporary,
                              name=f"{candidate['first_name']} {candidate['last_name']}",
                              email=candidate['email'], phone=candidate['phone'], job_title=pending['job'].title)
            temporary.replace(pending['pdf'])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='Could not fit or display the highlights. Edit them and try again.') from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail='Could not prepare the tailored resume.') from exc
        finally:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        pending['highlights'] = lines


@app.middleware('http')
async def local_headers(request: Request, call_next):
    if request.method == 'POST' and (request.url.path.startswith('/api/memory/') or request.url.path in ('/api/start', '/api/test-ai', '/api/tailor-resume', '/api/review', '/api/review-preview')):
        try:
            if 'content-length' not in request.headers:
                return HTMLResponse('A content length is required.', status_code=411)
            limit = 20_000 if request.url.path.startswith('/api/memory/') else 10_000 if request.url.path in ('/api/test-ai', '/api/review', '/api/review-preview') else 5_500_000
            if int(request.headers['content-length']) > limit:
                return HTMLResponse('Request is too large.', status_code=413)
        except ValueError:
            return HTMLResponse('Invalid request.', status_code=400)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    return response


@app.get('/', response_class=HTMLResponse)
async def index():
    return (WEB / 'index.html').read_text().replace('__APP_TOKEN__', TOKEN)


@app.get('/api/options')
async def options(x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    return PROVIDERS


@app.post('/api/memory/list')
async def list_memory(email: str = Form(...), x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    return {'profile': memory_call('profile', email), 'questions': memory_call('questions', email)}


@app.post('/api/memory/profile')
async def save_memory_profile(email: str = Form(...), first_name: str = Form(''), last_name: str = Form(''),
                              phone: str = Form(''), current_city: str = Form(''), current_country: str = Form(''),
                              preferred_first_name: str = Form(''), phone_country: str = Form(''),
                              x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    profile = {'first_name': first_name, 'last_name': last_name, 'phone': phone, 'current_city': current_city,
               'current_country': current_country, 'preferred_first_name': preferred_first_name, 'phone_country': phone_country}
    memory_call('save_profile', email, profile)
    return {'message': 'Your details are saved on this computer.'}


@app.post('/api/memory/answer')
async def save_memory_answer(email: str = Form(...), question_id: int = Form(...), answer: str = Form(...),
                             reuse_globally: bool = Form(False), x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    memory_call('save_answer', email, question_id, answer, reuse_globally)
    return {'message': 'Answer saved.'}


@app.post('/api/memory/add')
async def add_memory_answer(email: str = Form(...), label: str = Form(...), kind: str = Form(...),
                            answer: str = Form(...), x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    memory_call('add_answer', email, label, kind, answer)
    return {'message': 'Answer saved.'}


@app.post('/api/memory/forget')
async def forget_memory_answer(email: str = Form(...), question_id: int = Form(...),
                               x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    if not memory_call('delete_question', email, question_id):
        raise HTTPException(status_code=404, detail='That question is no longer saved.')
    return {'message': 'Answer removed.'}


@app.post('/api/memory/clear-profile')
async def clear_memory_profile(email: str = Form(...), x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    memory_call('delete_profile', email)
    return {'message': 'Saved details removed.'}


@app.post('/api/memory/clear')
async def clear_memory(email: str = Form(...), x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    memory_call('clear', email)
    return {'message': 'Saved details and answers removed.'}


@app.get('/api/status')
async def status(x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    task = state['task']
    running = bool(task and not task.done())
    pending = state.get('review')
    review = None
    if pending and not pending['future'].done() and not state['stop'].is_set():
        job = pending['job']
        review = {'id': pending['id'], 'title': job.title, 'company': job.company, 'url': job.apply_url,
                  'description': job.description[:16000], 'highlights': pending['highlights'],
                  'original_resume': pending['resume'][:30000], 'resume_truncated': len(pending['resume']) > 30000}
    return {'running': running, 'events': state['events'], 'review': review,
            'resume_mode': state.get('resume_mode') if running else None}


@app.post('/api/stop')
async def stop(x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    state['stop'].set()
    return {'message': 'Stopping the search.'}


@app.get('/api/review-resume')
async def review_resume(review_id: str, x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    pending = pending_review(review_id)
    try:
        content = pending['pdf'].read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=500, detail='Could not open the tailored resume.') from exc
    return Response(content, media_type='application/pdf', headers={'Content-Disposition': 'inline; filename="tailored-resume.pdf"'})


@app.post('/api/review-preview')
async def review_preview(review_id: str = Form(...), highlights: str = Form(...),
                         x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    pending = pending_review(review_id)
    update_review_pdf(pending, highlights)
    try:
        content = pending['pdf'].read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=500, detail='Could not open the tailored resume.') from exc
    return Response(content, media_type='application/pdf', headers={'Content-Disposition': 'inline; filename="tailored-resume.pdf"'})


@app.post('/api/review')
async def review_decision(review_id: str = Form(...), action: str = Form(...),
                          highlights: str | None = Form(default=None),
                          x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    if action not in ('approve', 'skip'):
        raise HTTPException(status_code=400, detail='Choose approve or skip.')
    pending = pending_review(review_id)
    if action == 'approve' and highlights is not None:
        update_review_pdf(pending, highlights)
    pending['future'].set_result(action)
    return {'message': 'Applying to this job.' if action == 'approve' else 'Skipping this job.'}


@app.post('/api/test-ai')
async def test_ai_connection(provider: str = Form('openai'), model: str = Form(''),
                             api_key: str = Form(...), workspace_id: str = Form(''),
                             x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    model = validate_ai_settings(provider, model, workspace_id, api_key)
    example = Job(source='greenhouse', source_id='connection-test', board='example', company='Example',
                  title='Customer Support Specialist', description='Help customers with everyday questions.',
                  location='Remote', url='https://example.invalid/job', apply_url='https://example.invalid/apply')
    try:
        client = AsyncOpenAI(api_key=api_key.strip(), timeout=60, max_retries=1) if provider == 'openai' else httpx.AsyncClient(timeout=60)
        async with client:
            await asyncio.wait_for(ai_match(client, example, 'Example experience helping customers.',
                                            example.title, 'Remote', provider=provider, model=model,
                                            api_key=api_key.strip(), workspace_id=workspace_id), timeout=70)
    except Exception as exc:
        raise HTTPException(status_code=400, detail='Connection failed. ' + ai_error_message(exc)) from exc
    return {'message': 'Connected. You can start your search.',
            'verification': make_connection_ticket(provider, model, workspace_id, api_key)}


@app.post('/api/tailor-resume')
async def tailor_resume(description: str = Form(...), provider: str = Form('openai'), model: str = Form(''),
                        api_key: str = Form(...), workspace_id: str = Form(''), connection_ticket: str = Form(''),
                        resume: UploadFile = File(...), x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    model = validate_ai_settings(provider, model, workspace_id, api_key)
    verify_connection(provider, model, workspace_id, api_key, connection_ticket)
    if not 20 <= len(description.strip()) <= 16000:
        raise HTTPException(status_code=400, detail='Paste a job description between 20 and 16,000 characters.')
    try:
        text = resume_text(await resume.read(5_000_001))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await resume.close()
    try:
        client = AsyncOpenAI(api_key=api_key.strip(), timeout=60, max_retries=1) if provider == 'openai' else httpx.AsyncClient(timeout=60)
        async with client:
            draft = await asyncio.wait_for(resume_draft(client, text, description.strip(), provider=provider, model=model,
                                                        api_key=api_key.strip(), workspace_id=workspace_id), timeout=120)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=ai_error_message(exc)) from exc
    return draft.model_dump()


@app.post('/api/start')
async def start(
    first_name: str = Form(...), last_name: str = Form(...), email: str = Form(...),
    phone: str = Form(...), role: str = Form(...), location: str = Form(''), region: str = Form('india'),
    current_city: str = Form(''), current_country: str = Form(''), preferred_first_name: str = Form(''),
    phone_country: str = Form(''), remember_questions: bool = Form(True),
    relocation: str = Form('unknown'), experience_flexibility: str = Form('strict'),
    resume_mode: str = Form('original'),
    max_applications: int = Form(5), api_key: str = Form(...), consent: bool = Form(False),
    provider: str = Form('openai'), model: str = Form(''), workspace_id: str = Form(''),
    connection_ticket: str = Form(''),
    resume: UploadFile = File(...), x_app_token: str | None = Header(default=None),
):
    verify_token(x_app_token)
    async with START_LOCK:
        if state['task'] and not state['task'].done():
            raise HTTPException(status_code=409, detail='A job search is already running.')
        if not consent:
            raise HTTPException(status_code=400, detail='Please confirm before starting automatic applications.')
        try:
            candidate = Candidate(first_name=first_name, last_name=last_name, email=email, phone=phone,
                                  current_city=current_city, current_country=current_country,
                                  preferred_first_name=preferred_first_name, phone_country=phone_country,
                                  remember_questions=remember_questions,
                                  role=role, location=location, region=region, relocation=relocation,
                                  experience_flexibility=experience_flexibility, resume_mode=resume_mode,
                                  max_applications=max_applications).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='Please check your contact details, job title, and application limit.') from exc
        model = validate_ai_settings(provider, model, workspace_id, api_key)
        verify_connection(provider, model, workspace_id, api_key, connection_ticket)
        try:
            content = await resume.read(5_000_001)
            text = resume_text(content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await resume.close()
        DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
        DATA.chmod(0o700)
        try:
            memory = AnswerMemory(DATA)
            if candidate['remember_questions']:
                memory.remember_profile(candidate['email'], {name: candidate[name] for name in (
                    'first_name', 'last_name', 'phone', 'current_city', 'current_country', 'preferred_first_name', 'phone_country')})
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HTTPException(status_code=500, detail='Could not access saved answers on this computer.') from exc
        path = None
        try:
            with tempfile.NamedTemporaryFile(dir=DATA, suffix='.pdf', delete=False) as handle:
                path = Path(handle.name)
                handle.write(content)
            path.chmod(0o600)
        except OSError as exc:
            if path:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise HTTPException(status_code=500, detail='Could not prepare the resume. Check storage access and the .local folder.') from exc
        state['events'] = []
        state['stop'] = asyncio.Event()
        state['review'] = None
        state['resume_mode'] = candidate['resume_mode']

        async def worker():
            try:
                await run_hunt(candidate, path, text, api_key.strip(), ApplicationLog(DATA), report, state['stop'],
                               provider=provider, model=model, workspace_id=workspace_id, request_review=wait_for_review,
                               memory=memory)
            except Exception:
                report('error', 'The search could not finish. Check your connection and try again.')
            finally:
                path.unlink(missing_ok=True)

        state['task'] = asyncio.create_task(worker())
        return {'message': 'Your search has started.'}
