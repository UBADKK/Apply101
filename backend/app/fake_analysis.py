"""Deterministic fake analysis provider for development and demos.

Used only when APPLY101_ANALYSIS_PROVIDER=fake is selected explicitly (see
openai_client.selected_analysis_provider); it is never a fallback for the
real provider. FakeAnalysisClient stands in for the OpenAI client at the
existing seams -- `client.with_options(...).responses.create(...)` in
routers/profiles.py and routers/jobs.py -- so the routes, guards, quotas,
schema validation, the job-analysis sanitizer, persistence and matching all
run unchanged on its output.

The output depends only on the candidate or job data section of the prompt,
never on the instructions or taxonomy lists before it:

- skill tags from taxonomy.SKILL_TAGS that literally occur in the data;
- for profiles, the fields the user entered: target role, visa sponsorship,
  work authorization, student status, years of experience, residence
  country, relocation preference and languages;
- for jobs, the title.

Everything else is "unknown", "other" or empty (a job's role tags are
["other"], which matching requires to be non-empty), so hard requirements
come only from the deterministic job-analysis sanitizer. Every summary says the
analysis is fake. No network, randomness or clock is involved.
"""
import json
import re

from .taxonomy import SKILL_TAGS


FAKE_ANALYSIS_SUMMARY = (
    "Fake demo analysis (APPLY101_ANALYSIS_PROVIDER=fake): built "
    "deterministically from the entered data, not by a language model."
)
FAKE_ANALYSIS_NOTE = "fake_analysis_provider"

# Where the user data starts in the prompts of routers/profiles.py and
# routers/jobs.py; everything before it is instructions and taxonomy lists.
PROFILE_DATA_MARKER = "User basic information:"
JOB_DATA_MARKER = "Job title:"
_JOB_SECTION_LABELS = ("Company:", "Location:", "Job description:")

_WORK_AUTHORIZATION_VALUES = frozenset({
    "germany", "eu_eea", "other_country_only", "none", "unknown",
})
_STUDENT_STATUS_VALUES = frozenset({
    "currently_enrolled", "not_enrolled", "unknown",
})
_VISA_SPONSORSHIP_VALUES = {"True": "yes", "False": "no"}

# Spellings beyond the tag itself; "_" in a tag also matches a space or "-".
_SKILL_ALIASES = {
    "nodejs": r"node\.?js",
    "postgresql": r"postgres(?:ql)?",
    "rest_api": r"rest(?:ful)?[ _-]?apis?",
    "openai_api": r"openai(?:[ _-]?api)?",
}
_SKILL_PATTERNS = [
    (
        tag,
        re.compile(
            r"(?<![a-z0-9])(?:"
            + _SKILL_ALIASES.get(tag, re.escape(tag).replace("_", "[ _-]?"))
            + r")(?![a-z0-9])",
            re.IGNORECASE,
        ),
    )
    for tag in SKILL_TAGS
    if tag != "other"
]

# A line of profiles.build_languages_text: "- {name} ({code}): {level} {scale}
# ({primary|not primary})". The level is free text, so the line is matched
# loosely and a language is never dropped (an unusable level becomes
# "unknown" in schema validation, which matching treats as review_required).
_LANGUAGE_LINE = re.compile(
    r"^- (?P<name>.+?) \([^)\n]*\): (?P<rest>.*?) ?\((?:not )?primary\)$",
    re.MULTILINE,
)


class FakeAnalysisUnsupported(RuntimeError):
    """The fake provider was asked for an output it does not produce."""


def _data_section(prompt: str, marker: str) -> str:
    # The first occurrence: the instructions never contain the marker, the
    # user data after it (CV, job description) may.
    index = prompt.find(marker)
    if index < 0:
        raise FakeAnalysisUnsupported(f"Prompt has no {marker!r} section.")
    return prompt[index + len(marker):]


def _line_value(text: str, label: str) -> str:
    match = re.search(rf"^{re.escape(label)}:[ \t]*(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _block(text: str, label: str, next_label: str | None) -> str:
    start = text.find(f"\n{label}:\n")
    if start < 0:
        return ""
    start += len(label) + 3
    end = text.find(f"\n{next_label}:\n", start) if next_label else -1
    return text[start:end] if end >= 0 else text[start:]


def _found_skills(text: str) -> list[str]:
    return [tag for tag, pattern in _SKILL_PATTERNS if pattern.search(text)]


def _years(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _language(match: re.Match) -> dict:
    # "{level} {scale}": the scale is the last word when there are two or
    # more, the level everything before it.
    words = match.group("rest").split()
    return {
        "language": match.group("name"),
        "level": " ".join(words[:-1]) if len(words) > 1 else (words[0] if words else "unknown"),
        "scale": words[-1] if len(words) > 1 else "unknown",
    }


def fake_profile_analysis(prompt: str) -> dict:
    """A ProfileAnalysisStructured payload for a routers/profiles.py prompt."""
    data = _data_section(prompt, PROFILE_DATA_MARKER)
    skill_text = "\n".join([
        _line_value(data, "Skills from user record"),
        _line_value(data, "Preferred technologies"),
        _block(data, "Self description", "CV text"),
        _block(data, "CV text", None),
    ])
    work_authorization = _line_value(data, "Work authorization status")
    student_status = _line_value(data, "Student status")
    target_role = _line_value(data, "Target role")
    languages = [
        _language(match)
        for match in _LANGUAGE_LINE.finditer(_block(data, "Languages", "Self description"))
    ]
    return {
        "candidate_summary": FAKE_ANALYSIS_SUMMARY,
        "current_role_family": "other",
        "target_role_families": [],
        "target_role_tags": [],
        "target_roles": [target_role] if target_role else [],
        "excluded_roles": [],
        "strong_skills": [],
        "moderate_skills": _found_skills(skill_text),
        "weak_or_basic_skills": [],
        "tools": [],
        "industries": [],
        "years_of_experience": _years(_line_value(data, "Years of experience")),
        "seniority_level": "unknown",
        "education_level": "unknown",
        "field_of_study": "unknown",
        "languages": languages,
        "visa_sponsorship_needed": _VISA_SPONSORSHIP_VALUES.get(
            _line_value(data, "Visa sponsorship needed"), "unknown"
        ),
        "work_authorization_status": (
            work_authorization if work_authorization in _WORK_AUTHORIZATION_VALUES else "unknown"
        ),
        "relocation_preference": _line_value(data, "Relocation preference") or "unknown",
        "current_residence_country": _line_value(data, "Current residence country") or "unknown",
        "student_status": (
            student_status if student_status in _STUDENT_STATUS_VALUES else "unknown"
        ),
        "match_notes": [FAKE_ANALYSIS_NOTE],
    }


def fake_job_analysis(prompt: str) -> dict:
    """A JobAnalysisStructured payload for a routers/jobs.py prompt."""
    data = _data_section(prompt, JOB_DATA_MARKER)
    title = data
    for label in _JOB_SECTION_LABELS:
        index = title.find(label)
        if index >= 0:
            title = title[:index]
    title = title.strip()
    description_index = data.find("Job description:")
    description = data[description_index:] if description_index >= 0 else ""
    return {
        "summary": FAKE_ANALYSIS_SUMMARY,
        "role_family": "other",
        "role_subfamily": "other",
        "normalized_role_title": title or "unknown",
        # Matching only accepts job analyses with at least one role tag; the
        # real prompt uses "other" when no allowed tag fits. Profiles keep
        # an empty list, so no role overlap is ever faked.
        "role_tags": ["other"],
        "required_skills": _found_skills(f"{title}\n{description}"),
        "preferred_skills": [],
        "responsibilities": [],
        "seniority_level": "unknown",
        "language_requirements": [],
        "visa_sponsorship": "unknown",
        "visa_sponsorship_evidence": None,
        "work_type": "unknown",
        "employment_type": "unknown",
        "hard_requirements": {
            "student_enrollment_required": False,
            "student_enrollment_evidence": None,
            "work_authorization": "unknown",
            "work_authorization_evidence": None,
            "residency": "unknown",
            "residency_locations": [],
            "residency_evidence": None,
            "minimum_years_experience": None,
            "minimum_years_experience_evidence": None,
        },
        "dealbreakers": [],
    }


LETTER_DATA_MARKER = "Cover letter data:"

_LETTER_TEMPLATES = {
    "en": {
        "greeting": "Dear hiring team,",
        "opening": "I am applying for the position of {title}{at_company}.",
        "role": "I am looking for a role as {role}.",
        "experience": "I have {years} years of professional experience.",
        "skills": "In my work I use {skills}.",
        "languages": "Languages: {languages}.",
        "achievement": "[Describe a concrete project or achievement that fits this job]",
        "company": "[Explain in your own words why you want to work for this company]",
        "closing": "I would be glad to talk about how I can contribute.",
        "signoff": "Kind regards,",
    },
    "de": {
        "greeting": "Sehr geehrte Damen und Herren,",
        "opening": "hiermit bewerbe ich mich auf die Stelle als {title}{at_company}.",
        "role": "Ich suche eine Position als {role}.",
        "experience": "Ich habe {years} Jahre Berufserfahrung.",
        "skills": "In meiner Arbeit nutze ich {skills}.",
        "languages": "Sprachen: {languages}.",
        "achievement": "[Beschreiben Sie ein konkretes Projekt oder einen Erfolg, der zu dieser Stelle passt]",
        "company": "[Erklären Sie mit eigenen Worten, warum Sie bei diesem Unternehmen arbeiten möchten]",
        "closing": "Über ein persönliches Gespräch freue ich mich.",
        "signoff": "Mit freundlichen Grüßen",
    },
    "tr": {
        "greeting": "Sayın işe alım ekibi,",
        "opening": "{title} pozisyonu için{at_company} başvuruyorum.",
        "role": "{role} olarak çalışabileceğim bir pozisyon arıyorum.",
        "experience": "{years} yıllık profesyonel deneyimim var.",
        "skills": "Çalışmalarımda {skills} kullanıyorum.",
        "languages": "Diller: {languages}.",
        "achievement": "[Bu işe uyan somut bir proje veya başarınızı yazın]",
        "company": "[Bu şirkette neden çalışmak istediğinizi kendi cümlelerinizle yazın]",
        "closing": "Katkı sağlayabileceğim konuları görüşmekten memnuniyet duyarım.",
        "signoff": "Saygılarımla,",
    },
}


def fake_cover_letter(prompt: str) -> dict:
    """A CoverLetterStructured payload for a backend/app/cover_letter.py
    prompt: a fixed template per language filled only with the given facts,
    with [placeholders] for everything else."""
    data = _data_section(prompt, LETTER_DATA_MARKER)
    language_match = re.search(r"^Language: (\w+)$", data, re.MULTILINE)
    language = language_match.group(1) if language_match else "en"
    template = _LETTER_TEMPLATES.get(language, _LETTER_TEMPLATES["en"])
    facts_start = data.find("Facts JSON:")
    gaps_start = data.find("Known gaps JSON:")
    try:
        facts = json.loads(data[facts_start + len("Facts JSON:"):gaps_start].strip()) if facts_start >= 0 else []
    except ValueError:
        facts = []
    by_id = {fact.get("id"): fact.get("text", "") for fact in facts if isinstance(fact, dict)}
    used = []

    def take(fact_id):
        value = by_id.get(fact_id)
        if value:
            used.append(fact_id)
        return value

    title = take("job.title") or "[job title]"
    company = take("company.name") or take("job.company")
    at_company = ""
    if company:
        at_company = {"en": f" at {company}", "de": f" bei {company}", "tr": f" {company} bünyesindeki"}.get(language, f" at {company}")
    lines = [template["greeting"], "", template["opening"].format(title=title, at_company=at_company)]
    role = take("profile.target_role")
    if role:
        lines.append(template["role"].format(role=role))
    years = take("profile.experience")
    if years:
        lines.append(template["experience"].format(years=years))
    skills = take("profile.skills")
    if skills:
        lines.append(template["skills"].format(skills=", ".join(skills.split(", ")[:6])))
    languages = take("profile.languages")
    if languages:
        lines.append(template["languages"].format(languages=languages))
    lines += ["", template["achievement"], template["company"], "", template["closing"], "", template["signoff"], "[Your name]"]
    return {
        "letter_text": "\n".join(lines),
        "used_fact_ids": used,
        "missing_information": [FAKE_ANALYSIS_NOTE + ": fake demo draft, not written by a language model."],
    }


_FAKE_PAYLOADS = {
    "profile_analysis": fake_profile_analysis,
    "job_analysis": fake_job_analysis,
    "cover_letter": fake_cover_letter,
}


class _FakeResponse:
    def __init__(self, output_text: str):
        self.output_text = output_text


class _FakeResponses:
    def create(self, *, input, text=None, **_ignored):
        schema_name = ((text or {}).get("format") or {}).get("name")
        build = _FAKE_PAYLOADS.get(schema_name)
        if build is None:
            raise FakeAnalysisUnsupported(
                "The fake analysis provider only produces the structured "
                "profile_analysis, job_analysis and cover_letter outputs."
            )
        return _FakeResponse(json.dumps(build(input), ensure_ascii=False))


class FakeAnalysisClient:
    """The subset of the OpenAI client the analysis seams use."""

    def __init__(self):
        self.responses = _FakeResponses()

    def with_options(self, **_ignored):
        return self
