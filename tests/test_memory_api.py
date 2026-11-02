from fastapi.testclient import TestClient

from app import server
from app.memory import AnswerMemory
from src.models import Job


def test_memory_api_saves_edits_and_removes_answers_per_email(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DATA', tmp_path)
    item = Job(source='greenhouse', source_id='123', board='example', company='Example',
               title='Engineer', description='Example', location='Remote',
               url='https://job-boards.greenhouse.io/example/jobs/123',
               apply_url='https://job-boards.greenhouse.io/example/jobs/123')
    memory = AnswerMemory(tmp_path)
    memory.record_questions('candidate@example.com', item, [
        {'label': 'Current country?', 'kind': 'choice', 'options': ['India', 'Canada']},
    ])
    headers = {'X-App-Token': server.TOKEN}
    with TestClient(server.app, base_url='http://127.0.0.1') as client:
        assert client.post('/api/memory/list', data={'email': 'candidate@example.com'}).status_code == 403
        assert client.post('/api/memory/list', headers=headers, data={'email': 'bad'}).status_code == 400
        details = {'email': 'candidate@example.com', 'first_name': 'Avery', 'last_name': 'Example',
                   'phone': '+1 555 0100', 'current_city': 'Example City', 'current_country': 'India'}
        assert client.post('/api/memory/profile', headers=headers, data=details).status_code == 200
        result = client.post('/api/memory/list', headers=headers, data={'email': details['email']}).json()
        assert result['profile']['current_city'] == 'Example City'
        question = result['questions'][0]
        assert question['pending'] and question['options'] == ['India', 'Canada']
        answer = {'email': details['email'], 'question_id': question['id'], 'answer': 'India', 'reuse_globally': 'true'}
        assert client.post('/api/memory/answer', headers=headers, data=answer).status_code == 200
        saved = client.post('/api/memory/list', headers=headers, data={'email': details['email']}).json()['questions'][0]
        assert saved['answer'] == 'India' and saved['reuse_globally'] and not saved['pending']
        assert client.post('/api/memory/list', headers=headers, data={'email': 'other@example.com'}).json() == {'profile': {}, 'questions': []}
        assert client.post('/api/memory/add', headers=headers, data={'email': details['email'], 'label': 'LinkedIn profile', 'kind': 'text', 'answer': 'https://example.com/profile'}).status_code == 200
        assert client.post('/api/memory/add', headers=headers, data={'email': details['email'], 'label': 'API key', 'kind': 'text', 'answer': 'dummy-api-key-not-real'}).status_code == 400
        assert client.post('/api/memory/forget', headers=headers, data={'email': details['email'], 'question_id': question['id']}).status_code == 200
        assert client.post('/api/memory/clear-profile', headers=headers, data={'email': details['email']}).status_code == 200
        remaining = client.post('/api/memory/list', headers=headers, data={'email': details['email']}).json()
        assert remaining['profile'] == {} and len(remaining['questions']) == 1
        assert client.post('/api/memory/clear', headers=headers, data={'email': details['email']}).status_code == 200
        assert client.post('/api/memory/list', headers=headers, data={'email': details['email']}).json() == {'profile': {}, 'questions': []}
    assert all(b'dummy-api-key-not-real' not in path.read_bytes() for path in tmp_path.rglob('*') if path.is_file())
