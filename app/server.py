import asyncio
import hmac
import io
import secrets
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader

from app.matching import PROVIDERS, selected_model
from app.runner import run_hunt
from app.storage import ApplicationLog

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / '.local'
WEB = ROOT / 'web'
TOKEN = secrets.token_urlsafe(32)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1'])
app.mount('/assets', StaticFiles(directory=WEB), name='assets')
state = {'task': None, 'events': [], 'stop': asyncio.Event()}
START_LOCK = asyncio.Lock()


class Candidate(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    phone: str = Field(min_length=5, max_length=40)
    role: str = Field(min_length=2, max_length=100)
    location: str = Field(default='', max_length=100)
    region: Literal['india', 'anywhere'] = 'india'
    max_applications: int = Field(default=10, ge=1, le=10)

    @field_validator('first_name', 'last_name', 'email', 'phone', 'role', 'location', mode='before')
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


@app.middleware('http')
async def local_headers(request: Request, call_next):
    if request.method == 'POST' and request.url.path == '/api/start':
        try:
            if 'content-length' not in request.headers:
                return HTMLResponse('A content length is required.', status_code=411)
            if int(request.headers['content-length']) > 5_500_000:
                return HTMLResponse('Resume is too large.', status_code=413)
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


@app.get('/api/status')
async def status(x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    task = state['task']
    return {'running': bool(task and not task.done()), 'events': state['events']}


@app.post('/api/stop')
async def stop(x_app_token: str | None = Header(default=None)):
    verify_token(x_app_token)
    state['stop'].set()
    return {'message': 'Stopping after the current job.'}


@app.post('/api/start')
async def start(
    first_name: str = Form(...), last_name: str = Form(...), email: str = Form(...),
    phone: str = Form(...), role: str = Form(...), location: str = Form(''), region: str = Form('india'),
    max_applications: int = Form(10), api_key: str = Form(...), consent: bool = Form(False),
    provider: str = Form('openai'), model: str = Form(''), workspace_id: str = Form(''),
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
                                  role=role, location=location, region=region, max_applications=max_applications).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail='Please check your contact details, job title, and application limit.') from exc
        try:
            model = selected_model(provider, model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        import re
        if workspace_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', workspace_id):
            raise HTTPException(status_code=400, detail='Enter a valid workspace ID.')
        if len(api_key.strip()) < 12 or len(api_key) > 500:
            raise HTTPException(status_code=400, detail='Enter your AI provider API key.')
        try:
            content = await resume.read(5_000_001)
            text = resume_text(content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await resume.close()
        DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
        DATA.chmod(0o700)
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

        async def worker():
            try:
                await run_hunt(candidate, path, text, api_key.strip(), ApplicationLog(DATA), report, state['stop'],
                               provider=provider, model=model, workspace_id=workspace_id)
            except Exception:
                report('error', 'The search could not finish. Check your connection and try again.')
            finally:
                path.unlink(missing_ok=True)

        state['task'] = asyncio.create_task(worker())
        return {'message': 'Your search has started.'}
