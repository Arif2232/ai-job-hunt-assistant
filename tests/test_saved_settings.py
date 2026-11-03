import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from app import server
from app.matching import Decision
from app.settings import LocalSettings
from app.tailoring import ResumeDraft


class FakeClient:
    def __init__(self, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


def test_preferences_restore_and_reject_secrets_and_unknown_fields(tmp_path):
    settings = LocalSettings(tmp_path)
    settings.update_preferences({'role': 'Engineer', 'region': 'india', 'remember_questions': True})
    LocalSettings(tmp_path).update_preferences({'role': 'Designer'})
    assert settings.preferences() == {'role': 'Designer', 'region': 'india', 'remember_questions': True}
    for invalid in ({'api_key': 'dummy-key'}, {'role': 'sk-test-not-a-real-key-1234567890'},
                    {'region': 'other'}, {'remember_questions': 'false'}):
        with pytest.raises(ValueError):
            settings.update_preferences(invalid)
    assert settings.preferences()['role'] == 'Designer'
    if os.name != 'nt':
        assert settings.path.stat().st_mode & 0o777 == 0o600


def test_changed_profile_fields_merge_without_erasing_other_details(monkeypatch, tmp_path):
    monkeypatch.setattr(server, 'DATA', tmp_path)
    memory = server.AnswerMemory(tmp_path)
    memory.save_profile('candidate@example.com', {'first_name': 'Avery', 'current_city': 'Old City'})
    headers = {'X-App-Token': server.TOKEN}
    with TestClient(server.app, base_url='http://127.0.0.1') as client:
        data = {'email': 'candidate@example.com', 'fields': json.dumps({'current_city': '', 'phone': '1234567890'})}
        assert client.post('/api/memory/profile-fields', data=data).status_code == 403
        assert client.post('/api/memory/profile-fields', headers=headers, data=data).status_code == 200
        assert client.post('/api/memory/profile-fields', data=data | {'_token': server.TOKEN}).status_code == 200
        assert client.post('/api/memory/profile-fields', headers=headers, data=data | {'fields': '{"api_key":"dummy-key"}'}).status_code == 400
        assert client.post('/api/settings/preferences', json={'role': 'Engineer'}).status_code == 403
        assert client.post('/api/settings/preferences', json={'role': 'Engineer', '_token': 123}).status_code == 403
        assert client.post('/api/settings/preferences', json={'role': 'Engineer', '_token': server.TOKEN}).status_code == 200
    assert memory.profile('candidate@example.com') == {'first_name': 'Avery', 'current_city': '', 'phone': '1234567890'}
    assert memory.profile('other@example.com') == {}


def test_out_of_order_autosaves_do_not_replace_newer_edits(tmp_path):
    client = 'a' * 32
    settings = LocalSettings(tmp_path)
    settings.update_preferences({'role': 'New role'}, client, {'role': 2})
    settings.update_preferences({'role': 'Old role'}, client, {'role': 1})
    assert settings.preferences()['role'] == 'New role'
    memory = server.AnswerMemory(tmp_path)
    memory.update_profile('candidate@example.com', {'current_city': 'New City'}, client, {'current_city': 2})
    memory.update_profile('candidate@example.com', {'current_city': 'Old City'}, client, {'current_city': 1})
    assert memory.profile('candidate@example.com')['current_city'] == 'New City'
    with pytest.raises(ValueError):
        settings.update_preferences({'role': 'Bad'}, client, {'role': -1})
    with pytest.raises(ValueError):
        memory.update_profile('candidate@example.com', {'phone': '1234567890'}, client, {'current_city': 3})


def test_verified_keys_are_saved_per_provider_and_never_returned(monkeypatch, tmp_path):
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(server.httpx, 'AsyncClient', FakeClient)
    monkeypatch.setattr(server, 'ai_match', match)
    headers = {'X-App-Token': server.TOKEN}
    form = {'provider': 'openai', 'model': 'gpt-6-luna', 'api_key': 'dummy-openai-test-key'}
    with TestClient(server.app, base_url='http://127.0.0.1') as client:
        assert client.get('/api/settings/preferences').status_code == 403
        assert client.post('/api/settings/credential', headers=headers, data=form | {'connection_ticket': 'invalid'}).status_code == 400
        assert LocalSettings(tmp_path).credential('openai') is None
        tested = client.post('/api/test-ai', headers=headers, data=form)
        assert tested.status_code == 200
        assert LocalSettings(tmp_path).credential('openai') is None
        ticket = tested.json()['verification']
        assert client.post('/api/settings/credential', headers=headers, data=form | {'connection_ticket': ticket}).status_code == 200
        assert client.post('/api/settings/credential', headers=headers, data=form | {
            'api_key': 'dummy-replacement-key', 'connection_ticket': ticket}).status_code == 400
        assert LocalSettings(tmp_path).credential('openai')['api_key'] == form['api_key']
        async def fail(*args, **kwargs): raise RuntimeError('dummy-replacement-key')
        monkeypatch.setattr(server, 'ai_match', fail)
        failed = client.post('/api/test-ai', headers=headers, data=form | {'api_key': 'dummy-replacement-key'})
        assert failed.status_code == 400 and 'dummy-replacement-key' not in failed.text
        assert LocalSettings(tmp_path).credential('openai')['api_key'] == form['api_key']
        monkeypatch.setattr(server, 'ai_match', match)
        status = client.post('/api/settings/credential-status', headers=headers, data={'provider': 'openai', 'model': 'gpt-6-luna'})
        assert status.json()['verified'] is True and 'dummy-openai-test-key' not in status.text
        assert not client.post('/api/settings/credential-status', headers=headers, data={'provider': 'openai', 'model': 'gpt-6.1-terra'}).json()['verified']
        changed = {'provider': 'gemini', 'model': 'gemini-3.5-flash-lite', 'api_key': 'dummy-gemini-test-key'}
        tested_other = client.post('/api/test-ai', headers=headers, data=changed)
        assert client.post('/api/settings/credential', headers=headers, data=changed | {'connection_ticket': tested_other.json()['verification']}).status_code == 200
        assert LocalSettings(tmp_path).credential('openai')['api_key'] == form['api_key']
        assert LocalSettings(tmp_path).credential('gemini')['api_key'] == changed['api_key']
        monkeypatch.setattr(server, 'CONNECTION_SECRET', b'x' * 32)
        restored = client.post('/api/settings/credential-status', headers=headers, data={'provider': 'openai', 'model': 'gpt-6-luna'}).json()
        assert restored['verified'] and restored['verification'] != ticket
        assert client.post('/api/settings/credential-remove', headers=headers, data={'provider': 'openai'}).status_code == 200
        assert LocalSettings(tmp_path).credential('openai') is None
        assert LocalSettings(tmp_path).credential('gemini')['api_key'] == changed['api_key']


def test_saved_resume_and_verified_key_work_after_restart_without_uploading_again(monkeypatch, tmp_path):
    calls = []
    async def match(*args, **kwargs): return Decision(suitable=True, reason='Example')
    async def hunt(candidate, resume, text, key, *args, **kwargs):
        calls.append((candidate['email'], resume.read_bytes(), text, key))
    async def draft(client, text, description, **kwargs):
        return ResumeDraft(resume=text, missing_skills=[])
    def read_resume(content):
        if content != b'%PDF-sample-resume': raise ValueError('Choose a readable PDF.')
        return 'Example resume text'
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'AsyncOpenAI', FakeClient)
    monkeypatch.setattr(server, 'ai_match', match)
    monkeypatch.setattr(server, 'resume_text', read_resume)
    monkeypatch.setattr(server, 'run_hunt', hunt)
    monkeypatch.setattr(server, 'resume_draft', draft)
    monkeypatch.setattr(server, 'CONNECTION_SECRET', b't' * 32)
    for name, value in {'task': None, 'events': [], 'review': None, 'stop': asyncio.Event(), 'resume_mode': None}.items():
        monkeypatch.setitem(server.state, name, value)
    headers = {'X-App-Token': server.TOKEN}
    email = 'candidate@example.com'
    ai = {'provider': 'openai', 'model': 'gpt-6-luna', 'api_key': 'dummy-openai-test-key'}
    with TestClient(server.app, base_url='http://127.0.0.1') as client:
        checked = client.post('/api/test-ai', headers=headers, data=ai).json()
        assert client.post('/api/settings/credential', headers=headers, data=ai | {'connection_ticket': checked['verification']}).status_code == 200
        saved = client.post('/api/settings/resume', headers=headers, data={'email': email},
                            files={'resume': ('../sample.pdf', b'%PDF-sample-resume', 'application/pdf')})
        assert saved.status_code == 200 and saved.json()['filename'] == 'sample.pdf'
        failed = client.post('/api/settings/resume', headers=headers, data={'email': email},
                             files={'resume': ('broken.pdf', b'%PDF-broken', 'application/pdf')})
        assert failed.status_code == 400
        info = client.post('/api/settings/resume-info', headers=headers, data={'email': email}).json()
        assert info['filename'] == 'sample.pdf' and len(info['digest']) == 64
        assert client.post('/api/settings/resume-info', headers=headers, data={'email': 'other@example.com'}).json()['filename'] == ''
        restored = client.post('/api/settings/credential-status', headers=headers, data={'provider': 'openai', 'model': 'gpt-6-luna'}).json()
        search = {'first_name': 'Avery', 'last_name': 'Example', 'email': email, 'phone': '1234567890', 'role': 'Engineer',
                  'provider': 'openai', 'model': 'gpt-6-luna', 'consent': 'true', 'connection_ticket': restored['verification'],
                  'resume_digest': info['digest']}
        assert client.post('/api/start', headers=headers, data=search | {'email': 'other@example.com'}).status_code == 400
        LocalSettings(tmp_path).save_resume(email, 'other.pdf', b'%PDF-different-resume')
        assert client.post('/api/start', headers=headers, data=search).status_code == 409
        assert calls == []
        LocalSettings(tmp_path).save_resume(email, 'sample.pdf', b'%PDF-sample-resume')
        assert client.post('/api/start', headers=headers, data=search).status_code == 200
        client.get('/api/status', headers=headers)
        generated = client.post('/api/tailor-resume', headers=headers, data={'email': email, 'provider': 'openai',
                               'model': 'gpt-6-luna', 'connection_ticket': restored['verification'],
                               'resume_digest': info['digest'],
                               'description': 'Example job requirements with enough detail.'})
        assert generated.status_code == 200 and generated.json()['resume'] == 'Example resume text'
    assert calls == [(email, b'%PDF-sample-resume', 'Example resume text', ai['api_key'])]
