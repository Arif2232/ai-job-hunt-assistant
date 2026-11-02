from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Salary(StrictModel):
    currency: str
    minimum: float | None = None
    maximum: float | None = None
    interval: str = "year"
    scope: str = ""


class Job(StrictModel):
    source: Literal[
        "greenhouse",
        "lever",
        "ashby",
        "feed",
        "ycombinator",
        "workatastartup",
        "himalayas",
        "remotive",
        "wellfound",
        "rippling",
        "naukri",
        "instahyre",
    ]
    source_id: str
    board: str
    company: str
    title: str
    description: str
    location: str = ""
    remote: bool | None = None
    workplace_type: str = ""
    employment_type: str = ""
    url: str
    apply_url: str
    published_at: datetime | None = None
    verified_at: datetime | None = None
    date_basis: str = "unknown"
    salaries: list[Salary] = Field(default_factory=list)
    provenance: str = ""
    discovery_urls: list[str] = Field(default_factory=list)
    country_restrictions: list[str] = Field(default_factory=list)
    timezone_restrictions: list[str] = Field(default_factory=list)
    experience_min: float | None = None
    experience_max: float | None = None

    @field_validator("published_at", "verified_at")
    @classmethod
    def aware_date(cls, value: datetime | None) -> datetime | None:
        if value and value.tzinfo is None:
            raise ValueError("Publication timestamps must have a timezone")
        return value

    @property
    def key(self) -> str:
        return f"{self.source}:{self.board}:{self.source_id}"

    @property
    def canonical_url(self) -> str:
        u = urlsplit(self.apply_url)
        # gh_jid identifies the vacancy on company-hosted Greenhouse pages.
        # Tracking parameters must not merge distinct vacancies or create duplicates.
        identity = [(k, v) for k, v in parse_qsl(u.query) if k == "gh_jid"]
        path = u.path.rstrip("/")
        if self.source in {"lever", "ashby"}:
            path = path.removesuffix("/apply").removesuffix("/application")
        return urlunsplit(
            (u.scheme.lower(), u.netloc.lower(), path, urlencode(sorted(identity)), "")
        )

    @property
    def artifact_id(self) -> str:
        return hashlib.sha256(self.key.encode()).hexdigest()[:12]


class Match(StrictModel):
    eligible: bool
    score: int = Field(ge=0, le=100)
    reasons: list[str]
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    semantic_similarity: float | None = None


class Status(StrEnum):
    APPLIED = "Applied (Success)"
    CAPTCHA = "Action Needed (CAPTCHA)"
    QUESTIONS = "Action Needed (Questions)"
    UNCERTAIN = "Action Needed (Uncertain Submission)"
    UNSUPPORTED = "Action Needed (Unsupported Form)"
    LOGIN = "Action Needed (Login)"
    SKIPPED = "Skipped"
    FAILED = "Failed"
    DRY_RUN = "Dry Run"
    LEARNED = "Learn Only"
    READY = "Ready (Form Checked)"
    SUBMITTING = "Submitting"


class Outcome(StrictModel):
    status: Status
    notes: str
    receipt: str = ""
    blocker_code: str = ""
    missing_fields: list[str] = Field(default_factory=list)


class Profile(StrictModel):
    skills: list[str] = Field(default_factory=list)
    minimum_annual_salary: dict[str, float] = Field(default_factory=dict)
    max_age_hours: int = Field(default=168, gt=0)
    max_required_experience: float = Field(default=6, ge=0)
    min_match_score: int = Field(default=55, ge=0, le=100)
    broad_development_roles: bool = False
    require_stack_match: bool = True
    excluded_application_sources: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    allow_unknown_salary: bool = True
    allow_active_undated_jobs: bool = False
    allow_active_older_jobs: bool = False
    allowed_local_locations: list[str] = Field(default_factory=list)
