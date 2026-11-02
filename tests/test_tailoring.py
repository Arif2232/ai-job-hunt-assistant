import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app import server, tailoring


async def test_automatic_highlights_must_be_complete_lines_from_the_resume(monkeypatch):
    resume = 'Example Person\nBuilt Java services for customers\nNo experience with Go'
    async def generate(*args, **kwargs):
        return tailoring.Highlights(lines=['Built Java services for customers'])
    monkeypatch.setattr(tailoring, 'ai_structured', generate)
    assert await tailoring.resume_highlights(None, resume, 'Engineer', 'Backend role', provider='openai', model='gpt-6-luna', api_key='dummy-key') == ['Built Java services for customers']
    async def invented(*args, **kwargs):
        return tailoring.Highlights(lines=['Go', 'Built Java services for customers'])
    monkeypatch.setattr(tailoring, 'ai_structured', invented)
    assert await tailoring.resume_highlights(None, resume, 'Engineer', 'Backend role', provider='openai', model='gpt-6-luna', api_key='dummy-key') == ['Built Java services for customers']
    async def entirely_invented(*args, **kwargs):
        return tailoring.Highlights(lines=['Go'])
    monkeypatch.setattr(tailoring, 'ai_structured', entirely_invented)
    with pytest.raises(ValueError):
        await tailoring.resume_highlights(None, resume, 'Engineer', 'Backend role', provider='openai', model='gpt-6-luna', api_key='dummy-key')


def test_automatic_pdf_keeps_the_original_and_escapes_highlights(tmp_path):
    original = tmp_path / 'original.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with original.open('wb') as handle: writer.write(handle)
    output = tmp_path / 'tailored.pdf'
    tailoring.make_tailored_pdf(original, ['Built <script> safely'], output)
    pages = PdfReader(output).pages
    assert len(pages) == 2
    assert 'Built <script> safely' in pages[0].extract_text()


async def test_draft_is_reviewable_and_separates_missing_skills(monkeypatch):
    seen = {}
    async def generate(*args, **kwargs):
        seen.update(kwargs)
        return tailoring.ResumeDraft(resume='Example resume draft', missing_skills=['Example skill'])
    monkeypatch.setattr(tailoring, 'ai_structured', generate)
    draft = await tailoring.resume_draft(None, 'Example resume text', 'Example job description',
                                          provider='openai', model='gpt-6-luna', api_key='dummy-key')
    assert draft.resume == 'Example resume draft' and draft.missing_skills == ['Example skill']
    assert 'do not invent' in seen['system'].lower()
    assert seen['payload']['resume'] == 'Example resume text'


@pytest.mark.parametrize('provider', ['gemini', 'claude'])
async def test_resume_drafts_work_with_the_selected_provider(provider):
    def handler(request):
        payload = json.loads(request.content)
        draft = json.dumps({'resume': 'Example draft', 'missing_skills': ['Example skill']})
        if provider == 'gemini':
            assert payload['response_format']['schema'] == tailoring.DRAFT_SCHEMA
            return httpx.Response(200, json={'status': 'completed', 'steps': [{'type': 'model_output', 'content': [{'type': 'text', 'text': draft}]}]})
        assert payload['output_config']['format']['schema'] == tailoring.DRAFT_SCHEMA
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': draft}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        draft = await tailoring.resume_draft(client, 'Example resume', 'Example job description',
                                              provider=provider, model='', api_key='dummy-key')
    assert draft.resume == 'Example draft' and draft.missing_skills == ['Example skill']


def test_draft_endpoint_requires_verified_settings_and_sends_no_application(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    calls = []
    async def draft(client, resume, description, **settings):
        calls.append((resume, description, settings))
        return tailoring.ResumeDraft(resume='Example draft', missing_skills=['Example skill'])
    monkeypatch.setattr(server, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(server, 'resume_text', lambda content: 'Example resume text')
    monkeypatch.setattr(server, 'resume_draft', draft, raising=False)
    monkeypatch.setattr(server, 'DATA', tmp_path)
    form = {'provider': 'openai', 'model': 'gpt-6-luna', 'api_key': 'dummy-test-key-only',
            'description': 'Example role and requirements for a real job.'}
    ticket = server.make_connection_ticket('openai', 'gpt-6-luna', '', form['api_key'])
    files = {'resume': ('resume.pdf', b'%PDF-test', 'application/pdf')}
    headers = {'X-App-Token': server.TOKEN}
    with TestClient(server.app, base_url='http://127.0.0.1') as client:
        assert client.post('/api/tailor-resume', data=form, files=files).status_code == 403
        assert client.post('/api/tailor-resume', headers=headers, data=form, files=files).status_code == 400
        response = client.post('/api/tailor-resume', headers=headers, data=form | {'connection_ticket': ticket}, files=files)
    assert response.status_code == 200
    assert response.json() == {'resume': 'Example draft', 'missing_skills': ['Example skill']}
    assert 'dummy-test-key-only' not in response.text and len(calls) == 1
    assert not list(tmp_path.rglob('*'))
