from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from src.models import Job, Match, Profile, Salary

ROLE = re.compile(
    r"\b(backend|back.end|platform|systems?|full[ -]?stack|infrastructure|sdk|runtime|python|node\.?js|golang|llm|machine learning|ai|software|distributed)\b",
    re.I,
)
ENGINEER = re.compile(r"\b(engineer|developer|architect|sde)\b", re.I)
TECH = {
    "Python": r"\bpython\b",
    "Django": r"\bdjango\b",
    "FastAPI": r"\bfastapi\b",
    "Node.js": r"\bnode\.?\s?js\b",
    "TypeScript": r"\btypescript\b",
    "JavaScript": r"\bjavascript\b",
    "Java": r"\bjava\b",
    "Ruby": r"\bruby\b",
    "Kotlin": r"\bkotlin\b",
    "Go": r"\b(?i:golang|go programming|go engineer)\b|\bGo\b",
    "Rust": r"\brust\b",
    "C++": r"\bc\+\+",
    "C#": r"\bc#",
    "PostgreSQL": r"\b(postgres|postgresql)\b",
    "SQL": r"\bsql\b",
    "Redis": r"\bredis\b",
    "Kafka": r"\bkafka\b",
    "Docker": r"\bdocker\b",
    "gRPC": r"\bgrpc\b",
    "MCP": r"\b(mcp|model context protocol)\b",
    "OpenTelemetry": r"\bopentelemetry\b",
    "Kubernetes": r"\bkubernetes\b",
    "AWS": r"\baws\b",
    "React": r"\breact(?:\.?js)?\b",
    "Fastify": r"\bfastify\b",
    "Express": r"\bexpress\b",
    "MongoDB": r"\bmongodb\b",
    "MySQL": r"\bmysql\b",
    "tRPC": r"\btrpc\b",
    "OpenAI": r"\bopenai\b",
    "LLM": r"\b(llms?|large language models?)\b",
}
LANGUAGES = {
    "Python",
    "Node.js",
    "TypeScript",
    "JavaScript",
    "Java",
    "Ruby",
    "Kotlin",
    "Go",
    "Rust",
    "C++",
    "C#",
}
ALLOWED_REGION = r"\b(india|worldwide|anywhere|global(?:ly)?|apac|asia(?:.pacific)?)\b"
RESTRICTED_REGION = (
    r"\b(north america|south america|americas|amer|korea|south korea|united arab emirates|uae|united states|u\.?s\.?a?\.?|canada|europe|european union|eu|eea|uk|united kingdom|"
    r"great britain|emea|latam|australia|new zealand|singapore|japan|spain|germany|deutschland|"
    r"ireland|sweden|france|netherlands|poland|italy|switzerland|austria|belgium|czech(?:ia)?|"
    r"portugal|denmark|finland|norway|romania|israel|brazil|mexico|colombia|argentina|chile)\b"
)


def technologies(text: str) -> list[str]:
    return [k for k, pattern in TECH.items() if re.search(pattern, text, 0 if k == "Go" else re.I)]


def stated_salaries(text: str) -> list[Salary]:
    """Parse only explicit annual INR/USD amounts, never infer FX or hours per year."""
    ranges = []
    for m in re.finditer(
        r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:lpa|lakhs? per annum)\b",
        text,
        re.I,
    ):
        ranges.append(
            Salary(currency="INR", minimum=float(m[1]) * 100000, maximum=float(m[2]) * 100000)
        )
    for m in re.finditer(
        r"\b(?:USD|INR)\s*([\d,]+)(k)?\s*(?:-|–|to)\s*(?:USD|INR)?\s*([\d,]+)(k)?\s*(?:/\s*(?:year|yr)|per (?:year|annum)|annually)\b",
        text,
        re.I,
    ):
        currency = m[0][:3].upper()
        lower = float(m[1].replace(",", "")) * (1000 if m[2] else 1)
        upper = float(m[3].replace(",", "")) * (1000 if m[4] else 1)
        ranges.append(Salary(currency=currency, minimum=lower, maximum=upper))
    return ranges


def required_experience(job: Job) -> float | None:
    """Recognize explicit candidate-tenure requirements, including flattened HTML prose."""
    found = [job.experience_min] if job.experience_min is not None else []
    number = r"(?:(?:overall|total)\s+)?(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)"
    patterns = (
        r"(?:^\s*(?:[-•*]\s*)?|:\s*|\b(?:at least|minimum(?: of)?|requires?|must have|you have|you bring|you possess)\s+)"
        + number
        + r"\s+(?:of\s+)?(?:(?:professional|relevant|commercial|industry|software|engineering|backend|development|hands.on)\s+){0,5}experience\b",
        r"\b(?:minimum|required|professional|overall|total)\s+experience\s*(?:of\s*)?(?:at least\s*)?" + number,
    )
    for sentence in re.split(r"[;\n]|(?<=[.!?])\s+", job.description):
        if re.search(r"preferred|nice.to.have|bonus|not required|up to", sentence, re.I):
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, sentence, re.I):
                found.append(float(match[1]))
    return max(found) if found else None


def software_delivery_evidence(description: str) -> bool:
    """Require creation of software, rather than tools, labels or operating duties."""
    duties = re.sub(r"^\s*Function:.*$", "", description, flags=re.I | re.M)
    creation = r"\b(?:build(?:s|ing)?|built|develop(?:s|ing|ed)?|architect(?:s|ing|ed)?|implement(?:s|ing|ed)?|ship(?:s|ping|ped)?)\b"
    software_objects = (
        r"\b(?:back[ -]?end|front[ -]?end|full[ -]?stack|apis?|software|"
        r"web applications?|microservices|developer (?:tooling|tools|platform|infra(?:structure)?)|"
        r"sdks?|async(?:hronous)? task runner|configuration management platform)\b"
    )
    if re.search(creation + r"[^.\n]{0,180}" + software_objects, duties, re.I):
        return True
    production_coding = re.search(
        r"\b(?:write|writing|build|develop|implement)\s+"
        r"(?:and\s+(?:ship|operate|deploy)\s+)?production(?:[ -]grade)?\s+"
        r"(?:code|software(?:\s+systems?)?)\b", duties, re.I
    )
    software_context = re.search(
        r"\b(?:backend engineering|apis?|distributed (?:services|systems)|"
        r"developer infra(?:structure)? products)\b", duties, re.I
    )
    # Developer infrastructure adverts may describe ownership of production code
    # and shipping, without prescribing a programming language or specific API.
    developer_product = re.search(r"\bdeveloper infra(?:structure)? products\b", duties, re.I)
    production_ownership = (
        re.search(r"\b(?:ship|shipped|shipping)\b", duties, re.I)
        and re.search(r"\bcode\b", duties, re.I)
        and re.search(r"\bproduction\b", duties, re.I)
    )
    agent_delivery = (
        re.search(creation + r"[^.\n]{0,100}\b(?:agentic tools|agentic workflows)\b", duties, re.I)
        and re.search(r"\bsoftware engineering fundamentals\b", duties, re.I)
        and re.search(r"\b(?:REST APIs|API-driven automation)\b", duties, re.I)
        and re.search(r"\bunit tests\b", duties, re.I)
    )
    web_service_coding = re.search(
        r"\b(?:write|writing|develop(?:ing)?|implement(?:ing)?)\s+RESTful\s+web\s+services\b",
        duties, re.I,
    )
    return bool(
        (production_coding and software_context)
        or (developer_product and production_ownership)
        or agent_delivery
        or web_service_coding
    )


def target_engineering_role(title: str, profile: Profile, description: str = "") -> bool:
    """Classify broad software roles without changing the advertised job title."""
    if not profile.broad_development_roles:
        return bool(ROLE.search(title) and ENGINEER.search(title))
    # Normalize observed title formatting only for broad-mode classification.
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"\b(full)\s*-\s*(stack)\b", r"\1-\2", title, flags=re.I)
    title = re.sub(r"\b(front|back)\s*-\s*(end)\b", r"\1-\2", title, flags=re.I)
    title = re.sub(r"\bbackened\b", "backend", title, flags=re.I)
    if re.search(
        r"\b(sales|recruiter|recruiting|marketing|hr|human resources|director|vp|head of)\b",
        title,
        re.I,
    ):
        return False
    if re.search(
        r"\b(mechanical|civil|structural|chemical|manufacturing|electrical|hardware|vlsi)\b",
        title,
        re.I,
    ) and not re.search(r"\b(software|developer|firmware|embedded)\b", title, re.I):
        return False
    # Generic MTS titles need an explicit software domain in the description.
    # A bare mention of integration APIs and programming languages is not enough.
    if re.search(r"\b(?:member of (?:the )?technical staff|mts)\b", title, re.I) and not re.search(
        r"\b(?:software|platform|systems?)\b", title, re.I
    ) and not re.search(
        r"\b(?:software\s+(?:engineering|development)|back[ -]?end|front[ -]?end|full[ -]?stack)\b",
        description,
        re.I,
    ):
        return False
    if re.search(
        r"\b(?:founding(?: (?:product|software))? engineer|forward (?:deployed|deployment) engineer|smts)\b",
        title,
        re.I,
    ):
        # Ambiguous titles need actual software-building duties, not merely
        # a Function label, language tags, field installation or physical design.
        duties = re.sub(r"^\s*Function:.*$", "", description, flags=re.I | re.M)
        software_building = re.search(
            r"\b(?:build(?:s|ing)?|built|develop(?:s|ing|ed)?|architect(?:s|ing|ed)?|"
            r"implement(?:s|ing|ed)?|ship(?:s|ping|ped)?)\b[^.\n]{0,140}"
            r"\b(?:back.end|backend|front.end|frontend|full[ -]?stack|apis?|"
            r"software|web applications?|microservices|developer tooling|sdks?)\b",
            duties,
            re.I,
        )
        explicit_software_hiring = re.search(
            r"\b(?:looking for|hiring|seeking)\s+(?:an?\s+)?"
            r"(?:(?:founding|senior|lead)\s+)?"
            r"(?:full[ -]?stack|back.end|front.end|software)\s+(?:engineer|developer)\b",
            duties,
            re.I,
        )
        # Some FDE postings specify production coding rather than a named
        # language. Require direct creation duties plus backend/API context;
        # deployment operations, sales collateral and tag lists are insufficient.
        production_software_building = re.search(
            r"\b(?:writ(?:e|es|ing)|build(?:s|ing)?|built|develop(?:s|ing|ed)?|"
            r"implement(?:s|ing|ed)?)\s+"
            r"(?:and\s+(?:ship(?:s|ping|ped)?|operat(?:e|es|ing)|deploy(?:s|ing|ed)?)\s+)?"
            r"production(?:[ -]grade)?\s+(?:code|software(?:\s+systems?)?)\b",
            duties,
            re.I,
        )
        backend_api_context = re.search(
            r"\b(?:back[ -]?end\s+(?:engineering|development|services?|systems?)|"
            r"apis?|distributed\s+(?:services|systems))\b",
            duties,
            re.I,
        )
        # Some FDE adverts name full-stack engineering directly and describe
        # production solution delivery separately from backend implementation.
        # Require both assigned software duties and production creation; a
        # language list or collaboration with a development team is insufficient.
        fullstack_production_delivery = (
            re.search(r"\bthis role entails full[ -]?stack solution engineering\b", duties, re.I)
            and re.search(r"\bbackend development in\s+(?:Python|Java|TypeScript|JavaScript|Go|Ruby)\b", duties, re.I)
            and re.search(r"\bdesign,\s*build,\s*and\s*deploy\s+production\s+solutions\b", duties, re.I)
        )
        return bool(
            (
                (software_building or explicit_software_hiring)
                and LANGUAGES.intersection(technologies(duties))
            )
            or (production_software_building and backend_api_context)
            or software_delivery_evidence(duties)
            or fullstack_production_delivery
        )
    # Some employers use Analyst/Associate as a grade for developers. Require
    # an explicit development domain AND actual software creation; a corporate
    # grade, Function label or language/tool list alone is insufficient.
    if re.search(r"\b(?:analyst|associate)\b", title, re.I) and re.search(
        r"\b(?:software\s+(?:engineering|development)|applications?\s+development|technology\s+development)\b",
        title, re.I,
    ):
        return bool(
            not re.search(r"\b(?:business|financial|finance|risk|compliance|support|operations)\b|\bdata\s+analyst\b", title, re.I)
            and software_delivery_evidence(description)
            and LANGUAGES.intersection(technologies(description))
        )
    conventional_title = bool(
        re.search(
            r"\b(software|development|backend|back.end|frontend|front.end|full[ -]?stack|"
            r"mobile|android|ios|platform|systems?|infrastructure|sdk|runtime|"
            r"devops|site reliability|data|machine learning|ai|llm|qa|test(?:ing)?|"
            r"python|java|node\.?js|golang|ruby)\b",
            title,
            re.I,
        )
        and re.search(r"\b(engineer|developer|programmer|architect)\b", title, re.I)
    ) or bool(re.search(r"\b(sde|sdet|software developer|developer)\b", title, re.I))
    if conventional_title:
        return True
    if re.fullmatch(r"(?:senior\s+)?applications? engineer", title, re.I):
        # The captured Application Engineer vacancy explicitly assigns the
        # candidate software-developer work. Language tags alone are insufficient.
        if re.search(r"\bwork(?:ing)? as an? software developer\b", description, re.I):
            return bool(LANGUAGES.intersection(technologies(description)))
    # These domains identify software work even when the title says lead or
    # specialist instead of engineer. Generic data/support/operations leads do not.
    if re.search(
        r"\b(software|back.end|backend|front.end|frontend|full[ -]?stack|platform|sdk|runtime|devops)\b",
        title,
        re.I,
    ) and re.search(r"\b(lead|specialist)\b", title, re.I):
        return True
    contextual_title = re.search(
        r"\b(tech(?:nical)? lead|member of (?:the )?technical staff|mts|sse|se|"
        r"implementation engineer|integration engineer|applications? dev engineer|agent(?:ic)? engineer)\b",
        title,
        re.I,
    ) or re.search(r"\b(back.end|backend|front.end|frontend|full[ -]?stack)\b", title, re.I)
    if not contextual_title:
        # An unconventional engineer title needs actual software delivery.
        # Customer support, sales and field operations cannot enter this route.
        return bool(
            ENGINEER.search(title)
            and not re.search(r"\b(?:support|customer|solutions?|field)\b", title, re.I)
            and software_delivery_evidence(description)
        )
    software_context = re.search(
        r"\b(?:software\s+(?:engineering|development)|"
        r"(?:back.end|backend|front.end|frontend|full[ -]?stack)\s+"
        r"(?:engineering|development|developers?|engineers?|services|systems)|(?:web|mobile)\s+applications?)\b",
        description,
        re.I,
    )
    qualified_mts = bool(
        re.search(r"\b(member of (?:the )?technical staff|mts)\b", title, re.I)
        and re.search(r"\b(software|platform|systems?)\b", title, re.I)
    )
    if not software_context and qualified_mts:
        software_context = re.search(
            r"\b(?:build(?:s|ing)?|built|develop(?:s|ing|ed)?|implement(?:s|ing|ed)?|"
            r"writ(?:e|es|ing)|wrote|cod(?:e|es|ing)|maintain(?:s|ing|ed)?)\b",
            description,
            re.I,
        )
    if not software_context and re.search(r"\bagent(?:ic)? engineer\b", title, re.I):
        software_context = re.search(
            r"\b(?:write|writing|build|develop|implement)\b[^.\n]{0,90}\b(?:code|python|apis?|software)\b",
            description,
            re.I,
        )
    # A Function label alone, or a cloud-tool keyword list alone, cannot admit
    # generic titles. This is role evidence, not a candidate skill-match override.
    coding_technologies = LANGUAGES | {
        "SQL",
        "Django",
        "FastAPI",
        "React",
        "Fastify",
        "Express",
        "tRPC",
        "gRPC",
    }
    return bool(
        (software_context and coding_technologies.intersection(technologies(description)))
        or software_delivery_evidence(description)
    )


def gate_reasons(job: Job, profile: Profile, now: datetime) -> list[str]:
    if job.source in profile.excluded_application_sources:
        return ["Application provider excluded by run policy"]
    if job.company.casefold().strip() in {c.casefold().strip() for c in profile.excluded_companies}:
        return ["Employer excluded by run policy"]
    role_geography = f"{job.title}\n{job.location}".lower()
    explicit_india_or_worldwide = bool(re.search(r"\b(india|worldwide)\b", role_geography))
    if re.search(RESTRICTED_REGION, role_geography) and not explicit_india_or_worldwide:
        # Explicit role geography takes precedence over corporate boilerplate in
        # the description, even if it mentions "global" or "work from anywhere".
        return ["Location restricts remote hiring outside India"]
    fail = []
    recently_verified = (
        job.verified_at is not None
        and now - timedelta(hours=24) <= job.verified_at <= now + timedelta(minutes=5)
    )
    location = job.location.lower()
    local = (
        bool(profile.allowed_local_locations)
        and any(
            re.search(r"(?<!\w)" + re.escape(city.lower()) + r"(?!\w)", location)
            for city in profile.allowed_local_locations
        )
        and not re.search(RESTRICTED_REGION, location)
    )
    verified_indian_local = recently_verified and local and bool(re.search(r"\bindia\b", location))
    if job.published_at is None:
        active_verified = (
            profile.allow_active_undated_jobs
            and job.source in {"greenhouse", "lever", "ashby", "instahyre", "wellfound", "rippling"}
            and job.verified_at is not None
            and now - timedelta(hours=24) <= job.verified_at <= now + timedelta(minutes=5)
        )
        if not active_verified:
            fail.append(
                f"Unknown publication time; cannot establish freshness under {profile.max_age_hours} hours"
            )
    elif job.published_at > now + timedelta(minutes=5):
        fail.append("Publication timestamp is in the future")
    elif now - job.published_at >= timedelta(hours=profile.max_age_hours):
        older_open_allowed = (
            profile.allow_active_older_jobs
            and job.source in {"greenhouse", "lever", "ashby", "instahyre", "wellfound", "rippling", "feed"}
            and recently_verified
        )
        if not older_open_allowed:
            fail.append(f"Published at least {profile.max_age_hours} hours ago")
    if not target_engineering_role(job.title, profile, job.description):
        fail.append("Not a target engineering role")
    if not profile.broad_development_roles and re.search(
        r"\b(intern|internship|junior|graduate|director|vp|head of|frontend|front.end)\b",
        job.title,
        re.I,
    ):
        fail.append("Role level or primary discipline outside target")
    text = f"{job.title}\n{job.location}\n{job.description}"
    # Keep sentence context so 'no security clearance required' does not exclude a role.
    sentences = re.split(
        r"(?<=[.!?;])\s+|\n+", text.lower().replace("u.s.", "us").replace("e.u.", "eu")
    )
    for sentence in sentences:
        sentence = re.sub(
            r"\b(?:(?:us|united states|eu|european)\s+)?citizenship(?:/visa)?\s+(?:is\s+)?not\s+required\b",
            "",
            sentence,
        )
        negated_clearance = re.search(
            r"\b(no|not)\s+(?:active\s+)?(?:security\s+)?clearance\s+(?:is\s+)?required|clearance (?:is )?not required|not required to (?:have|hold|obtain).{0,15}clearance",
            sentence,
        )
        if not negated_clearance and re.search(
            r"(active|current|require[ds]?|must|obtain|eligible for).{0,50}(security clearance|top.secret|ts/sci|secret clearance)|(security clearance|ts/sci).{0,35}(required|mandatory)",
            sentence,
        ):
            fail.append("Required security clearance")
        if re.search(
            r"(must|require[ds]?|only|need to).{0,65}(us|u\.s\.|united states|eu|european|uk|british).{0,20}(citizen|citizenship)|(us|u\.s\.|eu|european|british).{0,20}(citizens? only|citizenship required)",
            sentence,
        ):
            fail.append("Required foreign citizenship")
        if re.search(r"(must|required|mandatory|only)", sentence) and re.search(
            r"citizens? of (?:the )?"
            + RESTRICTED_REGION
            + r"|"
            + RESTRICTED_REGION
            + r".{0,15}citizenship.{0,20}(required|mandatory)",
            sentence,
        ):
            fail.append("Required foreign citizenship")
        if re.search(
            r"(must|require[ds]?|only|need to).{0,55}(reside|based|located|resident|live|authorized to work|right to work|eligible to work).{0,35}"
            + RESTRICTED_REGION,
            sentence,
        ):
            fail.append("Explicit residence or work-authorization restriction outside India")
        if re.search(
            r"(must|require[ds]?|only|mandatory).{0,40}"
            + RESTRICTED_REGION
            + r".{0,25}(work authori[sz]ation|work permit|residen)|(?:hiring|applicants|candidates|remote).{0,20}only.{0,20}"
            + RESTRICTED_REGION,
            sentence,
        ):
            fail.append("Restricted foreign hiring or work permit")
        # A list of possible work personas is not a role-specific onsite requirement.
        physical_sentence = re.sub(
            r"\(\s*flexible\s*,\s*remote\s*,?\s*or\s*required in office\s*\)",
            "", sentence,
        )
        if re.search(
            r"(on.site|onsite|in.office|in the office|hybrid|relocat).{0,50}(required|mandatory|must)|(?:must|required).{0,50}(on.site|onsite|in.office|in the office|relocat)",
            physical_sentence,
        ) and not re.search(
            r"\b(india|delhi|ncr|noida|gurgaon|gurugram|faridabad|ghaziabad|bengaluru|bangalore|hyderabad|mumbai|pune)\b",
            physical_sentence,
        ):
            # A generic attendance sentence need not repeat the exact employer
            # location already verified in India. Full matching deliberately
            # rejects added foreign-place wording instead of overriding it.
            generic_attendance = bool(re.fullmatch(
                r"(?:employees(?: in hybrid roles)? are |you are |this role is )?"
                r"(?:required to (?:be|work)|must (?:be|work)) "
                r"(?:in the office|on[ -]?site)"
                r"(?: (?:at least )?\d+(?:\s*[-–]\s*\d+)?\s*days?\s*(?:/|per\s+)(?:week|wk))?\.?"
                r"|(?:hybrid|on[ -]?site|in[ -]?office) (?:work|presence) (?:is )?(?:required|mandatory)\.?",
                physical_sentence.strip(),
            ))
            if not (verified_indian_local and generic_attendance):
                fail.append("Mandatory physical presence outside verified Indian location")
    if job.country_restrictions:
        allowed_countries = {
            "india",
            "in",
            "ind",
            "worldwide",
            "anywhere",
            "global",
            "apac",
            "asia",
        }
        if not (set(x.lower() for x in job.country_restrictions) & allowed_countries):
            fail.append("Structured country restrictions exclude India")
    if job.timezone_restrictions:
        offsets = []
        for zone in job.timezone_restrictions:
            m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-]?\d+(?:\.\d+)?)(?::(\d{2}))?", zone, re.I)
            if m:
                hours = float(m[1])
                offsets.append(hours + (-1 if m[1].startswith("-") else 1) * int(m[2] or 0) / 60)
        if 5.5 not in offsets:
            fail.append(
                "Advertised timezone residence restriction does not establish India eligibility"
            )
    minimum_experience = required_experience(job)
    if minimum_experience is not None and minimum_experience > profile.max_required_experience:
        fail.append(
            f"Role exceeds the configured experience band (>{profile.max_required_experience:g} years required)"
        )
    remote = job.remote is True or bool(
        re.search(r"\b(remote|work from anywhere)\b", location + " " + job.description.lower())
    )
    if not local and (
        job.workplace_type.lower().replace("-", "") in {"onsite", "hybrid"} or not remote
    ):
        fail.append("Remote work is not established")
    region_ok = explicit_india_or_worldwide or bool(re.search(ALLOWED_REGION, location))
    if re.search(RESTRICTED_REGION, location) and not explicit_india_or_worldwide:
        region_ok = False
    explicit_remote = re.search(
        r"(?:remote\s*[-(:,]?\s*(?:from |in |across |within )?|work from |hiring (?:in |across )?|candidates (?:in|from) )"
        + ALLOWED_REGION,
        job.description,
        re.I,
    )
    if not (local or region_ok or explicit_remote):
        fail.append("India/worldwide/APAC remote eligibility is not explicit")
    if re.search(RESTRICTED_REGION, location) and not region_ok:
        fail.append("Location restricts remote hiring outside India")
    # A remotely distributed global company is not itself proof of worldwide hiring.
    if re.search(r"\b(us|usa|u\.s\.|eu|europe|uk|canada)[ -]only\b", text, re.I):
        fail.append("Country-restricted role")
    if re.search(
        r"(excluding|except|not (?:available|hiring) in|cannot hire in)\s+(?:candidates (?:from|in)\s+)?india\b",
        text,
        re.I,
    ):
        fail.append("India is explicitly excluded")

    salaries = []
    for salary in job.salaries or stated_salaries(job.description):
        if salary.currency.upper() not in profile.minimum_annual_salary or salary.maximum is None:
            continue
        multiplier = {
            "year": 1,
            "yearly": 1,
            "annual": 1,
            "1 year": 1,
            "month": 12,
            "monthly": 12,
            "1 month": 12,
        }.get(salary.interval.lower())
        if multiplier:
            salaries.append(
                (
                    salary.maximum * multiplier,
                    profile.minimum_annual_salary[salary.currency.upper()],
                )
            )
    # Multiple geo tiers cannot establish India's actual offer: only reject if all known ceilings fail.
    if salaries and all(maximum < minimum for maximum, minimum in salaries):
        fail.append("Published salary ceiling is below the configured minimum")
    if not salaries and not profile.allow_unknown_salary:
        fail.append("Comparable annual salary is unknown")
    return list(dict.fromkeys(fail))


class Evaluator:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.known = set(technologies(" ".join(profile.skills)))

    async def evaluate(self, job: Job, now: datetime | None = None) -> Match:
        reasons = gate_reasons(job, self.profile, now or datetime.now(UTC))
        requested = technologies(job.title + " " + job.description)
        matched = sorted(set(requested) & self.known)
        missing = sorted(set(requested) - self.known)
        general_delivery_fit = (
            self.profile.broad_development_roles
            and not (set(requested) & LANGUAGES)
            and target_engineering_role(job.title, self.profile, job.description)
            and software_delivery_evidence(job.description)
        )
        if self.profile.require_stack_match and not matched and not general_delivery_fit:
            reasons.append("No demonstrated technical stack match")
        primary = set(technologies(job.title)) & LANGUAGES
        if self.profile.require_stack_match and primary and not (primary & self.known):
            reasons.append("Primary role language is not supported by candidate evidence")
        if reasons:
            return Match(
                eligible=False,
                score=0,
                reasons=reasons,
                matched_skills=matched,
                missing_skills=missing,
            )
        score = 70 if general_delivery_fit else round(35 + 65 * len(matched) / max(len(requested), 1))
        notes = [f"Freshness basis: {job.date_basis}", "Remote eligibility and known skills passed"]
        if general_delivery_fit:
            notes[1] = "Language-agnostic software delivery requirements detected"
        if job.published_at is None:
            notes.append(
                f"Publication age unknown; active listing verified at {job.verified_at.isoformat()}"
            )
        notes.append("Deterministic skill score; no model used")
        if not job.salaries:
            notes.append("Salary undisclosed; configured unknown-salary policy permits application")
        eligible = score >= self.profile.min_match_score
        if not eligible:
            notes.append("Below configured match threshold")
        return Match(
            eligible=eligible,
            score=score,
            reasons=notes,
            matched_skills=matched,
            missing_skills=missing,
        )
