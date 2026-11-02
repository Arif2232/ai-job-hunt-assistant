from datetime import UTC, datetime, timedelta

from src.matching.evaluator import Evaluator
from src.models import Job, Profile


def job(**changes):
    data = {
        "source": "greenhouse", "source_id": "123", "board": "example",
        "company": "Example", "title": "Python Backend Engineer",
        "description": "Build Python services", "location": "Remote India",
        "remote": True, "url": "https://example.com/job", "apply_url": "https://example.com/apply",
        "published_at": datetime.now(UTC) - timedelta(hours=1), "date_basis": "first_published",
    }
    return Job(**(data | changes))


async def test_matching_uses_each_profiles_own_skills():
    python = await Evaluator(Profile(skills=["Python"])).evaluate(job())
    java = await Evaluator(Profile(skills=["Java"])).evaluate(job())
    assert python.eligible and python.matched_skills == ["Python"]
    assert not java.eligible and "Python" in java.missing_skills


async def test_location_and_freshness_gates_are_not_overridden_by_skills():
    matcher = Evaluator(Profile(skills=["Python"]))
    assert not (await matcher.evaluate(job(location="Remote US"))).eligible
    assert not (await matcher.evaluate(job(published_at=None))).eligible


async def test_salary_and_older_postings_are_filtered():
    from src.models import Salary

    matcher = Evaluator(Profile(skills=["Python"], minimum_annual_salary={"USD": 50000}))
    below_floor = job(salaries=[Salary(currency="USD", maximum=40000)])
    stale = job(published_at=datetime.now(UTC) - timedelta(days=8))
    assert not (await matcher.evaluate(below_floor)).eligible
    assert not (await matcher.evaluate(stale)).eligible


async def test_verified_undated_jobs_require_explicit_opt_in():
    undated = job(published_at=None, verified_at=datetime.now(UTC))
    strict = Evaluator(Profile(skills=["Python"]))
    permissive = Evaluator(Profile(skills=["Python"], allow_active_undated_jobs=True))
    assert not (await strict.evaluate(undated)).eligible
    assert (await permissive.evaluate(undated)).eligible


def test_technology_names_are_matched_at_word_boundaries():
    from src.matching.evaluator import technologies

    assert "Java" not in technologies("JavaScript")
    assert "Go" not in technologies("we go to work")
    assert "Go" in technologies("Go engineer")
