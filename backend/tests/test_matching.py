import json
import unittest
from types import SimpleNamespace

from backend.app import schemas
from backend.app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
    MATCH_VERSION,
    PROFILE_ANALYSIS_MODEL,
    PROFILE_ANALYSIS_PROMPT_VERSION,
)
from backend.app.job_analysis_sanitizer import (
    sanitize_job_analysis_requirements,
)
from backend.routers.matches import (
    calculate_employment_type_score,
    calculate_nonblocking_match_warnings,
    calculate_excluded_role_matches,
    calculate_experience_score,
    calculate_language_score,
    calculate_location_score,
    calculate_role_score,
    skill_match_score,
    calculate_work_type_score,
    evaluate_hard_requirements,
    extract_employment_type_preferences,
    extract_work_type_preferences,
)


class MatchingContractTests(unittest.TestCase):
    def setUp(self):
        self.profile = SimpleNamespace(
            preferred_work_type="Full-time, hybrid or remote",
            extra_preferences="English-speaking software roles in Germany",
            target_location="Germany",
            target_locations_json=None,
            excluded_roles_json=None,
            visa_sponsorship_needed=True,
            work_authorization_status="none",
            years_of_experience=1.0,
            current_residence_country="Turkey",
            student_status="not_enrolled",
        )
        self.profile_analysis = SimpleNamespace(
            analysis_json=json.dumps({
                "visa_sponsorship_needed": "yes",
                "work_authorization_status": "none",
                "years_of_experience": 1.0,
                "current_residence_country": "turkey",
                "student_status": "not_enrolled",
            }),
            languages_json=json.dumps([
                {"language": "english", "level": "c1", "scale": "CEFR"},
                {"language": "german", "level": "a2", "scale": "CEFR"},
            ]),
            excluded_roles_json="[]",
        )

    def make_job_analysis(
        self,
        *,
        languages=None,
        visa_sponsorship="yes",
        student_required=False,
        work_authorization="none",
        residency="none",
        residency_locations=None,
        minimum_years=None,
    ):
        hard_requirements = {
            "student_enrollment_required": student_required,
            "work_authorization": work_authorization,
            "residency": residency,
            "residency_locations": residency_locations or [],
            "minimum_years_experience": minimum_years,
        }
        return SimpleNamespace(
            language_requirements_json=json.dumps(languages or []),
            visa_sponsorship=visa_sponsorship,
            dealbreakers_json=json.dumps({
                "hard_requirements": hard_requirements,
                "notes": [],
            }),
            analysis_json=json.dumps({"hard_requirements": hard_requirements}),
            employment_type="full_time",
            work_type="hybrid",
        )

    def test_shared_contract_versions(self):
        self.assertEqual(PROFILE_ANALYSIS_MODEL, "gpt-4.1-mini")
        self.assertEqual(PROFILE_ANALYSIS_PROMPT_VERSION, "profile_analysis_v5")
        self.assertEqual(JOB_ANALYSIS_MODEL, "gpt-4.1-mini")
        self.assertEqual(JOB_ANALYSIS_PROMPT_VERSION, "job_analysis_v12")
        self.assertEqual(MATCH_VERSION, "backend_match_v5")

    def test_work_type_preferences_support_multiple_values(self):
        self.assertEqual(
            extract_work_type_preferences(self.profile.preferred_work_type),
            {"hybrid", "remote"},
        )
        self.assertEqual(
            calculate_work_type_score(
                self.profile,
                SimpleNamespace(work_type="hybrid"),
            ),
            100,
        )

    def test_employment_type_is_separate_from_work_arrangement(self):
        self.assertEqual(
            extract_employment_type_preferences(self.profile.preferred_work_type),
            {"full_time"},
        )
        self.assertEqual(
            calculate_employment_type_score(
                self.profile,
                SimpleNamespace(employment_type="full_time"),
            ),
            100,
        )
        self.assertEqual(
            calculate_employment_type_score(
                self.profile,
                SimpleNamespace(employment_type="working_student"),
            ),
            20,
        )

    def test_company_text_does_not_mean_any_preference(self):
        self.assertEqual(
            extract_work_type_preferences("Remote role at a company"),
            {"remote"},
        )

    def test_german_city_from_arbeitnow_matches_germany_target(self):
        job = SimpleNamespace(location="Ellwangen", source="arbeitnow")
        self.assertEqual(calculate_location_score(self.profile, job), 90)

    def test_excluded_role_matches_normalized_job_text(self):
        profile = SimpleNamespace(excluded_roles_json='["IT support"]')
        profile_analysis = SimpleNamespace(excluded_roles_json="[]")
        job = SimpleNamespace(title="Junior IT Support Specialist")
        job_analysis = SimpleNamespace(
            normalized_role_title="it support specialist",
            role_family="it",
            role_subfamily="it_support",
            role_tags_json='["technical_support"]',
        )

        self.assertEqual(
            calculate_excluded_role_matches(
                profile, profile_analysis, job, job_analysis
            ),
            ["it support"],
        )

    def test_unknown_job_subfamilies_use_safe_canonical_fallbacks(self):
        base_payload = {
            "summary": "Test",
            "role_family": "engineering",
            "normalized_role_title": "test engineer",
            "role_tags": ["other"],
            "required_skills": ["other"],
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

        embedded = schemas.JobAnalysisStructured.model_validate({
            **base_payload,
            "role_subfamily": "embedded_engineering",
        })
        hardware = schemas.JobAnalysisStructured.model_validate({
            **base_payload,
            "role_subfamily": "hardware_engineering",
        })
        invented = schemas.JobAnalysisStructured.model_validate({
            **base_payload,
            "role_subfamily": "totally_invented_engineering",
        })

        self.assertEqual(embedded.role_subfamily, "software_engineering_general")
        self.assertEqual(hardware.role_subfamily, "other")
        self.assertEqual(invented.role_subfamily, "other")

    def test_german_a2_fails_c1_requirement(self):
        job_analysis = self.make_job_analysis(languages=[{
            "language": "german",
            "minimum_level": "c1",
            "required": True,
        }])
        score = calculate_language_score(self.profile_analysis, job_analysis)
        failures, reviews = evaluate_hard_requirements(
            self.profile, self.profile_analysis, job_analysis
        )

        self.assertLess(score, 50)
        self.assertIn(
            "required_language_level_not_met",
            {issue["code"] for issue in failures},
        )
        self.assertEqual(reviews, [])

    def test_student_requirement_is_a_hard_failure(self):
        job_analysis = self.make_job_analysis(student_required=True)
        failures, _ = evaluate_hard_requirements(
            self.profile, self.profile_analysis, job_analysis
        )
        self.assertIn(
            "student_enrollment_required",
            {issue["code"] for issue in failures},
        )

    def test_sponsorship_unavailable_is_a_hard_failure(self):
        job_analysis = self.make_job_analysis(visa_sponsorship="no")
        failures, _ = evaluate_hard_requirements(
            self.profile, self.profile_analysis, job_analysis
        )
        self.assertIn(
            "visa_sponsorship_unavailable",
            {issue["code"] for issue in failures},
        )

    def test_germany_residency_requirement_is_a_hard_failure(self):
        job_analysis = self.make_job_analysis(
            residency="germany",
            residency_locations=["germany"],
        )
        failures, _ = evaluate_hard_requirements(
            self.profile, self.profile_analysis, job_analysis
        )
        self.assertIn(
            "germany_residency_required",
            {issue["code"] for issue in failures},
        )

    def test_minimum_experience_is_structured_and_scored(self):
        job_analysis = self.make_job_analysis(minimum_years=3)
        self.assertEqual(
            calculate_experience_score(
                self.profile, self.profile_analysis, job_analysis
            ),
            10,
        )
        failures, _ = evaluate_hard_requirements(
            self.profile, self.profile_analysis, job_analysis
        )
        self.assertIn(
            "minimum_experience_not_met",
            {issue["code"] for issue in failures},
        )

    def test_unknown_candidate_fact_requires_review_not_rejection(self):
        profile = SimpleNamespace(
            visa_sponsorship_needed=False,
            work_authorization_status="unknown",
            years_of_experience=None,
            current_residence_country="unknown",
            student_status="unknown",
        )
        profile_analysis = SimpleNamespace(
            analysis_json=json.dumps({
                "visa_sponsorship_needed": "no",
                "work_authorization_status": "unknown",
                "years_of_experience": None,
                "current_residence_country": "unknown",
                "student_status": "unknown",
            }),
            languages_json='[{"language":"english","level":"c1","scale":"CEFR"}]',
        )
        job_analysis = self.make_job_analysis(work_authorization="germany")

        failures, reviews = evaluate_hard_requirements(
            profile, profile_analysis, job_analysis
        )
        self.assertEqual(failures, [])
        self.assertIn(
            "candidate_work_authorization_unknown",
            {issue["code"] for issue in reviews},
        )

    def test_v8_sanitizer_removes_unsupported_hard_requirements(self):
        analysis = {
            "language_requirements": [{
                "language": "german",
                "minimum_level": "c1",
                "required": True,
                "evidence": "German C1 required",
            }],
            "visa_sponsorship": "no",
            "visa_sponsorship_evidence": "No sponsorship available",
            "hard_requirements": {
                "student_enrollment_required": False,
                "student_enrollment_evidence": None,
                "work_authorization": "germany",
                "work_authorization_evidence": "Germany",
                "residency": "specific_location",
                "residency_locations": ["Munich"],
                "residency_evidence": "office in Munich",
                "minimum_years_experience": 3,
                "minimum_years_experience_evidence": "professional experience",
            },
            "dealbreakers": ["hallucinated"],
        }
        source_text = "The office is in Munich. Professional experience is welcome."

        sanitized = sanitize_job_analysis_requirements(analysis, source_text)

        self.assertEqual(sanitized["language_requirements"], [])
        self.assertEqual(sanitized["visa_sponsorship"], "unknown")
        self.assertEqual(
            sanitized["hard_requirements"]["work_authorization"],
            "none",
        )
        self.assertEqual(sanitized["hard_requirements"]["residency"], "none")
        self.assertIsNone(
            sanitized["hard_requirements"]["minimum_years_experience"]
        )
        self.assertEqual(sanitized["dealbreakers"], [])

    def test_v8_sanitizer_keeps_requirements_with_exact_evidence(self):
        analysis = {
            "language_requirements": [{
                "language": "german",
                "minimum_level": "c1",
                "required": True,
                "evidence": "Fließende Deutschkenntnisse",
            }],
            "visa_sponsorship": "no",
            "visa_sponsorship_evidence": "We do not offer visa sponsorship",
            "hard_requirements": {
                "student_enrollment_required": True,
                "student_enrollment_evidence": "Du bist immatrikuliert",
                "work_authorization": "germany",
                "work_authorization_evidence": "Valid work permit for Germany required",
                "residency": "germany",
                "residency_locations": ["Germany"],
                "residency_evidence": "You must currently reside in Germany",
                "minimum_years_experience": 2,
                "minimum_years_experience_evidence": "At least 2 years of experience",
            },
            "dealbreakers": [],
        }
        source_text = (
            "Fließende Deutschkenntnisse. We do not offer visa sponsorship. "
            "Du bist immatrikuliert. Valid work permit for Germany required. "
            "You must currently reside in Germany. At least 2 years of experience."
        )

        sanitized = sanitize_job_analysis_requirements(analysis, source_text)

        self.assertEqual(len(sanitized["language_requirements"]), 1)
        self.assertEqual(sanitized["visa_sponsorship"], "no")
        self.assertTrue(
            sanitized["hard_requirements"]["student_enrollment_required"]
        )
        self.assertEqual(
            sanitized["hard_requirements"]["work_authorization"],
            "germany",
        )
        self.assertEqual(sanitized["hard_requirements"]["residency"], "germany")
        self.assertEqual(
            sanitized["hard_requirements"]["minimum_years_experience"],
            2,
        )
        self.assertGreaterEqual(len(sanitized["dealbreakers"]), 5)


    def test_unknown_sponsorship_is_warning_not_manual_review(self):
        job_analysis = self.make_job_analysis(visa_sponsorship="unknown")
        failures, reviews = evaluate_hard_requirements(
            self.profile, self.profile_analysis, job_analysis
        )
        warnings = calculate_nonblocking_match_warnings(
            self.profile,
            self.profile_analysis,
            job_analysis,
            employment_type_score=100,
        )

        self.assertEqual(failures, [])
        self.assertEqual(reviews, [])
        self.assertTrue(any("sponsorship" in warning.lower() for warning in warnings))

    def test_working_student_title_sets_student_requirement(self):
        analysis = {
            "employment_type": "working_student",
            "language_requirements": [],
            "visa_sponsorship": "unknown",
            "visa_sponsorship_evidence": None,
            "hard_requirements": {
                "student_enrollment_required": False,
                "student_enrollment_evidence": None,
                "work_authorization": "none",
                "work_authorization_evidence": None,
                "residency": "none",
                "residency_locations": [],
                "residency_evidence": None,
                "minimum_years_experience": None,
                "minimum_years_experience_evidence": None,
            },
            "dealbreakers": [],
        }
        source_text = (
            "Agentic Innovation Developer - Werkstudent (m/w/d)\n"
            "Flexible part-time work compatible with studies."
        )

        sanitized = sanitize_job_analysis_requirements(analysis, source_text)

        self.assertTrue(
            sanitized["hard_requirements"]["student_enrollment_required"]
        )
        self.assertEqual(
            sanitized["hard_requirements"]["student_enrollment_evidence"].casefold(),
            "werkstudent",
        )

    def test_numeric_minimum_experience_is_inferred_from_explicit_text(self):
        analysis = {
            "employment_type": "full_time",
            "language_requirements": [],
            "visa_sponsorship": "unknown",
            "visa_sponsorship_evidence": None,
            "hard_requirements": {
                "student_enrollment_required": False,
                "student_enrollment_evidence": None,
                "work_authorization": "none",
                "work_authorization_evidence": None,
                "residency": "none",
                "residency_locations": [],
                "residency_evidence": None,
                "minimum_years_experience": None,
                "minimum_years_experience_evidence": None,
            },
            "dealbreakers": [],
        }
        source_text = "Das bringst Du mit: mind. 1-3 Jahre Erfahrung im Online Marketing."

        sanitized = sanitize_job_analysis_requirements(analysis, source_text)

        self.assertEqual(
            sanitized["hard_requirements"]["minimum_years_experience"],
            1.0,
        )
        self.assertIn(
            "mind. 1-3 Jahre",
            sanitized["hard_requirements"]["minimum_years_experience_evidence"],
        )

    def test_working_student_with_part_time_alternative_is_not_student_only(self):
        analysis = {
            "employment_type": "working_student",
            "language_requirements": [],
            "visa_sponsorship": "unknown",
            "visa_sponsorship_evidence": None,
            "hard_requirements": {
                "student_enrollment_required": True,
                "student_enrollment_evidence": "Werkstudent",
                "work_authorization": "none",
                "work_authorization_evidence": None,
                "residency": "none",
                "residency_locations": [],
                "residency_evidence": None,
                "minimum_years_experience": None,
                "minimum_years_experience_evidence": None,
            },
            "dealbreakers": [],
        }
        source_text = (
            "Wir suchen Dich als Werkstudent oder in Teilzeit als Visitenkarte "
            "unseres Shared Office Centers."
        )

        sanitized = sanitize_job_analysis_requirements(analysis, source_text)

        self.assertFalse(
            sanitized["hard_requirements"]["student_enrollment_required"]
        )
        self.assertIsNone(
            sanitized["hard_requirements"]["student_enrollment_evidence"]
        )
        self.assertEqual(sanitized["employment_type"], "part_time")

    def test_written_minimum_experience_is_inferred_in_english_and_german(self):
        examples = [
            ("At least four years of relevant project experience.", 4.0),
            ("Mit mindestens drei Jahren Berufspraxis.", 3.0),
        ]

        for source_text, expected_years in examples:
            with self.subTest(source_text=source_text):
                analysis = {
                    "employment_type": "full_time",
                    "language_requirements": [],
                    "visa_sponsorship": "unknown",
                    "visa_sponsorship_evidence": None,
                    "hard_requirements": {
                        "student_enrollment_required": False,
                        "student_enrollment_evidence": None,
                        "work_authorization": "none",
                        "work_authorization_evidence": None,
                        "residency": "none",
                        "residency_locations": [],
                        "residency_evidence": None,
                        "minimum_years_experience": None,
                        "minimum_years_experience_evidence": None,
                    },
                    "dealbreakers": [],
                }

                sanitized = sanitize_job_analysis_requirements(
                    analysis,
                    source_text,
                )

                self.assertEqual(
                    sanitized["hard_requirements"]["minimum_years_experience"],
                    expected_years,
                )

    def test_soft_experience_preference_is_not_a_hard_minimum(self):
        analysis = {
            "employment_type": "full_time",
            "language_requirements": [],
            "visa_sponsorship": "unknown",
            "visa_sponsorship_evidence": None,
            "hard_requirements": {
                "student_enrollment_required": False,
                "student_enrollment_evidence": None,
                "work_authorization": "none",
                "work_authorization_evidence": None,
                "residency": "none",
                "residency_locations": [],
                "residency_evidence": None,
                "minimum_years_experience": 1,
                "minimum_years_experience_evidence": "Ideally 1 to 3 years of experience",
            },
            "dealbreakers": [],
        }
        source_text = "Ideally 1 to 3 years of experience in operations."

        sanitized = sanitize_job_analysis_requirements(analysis, source_text)

        self.assertIsNone(
            sanitized["hard_requirements"]["minimum_years_experience"]
        )
        self.assertIsNone(
            sanitized["hard_requirements"]["minimum_years_experience_evidence"]
        )

    def test_explicit_cefr_language_minimum_is_inferred(self):
        analysis = {
            "employment_type": "full_time",
            "language_requirements": [],
            "visa_sponsorship": "unknown",
            "visa_sponsorship_evidence": None,
            "hard_requirements": {
                "student_enrollment_required": False,
                "student_enrollment_evidence": None,
                "work_authorization": "none",
                "work_authorization_evidence": None,
                "residency": "none",
                "residency_locations": [],
                "residency_evidence": None,
                "minimum_years_experience": None,
                "minimum_years_experience_evidence": None,
            },
            "dealbreakers": [],
        }
        source_text = (
            "Strong professional English and good German skills ( B2 minimum )."
        )

        sanitized = sanitize_job_analysis_requirements(analysis, source_text)

        self.assertEqual(len(sanitized["language_requirements"]), 1)
        requirement = sanitized["language_requirements"][0]
        self.assertEqual(requirement["language"], "german")
        self.assertEqual(requirement["minimum_level"], "b2")
        self.assertTrue(requirement["required"])
        self.assertIn("B2 minimum", requirement["evidence"])

    def test_plain_experience_ranges_are_inferred_as_hard_minimums(self):
        examples = [
            ("3-5 years of relevant experience are required.", 3.0),
            ("Du bringst 3–5 Jahre Berufserfahrung mit.", 3.0),
            ("4 to 7 years in finance and controlling.", 4.0),
            ("Gesucht werden 3 bis 5 Jahre Erfahrung.", 3.0),
            (
                "4–7 years in finance, FP&A, or controlling, ideally in SaaS.",
                4.0,
            ),
            (
                "3–5 Jahre Erfahrung als Product Owner – idealerweise mit Data Analytics.",
                3.0,
            ),
        ]

        for source_text, expected_years in examples:
            with self.subTest(source_text=source_text):
                analysis = {
                    "employment_type": "full_time",
                    "language_requirements": [],
                    "visa_sponsorship": "unknown",
                    "visa_sponsorship_evidence": None,
                    "hard_requirements": {
                        "student_enrollment_required": False,
                        "student_enrollment_evidence": None,
                        "work_authorization": "none",
                        "work_authorization_evidence": None,
                        "residency": "none",
                        "residency_locations": [],
                        "residency_evidence": None,
                        "minimum_years_experience": None,
                        "minimum_years_experience_evidence": None,
                    },
                    "dealbreakers": [],
                }

                sanitized = sanitize_job_analysis_requirements(analysis, source_text)

                self.assertEqual(
                    sanitized["hard_requirements"]["minimum_years_experience"],
                    expected_years,
                )

    def test_soft_plain_experience_ranges_are_not_hard_minimums(self):
        examples = [
            "Idealerweise 1 bis 2 Jahre Berufserfahrung.",
            "Ideally 2-4 years of experience.",
            "Nice to have: 3–5 years in e-commerce.",
            "2 to 3 years would be preferred.",
            "Ideally at least 3 years of relevant experience.",
            "Wünschenswert sind 3+ Jahre Erfahrung.",
        ]

        for source_text in examples:
            with self.subTest(source_text=source_text):
                analysis = {
                    "employment_type": "full_time",
                    "language_requirements": [],
                    "visa_sponsorship": "unknown",
                    "visa_sponsorship_evidence": None,
                    "hard_requirements": {
                        "student_enrollment_required": False,
                        "student_enrollment_evidence": None,
                        "work_authorization": "none",
                        "work_authorization_evidence": None,
                        "residency": "none",
                        "residency_locations": [],
                        "residency_evidence": None,
                        "minimum_years_experience": None,
                        "minimum_years_experience_evidence": None,
                    },
                    "dealbreakers": [],
                }

                sanitized = sanitize_job_analysis_requirements(analysis, source_text)

                self.assertIsNone(
                    sanitized["hard_requirements"]["minimum_years_experience"]
                )

    def test_backend_target_recognizes_full_stack_title_spelling(self):
        profile_json = {
            "target_roles": ["junior backend developer"],
            "target_role_families": ["engineering"],
        }
        profile_analysis = SimpleNamespace(
            target_role_tags_json=json.dumps([
                "backend_development",
                "api_development",
                "python_development",
            ])
        )
        job_analysis = SimpleNamespace(
            normalized_role_title="Shopify Full Stack Developer",
            role_family="engineering",
            role_subfamily="fullstack_engineering",
            role_tags_json=json.dumps([
                "fullstack_development",
                "api_development",
                "backend_development",
                "python_development",
                "other",
            ]),
        )

        score = calculate_role_score(profile_json, profile_analysis, job_analysis)

        self.assertGreaterEqual(score, 69)

    def test_other_skill_placeholder_never_creates_a_false_match(self):
        self.assertEqual(skill_match_score(["other"], ["other"]), 0)
        self.assertEqual(
            skill_match_score(["python", "other"], ["python", "other"]),
            50,
        )

    def test_employment_type_mismatch_is_nonblocking_warning(self):
        job_analysis = self.make_job_analysis(visa_sponsorship="yes")
        warnings = calculate_nonblocking_match_warnings(
            self.profile,
            self.profile_analysis,
            job_analysis,
            employment_type_score=20,
        )
        self.assertTrue(any("employment type" in warning.lower() for warning in warnings))


if __name__ == "__main__":
    unittest.main()
