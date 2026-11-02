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
