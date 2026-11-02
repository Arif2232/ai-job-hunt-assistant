import httpx
import pytest
from fastapi.testclient import TestClient

from app import server
from app.matching import Decision


@pytest.mark.parametrize('provider,model', [
    ('openai', 'gpt-6-luna'), ('gemini', 'gemini-3.5-flash-lite'), ('claude', 'claude-haiku-5-5'),
])
def test_connection_uses_only_example_data_and_the_selected_model(monkeypatch, provider, model):
    calls = []
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def match(client, job, resume, role, location, **settings):
        calls.append((job, resume, role, location, settings))
        return Decision(suitable=True, reason='Example match')
    monkeypatch.setattr(server, 'AsyncOpenAI', FakeClient, raising=False)
    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    monkeypatch.setattr(server, 'ai_match', match, raising=False)
    form = {'provider': provider, 'model': model, 'api_key': 'dummy-test-key-only'}
    with TestClient(server.app, base_url='http://127.0.0.1') as client:
        assert client.post('/api/test-ai', data=form).status_code == 403
        assert client.post('/api/test-ai', headers={'X-App-Token': server.TOKEN},
                           data=dict(form, model='../../bad')).status_code == 400
        assert not calls
        response = client.post('/api/test-ai', headers={'X-App-Token': server.TOKEN}, data=form)
    assert response.status_code == 200
    assert 'dummy-test-key-only' not in response.text
    assert len(calls) == 1
    job, resume, role, location, settings = calls[0]
    assert job.company == 'Example' and 'example.invalid' in job.url
    assert 'example' in resume.lower()
    assert settings['provider'] == provider and settings['model'] == model
    assert settings['api_key'] == 'dummy-test-key-only'


@pytest.mark.parametrize('code,keyword', [(401, 'rejected'), (429, 'limit')])
def test_connection_errors_are_actionable_and_do_not_expose_the_key(monkeypatch, code, keyword):
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def fail(*args, **kwargs):
        request = httpx.Request('POST', 'https://example.invalid', headers={'Authorization': 'Bearer dummy-secret-key'})
        raise httpx.HTTPStatusError('dummy-secret-key', request=request, response=httpx.Response(code, request=request))
    monkeypatch.setattr(server, 'AsyncOpenAI', FakeClient, raising=False)
    monkeypatch.setattr(server, 'ai_match', fail, raising=False)
    with TestClient(server.app, base_url='http://127.0.0.1') as client:
        response = client.post('/api/test-ai', headers={'X-App-Token': server.TOKEN},
                               data={'provider': 'openai', 'model': 'gpt-6-luna', 'api_key': 'dummy-secret-key'})
    assert response.status_code == 400
    assert keyword in response.json()['detail'].lower()
    assert 'dummy-secret-key' not in response.text


def test_search_requires_a_successful_test_for_the_same_ai_settings(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    calls = []
    async def hunt(*args, **kwargs): calls.append('started')
    monkeypatch.setattr(server, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(server, 'ai_match', match)
    monkeypatch.setattr(server, 'run_hunt', hunt)
    monkeypatch.setattr(server, 'resume_text', lambda content: 'Example resume')
    monkeypatch.setattr(server, 'DATA', tmp_path)
    server.state.update({'task': None, 'events': []})
    settings = {'provider': 'openai', 'model': 'gpt-6-luna', 'api_key': 'dummy-test-key-only'}
    form = settings | {'first_name': 'Avery', 'last_name': 'Example', 'email': 'candidate@example.com',
                       'phone': '1234567890', 'role': 'Manager', 'consent': 'true'}
    files = {'resume': ('resume.pdf', b'%PDF-test', 'application/pdf')}
    headers = {'X-App-Token': server.TOKEN}
    with TestClient(server.app, base_url='http://127.0.0.1') as client:
        assert client.post('/api/start', headers=headers, data=form, files=files).status_code == 400
        tested = client.post('/api/test-ai', headers=headers, data=settings)
        assert tested.status_code == 200
        ticket = tested.json()['verification']
        assert 'dummy-test-key-only' not in ticket
        for changed in ({'api_key': 'other-dummy-key-only'}, {'model': 'gpt-6.1-terra'}, {'workspace_id': 'different'}, {'connection_ticket': 'invalid'}, {'connection_ticket': '☃'}):
            attempt = form | {'connection_ticket': ticket} | changed
            assert client.post('/api/start', headers=headers, data=attempt, files=files).status_code == 400
        assert not calls
        response = client.post('/api/start', headers=headers, data=form | {'connection_ticket': ticket}, files=files)
        assert response.status_code == 200
        client.get('/api/status', headers=headers)
    assert calls == ['started']
