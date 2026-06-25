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

SOFT_LANGUAGE_REQUIREMENT_MARKERS = re.compile(
    r"\b(?:preferred|preferably|nice\s+to\s+have|optional|bonus|"
    r"wünschenswert|wuenschenswert|von\s+vorteil|ideally|idealerweise)\b",
    flags=re.IGNORECASE,
)

TITLE_LANGUAGE_REQUIREMENT_PATTERNS = {
    "german": [
        r"\bgerman[ -]speaking\b",
        r"\bdeutschsprachig(?:e|er|es|en)?\b",
    ],
    "english": [
        r"\benglish[ -]speaking\b",
        r"\benglischsprachig(?:e|er|es|en)?\b",
    ],
    "french": [r"\bfrench[ -]speaking\b"],
    "spanish": [r"\bspanish[ -]speaking\b"],
    "italian": [r"\bitalian[ -]speaking\b"],
}

QUALITATIVE_LANGUAGE_REQUIREMENT_PATTERNS = [
    r"\bsehr\s+gute(?:n|r|s)?\b[^.\n;]{0,160}",
    r"\bvery\s+good\b[^.\n;]{0,160}",
]

ALTERNATIVE_LANGUAGE_MARKERS = [
    r"\bat\s+least\s+one\s+of\b",
    r"\bone\s+of\b",
    r"\bmindestens\s+eine(?:r|s)?\s+(?:der|von)\b",
]

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

PLAIN_EXPERIENCE_RANGE_PATTERNS = [
    rf"(?P<number>{_NUMBER_TOKEN_PATTERN})\s*(?:[-–—]|to|bis)\s*"
    rf"{_NUMBER_TOKEN_PATTERN}\s*(?:jahre|jahren|years?)",
]

SOFT_EXPERIENCE_PREFIX_MARKERS = re.compile(
    r"\b(?:ideally|ideal(?:erweise)?|preferably|preferred|nice\s+to\s+have|"
    r"wünschenswert|wuenschenswert|von\s+vorteil|optional|bonus)\b",
    flags=re.IGNORECASE,
)

SOFT_EXPERIENCE_SUFFIX_MARKERS = re.compile(
    r"^\s*(?:[,;:–—-]\s*)?(?:(?:would|should)\s+be\s+|"
    r"(?:is|are|wäre|waere|wären|waeren|ist|sind)\s+)?"
    r"(?:preferred|preferable|wünschenswert|wuenschenswert|optional|"
    r"von\s+vorteil|nice\s+to\s+have)\b",
    flags=re.IGNORECASE,
)

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


def experience_range_has_soft_context(
    source_text: str,
    match: re.Match[str],
) -> bool:
    """Reject ranges only when wording clearly makes the years optional."""
    context_start = max(0, match.start() - 100)
    context_end = min(len(source_text), match.end() + 100)
    context = source_text[context_start:context_end]

    # Keep the check local to the current sentence/bullet so a marker in a
    # neighboring requirement does not suppress a hard range.
    relative_start = match.start() - context_start
    relative_end = match.end() - context_start
    left_boundaries = [
        context.rfind(separator, 0, relative_start)
        for separator in ("\n", ".", ";", "•")
    ]
    right_boundaries = [
        context.find(separator, relative_end)
        for separator in ("\n", ".", ";", "•")
    ]
    sentence_start = max(left_boundaries) + 1
    valid_right_boundaries = [value for value in right_boundaries if value >= 0]
    sentence_end = min(valid_right_boundaries) if valid_right_boundaries else len(context)

    before = context[sentence_start:relative_start]
    after = context[relative_end:sentence_end]

    # Prefix markers directly frame the amount as optional, e.g.
    # "ideally 1-2 years" or "nice to have: 3-5 years".
    if SOFT_EXPERIENCE_PREFIX_MARKERS.search(before[-80:]):
        return True

    # Suffix markers must immediately describe the amount. This intentionally
    # does not reject "4-7 years in finance, ideally in SaaS", where "ideally"
    # qualifies the industry rather than the number of years.
    return SOFT_EXPERIENCE_SUFFIX_MARKERS.search(after[:60]) is not None


def infer_minimum_experience(
    source_text: str | None,
) -> tuple[float | None, str | None]:
    text = str(source_text or "")
    for pattern in MINIMUM_EXPERIENCE_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match or experience_range_has_soft_context(text, match):
            continue
        years = parse_year_value(match.group("number"))
        if years is not None:
            return years, match.group(0).strip()

    for pattern in PLAIN_EXPERIENCE_RANGE_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            if experience_range_has_soft_context(text, match):
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


def infer_title_language_requirements(
    source_text: str | None,
) -> list[dict]:
    """Treat explicit title labels such as 'German Speaking' as mandatory."""
    text = str(source_text or "")
    title = next((line.strip() for line in text.splitlines() if line.strip()), "")
    requirements = []

    for language, patterns in TITLE_LANGUAGE_REQUIREMENT_PATTERNS.items():
        evidence = find_exact_pattern_excerpt(title, patterns)
        if evidence:
            requirements.append({
                "language": language,
                "minimum_level": "unknown",
                "required": True,
                "evidence": evidence,
            })

    return requirements


def _context_has_soft_language_marker(
    source_text: str,
    match: re.Match[str],
) -> bool:
    start = max(0, match.start() - 70)
    end = min(len(source_text), match.end() + 70)
    return SOFT_LANGUAGE_REQUIREMENT_MARKERS.search(source_text[start:end]) is not None


def infer_qualitative_language_requirements(
    source_text: str | None,
) -> list[dict]:
    """Normalize explicit 'very good / sehr gute' language wording to C1."""
    text = str(source_text or "")
    inferred: dict[str, dict] = {}

    for pattern in QUALITATIVE_LANGUAGE_REQUIREMENT_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            if _context_has_soft_language_marker(text, match):
                continue
            evidence = match.group(0).strip(" \t\r\n-–—•,.;:()[]")
            for language, aliases in LANGUAGE_EVIDENCE_ALIASES.items():
                if evidence_contains_any(evidence, aliases):
                    inferred[language] = {
                        "language": language,
                        "minimum_level": "c1",
                        "required": True,
                        "evidence": evidence,
                    }

    return list(inferred.values())


def _sentence_around_match(text: str, match: re.Match[str]) -> str:
    left_candidates = [text.rfind(separator, 0, match.start()) for separator in ("\n", ".", ";", "•")]
    right_candidates = [text.find(separator, match.end()) for separator in ("\n", ".", ";", "•")]
    start = max(left_candidates) + 1
    valid_right = [value for value in right_candidates if value >= 0]
    end = min(valid_right) if valid_right else min(len(text), match.end() + 180)
    return text[start:end].strip(" \t\r\n-–—•,.;:()[]")


def _infer_language_level_from_evidence(evidence: str) -> str:
    explicit = re.search(r"\b(a1|a2|b1|b2|c1|c2)\b", evidence, flags=re.IGNORECASE)
    if explicit:
        return explicit.group(1).casefold()
    if re.search(r"\b(?:native(?:[- ]level)?|mother\s+tongue|muttersprachlich)\b", evidence, flags=re.IGNORECASE):
        return "native"
    if re.search(
        r"\b(?:fluent|business\s+fluent|professional\s+proficiency|"
        r"fließend|fliessend|verhandlungssicher|very\s+good|sehr\s+gute)\b",
        evidence,
        flags=re.IGNORECASE,
    ):
        return "c1"
    return "unknown"


def infer_alternative_language_groups(
    source_text: str | None,
) -> list[dict]:
    """Infer OR language groups such as 'at least one of German, French or Italian'."""
    text = str(source_text or "")
    groups = []
    group_number = 1
    seen_marker_positions: list[int] = []

    for pattern in ALTERNATIVE_LANGUAGE_MARKERS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            if any(abs(match.start() - position) <= 20 for position in seen_marker_positions):
                continue
            seen_marker_positions.append(match.start())
            evidence = _sentence_around_match(text, match)
            if _context_has_soft_language_marker(text, match):
                continue

            list_tail = text[match.end():match.end() + 180]
            qualifier_boundary = re.search(
                r",\s*(?:combined|together|plus|along\s+with|with)\b|[;\n.]",
                list_tail,
                flags=re.IGNORECASE,
            )
            language_list_text = (
                list_tail[:qualifier_boundary.start()]
                if qualifier_boundary
                else list_tail
            )

            languages = []
            for language, aliases in LANGUAGE_EVIDENCE_ALIASES.items():
                if evidence_contains_any(language_list_text, aliases):
                    languages.append(language)

            if len(languages) < 2:
                continue

            group_id = f"language_alternative_{group_number}"
            group_number += 1
            level = _infer_language_level_from_evidence(evidence)
            groups.append({
                "group_id": group_id,
                "languages": languages,
                "minimum_level": level,
                "minimum_required_count": 1,
                "evidence": evidence,
            })

    return groups


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


def replace_language_requirements(
    requirements: list[dict],
    authoritative: list[dict],
) -> list[dict]:
    """Replace a language requirement when deterministic evidence is stronger."""
    merged = {
        normalize_evidence_text(item.get("language")): item
        for item in requirements
        if normalize_evidence_text(item.get("language"))
    }
    order = [
        normalize_evidence_text(item.get("language"))
        for item in requirements
        if normalize_evidence_text(item.get("language"))
    ]

    for item in authoritative:
        language = normalize_evidence_text(item.get("language"))
        if not language:
            continue
        if language not in merged:
            order.append(language)
        merged[language] = item

    return [merged[language] for language in order]


def apply_alternative_language_groups(
    requirements: list[dict],
    groups: list[dict],
) -> list[dict]:
    by_language = {
        normalize_evidence_text(item.get("language")): dict(item)
        for item in requirements
        if normalize_evidence_text(item.get("language"))
    }
    order = list(by_language)

    for group in groups:
        for language in group["languages"]:
            requirement = by_language.get(language, {
                "language": language,
                "minimum_level": group["minimum_level"],
                "required": True,
                "evidence": group["evidence"],
            })
            if LANGUAGE_LEVEL_ORDER.get(group["minimum_level"], 0) > LANGUAGE_LEVEL_ORDER.get(
                normalize_evidence_text(requirement.get("minimum_level", "unknown")),
                0,
            ):
                requirement["minimum_level"] = group["minimum_level"]
                requirement["evidence"] = group["evidence"]
            requirement["alternative_group"] = group["group_id"]
            requirement["minimum_required_count"] = group["minimum_required_count"]
            requirement["alternative_group_evidence"] = group["evidence"]
            if language not in by_language:
                order.append(language)
            by_language[language] = requirement

    return [by_language[language] for language in order]


def build_structured_dealbreaker_notes(analysis: dict) -> list[str]:
    notes: list[str] = []

    grouped_requirements: dict[str, list[dict]] = {}
    ungrouped_requirements = []

    for requirement in analysis.get("language_requirements", []):
        if not requirement.get("required"):
            continue
        group_id = requirement.get("alternative_group")
        if group_id:
            grouped_requirements.setdefault(group_id, []).append(requirement)
        else:
            ungrouped_requirements.append(requirement)

    for requirement in ungrouped_requirements:
        language = requirement.get("language", "unknown")
        level = requirement.get("minimum_level", "unknown")
        notes.append(f"Required language: {language} (minimum {level}).")

    for requirements in grouped_requirements.values():
        languages = ", ".join(
            requirement.get("language", "unknown")
            for requirement in requirements
        )
        level = requirements[0].get("minimum_level", "unknown")
        minimum_count = requirements[0].get("minimum_required_count", 1)
        notes.append(
            f"Required language alternative: at least {minimum_count} of "
            f"{languages} (minimum {level})."
        )

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

    inferred_title_languages = infer_title_language_requirements(source_text)
    inferred_qualitative_languages = infer_qualitative_language_requirements(
        source_text
    )
    inferred_explicit_languages = infer_explicit_language_requirements(source_text)
    merged_languages = merge_language_requirements(
        supported_languages,
        [*inferred_title_languages, *inferred_qualitative_languages],
    )
    merged_languages = replace_language_requirements(
        merged_languages,
        inferred_explicit_languages,
    )
    analysis["language_requirements"] = apply_alternative_language_groups(
        merged_languages,
        infer_alternative_language_groups(source_text),
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
