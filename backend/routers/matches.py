import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..app.database import get_db
from ..app import models
from ..app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
    MATCH_MODEL,
    MATCH_VERSION,
    PROFILE_ANALYSIS_MODEL,
    PROFILE_ANALYSIS_PROMPT_VERSION,
)


router = APIRouter(
    prefix="/users",
    tags=["Job Matching"]
)


MATCH_PROMPT_VERSION = MATCH_VERSION
MAX_BATCH_MATCH_JOBS = 500

REQUIRED_PROFILE_ANALYSIS_MODEL = PROFILE_ANALYSIS_MODEL
REQUIRED_PROFILE_ANALYSIS_PROMPT_VERSION = PROFILE_ANALYSIS_PROMPT_VERSION
REQUIRED_JOB_ANALYSIS_MODEL = JOB_ANALYSIS_MODEL
REQUIRED_JOB_ANALYSIS_PROMPT_VERSION = JOB_ANALYSIS_PROMPT_VERSION


def safe_json_loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def normalize_text(value: str) -> str:
    return (value or "").lower().strip()


def normalize_list(values):
    if not values:
        return []
    return [normalize_text(v) for v in values if isinstance(v, str) and v.strip()]


def overlap_score(candidate_items, job_items):
    candidate_set = set(normalize_list(candidate_items))
    job_set = set(normalize_list(job_items))

    if not job_set:
        return 70

    if not candidate_set:
        return 0

    matched = candidate_set.intersection(job_set)
    return round((len(matched) / len(job_set)) * 100)


def calculate_role_score(profile_json, profile_analysis, job_analysis):
    tag_score = calculate_role_tag_score(profile_analysis, job_analysis)

    target_roles = normalize_list(profile_json.get("target_roles", []))
    target_families = normalize_list(profile_json.get("target_role_families", []))

    job_title = normalize_text(job_analysis.normalized_role_title)
    job_family = normalize_text(job_analysis.role_family)
    job_subfamily = normalize_text(job_analysis.role_subfamily)

    title_score = 0

    for role in target_roles:
        if role and role == job_title:
            title_score = max(title_score, 100)
        elif role and (role in job_title or job_title in role):
            title_score = max(title_score, 85)

    family_score = 0

    for family in target_families:
        if family and family == job_family:
            family_score = max(family_score, 70)

    profile_role_text = " ".join(target_roles)
    compact_job_title = re.sub(r"[\s_-]+", "", job_title)

    if "backend" in profile_role_text and "fullstack" in compact_job_title:
        title_score = max(title_score, 75)

    if "backend" in profile_role_text and "backend" in job_title:
        title_score = max(title_score, 95)

    if "software" in profile_role_text and job_family in ["engineering", "it", "devops", "qa_testing"]:
        family_score = max(family_score, 60)

    if title_score == 0 and job_subfamily:
        for role in target_roles:
            if any(part in job_subfamily for part in role.split()):
                title_score = max(title_score, 50)

    return round(
        tag_score * 0.60
        + title_score * 0.30
        + family_score * 0.10
    )


def calculate_role_tag_score(profile_analysis, job_analysis):
    ignored_tags = {"other"}

    profile_tags = set(
        normalize_list(
            safe_json_loads(profile_analysis.target_role_tags_json, [])
        )
    ) - ignored_tags

    job_tags = set(
        normalize_list(
            safe_json_loads(job_analysis.role_tags_json, [])
        )
    ) - ignored_tags

    if not profile_tags or not job_tags:
        return 0

    matched_tags = profile_tags.intersection(job_tags)

    if not matched_tags:
        return 0

    job_coverage = len(matched_tags) / len(job_tags)
    profile_coverage = len(matched_tags) / len(profile_tags)

    return round(
        job_coverage * 75
        + profile_coverage * 25
    )


SKILL_ALIASES = {
    "rest api": "rest apis",
    "rest": "rest apis",
    "api": "rest apis",
    "apis": "rest apis",

    "postgres": "postgresql",
    "sql": "database",
    "sqlite": "database",
    "postgresql": "database",

    "backend development": "backend",
    "backend developer": "backend",

    "openai": "openai api",
    "ai integration": "ai integrations",
    "ai integrations": "ai integrations",

    "js": "javascript",
}


def normalize_skill(value: str) -> str:
    value = normalize_text(value)

    value = value.replace("-", " ")
    value = value.replace("_", " ")
    value = value.replace("/", " ")
    value = " ".join(value.split())

    return SKILL_ALIASES.get(value, value)


SKILL_PLACEHOLDERS = {"other", "unknown", "none", "n a", "na"}


def normalize_skill_list(values):
    if not values:
        return []

    return [
        normalize_skill(v)
        for v in values
        if isinstance(v, str) and v.strip()
    ]


def fuzzy_skill_match(candidate_skill, job_skill):
    candidate_skill = normalize_skill(candidate_skill)
    job_skill = normalize_skill(job_skill)

    if candidate_skill == job_skill:
        return True

    if candidate_skill in job_skill or job_skill in candidate_skill:
        return True

    candidate_parts = set(candidate_skill.split())
    job_parts = set(job_skill.split())

    if not candidate_parts or not job_parts:
        return False

    overlap = candidate_parts.intersection(job_parts)

    return len(overlap) >= 2


def skill_match_score(candidate_skills, job_skills):
    # Candidate-side placeholders must never satisfy an unknown job skill.
    # Job-side "other" remains as an unmatched specialized requirement so
    # taxonomy gaps (for example Shopify/Liquid) still reduce the score.
    candidate_skills = [
        value
        for value in normalize_skill_list(candidate_skills)
        if value not in SKILL_PLACEHOLDERS
    ]
    job_skills = normalize_skill_list(job_skills)

    if not job_skills:
        return 70

    if not candidate_skills:
        return 0

    matched_count = 0

    for job_skill in job_skills:
        if any(
            fuzzy_skill_match(candidate_skill, job_skill)
            for candidate_skill in candidate_skills
        ):
            matched_count += 1

    return round((matched_count / len(job_skills)) * 100)


def calculate_skills_score(profile_analysis, job_analysis):
    strong_skills = safe_json_loads(profile_analysis.strong_skills_json, [])
    moderate_skills = safe_json_loads(profile_analysis.moderate_skills_json, [])
    weak_skills = safe_json_loads(profile_analysis.weak_or_basic_skills_json, [])
    tools = safe_json_loads(profile_analysis.tools_json, [])

    required_skills = safe_json_loads(job_analysis.required_skills_json, [])
    preferred_skills = safe_json_loads(job_analysis.preferred_skills_json, [])

    candidate_strong = strong_skills + tools
    candidate_all = strong_skills + moderate_skills + weak_skills + tools

    required_score = skill_match_score(candidate_all, required_skills)
    preferred_score = skill_match_score(candidate_all, preferred_skills)
    strong_required_score = skill_match_score(candidate_strong, required_skills)

    return round(
        required_score * 0.65
        + preferred_score * 0.20
        + strong_required_score * 0.15
    )


def calculate_seniority_score(profile_analysis, job_analysis):
    profile_level = normalize_text(profile_analysis.seniority_level)
    job_level = normalize_text(job_analysis.seniority_level)

    levels = {
        "intern": 0,
        "junior": 1,
        "mid": 2,
        "senior": 3,
        "lead": 4,
        "executive": 5
    }

    if not job_level or job_level == "unknown":
        return 70

    if not profile_level or profile_level == "unknown":
        return 50

    p = levels.get(profile_level)
    j = levels.get(job_level)

    if p is None or j is None:
        return 50

    if p == j:
        return 100

    if p > j:
        return 85

    diff = j - p

    if diff == 1:
        return 55

    if diff == 2:
        return 25

    return 5


LANGUAGE_ALIASES = {
    "de": "german",
    "deutsch": "german",
    "german language": "german",
    "en": "english",
    "englisch": "english",
    "english language": "english",
    "tr": "turkish",
    "türkisch": "turkish",
    "turkisch": "turkish",
}

CEFR_RANKS = {
    "a1": 1,
    "a2": 2,
    "b1": 3,
    "b2": 4,
    "c1": 5,
    "c2": 6,
    "native": 7,
}

LANGUAGE_LEVEL_ALIASES = {
    "beginner": "a1",
    "basic": "a2",
    "elementary": "a2",
    "intermediate": "b1",
    "upper intermediate": "b2",
    "upper_intermediate": "b2",
    "good": "b2",
    "professional": "c1",
    "business fluent": "c1",
    "business_fluent": "c1",
    "fluent": "c1",
    "advanced": "c1",
    "mother tongue": "native",
    "mother_tongue": "native",
    "native speaker": "native",
}


def normalize_language_name(value: str) -> str:
    normalized = normalize_phrase(value)
    return LANGUAGE_ALIASES.get(normalized, normalized)


def normalize_language_level(value: str) -> str:
    normalized = normalize_phrase(value).replace(" ", "_")
    normalized = LANGUAGE_LEVEL_ALIASES.get(
        normalized,
        LANGUAGE_LEVEL_ALIASES.get(normalized.replace("_", " "), normalized),
    )
    return normalized if normalized in {*CEFR_RANKS, "unknown"} else "unknown"


def make_requirement_issue(code: str, message: str, **details):
    issue = {"code": code, "message": message}
    if details:
        issue["details"] = details
    return issue


def get_candidate_languages(profile_analysis) -> dict[str, str]:
    profile_languages = safe_json_loads(profile_analysis.languages_json, [])
    result = {}

    for item in profile_languages:
        if isinstance(item, dict):
            language = normalize_language_name(item.get("language"))
            level = normalize_language_level(item.get("level"))
        elif isinstance(item, str):
            language = normalize_language_name(item)
            level = "unknown"
        else:
            continue

        if language and language != "unknown":
            result[language] = level

    return result


def get_job_language_requirements(job_analysis) -> list[dict]:
    raw_requirements = safe_json_loads(
        job_analysis.language_requirements_json, []
    )
    requirements = []

    for item in raw_requirements:
        if isinstance(item, dict):
            language = normalize_language_name(item.get("language"))
            minimum_level = normalize_language_level(item.get("minimum_level"))
            required = bool(item.get("required", True))
            alternative_group = normalize_text(item.get("alternative_group"))
            try:
                minimum_required_count = int(
                    item.get("minimum_required_count", 1)
                )
            except (TypeError, ValueError):
                minimum_required_count = 1
        elif isinstance(item, str):
            # Defensive compatibility for malformed v7 data. Matching only accepts
            # v7, but this keeps a single bad row from crashing the batch.
            language = normalize_language_name(item)
            minimum_level = "unknown"
            required = True
            alternative_group = ""
            minimum_required_count = 1
        else:
            continue

        if required and language and language != "unknown":
            requirements.append({
                "language": language,
                "minimum_level": minimum_level,
                "required": True,
                "alternative_group": alternative_group or None,
                "minimum_required_count": max(1, minimum_required_count),
            })

    return requirements


def evaluate_language_requirements(profile_analysis, job_analysis):
    candidate_languages = get_candidate_languages(profile_analysis)
    requirements = get_job_language_requirements(job_analysis)

    if not requirements:
        return 80, [], []

    hard_failures = []
    review_reasons = []
    scores = []

    grouped_requirements = {}
    ungrouped_requirements = []
    for requirement in requirements:
        group_id = requirement.get("alternative_group")
        if group_id:
            grouped_requirements.setdefault(group_id, []).append(requirement)
        else:
            ungrouped_requirements.append(requirement)

    for requirement in ungrouped_requirements:
        language = requirement["language"]
        minimum_level = requirement["minimum_level"]
        candidate_level = candidate_languages.get(language)

        if candidate_level is None:
            scores.append(0)
            hard_failures.append(make_requirement_issue(
                "required_language_missing",
                f"Required language is missing: {language}.",
                language=language,
                minimum_level=minimum_level,
            ))
            continue

        if minimum_level == "unknown":
            scores.append(55)
            review_reasons.append(make_requirement_issue(
                "required_language_level_unknown",
                (
                    f"The job explicitly requires {language}, but the required "
                    "proficiency level is not specified."
                ),
                language=language,
                candidate_level=candidate_level,
            ))
            continue

        if candidate_level == "unknown":
            scores.append(55)
            review_reasons.append(make_requirement_issue(
                "candidate_language_level_unknown",
                f"Candidate level for {language} is unknown.",
                language=language,
                minimum_level=minimum_level,
            ))
            continue

        candidate_rank = CEFR_RANKS.get(candidate_level)
        required_rank = CEFR_RANKS.get(minimum_level)

        if candidate_rank is None or required_rank is None:
            scores.append(55)
            review_reasons.append(make_requirement_issue(
                "language_level_comparison_unavailable",
                f"Language level could not be compared for {language}.",
                language=language,
                candidate_level=candidate_level,
                minimum_level=minimum_level,
            ))
        elif candidate_rank < required_rank:
            scores.append(max(0, 100 - ((required_rank - candidate_rank) * 35)))
            hard_failures.append(make_requirement_issue(
                "required_language_level_not_met",
                (
                    f"Candidate {language} level {candidate_level.upper()} is below "
                    f"required {minimum_level.upper()}."
                ),
                language=language,
                candidate_level=candidate_level,
                minimum_level=minimum_level,
            ))
        else:
            scores.append(100)

    for group_id, alternatives in grouped_requirements.items():
        minimum_required_count = max(
            1,
            min(
                alternatives[0].get("minimum_required_count", 1),
                len(alternatives),
            ),
        )
        qualifying_languages = []
        potentially_qualifying_languages = []
        evaluated_scores = []

        for requirement in alternatives:
            language = requirement["language"]
            minimum_level = requirement["minimum_level"]
            candidate_level = candidate_languages.get(language)

            if candidate_level is None:
                evaluated_scores.append(0)
                continue

            if minimum_level == "unknown":
                potentially_qualifying_languages.append(language)
                evaluated_scores.append(55)
                continue

            if candidate_level == "unknown":
                potentially_qualifying_languages.append(language)
                evaluated_scores.append(55)
                continue

            candidate_rank = CEFR_RANKS.get(candidate_level)
            required_rank = CEFR_RANKS.get(minimum_level)
            if candidate_rank is None or required_rank is None:
                potentially_qualifying_languages.append(language)
                evaluated_scores.append(55)
            elif candidate_rank >= required_rank:
                qualifying_languages.append(language)
                evaluated_scores.append(100)
            else:
                evaluated_scores.append(
                    max(0, 100 - ((required_rank - candidate_rank) * 35))
                )

        if len(qualifying_languages) >= minimum_required_count:
            scores.append(100)
            continue

        if (
            len(qualifying_languages) + len(potentially_qualifying_languages)
            >= minimum_required_count
        ):
            scores.append(55)
            review_reasons.append(make_requirement_issue(
                "language_alternative_requires_review",
                "The candidate may satisfy an alternative language requirement, but the level cannot be confirmed.",
                alternative_group=group_id,
                required_count=minimum_required_count,
                alternatives=[item["language"] for item in alternatives],
                qualifying_languages=qualifying_languages,
                unresolved_languages=potentially_qualifying_languages,
            ))
            continue

        scores.append(max(evaluated_scores) if evaluated_scores else 0)
        hard_failures.append(make_requirement_issue(
            "required_language_alternative_not_met",
            (
                f"Candidate does not meet the requirement to know at least "
                f"{minimum_required_count} of the listed languages."
            ),
            alternative_group=group_id,
            required_count=minimum_required_count,
            alternatives=[item["language"] for item in alternatives],
            minimum_level=alternatives[0].get("minimum_level", "unknown"),
            qualifying_languages=qualifying_languages,
        ))

    return round(sum(scores) / len(scores)), hard_failures, review_reasons


def calculate_language_score(profile_analysis, job_analysis):
    score, _, _ = evaluate_language_requirements(
        profile_analysis, job_analysis
    )
    return score


def get_profile_analysis_json(profile_analysis) -> dict:
    value = safe_json_loads(profile_analysis.analysis_json, {})
    return value if isinstance(value, dict) else {}


def get_profile_sponsorship_status(profile, profile_analysis) -> str:
    direct_value = getattr(profile, "visa_sponsorship_needed", None)
    if direct_value is True:
        return "yes"
    if direct_value is False:
        return "no"

    value = normalize_text(
        get_profile_analysis_json(profile_analysis).get(
            "visa_sponsorship_needed", "unknown"
        )
    )
    return value if value in {"yes", "no", "unknown"} else "unknown"


def calculate_visa_score(profile, profile_analysis, job_analysis):
    needs_sponsorship = get_profile_sponsorship_status(profile, profile_analysis)
    visa = normalize_text(job_analysis.visa_sponsorship)

    if needs_sponsorship == "no":
        return 100

    if needs_sponsorship == "unknown":
        if visa == "yes":
            return 90
        if visa == "no":
            return 45
        return 60

    if visa == "yes":
        return 100
    if visa == "unknown":
        return 50
    if visa == "no":
        return 5
    return 50


def normalize_phrase(value: str) -> str:
    value = normalize_text(value).replace("_", " ").replace("-", " ")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def contains_normalized_phrase(text: str, phrase: str) -> bool:
    normalized_text = normalize_phrase(text)
    normalized_phrase_value = normalize_phrase(phrase)

    if not normalized_text or not normalized_phrase_value:
        return False

    return f" {normalized_phrase_value} " in f" {normalized_text} "


def extract_work_type_preferences(value: str) -> set[str]:
    text = normalize_phrase(value)

    if not text or contains_normalized_phrase(text, "any"):
        return set()

    preferences = set()

    if contains_normalized_phrase(text, "remote"):
        preferences.add("remote")
    if contains_normalized_phrase(text, "hybrid"):
        preferences.add("hybrid")
    if any(
        contains_normalized_phrase(text, phrase)
        for phrase in ["onsite", "on site", "office based"]
    ):
        preferences.add("onsite")

    return preferences


def extract_employment_type_preferences(value: str) -> set[str]:
    text = normalize_phrase(value)

    if not text or contains_normalized_phrase(text, "any"):
        return set()

    preferences = set()

    phrase_map = {
        "full_time": ["full time", "fulltime"],
        "part_time": ["part time", "parttime"],
        "internship": ["internship", "intern"],
        "working_student": ["working student", "werkstudent"],
        "contract": ["contract"],
        "freelance": ["freelance"],
        "temporary": ["temporary", "fixed term"],
    }

    for canonical_value, phrases in phrase_map.items():
        if any(
            contains_normalized_phrase(text, phrase)
            for phrase in phrases
        ):
            preferences.add(canonical_value)

    return preferences


def calculate_work_type_score(profile, job_analysis):
    preferred_types = extract_work_type_preferences(
        profile.preferred_work_type or ""
    )
    job_work_type = normalize_text(job_analysis.work_type).replace("-", "_")

    if not preferred_types:
        return 80

    if not job_work_type or job_work_type == "unknown":
        return 60

    if job_work_type in preferred_types:
        return 100

    if job_work_type == "hybrid" and "remote" in preferred_types:
        return 75

    if job_work_type == "remote" and "hybrid" in preferred_types:
        return 85

    if job_work_type == "onsite" and "hybrid" in preferred_types:
        return 55

    return 35


def calculate_employment_type_score(profile, job_analysis):
    preference_text = " ".join([
        profile.preferred_work_type or "",
        profile.extra_preferences or "",
    ])
    preferred_types = extract_employment_type_preferences(preference_text)
    job_employment_type = normalize_text(job_analysis.employment_type)

    if not preferred_types:
        return 80

    if not job_employment_type or job_employment_type == "unknown":
        return 60

    if job_employment_type in preferred_types:
        return 100

    if "full_time" in preferred_types:
        if job_employment_type in {"internship", "working_student", "part_time"}:
            return 20
        if job_employment_type in {"freelance", "temporary"}:
            return 30
        if job_employment_type == "contract":
            return 50

    return 35


def get_target_locations(profile) -> list[str]:
    locations = []

    if profile.target_location:
        locations.append(profile.target_location)

    locations.extend(
        safe_json_loads(profile.target_locations_json, [])
        if profile.target_locations_json else []
    )

    return normalize_list(locations)


def score_single_location(target_location: str, job_location: str) -> int:
    if target_location in job_location or job_location in target_location:
        return 100

    german_cities = {
        "berlin", "munich", "münchen", "hamburg", "cologne", "köln",
        "frankfurt", "stuttgart", "düsseldorf", "dusseldorf", "leipzig",
        "dresden", "nuremberg", "nürnberg", "hannover", "bremen",
    }

    if "germany" in target_location and (
        "germany" in job_location
        or any(city in job_location for city in german_cities)
    ):
        return 90

    if "remote" in job_location:
        return 85

    return 45


def calculate_location_score(profile, job):
    target_locations = get_target_locations(profile)
    job_location = normalize_text(job.location)
    job_source = normalize_text(job.source)

    if not target_locations:
        return 70

    if not job_location:
        return 60

    scores = [
        score_single_location(target_location, job_location)
        for target_location in target_locations
    ]

    # Arbeitnow is currently used as a Germany-focused source in this project.
    # Its location field often contains only a city name, not the country.
    if job_source == "arbeitnow" and any(
        "germany" in target_location for target_location in target_locations
    ):
        if "remote" in job_location:
            scores.append(85)
        else:
            scores.append(90)

    return max(scores)


def get_excluded_roles(profile, profile_analysis) -> list[str]:
    excluded_roles = []

    excluded_roles.extend(
        safe_json_loads(profile.excluded_roles_json, [])
        if profile.excluded_roles_json else []
    )
    excluded_roles.extend(
        safe_json_loads(profile_analysis.excluded_roles_json, [])
    )

    normalized_roles = normalize_list(excluded_roles)
    return list(dict.fromkeys(normalized_roles))


def phrase_is_present(phrase: str, text: str) -> bool:
    return contains_normalized_phrase(text, phrase)


def calculate_excluded_role_matches(profile, profile_analysis, job, job_analysis):
    excluded_roles = get_excluded_roles(profile, profile_analysis)

    if not excluded_roles:
        return []

    job_role_text = " ".join([
        job.title or "",
        job_analysis.normalized_role_title or "",
        job_analysis.role_family or "",
        job_analysis.role_subfamily or "",
        " ".join(safe_json_loads(job_analysis.role_tags_json, [])),
    ])

    return [
        role for role in excluded_roles
        if phrase_is_present(role, job_role_text)
    ]


def get_job_hard_requirements(job_analysis) -> dict:
    stored = safe_json_loads(job_analysis.dealbreakers_json, {})
    if isinstance(stored, dict):
        hard_requirements = stored.get("hard_requirements", {})
        if isinstance(hard_requirements, dict):
            return hard_requirements

    analysis_json = safe_json_loads(job_analysis.analysis_json, {})
    if isinstance(analysis_json, dict):
        hard_requirements = analysis_json.get("hard_requirements", {})
        if isinstance(hard_requirements, dict):
            return hard_requirements

    return {}


def get_job_dealbreaker_notes(job_analysis) -> list[str]:
    stored = safe_json_loads(job_analysis.dealbreakers_json, {})
    if isinstance(stored, dict):
        notes = stored.get("notes", [])
        return [str(note) for note in notes if str(note).strip()]
    if isinstance(stored, list):
        return [str(note) for note in stored if str(note).strip()]
    return []


def get_profile_years_of_experience(profile, profile_analysis):
    direct_value = getattr(profile, "years_of_experience", None)
    if direct_value is not None:
        return float(direct_value)

    value = get_profile_analysis_json(profile_analysis).get("years_of_experience")
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def calculate_experience_score(profile, profile_analysis, job_analysis):
    hard_requirements = get_job_hard_requirements(job_analysis)
    minimum_years = hard_requirements.get("minimum_years_experience")

    if minimum_years is None:
        return 80

    try:
        minimum_years = float(minimum_years)
    except (TypeError, ValueError):
        return 60

    candidate_years = get_profile_years_of_experience(profile, profile_analysis)
    if candidate_years is None:
        return 50

    if candidate_years >= minimum_years:
        return 100

    gap = minimum_years - candidate_years
    if gap <= 0.5:
        return 65
    if gap <= 1:
        return 40
    return 10


COUNTRY_ALIASES = {
    "de": "germany",
    "deutschland": "germany",
    "german": "germany",
    "tr": "turkey",
    "türkiye": "turkey",
    "turkiye": "turkey",
    "turkey": "turkey",
    "uk": "united kingdom",
    "u k": "united kingdom",
    "usa": "united states",
    "u s a": "united states",
}

EU_EEA_COUNTRIES = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia",
    "czech republic", "denmark", "estonia", "finland", "france",
    "germany", "greece", "hungary", "ireland", "italy", "latvia",
    "lithuania", "luxembourg", "malta", "netherlands", "poland",
    "portugal", "romania", "slovakia", "slovenia", "spain", "sweden",
    "iceland", "liechtenstein", "norway",
}


def normalize_country(value: str) -> str:
    normalized = normalize_phrase(value)
    return COUNTRY_ALIASES.get(normalized, normalized)


def get_profile_work_authorization(profile, profile_analysis) -> str:
    direct = normalize_text(getattr(profile, "work_authorization_status", None))
    if direct in {"germany", "eu_eea", "other_country_only", "none", "unknown"}:
        if direct != "unknown":
            return direct

    value = normalize_text(
        get_profile_analysis_json(profile_analysis).get(
            "work_authorization_status", "unknown"
        )
    )
    return value if value in {
        "germany", "eu_eea", "other_country_only", "none", "unknown"
    } else "unknown"


def get_profile_student_status(profile, profile_analysis) -> str:
    direct = normalize_text(getattr(profile, "student_status", None))
    if direct in {"currently_enrolled", "not_enrolled", "unknown"}:
        if direct != "unknown":
            return direct

    value = normalize_text(
        get_profile_analysis_json(profile_analysis).get(
            "student_status", "unknown"
        )
    )
    return value if value in {
        "currently_enrolled", "not_enrolled", "unknown"
    } else "unknown"


def get_profile_residence_country(profile, profile_analysis) -> str:
    direct = normalize_country(getattr(profile, "current_residence_country", None))
    if direct and direct != "unknown":
        return direct

    return normalize_country(
        get_profile_analysis_json(profile_analysis).get(
            "current_residence_country", "unknown"
        )
    ) or "unknown"


def evaluate_hard_requirements(profile, profile_analysis, job_analysis):
    hard_failures = []
    review_reasons = []

    _, language_failures, language_reviews = evaluate_language_requirements(
        profile_analysis, job_analysis
    )
    hard_failures.extend(language_failures)
    review_reasons.extend(language_reviews)

    hard_requirements = get_job_hard_requirements(job_analysis)

    needs_sponsorship = get_profile_sponsorship_status(profile, profile_analysis)
    visa_sponsorship = normalize_text(job_analysis.visa_sponsorship)

    if needs_sponsorship == "yes":
        if visa_sponsorship == "no":
            hard_failures.append(make_requirement_issue(
                "visa_sponsorship_unavailable",
                "Candidate needs sponsorship, but the job does not offer it.",
            ))
    elif needs_sponsorship == "unknown" and visa_sponsorship == "no":
        review_reasons.append(make_requirement_issue(
            "candidate_sponsorship_need_unknown",
            "Job does not offer sponsorship, but candidate sponsorship need is unknown.",
        ))

    student_enrollment_required = bool(
        hard_requirements.get("student_enrollment_required")
    ) or normalize_text(job_analysis.employment_type) == "working_student"

    if student_enrollment_required:
        student_status = get_profile_student_status(profile, profile_analysis)
        if student_status == "not_enrolled":
            hard_failures.append(make_requirement_issue(
                "student_enrollment_required",
                "The job requires active student enrollment.",
                candidate_student_status=student_status,
            ))
        elif student_status == "unknown":
            review_reasons.append(make_requirement_issue(
                "candidate_student_status_unknown",
                "The job requires student enrollment, but candidate status is unknown.",
            ))

    authorization_requirement = normalize_text(
        hard_requirements.get("work_authorization", "unknown")
    )
    candidate_authorization = get_profile_work_authorization(
        profile, profile_analysis
    )

    if authorization_requirement in {"germany", "any_valid"}:
        if candidate_authorization in {"other_country_only", "none"}:
            hard_failures.append(make_requirement_issue(
                "required_work_authorization_not_met",
                "The job requires valid work authorization in Germany.",
                required=authorization_requirement,
                candidate=candidate_authorization,
            ))
        elif candidate_authorization == "unknown":
            review_reasons.append(make_requirement_issue(
                "candidate_work_authorization_unknown",
                "The job requires work authorization, but candidate status is unknown.",
                required=authorization_requirement,
            ))
    elif authorization_requirement == "eu_eea":
        if candidate_authorization in {"other_country_only", "none"}:
            hard_failures.append(make_requirement_issue(
                "eu_eea_work_authorization_required",
                "The job explicitly requires EU/EEA work authorization.",
                candidate=candidate_authorization,
            ))
        elif candidate_authorization != "eu_eea":
            review_reasons.append(make_requirement_issue(
                "eu_eea_authorization_not_confirmed",
                "EU/EEA authorization is required but not confirmed.",
                candidate=candidate_authorization,
            ))

    residency_requirement = normalize_text(
        hard_requirements.get("residency", "unknown")
    )
    residency_locations = [
        normalize_country(value)
        for value in hard_requirements.get("residency_locations", [])
        if isinstance(value, str) and value.strip()
    ]
    candidate_country = get_profile_residence_country(profile, profile_analysis)

    if residency_requirement == "germany":
        if candidate_country == "unknown":
            review_reasons.append(make_requirement_issue(
                "candidate_residence_unknown",
                "Current residence in Germany is required but candidate residence is unknown.",
            ))
        elif candidate_country != "germany":
            hard_failures.append(make_requirement_issue(
                "germany_residency_required",
                "The job requires current residence in Germany.",
                candidate_country=candidate_country,
            ))
    elif residency_requirement == "eu_eea":
        if candidate_country == "unknown":
            review_reasons.append(make_requirement_issue(
                "candidate_residence_unknown",
                "Current EU/EEA residence is required but candidate residence is unknown.",
            ))
        elif candidate_country not in EU_EEA_COUNTRIES:
            hard_failures.append(make_requirement_issue(
                "eu_eea_residency_required",
                "The job requires current residence in the EU/EEA.",
                candidate_country=candidate_country,
            ))
    elif residency_requirement == "specific_location":
        if candidate_country == "unknown":
            review_reasons.append(make_requirement_issue(
                "candidate_residence_unknown",
                "A specific residence location is required but candidate residence is unknown.",
                required_locations=residency_locations,
            ))
        elif residency_locations and candidate_country not in residency_locations:
            if "germany" in residency_locations or any(
                location in EU_EEA_COUNTRIES for location in residency_locations
            ):
                hard_failures.append(make_requirement_issue(
                    "specific_residency_requirement_not_met",
                    "The candidate does not meet the stated residence requirement.",
                    required_locations=residency_locations,
                    candidate_country=candidate_country,
                ))
            else:
                review_reasons.append(make_requirement_issue(
                    "specific_residency_requires_manual_review",
                    "A city or region-specific residence requirement needs manual review.",
                    required_locations=residency_locations,
                    candidate_country=candidate_country,
                ))

    minimum_years = hard_requirements.get("minimum_years_experience")
    if minimum_years is not None:
        try:
            minimum_years = float(minimum_years)
        except (TypeError, ValueError):
            minimum_years = None

    if minimum_years is not None:
        candidate_years = get_profile_years_of_experience(
            profile, profile_analysis
        )
        if candidate_years is None:
            review_reasons.append(make_requirement_issue(
                "candidate_experience_unknown",
                "The job has a minimum experience requirement, but candidate experience is unknown.",
                minimum_years=minimum_years,
            ))
        elif candidate_years < minimum_years:
            hard_failures.append(make_requirement_issue(
                "minimum_experience_not_met",
                (
                    f"Candidate has {candidate_years:g} years of experience; "
                    f"the job requires at least {minimum_years:g}."
                ),
                candidate_years=candidate_years,
                minimum_years=minimum_years,
            ))

    return hard_failures, review_reasons


def calculate_nonblocking_match_warnings(
    profile,
    profile_analysis,
    job_analysis,
    employment_type_score,
):
    warnings = []

    if (
        get_profile_sponsorship_status(profile, profile_analysis) == "yes"
        and normalize_text(job_analysis.visa_sponsorship) == "unknown"
    ):
        warnings.append(
            "Candidate needs visa sponsorship, but the listing does not state whether sponsorship is available."
        )

    if employment_type_score <= 25:
        warnings.append(
            "Employment type does not match the candidate's stated preference."
        )

    return warnings


def calculate_dealbreaker_warnings(
    job_analysis,
    hard_failures,
    review_reasons,
    nonblocking_warnings=None,
):
    warnings = [issue["message"] for issue in hard_failures + review_reasons]
    warnings.extend(nonblocking_warnings or [])
    warnings.extend(get_job_dealbreaker_notes(job_analysis))
    return list(dict.fromkeys(warnings))


def recommendation_from_score(score):
    if score >= 85:
        return "strong_apply"
    if score >= 72:
        return "apply"
    if score >= 58:
        return "maybe"
    return "weak_match"


def calculate_backend_match(profile, profile_analysis, job, job_analysis):
    profile_json = get_profile_analysis_json(profile_analysis)

    role_score = calculate_role_score(profile_json, profile_analysis, job_analysis)
    skills_score = calculate_skills_score(profile_analysis, job_analysis)
    seniority_score = calculate_seniority_score(profile_analysis, job_analysis)
    experience_score = calculate_experience_score(
        profile, profile_analysis, job_analysis
    )
    language_score = calculate_language_score(profile_analysis, job_analysis)
    visa_score = calculate_visa_score(profile, profile_analysis, job_analysis)
    work_type_score = calculate_work_type_score(profile, job_analysis)
    employment_type_score = calculate_employment_type_score(profile, job_analysis)
    location_score = calculate_location_score(profile, job)
    excluded_role_matches = calculate_excluded_role_matches(
        profile, profile_analysis, job, job_analysis
    )

    base_score = round(
        role_score * 0.30
        + skills_score * 0.40
        + experience_score * 0.15
        + seniority_score * 0.15
    )

    practical_score = round(
        location_score * 0.20
        + work_type_score * 0.15
        + employment_type_score * 0.15
        + language_score * 0.20
        + visa_score * 0.30
    )

    overall_score = round(
        base_score * 0.70
        + practical_score * 0.30
    )

    hard_failures, review_reasons = evaluate_hard_requirements(
        profile, profile_analysis, job_analysis
    )

    for excluded_role in excluded_role_matches:
        hard_failures.append(make_requirement_issue(
            "excluded_role_match",
            f"Job matches an excluded role preference: {excluded_role}.",
            excluded_role=excluded_role,
        ))

    job_level = normalize_text(job_analysis.seniority_level)

    if role_score == 0:
        overall_score = min(overall_score, 45)

    if role_score < 25 and skills_score < 40:
        overall_score = min(overall_score, 45)

    if role_score < 40 and skills_score < 30:
        overall_score = min(overall_score, 50)

    if job_level in ["senior", "lead", "executive"]:
        overall_score = min(overall_score, 55)

    if job_level in ["lead", "executive"]:
        overall_score = min(overall_score, 45)

    if hard_failures:
        eligibility_status = "ineligible"
        overall_score = min(overall_score, 20)
        recommendation = "ineligible"
    elif review_reasons:
        eligibility_status = "review_required"
        overall_score = min(overall_score, 57)
        recommendation = "manual_review"
    else:
        eligibility_status = "eligible"
        recommendation = recommendation_from_score(overall_score)

    nonblocking_warnings = calculate_nonblocking_match_warnings(
        profile,
        profile_analysis,
        job_analysis,
        employment_type_score,
    )
    warnings = calculate_dealbreaker_warnings(
        job_analysis,
        hard_failures,
        review_reasons,
        nonblocking_warnings,
    )

    strengths = []
    weaknesses = []

    if role_score >= 75:
        strengths.append("Role is close to the candidate target role.")
    else:
        weaknesses.append("Role is not very close to the candidate target role.")

    if skills_score >= 70:
        strengths.append("Candidate skills match many job requirements.")
    else:
        weaknesses.append("Required skills do not strongly overlap with the candidate profile.")

    if experience_score >= 80:
        strengths.append("Candidate experience meets the structured minimum requirement.")
    elif experience_score <= 40:
        weaknesses.append("Minimum experience requirement may not be met.")

    if visa_score <= 45:
        weaknesses.append("Visa or work authorization may be a problem.")

    if language_score <= 55:
        weaknesses.append("Language requirements may be a problem.")

    if employment_type_score <= 25:
        weaknesses.append("Employment type does not match the candidate preference.")

    if excluded_role_matches:
        weaknesses.append("Job falls under a role the candidate excluded.")

    if eligibility_status == "ineligible":
        summary = (
            f"Candidate is ineligible due to {len(hard_failures)} confirmed hard requirement issue(s)."
        )
    elif eligibility_status == "review_required":
        summary = (
            f"Manual review is required for {len(review_reasons)} unresolved hard requirement(s). "
            f"Backend score before review cap is represented by the component scores."
        )
    else:
        summary = f"Candidate is eligible. Backend rule-based score is {overall_score}."

    return {
        "match_version": MATCH_VERSION,
        "eligibility_status": eligibility_status,
        "eligible": (
            True if eligibility_status == "eligible"
            else False if eligibility_status == "ineligible"
            else None
        ),
        "hard_fail_reasons": hard_failures,
        "review_reasons": review_reasons,
        "role_fit_score": role_score,
        "skills_fit_score": skills_score,
        "experience_fit_score": experience_score,
        "seniority_fit_score": seniority_score,
        "location_fit_score": location_score,
        "work_type_fit_score": work_type_score,
        "employment_type_fit_score": employment_type_score,
        "language_fit_score": language_score,
        "visa_fit_score": visa_score,
        "base_match_score": base_score,
        "practical_match_score": practical_score,
        "overall_score": overall_score,
        "recommendation": recommendation,
        "summary": summary,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "dealbreaker_warnings": warnings,
        "excluded_role_matches": excluded_role_matches,
        "scoring_notes": [
            "Hard requirements are evaluated before the final recommendation.",
            "Confirmed hard failures cap the score at 20 and return ineligible.",
            "Unknown candidate facts for explicit hard requirements return manual_review and cap the score at 57.",
            "Unknown visa sponsorship policy is a non-blocking warning, not manual_review.",
            "Employment-type preference mismatches reduce the score but are not hard failures.",
        ],
    }


@router.post("/{user_id}/profiles/{profile_id}/jobs/{job_id}/match")
def match_profile_with_job(
    user_id: int,
    profile_id: int,
    job_id: int,
    force_rematch: bool = Query(default=False),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(
        models.User.user_id == user_id
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_USER_NOT_FOUND",
                "message": f"User with id {user_id} was not found."
            }
        )

    profile = db.query(models.CandidateProfile).filter(
        models.CandidateProfile.profile_id == profile_id,
        models.CandidateProfile.user_id == user_id
    ).first()

    if not profile:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_PROFILE_NOT_FOUND",
                "message": f"Profile with id {profile_id} was not found for user {user_id}."
            }
        )

    job = db.query(models.Job).filter(
        models.Job.job_id == job_id
    ).first()

    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_JOB_NOT_FOUND",
                "message": f"Job with id {job_id} was not found."
            }
        )

    profile_analysis = db.query(models.ProfileAnalysis).filter(
        models.ProfileAnalysis.profile_id == profile_id,
        models.ProfileAnalysis.analysis_status == "completed",
        models.ProfileAnalysis.analysis_model == REQUIRED_PROFILE_ANALYSIS_MODEL,
        models.ProfileAnalysis.analysis_prompt_version == REQUIRED_PROFILE_ANALYSIS_PROMPT_VERSION,
        models.ProfileAnalysis.is_current == True
    ).order_by(
        models.ProfileAnalysis.analysis_id.desc()
    ).first()

    if not profile_analysis:
        latest_analysis = db.query(models.ProfileAnalysis).filter(
            models.ProfileAnalysis.profile_id == profile_id,
            models.ProfileAnalysis.analysis_status == "completed"
        ).order_by(
            models.ProfileAnalysis.analysis_id.desc()
        ).first()

        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING",
                "message": (
                    "Profile must be analyzed with the latest profile analysis "
                    "version before matching."
                ),
                "profile_id": profile_id,
                "required_analysis_model": REQUIRED_PROFILE_ANALYSIS_MODEL,
                "required_analysis_prompt_version": REQUIRED_PROFILE_ANALYSIS_PROMPT_VERSION,
                "latest_existing_analysis_id": (
                    latest_analysis.analysis_id if latest_analysis else None
                ),
                "latest_existing_analysis_model": (
                    latest_analysis.analysis_model if latest_analysis else None
                ),
                "latest_existing_analysis_prompt_version": (
                    latest_analysis.analysis_prompt_version if latest_analysis else None
                ),
                "hint": (
                    f"Run POST /users/{user_id}/profiles/{profile_id}/analyze"
                    "?force_reanalyze=true and then try matching again."
                )
            }
        )

    job_analysis = db.query(models.JobAnalysis).filter(
        models.JobAnalysis.job_id == job_id,
        models.JobAnalysis.analysis_status == "completed",
        models.JobAnalysis.analysis_model == REQUIRED_JOB_ANALYSIS_MODEL,
        models.JobAnalysis.analysis_prompt_version == REQUIRED_JOB_ANALYSIS_PROMPT_VERSION,
        models.JobAnalysis.is_current == True,
        models.JobAnalysis.role_tags_json.isnot(None),
        models.JobAnalysis.role_tags_json != "[]"
    ).order_by(
        models.JobAnalysis.analysis_id.desc()
    ).first()

    if not job_analysis:
        latest_analysis = db.query(models.JobAnalysis).filter(
            models.JobAnalysis.job_id == job_id,
            models.JobAnalysis.analysis_status == "completed"
        ).order_by(
            models.JobAnalysis.analysis_id.desc()
        ).first()

        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_JOB_ANALYSIS_OUTDATED_OR_MISSING",
                "message": (
                    "Job must be analyzed with the latest job analysis version "
                    "before matching."
                ),
                "job_id": job_id,
                "required_analysis_model": REQUIRED_JOB_ANALYSIS_MODEL,
                "required_analysis_prompt_version": REQUIRED_JOB_ANALYSIS_PROMPT_VERSION,
                "latest_existing_analysis_id": (
                    latest_analysis.analysis_id if latest_analysis else None
                ),
                "latest_existing_analysis_model": (
                    latest_analysis.analysis_model if latest_analysis else None
                ),
                "latest_existing_analysis_prompt_version": (
                    latest_analysis.analysis_prompt_version if latest_analysis else None
                ),
                "hint": (
                    f"Run POST /jobs/{job_id}/analyze?force_reanalyze=true "
                    "and then try matching again."
                )
            }
        )

    existing_match = db.query(models.JobMatch).filter(
        models.JobMatch.profile_id == profile_id,
        models.JobMatch.job_id == job_id,
        models.JobMatch.profile_analysis_id == profile_analysis.analysis_id,
        models.JobMatch.job_analysis_id == job_analysis.analysis_id,
        models.JobMatch.match_model == MATCH_MODEL,
        models.JobMatch.match_prompt_version == MATCH_PROMPT_VERSION,
        models.JobMatch.match_status == "completed",
        models.JobMatch.is_current == True
    ).first()

    if existing_match and not force_rematch:
        return {
            "status": "cached",
            "message": "This profile and job already have a current match for this model and prompt version.",
            "match_id": existing_match.match_id,
            "profile_id": profile_id,
            "job_id": job_id,
            "match_version": MATCH_VERSION,
            "overall_score": existing_match.overall_score,
            "recommendation": existing_match.recommendation,
            "match": json.loads(existing_match.match_json)
            if existing_match.match_json else None
        }


    now = datetime.now(timezone.utc)

    try:
        match_result = calculate_backend_match(
            profile=profile,
            profile_analysis=profile_analysis,
            job=job,
            job_analysis=job_analysis
        )

        db.query(models.JobMatch).filter(
            models.JobMatch.profile_id == profile_id,
            models.JobMatch.job_id == job_id,
            models.JobMatch.is_current == True
        ).update({
            "is_current": False
        })

        new_match = models.JobMatch(
            profile_id=profile_id,
            job_id=job_id,
            profile_analysis_id=profile_analysis.analysis_id,
            job_analysis_id=job_analysis.analysis_id,

            match_status="completed",
            match_json=json.dumps(match_result, ensure_ascii=False),

            role_fit_score=match_result.get("role_fit_score"),
            skills_fit_score=match_result.get("skills_fit_score"),
            experience_fit_score=match_result.get("experience_fit_score"),
            seniority_fit_score=match_result.get("seniority_fit_score"),
            location_fit_score=match_result.get("location_fit_score"),
            work_type_fit_score=match_result.get("work_type_fit_score"),
            language_fit_score=match_result.get("language_fit_score"),
            visa_fit_score=match_result.get("visa_fit_score"),

            base_match_score=match_result.get("base_match_score"),
            practical_match_score=match_result.get("practical_match_score"),
            overall_score=match_result.get("overall_score"),

            recommendation=match_result.get("recommendation"),
            summary=match_result.get("summary"),

            strengths_json=json.dumps(
                match_result.get("strengths", []),
                ensure_ascii=False
            ),
            weaknesses_json=json.dumps(
                match_result.get("weaknesses", []),
                ensure_ascii=False
            ),
            dealbreaker_warnings_json=json.dumps(
                match_result.get("dealbreaker_warnings", []),
                ensure_ascii=False
            ),

            match_model=MATCH_MODEL,
            match_prompt_version=MATCH_PROMPT_VERSION,
            matched_at=now,
            match_error=None,
            is_current=True,
            created_at=now
        )

        db.add(new_match)
        db.commit()
        db.refresh(new_match)

        return {
            "status": "created",
            "match_id": new_match.match_id,
            "profile_id": profile_id,
            "job_id": job_id,
            "profile_analysis_id": profile_analysis.analysis_id,
            "job_analysis_id": job_analysis.analysis_id,
            "match_version": MATCH_VERSION,
            "eligibility_status": match_result.get("eligibility_status"),
            "overall_score": new_match.overall_score,
            "recommendation": new_match.recommendation,
            "match": match_result
        }

    except Exception as e:
        db.rollback()

        failed_match = models.JobMatch(
            profile_id=profile_id,
            job_id=job_id,
            profile_analysis_id=profile_analysis.analysis_id,
            job_analysis_id=job_analysis.analysis_id,
            match_status="failed",
            match_json=None,
            match_model=MATCH_MODEL,
            match_prompt_version=MATCH_PROMPT_VERSION,
            matched_at=now,
            match_error=str(e),
            is_current=False,
            created_at=now
        )

        db.add(failed_match)
        db.commit()

        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "ERR_BACKEND_MATCH_FAILED",
                "message": "Backend job match failed.",
                "profile_id": profile_id,
                "job_id": job_id,
                "error": str(e)
            }
        )


@router.post("/{user_id}/profiles/{profile_id}/jobs/match-analyzed")
def match_profile_with_analyzed_jobs(
    user_id: int,
    profile_id: int,
    limit: int = Query(default=20, ge=1, le=MAX_BATCH_MATCH_JOBS),
    offset: int = Query(default=0, ge=0),
    force_rematch: bool = Query(default=False),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(
        models.User.user_id == user_id
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_USER_NOT_FOUND",
                "message": f"User with id {user_id} was not found."
            }
        )

    profile = db.query(models.CandidateProfile).filter(
        models.CandidateProfile.profile_id == profile_id,
        models.CandidateProfile.user_id == user_id
    ).first()

    if not profile:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "ERR_PROFILE_NOT_FOUND",
                "message": f"Profile with id {profile_id} was not found for user {user_id}."
            }
        )

    profile_analysis = db.query(models.ProfileAnalysis).filter(
        models.ProfileAnalysis.profile_id == profile_id,
        models.ProfileAnalysis.analysis_status == "completed",
        models.ProfileAnalysis.analysis_model == REQUIRED_PROFILE_ANALYSIS_MODEL,
        models.ProfileAnalysis.analysis_prompt_version == REQUIRED_PROFILE_ANALYSIS_PROMPT_VERSION,
        models.ProfileAnalysis.is_current == True
    ).order_by(
        models.ProfileAnalysis.analysis_id.desc()
    ).first()

    if not profile_analysis:
        latest_analysis = db.query(models.ProfileAnalysis).filter(
            models.ProfileAnalysis.profile_id == profile_id,
            models.ProfileAnalysis.analysis_status == "completed"
        ).order_by(
            models.ProfileAnalysis.analysis_id.desc()
        ).first()

        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING",
                "message": (
                    "Profile must be analyzed with the latest profile analysis "
                    "version before matching."
                ),
                "profile_id": profile_id,
                "required_analysis_model": REQUIRED_PROFILE_ANALYSIS_MODEL,
                "required_analysis_prompt_version": REQUIRED_PROFILE_ANALYSIS_PROMPT_VERSION,
                "latest_existing_analysis_id": (
                    latest_analysis.analysis_id if latest_analysis else None
                ),
                "latest_existing_analysis_model": (
                    latest_analysis.analysis_model if latest_analysis else None
                ),
                "latest_existing_analysis_prompt_version": (
                    latest_analysis.analysis_prompt_version if latest_analysis else None
                ),
                "hint": (
                    f"Run POST /users/{user_id}/profiles/{profile_id}/analyze"
                    "?force_reanalyze=true and then try matching again."
                )
            }
        )

    analyzed_jobs = (
        db.query(models.Job)
        .join(
            models.JobAnalysis,
            models.Job.job_id == models.JobAnalysis.job_id
        )
        .filter(
            models.JobAnalysis.analysis_status == "completed",
            models.JobAnalysis.analysis_model == REQUIRED_JOB_ANALYSIS_MODEL,
            models.JobAnalysis.analysis_prompt_version == REQUIRED_JOB_ANALYSIS_PROMPT_VERSION,
            models.JobAnalysis.is_current == True,
            models.JobAnalysis.role_tags_json.isnot(None),
            models.JobAnalysis.role_tags_json != "[]"
        )
        .order_by(models.Job.job_id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    if not analyzed_jobs:
        return {
            "status": "no_analyzed_jobs_found",
            "user_id": user_id,
            "profile_id": profile_id,
            "requested_limit": limit,
            "offset": offset,
            "matched_count": 0,
            "failed_count": 0,
            "results": []
        }

    results = []

    for job in analyzed_jobs:
        try:
            result = match_profile_with_job(
                user_id=user_id,
                profile_id=profile_id,
                job_id=job.job_id,
                force_rematch=force_rematch,
                db=db
            )

            results.append({
                "job_id": job.job_id,
                "title": job.title,
                "company_name": job.company_name,
                "location": job.location,
                "url": job.url,
                "status": result.get("status"),
                "match_id": result.get("match_id"),
                "match_version": result.get("match_version") or MATCH_VERSION,
                "eligibility_status": (
                    result.get("match", {}).get("eligibility_status")
                    if result.get("match") else result.get("eligibility_status")
                ),
                "hard_fail_reason_codes": [
                    issue.get("code")
                    for issue in (result.get("match", {}).get("hard_fail_reasons", []) if result.get("match") else [])
                ],
                "overall_score": result.get("overall_score"),
                "recommendation": result.get("recommendation"),
                "summary": (
                    result.get("match", {}).get("summary")
                    if result.get("match") else None
                )
            })

        except HTTPException as e:
            db.rollback()

            results.append({
                "job_id": job.job_id,
                "title": job.title,
                "company_name": job.company_name,
                "status": "failed",
                "error": e.detail
            })

        except Exception as e:
            db.rollback()

            results.append({
                "job_id": job.job_id,
                "title": job.title,
                "company_name": job.company_name,
                "status": "failed",
                "error": str(e)
            })

    matched_count = len([
        result for result in results
        if result.get("status") in ["created", "cached"]
    ])

    failed_count = len([
        result for result in results
        if result.get("status") == "failed"
    ])

    sorted_results = sorted(
        results,
        key=lambda item: item.get("overall_score") or 0,
        reverse=True
    )

    return {
        "status": "completed",
        "user_id": user_id,
        "profile_id": profile_id,
        "requested_limit": limit,
        "offset": offset,
        "selected_job_count": len(analyzed_jobs),
        "matched_count": matched_count,
        "failed_count": failed_count,
        "max_allowed_limit": MAX_BATCH_MATCH_JOBS,
        "results": sorted_results
    }