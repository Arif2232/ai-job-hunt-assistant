import asyncio
import re
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx
from openai import AsyncOpenAI
from playwright.async_api import async_playwright
from pydantic import ValidationError

from app.apply import apply_to_job, matches_job
from app.matching import ai_match, selected_model
from src.config import Board
from src.discovery import ashby, greenhouse, lever
from src.discovery.common import HTTP

SOURCES = {
    'greenhouse': [
        Board(name='Razorpay', board='razorpaysoftwareprivatelimited'),
        Board(name='Databricks', board='databricks'), Board(name='Zscaler', board='zscaler'),
        Board(name='Stripe', board='stripe'), Board(name='Okta', board='okta'),
        Board(name='InMobi', board='inmobi'), Board(name='Groww', board='groww'),
        Board(name='GitLab', board='gitlab'), Board(name='Asana', board='asana'),
        Board(name='Dropbox', board='dropbox'), Board(name='Rubrik', board='rubrik'),
        Board(name='bswift India', board='bswiftindia'),
    ],
    'lever': [
        Board(name='Binance', board='binance'),
        Board(name='Kinsta', board='kinsta'), Board(name='HighLevel', board='gohighlevel'),
        Board(name='JumpCloud', board='jumpcloud'), Board(name='Palantir', board='palantir'),
        Board(name='Spotify', board='spotify'), Board(name='Superside', board='superside'),
    ],
    'ashby': [
        Board(name='Atlys', board='atlys'), Board(name='Josys', board='josys'),
        Board(name='Ashby', board='ashby'), Board(name='Baseten', board='baseten'),
        Board(name='ClickHouse', board='clickhouse'), Board(name='Cohere', board='cohere'),
        Board(name='Cursor', board='cursor'), Board(name='Docker', board='docker'),
        Board(name='ElevenLabs', board='elevenlabs'), Board(name='Linear', board='linear'),
    ],
}
MAX_JOBS_TO_REVIEW = 100


def title_matches(title: str, requested: str) -> bool:
    # shortcut: exact words miss synonyms; broaden when related titles need discovery.
    words = [word for word in re.findall(r'[a-z0-9]+', requested.lower()) if len(word) >= 2]
    role_words = [word for word in words if word not in {'senior', 'sr', 'junior', 'jr', 'staff', 'principal', 'lead', 'associate', 'intern', 'ii', 'iii', 'iv'}]
    title_words = set(re.findall(r'[a-z0-9]+', title.lower()))
    return any(word in title_words for word in (role_words or words))


def location_matches(job, requested: str, region: str) -> bool:
    location = job.location.casefold()
    indian = bool(re.search(r'\b(india|bengaluru|bangalore|hyderabad|delhi|gurugram|gurgaon|noida|mumbai|pune|chennai|kolkata)\b', location))
    worldwide = bool(re.search(r'\b(worldwide|global|anywhere in the world)\b|^remote\s*[-–,:]?\s*anywhere$', location)) and (job.remote is True or 'remote' in location)
    if worldwide and not indian and re.search(r'\b(us|usa|united states|uk|united kingdom|europe|canada)\b', location):
        worldwide = False
    if region == 'india':
        restrictions = {value.casefold().strip() for value in job.country_restrictions}
        if restrictions and not restrictions.intersection({'india', 'in', 'ind', 'worldwide', 'global', 'anywhere'}):
            return False
        if not (indian or worldwide):
            return False
    if not requested:
        return True
    if requested.casefold().strip() == 'india' and indian:
        return True
    return requested.casefold().strip() in location


def newest_jobs(jobs, candidate, ledger, *, now=None):
    now = now or datetime.now(UTC)
    recent = [job for job in jobs if job.published_at is not None
              and now - timedelta(days=30) <= job.published_at <= now + timedelta(minutes=5)
              and title_matches(job.title, candidate['role'])
              and location_matches(job, candidate['location'], candidate.get('region', 'india'))
              and matches_job(job.apply_url, job)
              and not ledger.has_attempt(candidate['email'], job.key)]
    recent.sort(key=lambda job: job.published_at, reverse=True)
    unique = {}
    for job in recent:
        unique.setdefault(job.canonical_url, job)
    return list(unique.values())


def ai_error_message(error):
    status = getattr(error, 'status_code', None)
    if status is None and getattr(error, 'response', None) is not None:
        status = getattr(error.response, 'status_code', None)
    if status == 401:
        return 'The AI provider rejected the API key. Check the key and selected provider.'
    if status == 403:
        return 'This API key cannot access the selected model. Check model access or choose another model.'
    if status == 404:
        return 'The selected model is unavailable. Check its name or choose another model.'
    if status == 429:
        return 'The AI provider reached a usage limit. Check API billing, quota, or rate limits.'
    if status == 400:
        return 'The AI provider rejected the request. Check the selected model and provider.'
    if isinstance(error, (httpx.TimeoutException, asyncio.TimeoutError)) or type(error).__name__ == 'APITimeoutError':
        return 'The AI provider timed out. Check your connection and try again.'
    if isinstance(error, httpx.ConnectError) or type(error).__name__ == 'APIConnectionError':
        return 'Could not connect to the AI provider. Check your internet connection.'
    if isinstance(error, ValidationError):
        errors = error.errors(include_url=False, include_context=False, include_input=False)
        if any(item['type'] == 'json_invalid' for item in errors):
            return 'The AI returned incomplete or malformed data. Try another model or retry.'
        return 'The AI returned data in an unexpected format. Try another model or retry.'
    if isinstance(error, ValueError):
        return 'The AI did not return a usable answer. Try another model or retry.'
    return 'AI matching failed. Check your API key, model access, and connection, then try again.'


async def run_hunt(candidate: dict, resume: Path, resume_text: str, api_key: str, ledger, report, stop,
                   *, provider='openai', model='', workspace_id=''):
    model = selected_model(provider, model)
    report('searching', 'Looking for recent jobs that match your preferences.')
    async with HTTP(concurrency=8) as http:
        async def fetch(source, board):
            try:
                if source == 'greenhouse':
                    return await asyncio.wait_for(greenhouse.discover(http, board, limit=10000, enrich=False), timeout=35)
                if source == 'lever':
                    return await asyncio.wait_for(lever.discover(http, board, limit=500, title_filter=partial(title_matches, requested=candidate['role'])), timeout=35)
                return await asyncio.wait_for(ashby.discover(http, board, limit=10000), timeout=35)
            except Exception:
                return None
        pages = await asyncio.gather(*(fetch(source, board) for source, boards in SOURCES.items() for board in boards))
    if any(page is None for page in pages):
        report('source_unavailable', 'Some sources could not be checked. Continuing with available jobs.')
    jobs = newest_jobs([job for page in pages if page is not None for job in page], candidate, ledger)
    if not jobs:
        report('done', 'No recent jobs with verified dates matched your search. Try a broader title or location.')
        return
    report('searching', f'Found {len(jobs)} recent jobs. AI will check up to the newest {min(len(jobs), MAX_JOBS_TO_REVIEW)} against your resume.')
    attempts = 0
    confirmed = 0
    needs_review = 0
    requested_location = candidate['location'] or ('India and worldwide remote' if candidate.get('region', 'india') == 'india' else 'Anywhere')
    client = AsyncOpenAI(api_key=api_key, timeout=60, max_retries=1) if provider == 'openai' else httpx.AsyncClient(timeout=60)
    async with client:
        async with async_playwright() as playwright:
            browser = None
            try:
                invalid_responses = 0
                for job in jobs[:MAX_JOBS_TO_REVIEW]:
                    if stop.is_set() or attempts >= candidate['max_applications']:
                        break
                    report('reviewing', f'Checking {job.title} at {job.company}.', job)
                    try:
                        decision = await ai_match(client, job, resume_text, candidate['role'], requested_location,
                                                  provider=provider, model=model, api_key=api_key, workspace_id=workspace_id,
                                                  relocation=candidate.get('relocation', 'unknown'),
                                                  experience_flexibility=candidate.get('experience_flexibility', 'strict'))
                    except ValueError as exc:
                        invalid_responses += 1
                        needs_review += 1
                        report('needs_attention', 'AI could not evaluate this job. You can review it manually.', job)
                        if invalid_responses >= 3:
                            report('error', 'Several jobs could not be evaluated. ' + ai_error_message(exc))
                            return
                        continue
                    except Exception as exc:
                        report('error', ai_error_message(exc))
                        return
                    invalid_responses = 0
                    if not decision.suitable:
                        reason = decision.reason.strip()
                        report('skipped', f'Not a close enough match. {reason}' if reason else 'Not a close enough match.', job)
                        continue
                    if stop.is_set():
                        break
                    if browser is None:
                        try:
                            browser = await playwright.chromium.launch(headless=False)
                        except Exception:
                            report('error', 'The application browser is not installed. Restart the app using the startup file for your computer.')
                            return
                    page = await browser.new_page()
                    try:
                        status, message = await apply_to_job(page, job, candidate, resume, ledger)
                    finally:
                        await page.close()
                    report(status, message, job)
                    if status in ('submitted', 'uncertain'):
                        attempts += 1
                    if status in ('needs_attention', 'uncertain'):
                        needs_review += 1
                    if status == 'submitted':
                        confirmed += 1
            finally:
                if browser:
                    await browser.close()
    if stop.is_set():
        report('stopped', 'Stopped after the current job.')
    else:
        report('done', f'Finished. {confirmed} application(s) confirmed; {needs_review} need review.')
