import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.memory import applicant, looks_like_secret


TEXT_LIMITS = {'email': 254, 'role': 100, 'location': 100, 'model': 128,
               'custom_model': 128, 'workspace_id': 128}
CHOICES = {'region': {'india', 'anywhere'}, 'relocation': {'unknown', 'india', 'anywhere', 'no'},
           'experience_flexibility': {'strict', 'one_year', 'flexible'},
           'resume_mode': {'original', 'tailored', 'review'}, 'provider': {'openai', 'gemini', 'claude'}}


class LocalSettings:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.path = root / 'settings.sqlite3'
        if self.path.is_symlink():
            raise OSError('The settings file cannot be a symbolic link.')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS preferences (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS preference_versions (
                name TEXT NOT NULL, client TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(name, client))''')
            db.execute('''CREATE TABLE IF NOT EXISTS credentials (
                provider TEXT PRIMARY KEY, api_key TEXT NOT NULL, model TEXT NOT NULL, workspace_id TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS resumes (
                applicant TEXT PRIMARY KEY, filename TEXT NOT NULL, content BLOB NOT NULL)''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA secure_delete = ON')
            with db:
                yield db
        finally:
            db.close()

    def preferences(self):
        with self.connect() as db:
            rows = db.execute('SELECT name, value FROM preferences').fetchall()
        allowed = set(TEXT_LIMITS) | set(CHOICES) | {'remember_questions'}
        return {row['name']: json.loads(row['value']) for row in rows if row['name'] in allowed}

    def update_preferences(self, changes, client_id='', revisions=None):
        allowed = set(TEXT_LIMITS) | set(CHOICES) | {'remember_questions'}
        if not isinstance(changes, dict) or not set(changes).issubset(allowed):
            raise ValueError('Check the settings you are saving.')
        for name, value in changes.items():
            if name in TEXT_LIMITS and (not isinstance(value, str) or len(value) > TEXT_LIMITS[name] or looks_like_secret(value)):
                raise ValueError('Check the settings you are saving.')
            if name in CHOICES and (not isinstance(value, str) or value not in CHOICES[name]):
                raise ValueError('Check the settings you are saving.')
            if name == 'remember_questions' and type(value) is not bool:
                raise ValueError('Check the settings you are saving.')
        if client_id or revisions is not None:
            if (not re.fullmatch(r'[0-9a-f]{32}', client_id or '') or not isinstance(revisions, dict) or
                    set(revisions) != set(changes) or any(type(value) is not int or not 1 <= value <= 2**53 for value in revisions.values())):
                raise ValueError('Check the settings you are saving.')
        with self.connect() as db:
            for name, value in changes.items():
                if client_id:
                    updated = db.execute('''INSERT INTO preference_versions(name, client, revision) VALUES (?, ?, ?)
                        ON CONFLICT(name, client) DO UPDATE SET revision=excluded.revision
                        WHERE excluded.revision > preference_versions.revision''', (name, client_id, revisions[name])).rowcount
                    if not updated:
                        continue
                db.execute('INSERT INTO preferences(name, value) VALUES (?, ?) '
                           'ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                           (name, json.dumps(value, ensure_ascii=False)))

    def credential(self, provider):
        with self.connect() as db:
            row = db.execute('SELECT api_key, model, workspace_id FROM credentials WHERE provider = ?', (provider,)).fetchone()
        return dict(row) if row else None

    def save_credential(self, provider, api_key, model, workspace_id):
        if provider not in CHOICES['provider'] or not 12 <= len(api_key) <= 500 or not 1 <= len(model) <= 128 or len(workspace_id) > 128:
            raise ValueError('Check your AI settings.')
        with self.connect() as db:
            db.execute('''INSERT INTO credentials(provider, api_key, model, workspace_id) VALUES (?, ?, ?, ?)
                          ON CONFLICT(provider) DO UPDATE SET api_key=excluded.api_key,
                          model=excluded.model, workspace_id=excluded.workspace_id''',
                       (provider, api_key, model, workspace_id))

    def remove_credential(self, provider):
        if provider not in CHOICES['provider']:
            raise ValueError('Choose an AI provider.')
        with self.connect() as db:
            db.execute('DELETE FROM credentials WHERE provider = ?', (provider,))

    def save_resume(self, email, filename, content):
        owner = applicant(email)
        filename = re.split(r'[/\\]', filename or '')[-1]
        filename = re.sub(r'[\x00-\x1f\x7f]', '', filename).strip()[:200] or 'Resume.pdf'
        if not isinstance(content, bytes) or not content.startswith(b'%PDF-') or len(content) > 5_000_000:
            raise ValueError('Choose a PDF resume smaller than 5 MB.')
        with self.connect() as db:
            db.execute('''INSERT INTO resumes(applicant, filename, content) VALUES (?, ?, ?)
                          ON CONFLICT(applicant) DO UPDATE SET filename=excluded.filename, content=excluded.content''',
                       (owner, filename, content))
        return {'filename': filename, 'digest': hashlib.sha256(content).hexdigest()}

    def resume(self, email):
        with self.connect() as db:
            row = db.execute('SELECT filename, content FROM resumes WHERE applicant = ?', (applicant(email),)).fetchone()
        return dict(row) if row else None

    def resume_info(self, email):
        saved = self.resume(email)
        return {'filename': saved['filename'], 'digest': hashlib.sha256(saved['content']).hexdigest()} if saved else {'filename': '', 'digest': ''}
