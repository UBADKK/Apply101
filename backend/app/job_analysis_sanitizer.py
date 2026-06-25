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

LANGUAGE_LEVEL_ORDER = {
    "unknown": 0,
    "a1": 1,
    "a2": 2,
    "b1": 3,
    "b2": 4,
    "c1": 5,
    "c2": 6,
    "native": 7,
}

EXPLICIT_LANGUAGE_REQUIREMENT_MARKERS = re.compile(
    r"\b(?:minimum|min\.?|mindestens|at\s+least|required|mandatory|"
    r"erforderlich|or\s+higher|oder\s+h[oö]her)\b",
    flags=re.IGNORECASE,
)

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

NON_STUDENT_EMPLOYMENT_ALTERNATIVE_PATTERNS = [
    (
        "part_time",
        r"\bwerkstudent(?:in)?\b.{0,80}?\boder\b.{0,40}?\b(?:in\s+)?teilzeit\b",
    ),
    (
        "part_time",
        r"\b(?:in\s+)?teilzeit\b.{0,80}?\boder\b.{0,40}?\bwerkstudent(?:in)?\b",
    ),
    (
        "full_time",
        r"\bwerkstudent(?:in)?\b.{0,80}?\boder\b.{0,40}?\b(?:in\s+)?vollzeit\b",
    ),
    (
        "full_time",
        r"\b(?:in\s+)?vollzeit\b.{0,80}?\boder\b.{0,40}?\bwerkstudent(?:in)?\b",
    ),
    (
        "part_time",
        r"\bworking[ -]student\b.{0,80}?\bor\b.{0,40}?\bpart[ -]?time\b",
    ),
    (
        "part_time",
        r"\bpart[ -]?time\b.{0,80}?\bor\b.{0,40}?\bworking[ -]student\b",
    ),
    (
        "full_time",
        r"\bworking[ -]student\b.{0,80}?\bor\b.{0,40}?\bfull[ -]?time\b",
    ),
    (
        "full_time",
        r"\bfull[ -]?time\b.{0,80}?\bor\b.{0,40}?\bworking[ -]student\b",
    ),
]

NUMBER_WORD_VALUES = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "ein": 1,
    "eins": 1,
    "eine": 1,
    "einen": 1,
    "einem": 1,
    "einer": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "fuenf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
}

_NUMBER_WORD_PATTERN = "|".join(
    sorted((re.escape(value) for value in NUMBER_WORD_VALUES), key=len, reverse=True)
)
_NUMBER_TOKEN_PATTERN = rf"(?:\d+(?:[.,]\d+)?|{_NUMBER_WORD_PATTERN})"

MINIMUM_EXPERIENCE_PATTERNS = [
    rf"(?:mind(?:estens)?\.?|minimum(?:\s+of)?|at\s+least)\s*"
    rf"(?P<number>{_NUMBER_TOKEN_PATTERN})"
    rf"(?:\s*(?:[-–—]|to|bis)\s*{_NUMBER_TOKEN_PATTERN})?\s*"
    rf"(?:jahre|jahren|years?)",
    rf"(?P<number>{_NUMBER_TOKEN_PATTERN})\s*\+\s*(?:jahre|jahren|years?)",
]

VISA_SPONSORSHIP_EVIDENCE_TERMS = {
    "visa sponsorship",
    "sponsorship",
    "sponsor visa",
    "visa support",
    "visumssponsoring",
    "visumsponsoring",
}

NO_VISA_SPONSORSHIP_PATTERNS = [
    r"\bwe are not sponsoring (?:working )?visas?\b",
    r"\bwe do not offer visa sponsorship\b",
    r"\bvisa sponsorship is not available\b",
    r"\bno visa sponsorship\b",
    r"\bwe cannot sponsor visas?\b",
    r"\bwe don't sponsor visas?\b",
    r"\bunable to support.{0,80}?visa sponsorship\b",
]


def find_exact_pattern_excerpt(
    source_text: str | None,
    patterns: list[str],
) -> str | None:
    text = str(source_text or "")
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(0).strip()
    return None


def infer_no_visa_sponsorship(
    source_text: str | None,
) -> str | None:
    return find_exact_pattern_excerpt(
        source_text,
        NO_VISA_SPONSORSHIP_PATTERNS,
    )


def infer_non_student_employment_alternative(
    source_text: str | None,
) -> tuple[str | None, str | None]:
    text = str(source_text or "")
    for employment_type, pattern in NON_STUDENT_EMPLOYMENT_ALTERNATIVE_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return employment_type, match.group(0).strip()
    return None, None


def parse_year_value(raw_value: str | None) -> float | None:
    normalized = str(raw_value or "").strip().casefold()
    if normalized in NUMBER_WORD_VALUES:
        return float(NUMBER_WORD_VALUES[normalized])
    try:
        return float(normalized.replace(",", "."))
    except ValueError:
        return None


def infer_minimum_experience(
    source_text: str | None,
) -> tuple[float | None, str | None]:
    text = str(source_text or "")
    for pattern in MINIMUM_EXPERIENCE_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        years = parse_year_value(match.group("number"))
        if years is not None:
            return years, match.group(0).strip()
    return None, None


def _language_alias_pattern(aliases: set[str]) -> re.Pattern[str]:
    alternatives = "|".join(
        sorted((re.escape(alias) for alias in aliases), key=len, reverse=True)
    )
    return re.compile(rf"\b(?:{alternatives})\b", flags=re.IGNORECASE)


def infer_explicit_language_requirements(
    source_text: str | None,
) -> list[dict]:
    """Infer explicit CEFR minimums such as 'German skills (B2 minimum)'."""
    text = str(source_text or "")
    alias_occurrences: list[tuple[str, re.Match[str]]] = []

    for language, aliases in LANGUAGE_EVIDENCE_ALIASES.items():
        alias_pattern = _language_alias_pattern(aliases)
        alias_occurrences.extend(
            (language, match) for match in alias_pattern.finditer(text)
        )

    best_by_language: dict[str, dict] = {}
    for level_match in re.finditer(
        r"\b(a1|a2|b1|b2|c1|c2)\b",
        text,
        flags=re.IGNORECASE,
    ):
        nearest: tuple[int, str, re.Match[str]] | None = None
        for language, alias_match in alias_occurrences:
            if alias_match.end() <= level_match.start():
                distance = level_match.start() - alias_match.end()
            elif level_match.end() <= alias_match.start():
                distance = alias_match.start() - level_match.end()
            else:
                distance = 0

            if distance > 100:
                continue
            if nearest is None or distance < nearest[0]:
                nearest = (distance, language, alias_match)

        if nearest is None:
            continue

        _, language, alias_match = nearest
        pair_start = min(alias_match.start(), level_match.start())
        pair_end = max(alias_match.end(), level_match.end())
        context_start = max(0, pair_start - 45)
        context_end = min(len(text), pair_end + 45)
        context = text[context_start:context_end]
        marker_match = EXPLICIT_LANGUAGE_REQUIREMENT_MARKERS.search(context)
        if not marker_match:
            continue

        marker_start = context_start + marker_match.start()
        marker_end = context_start + marker_match.end()
        evidence_start = min(pair_start, marker_start)
        evidence_end = max(pair_end, marker_end)
        evidence = text[evidence_start:evidence_end].strip(
            " \t\r\n-–—•,.;:()[]"
        )
        level = level_match.group(1).casefold()
        candidate = {
            "language": language,
            "minimum_level": level,
            "required": True,
            "evidence": evidence,
        }

        current = best_by_language.get(language)
        if current is None or (
            LANGUAGE_LEVEL_ORDER[level]
            > LANGUAGE_LEVEL_ORDER[current["minimum_level"]]
        ):
            best_by_language[language] = candidate

    return list(best_by_language.values())


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
    inferred_years, _ = infer_minimum_experience(evidence)
    return inferred_years is not None


def merge_language_requirements(
    supported: list[dict],
    inferred: list[dict],
) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []

    for requirement in [*supported, *inferred]:
        language = normalize_evidence_text(requirement.get("language"))
        if not language:
            continue
        if language not in merged:
            order.append(language)
            merged[language] = requirement
            continue

        current_level = normalize_evidence_text(
            merged[language].get("minimum_level", "unknown")
        )
        new_level = normalize_evidence_text(
            requirement.get("minimum_level", "unknown")
        )
        if LANGUAGE_LEVEL_ORDER.get(new_level, 0) > LANGUAGE_LEVEL_ORDER.get(
            current_level,
            0,
        ):
            merged[language] = requirement

    return [merged[language] for language in order]


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
    """Keep or infer hard requirements only when supported by job text."""
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

    inferred_languages = infer_explicit_language_requirements(source_text)
    analysis["language_requirements"] = merge_language_requirements(
        supported_languages,
        inferred_languages,
    )

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

    explicit_no_sponsorship = infer_no_visa_sponsorship(source_text)
    if explicit_no_sponsorship:
        analysis["visa_sponsorship"] = "no"
        analysis["visa_sponsorship_evidence"] = explicit_no_sponsorship

    hard_requirements = analysis.get("hard_requirements", {})

    explicit_student_role = find_exact_pattern_excerpt(
        source_text,
        STUDENT_ROLE_PATTERNS,
    )
    alternative_employment_type, _ = infer_non_student_employment_alternative(
        source_text
    )
    employment_type = normalize_evidence_text(analysis.get("employment_type"))

    if alternative_employment_type:
        hard_requirements["student_enrollment_required"] = False
        hard_requirements["student_enrollment_evidence"] = None
        if employment_type == "working_student":
            analysis["employment_type"] = alternative_employment_type
    elif employment_type == "working_student" and explicit_student_role:
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
