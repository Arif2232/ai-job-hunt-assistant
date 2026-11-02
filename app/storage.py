import hashlib
import sqlite3
from pathlib import Path


class ApplicationLog:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / 'applications.sqlite3'
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS attempts (
                applicant TEXT NOT NULL, job_key TEXT NOT NULL, status TEXT NOT NULL,
                company TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (applicant, job_key))''')
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def applicant_id(email: str) -> str:
        return hashlib.sha256(email.strip().casefold().encode()).hexdigest()

    def reserve(self, email: str, job_key: str, company: str, title: str, url: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                'INSERT OR IGNORE INTO attempts (applicant, job_key, status, company, title, url) VALUES (?, ?, ?, ?, ?, ?)',
                (self.applicant_id(email), job_key, 'submitting', company, title, url),
            )
            return cursor.rowcount == 1

    def finish(self, email: str, job_key: str, status: str):
        with self.connect() as db:
            db.execute('UPDATE attempts SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE applicant = ? AND job_key = ?',
                       (status, self.applicant_id(email), job_key))

    def has_attempt(self, email: str, job_key: str) -> bool:
        with self.connect() as db:
            return db.execute('SELECT 1 FROM attempts WHERE applicant = ? AND job_key = ?',
                              (self.applicant_id(email), job_key)).fetchone() is not None
