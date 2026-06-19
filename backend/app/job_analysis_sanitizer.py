"""Evidence-backed normalization for structured job hard requirements."""

import re

LANGUAGE_EVIDENCE_ALIASES = {
    "german": {"german", "deutsch", "deutschkenntnisse"},
    "english": {"english", "englisch", "englischkenntnisse"},
    "french": {"french", "französisch", "franzoesisch"},
    "spanish": {"spanish", "spanisch"},
    "italian": {"italian", "italienisch"},
    "dutch": {"dutch", "niederländisch", "niederlaendisch"},
    "polish": {"polish", "polnisch"},
    "turkish": {"turkish", "türkisch", "tuerkisch"},
}

WORK_AUTHORIZATION_EVIDENCE_TERMS = {
    "work authorization",
    "right to work",
    "work permit",
    "visa sponsorship",
    "sponsorship",
    "arbeitserlaubnis",
    "arbeitsgenehmigung",
    "arbeitsberechtigung",
    "aufenthaltstitel",
    "eu citizenship",
    "eu citizen",
    "eu staatsbürgerschaft",
    "eu staatsbuergerschaft",
}

RESIDENCY_EVIDENCE_TERMS = {
    "must reside",
    "currently reside",
    "reside in",
    "must live",
    "current residence",
    "resident in",
    "residence in",
    "living in",
    "wohnhaft",
    "wohnsitz",
    "wohnort",
    "ansässig",
    "ansaessig",
}

STUDENT_EVIDENCE_TERMS = {
    "werkstudent",
    "working student",
    "enrolled",
    "enrollment",
    "student status",
    "currently studying",
    "immatrikuliert",
    "immatrikulation",
    "eingeschriebener student",
    "eingeschriebene studentin",
}



STUDENT_ROLE_PATTERNS = [
    r"\bwerkstudent(?:in)?\b",
    r"\bworking[ -]student\b",
    r"\bstudentische(?:r|n|s)?\s+(?:mitarbeiter(?:in)?|aushilfe)\b",
]

MINIMUM_EXPERIENCE_PATTERNS = [
    r"(?:mind(?:estens)?\.?|minimum|at least)\s*(\d+(?:[.,]\d+)?)\s*(?:[-–—]\s*\d+(?:[.,]\d+)?)?\s*(?:jahre|jahren|years?)",
    r"(\d+(?:[.,]\d+)?)\s*\+\s*(?:jahre|jahren|years?)",
]

VISA_SPONSORSHIP_EVIDENCE_TERMS = {
    "visa sponsorship",
    "sponsorship",
    "sponsor visa",
    "visa support",
    "visumssponsoring",
    "visumsponsoring",
}




def find_exact_pattern_excerpt(
    source_text: str | None,
    patterns: list[str],
) -> str | None:
    text = str(source_text or "")
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return None


def infer_minimum_experience(
    source_text: str | None,
) -> tuple[float | None, str | None]:
    text = str(source_text or "")
    for pattern in MINIMUM_EXPERIENCE_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            years = float(match.group(1).replace(",", "."))
        except (TypeError, ValueError):
            continue
        return years, match.group(0).strip()
    return None, None


def normalize_evidence_text(value: str | None) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n\"'`.,;:()[]{}")


def evidence_is_exact_excerpt(evidence: str | None, source_text: str | None) -> bool:
    normalized_evidence = normalize_evidence_text(evidence)
    normalized_source = normalize_evidence_text(source_text)
    return bool(
        len(normalized_evidence) >= 4
        and normalized_evidence in normalized_source
    )


def evidence_contains_any(evidence: str | None, terms: set[str]) -> bool:
    normalized_evidence = normalize_evidence_text(evidence)
    return any(term.casefold() in normalized_evidence for term in terms)


def language_evidence_is_supported(
    language: str | None,
    evidence: str | None,
    source_text: str | None,
) -> bool:
    if not evidence_is_exact_excerpt(evidence, source_text):
        return False

    normalized_language = normalize_evidence_text(language)
    aliases = LANGUAGE_EVIDENCE_ALIASES.get(
        normalized_language,
        {normalized_language} if normalized_language else set(),
    )
    return bool(aliases) and evidence_contains_any(evidence, aliases)


def experience_evidence_is_supported(
    evidence: str | None,
    source_text: str | None,
) -> bool:
    if not evidence_is_exact_excerpt(evidence, source_text):
        return False

    normalized_evidence = normalize_evidence_text(evidence)
    has_number = bool(re.search(r"\b\d+(?:[.,]\d+)?\b", normalized_evidence))
    has_year_term = any(term in normalized_evidence for term in {
        "year", "years", "jahr", "jahre", "jahren",
    })
    return has_number and has_year_term


def build_structured_dealbreaker_notes(analysis: dict) -> list[str]:
    notes: list[str] = []

    for requirement in analysis.get("language_requirements", []):
        if not requirement.get("required"):
            continue
        language = requirement.get("language", "unknown")
        level = requirement.get("minimum_level", "unknown")
        notes.append(f"Required language: {language} (minimum {level}).")

    visa_sponsorship = analysis.get("visa_sponsorship", "unknown")
    if visa_sponsorship == "no":
        notes.append("Visa sponsorship is explicitly unavailable.")
    elif visa_sponsorship == "yes":
        notes.append("Visa sponsorship is explicitly available.")

    hard_requirements = analysis.get("hard_requirements", {})
    if hard_requirements.get("student_enrollment_required"):
        notes.append("Active student enrollment is required.")

    work_authorization = hard_requirements.get("work_authorization", "none")
    if work_authorization not in {"none", "unknown"}:
        notes.append(f"Existing work authorization required: {work_authorization}.")

    residency = hard_requirements.get("residency", "none")
    if residency not in {"none", "unknown"}:
        locations = hard_requirements.get("residency_locations", [])
        location_text = ", ".join(locations) if locations else residency
        notes.append(f"Current residence required: {location_text}.")

    minimum_years = hard_requirements.get("minimum_years_experience")
    if minimum_years is not None:
        notes.append(f"Minimum experience required: {minimum_years:g} years.")

    return notes


def sanitize_job_analysis_requirements(
    analysis: dict,
    source_text: str | None,
) -> dict:
    """Keep hard requirements only when supported by an exact job-text excerpt."""
    supported_languages = []
    for requirement in analysis.get("language_requirements", []):
        if not requirement.get("required"):
            continue
        if language_evidence_is_supported(
            requirement.get("language"),
            requirement.get("evidence"),
            source_text,
        ):
            supported_languages.append(requirement)
    analysis["language_requirements"] = supported_languages

    sponsorship = analysis.get("visa_sponsorship", "unknown")
    sponsorship_evidence = analysis.get("visa_sponsorship_evidence")
    if sponsorship in {"yes", "no"} and not (
        evidence_is_exact_excerpt(sponsorship_evidence, source_text)
        and evidence_contains_any(
            sponsorship_evidence,
            VISA_SPONSORSHIP_EVIDENCE_TERMS,
        )
    ):
        analysis["visa_sponsorship"] = "unknown"
        analysis["visa_sponsorship_evidence"] = None

    hard_requirements = analysis.get("hard_requirements", {})

    explicit_student_role = find_exact_pattern_excerpt(
        source_text,
        STUDENT_ROLE_PATTERNS,
    )
    employment_type = normalize_evidence_text(analysis.get("employment_type"))

    if employment_type == "working_student" and explicit_student_role:
        hard_requirements["student_enrollment_required"] = True
        hard_requirements["student_enrollment_evidence"] = explicit_student_role
    elif hard_requirements.get("student_enrollment_required") and not (
        evidence_is_exact_excerpt(
            hard_requirements.get("student_enrollment_evidence"),
            source_text,
        )
        and evidence_contains_any(
            hard_requirements.get("student_enrollment_evidence"),
            STUDENT_EVIDENCE_TERMS,
        )
    ):
        hard_requirements["student_enrollment_required"] = False
        hard_requirements["student_enrollment_evidence"] = None

    if hard_requirements.get("work_authorization") not in {"none", "unknown"} and not (
        evidence_is_exact_excerpt(
            hard_requirements.get("work_authorization_evidence"),
            source_text,
        )
        and evidence_contains_any(
            hard_requirements.get("work_authorization_evidence"),
            WORK_AUTHORIZATION_EVIDENCE_TERMS,
        )
    ):
        hard_requirements["work_authorization"] = "none"
        hard_requirements["work_authorization_evidence"] = None

    if hard_requirements.get("residency") not in {"none", "unknown"} and not (
        evidence_is_exact_excerpt(
            hard_requirements.get("residency_evidence"),
            source_text,
        )
        and evidence_contains_any(
            hard_requirements.get("residency_evidence"),
            RESIDENCY_EVIDENCE_TERMS,
        )
    ):
        hard_requirements["residency"] = "none"
        hard_requirements["residency_locations"] = []
        hard_requirements["residency_evidence"] = None

    if hard_requirements.get("minimum_years_experience") is not None and not (
        experience_evidence_is_supported(
            hard_requirements.get("minimum_years_experience_evidence"),
            source_text,
        )
    ):
        hard_requirements["minimum_years_experience"] = None
        hard_requirements["minimum_years_experience_evidence"] = None

    if hard_requirements.get("minimum_years_experience") is None:
        inferred_years, inferred_evidence = infer_minimum_experience(source_text)
        if inferred_years is not None and inferred_evidence:
            hard_requirements["minimum_years_experience"] = inferred_years
            hard_requirements["minimum_years_experience_evidence"] = inferred_evidence

    analysis["hard_requirements"] = hard_requirements
    analysis["dealbreakers"] = build_structured_dealbreaker_notes(analysis)
    return analysis


