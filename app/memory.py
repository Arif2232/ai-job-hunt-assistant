import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from app.storage import ApplicationLog


PROFILE_LIMITS = {'first_name': 100, 'last_name': 100, 'phone': 40, 'current_city': 100,
                  'current_country': 100, 'preferred_first_name': 100, 'phone_country': 200}


def normalize_label(value: str) -> str:
    value = unicodedata.normalize('NFKC', value or '')
    value = re.sub(r'\s+', ' ', value).strip()
    value = re.sub(r'\s*(?:\(required\)|[*✱✳＊]+)\s*$', '', value, flags=re.I)
    return value.strip().casefold()


def is_placeholder(value: str) -> bool:
    text = normalize_label(value).strip(' -–—')
    return bool(re.fullmatch(r'(?:please )?(?:select|choose)(?: (?:an? )?(?:option|answer|one))?(?:\.{1,3})?', text))


def restricted_question(label: str) -> bool:
    return bool(re.search(r'\b(password|passcode|one.time code|verification code|otp|api[ _-]?(?:key|token)|access token|secret key|credit card|cvv|bank account|ssn|social security number|passport number|national id|national identification|aadhaar|tax id)\b', label, re.I) or
                re.search(r'\b(i|you)\s+(?:have\s+)?(agree|accept|consent|acknowledge|certify|attest|read)\b|\bterms (?:and|&) conditions\b|\bterms of (?:service|use)\b|\bprivacy (?:policy|notice)\b|\bconsent\b', label, re.I))


def looks_like_secret(value: str) -> bool:
    return bool(re.search(r'\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{16,})\b|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----', value))


def can_reuse_globally(label: str) -> bool:
    ambiguous = {'country', 'location', 'city', 'state', 'details', 'other', 'answer', 'question'}
    return normalize_label(label).rstrip('?') not in ambiguous and not bool(re.search(r'\b(?:this|the)\s+(?:job|role|position|company|employer|country|location|office|workplace|opportunity)\b|\bhere\b|\bcountry\b.{0,80}\b(?:applying|apply|application)\b|\b(?:salary|compensation|pay rate|expected pay|desired pay|referral|referred|referrer)\b', label, re.I))


def scope_for(job, label='', per_job=False) -> str:
    restrictions = sorted(value.strip().casefold() for value in job.country_restrictions)
    parts = [job.source, job.board.casefold(), re.sub(r'\s+', ' ', job.location.strip()).casefold(), restrictions]
    if per_job or not can_reuse_globally(label):
        parts.append(job.source_id)
    return json.dumps(parts, ensure_ascii=False)


def applicant(email: str) -> str:
    value = email.strip()
    if len(value) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise ValueError('Enter your application email address first.')
    return ApplicationLog.applicant_id(value)


def options_for(question):
    values = question.get('options') or []
    if not isinstance(values, list) or len(values) > 300:
        return []
    return [item.strip() for item in values if isinstance(item, str) and 0 < len(item.strip()) <= 200 and not is_placeholder(item)]


def clean_profile(profile):
    if not isinstance(profile, dict) or not set(profile).issubset(PROFILE_LIMITS):
        raise ValueError('Only application details can be saved.')
    if any(not isinstance(value, str) or len(value) > PROFILE_LIMITS[key] for key, value in profile.items()):
        raise ValueError('Check the saved application details.')
    if any(looks_like_secret(value) for value in profile.values()):
        raise ValueError('Do not save API keys or passwords in application memory.')
    return {key: value.strip() for key, value in profile.items()}


class AnswerMemory:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.path = root / 'answer-memory.sqlite3'
        if self.path.is_symlink():
            raise OSError('The answer memory cannot be a symbolic link.')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS profiles (applicant TEXT PRIMARY KEY, details TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, applicant TEXT NOT NULL, scope TEXT NOT NULL,
                normalized TEXT NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL, options TEXT NOT NULL,
                company TEXT NOT NULL, title TEXT NOT NULL, location TEXT NOT NULL, url TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '', reuse_globally INTEGER NOT NULL DEFAULT 0,
                pending INTEGER NOT NULL DEFAULT 1,
                UNIQUE(applicant, scope, normalized, kind))''')

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

    def profile(self, email):
        with self.connect() as db:
            row = db.execute('SELECT details FROM profiles WHERE applicant = ?', (applicant(email),)).fetchone()
        return json.loads(row['details']) if row else {}

    def save_profile(self, email, profile):
        details = clean_profile(profile)
        with self.connect() as db:
            db.execute('INSERT INTO profiles(applicant, details) VALUES (?, ?) '
                       'ON CONFLICT(applicant) DO UPDATE SET details=excluded.details',
                       (applicant(email), json.dumps(details, ensure_ascii=False)))

    def remember_profile(self, email, profile):
        details = {key: value for key, value in clean_profile(profile).items() if value}
        owner = applicant(email)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT details FROM profiles WHERE applicant = ?', (owner,)).fetchone()
            previous = json.loads(row['details']) if row else {}
            previous.update(details)
            db.execute('INSERT INTO profiles(applicant, details) VALUES (?, ?) '
                       'ON CONFLICT(applicant) DO UPDATE SET details=excluded.details',
                       (owner, json.dumps(previous, ensure_ascii=False)))

    def delete_profile(self, email):
        with self.connect() as db:
            db.execute('DELETE FROM profiles WHERE applicant = ?', (applicant(email),))

    def record_questions(self, email, job, questions):
        owner = applicant(email)
        with self.connect() as db:
            for question in questions:
                label = (question.get('label') or '').strip()
                kind = question.get('kind')
                if len(label) > 600:
                    label = label[:597] + '…'
                    kind = 'manual'
                normalized = normalize_label(label)
                if not normalized or kind not in {'text', 'choice', 'manual'}:
                    continue
                if restricted_question(label):
                    kind = 'manual'
                scope = scope_for(job, label, per_job=kind == 'manual')
                db.execute('''INSERT INTO questions
                    (applicant, scope, normalized, label, kind, options, company, title, location, url, pending)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(applicant, scope, normalized, kind) DO UPDATE SET
                        label=excluded.label, options=excluded.options, company=excluded.company,
                        title=excluded.title, location=excluded.location, url=excluded.url, pending=1''',
                    (owner, scope, normalized, label, kind, json.dumps(options_for(question), ensure_ascii=False),
                     job.company[:200], job.title[:300], job.location[:300], job.apply_url[:2000]))

    def questions(self, email):
        with self.connect() as db:
            rows = db.execute('''SELECT id, label, kind, options, company, title, location, url, answer,
                                 reuse_globally, pending FROM questions WHERE applicant = ? ORDER BY pending DESC, id DESC''',
                              (applicant(email),)).fetchall()
        return [dict(row) | {'options': json.loads(row['options']), 'pending': bool(row['pending']),
                             'reuse_globally': bool(row['reuse_globally']),
                             'can_reuse_globally': row['kind'] != 'manual' and can_reuse_globally(row['label'])} for row in rows]

    def save_answer(self, email, question_id, answer, reuse_globally):
        answer = answer.strip() if isinstance(answer, str) else ''
        if not answer or len(answer) > 2000:
            raise ValueError('Enter an answer of up to 2,000 characters.')
        if looks_like_secret(answer):
            raise ValueError('Do not save API keys or passwords in application memory.')
        with self.connect() as db:
            row = db.execute('SELECT kind, options, label, scope FROM questions WHERE applicant = ? AND id = ?',
                             (applicant(email), question_id)).fetchone()
            if not row or row['kind'] == 'manual' or restricted_question(row['label']):
                raise ValueError('This question needs to be completed on the employer site.')
            if reuse_globally and not can_reuse_globally(row['label']):
                raise ValueError('This answer depends on the job or location and cannot be reused everywhere.')
            if row['scope'] == '__global__' and not reuse_globally:
                raise ValueError('This answer was added for all employers. Remove it if you do not want to reuse it.')
            options = json.loads(row['options'])
            if row['kind'] == 'choice' and is_placeholder(answer):
                raise ValueError('Choose an actual answer, not the placeholder.')
            if row['kind'] == 'choice' and options:
                matching = [option for option in options if normalize_label(option) == normalize_label(answer)]
                if len(matching) != 1:
                    raise ValueError('Choose one of the available answers.')
                answer = matching[0]
            db.execute('UPDATE questions SET answer = ?, reuse_globally = ?, pending = 0 WHERE applicant = ? AND id = ?',
                       (answer, int(bool(reuse_globally)), applicant(email), question_id))

    def add_answer(self, email, label, kind, answer):
        label = label.strip() if isinstance(label, str) else ''
        answer = answer.strip() if isinstance(answer, str) else ''
        if not 3 <= len(label) <= 600 or kind not in {'text', 'choice'} or not answer or len(answer) > 2000:
            raise ValueError('Enter a question and answer.')
        if looks_like_secret(answer):
            raise ValueError('Do not save API keys or passwords in application memory.')
        if kind == 'choice' and is_placeholder(answer):
            raise ValueError('Choose an actual answer, not the placeholder.')
        if restricted_question(label) or not can_reuse_globally(label):
            raise ValueError('This question needs to be answered for a specific job instead of reused everywhere.')
        with self.connect() as db:
            db.execute('''INSERT INTO questions
                (applicant, scope, normalized, label, kind, options, company, title, location, url, answer, reuse_globally, pending)
                VALUES (?, ?, ?, ?, ?, '[]', '', '', '', '', ?, 1, 0)
                ON CONFLICT(applicant, scope, normalized, kind) DO UPDATE SET answer=excluded.answer, pending=0, reuse_globally=1''',
                (applicant(email), '__global__', normalize_label(label), label, kind, answer))

    def delete_question(self, email, question_id):
        with self.connect() as db:
            return bool(db.execute('DELETE FROM questions WHERE applicant = ? AND id = ?', (applicant(email), question_id)).rowcount)

    def clear(self, email):
        owner = applicant(email)
        with self.connect() as db:
            db.execute('DELETE FROM profiles WHERE applicant = ?', (owner,))
            db.execute('DELETE FROM questions WHERE applicant = ?', (owner,))

    def lookup(self, email, job, question):
        kind = question.get('kind')
        label = question.get('label') or ''
        if kind not in {'text', 'choice'} or restricted_question(label):
            return None
        scope = scope_for(job, label)
        with self.connect() as db:
            rows = db.execute('''SELECT scope, answer, reuse_globally FROM questions
                                 WHERE applicant = ? AND normalized = ? AND kind = ?
                                   AND pending = 0 AND answer <> '' AND (scope = ? OR reuse_globally = 1)''',
                              (applicant(email), normalize_label(label), kind, scope)).fetchall()
        scoped = [row['answer'] for row in rows if row['scope'] == scope]
        choices = scoped or [row['answer'] for row in rows if row['reuse_globally'] and can_reuse_globally(label)]
        distinct = {normalize_label(value) for value in choices}
        if len(distinct) != 1:
            return None
        answer = choices[0]
        if kind == 'choice' and is_placeholder(answer):
            return None
        options = options_for(question)
        if kind == 'choice' and options:
            matching = [option for option in options if normalize_label(option) == normalize_label(answer)]
            return matching[0] if len(matching) == 1 else None
        return answer
