# Apply101 Matching Contract

This document is the compatibility contract between profile analysis, job analysis,
and backend matching.

## Current versions

Versions are defined in `backend/app/analysis_contract.py`.

- Profile analysis: `gpt-4.1-mini` / `profile_analysis_v5`
- Job analysis: `gpt-4.1-mini` / `job_analysis_v8`
- Backend matching: `backend_rule_based` / `backend_match_v3`

The match endpoints reject profile or job analyses that do not match these
required versions. A change to a prompt, structured schema, taxonomy,
normalization rule, eligibility rule, or scoring behavior must increment the
related version.

## Candidate facts used for eligibility

The following facts may be supplied directly on `CandidateProfile`. Direct user
input takes priority over an LLM inference:

- `visa_sponsorship_needed`
- `work_authorization_status`
- `years_of_experience`
- `current_residence_country`
- `student_status`
- `relocation_preference`

Profile analysis v5 also stores structured fallbacks in `analysis_json`:

- `visa_sponsorship_needed`: `yes`, `no`, or `unknown`
- `work_authorization_status`: `germany`, `eu_eea`, `other_country_only`, `none`, or `unknown`
- `current_residence_country`
- `student_status`: `currently_enrolled`, `not_enrolled`, or `unknown`
- `years_of_experience`: number or `null`
- languages with canonical CEFR levels

The database migration `backend/migrations/phase2_profile_fields.py` adds the
`current_residence_country` and `student_status` columns to existing SQLite
databases.

## Job hard-requirement contract

Job analysis v8 stores typed, evidence-backed language requirements:

```json
{
  "language": "german",
  "minimum_level": "c1",
  "required": true,
  "evidence": "Fließende Deutschkenntnisse"
}
```

It also stores:

```json
{
  "hard_requirements": {
    "student_enrollment_required": false,
    "student_enrollment_evidence": null,
    "work_authorization": "germany",
    "work_authorization_evidence": "Valid work permit for Germany required",
    "residency": "none",
    "residency_locations": [],
    "residency_evidence": null,
    "minimum_years_experience": 2,
    "minimum_years_experience_evidence": "At least 2 years of experience"
  }
}
```

`dealbreakers_json` contains both the structured requirements and short
human-readable notes. Matching uses the structured values, not keyword searches
in free text.

Every hard requirement must include a short exact excerpt from the job description.
After LLM validation, `job_analysis_sanitizer.py` checks that the excerpt exists in
the original description and contains requirement-specific wording. Unsupported
language, sponsorship, authorization, residency, student, and experience claims
are cleared before the analysis is saved. Job location, onsite/hybrid status,
listing language, and general legal assumptions are never sufficient evidence.

## Match v3 eligibility behavior

The matcher first evaluates hard requirements and then produces scores.

- Confirmed missing hard requirements produce `eligibility_status=ineligible`,
  `recommendation=ineligible`, and cap `overall_score` at 20.
- Missing candidate/job facts produce `eligibility_status=review_required`,
  `recommendation=manual_review`, and cap `overall_score` at 57.
- No hard-requirement issue produces `eligibility_status=eligible`, after which
  the normal score recommendation is used.

Current typed checks:

1. Required language and minimum CEFR level.
2. Employer sponsorship availability.
3. Existing work authorization requirement.
4. Current residency requirement.
5. Active student enrollment requirement.
6. Minimum years of experience.
7. Candidate-excluded roles.
8. Explicit employment-type conflicts.

Each hard failure and review reason has a stable machine-readable `code` and a
human-readable `message` in `match_json`.

## Match scoring inputs

Profile signals:

- target roles, role families, and role tags
- strong/moderate/basic skills and tools
- seniority and years of experience
- languages and CEFR levels
- direct location, work-type, employment-type, and excluded-role preferences

Job signals:

- normalized role, family, subfamily, and role tags
- required and preferred skills
- seniority and structured minimum experience
- typed language requirements
- sponsorship, authorization, residency, and student requirements
- work type and employment type

## Version and cache rules

- v3 match rows never reuse v2 rows as cache results.
- Reanalyzing either side produces a new analysis ID, so old match rows are not
  reused.
- A forced rematch marks the previous current row false and creates one new
  current row.
- Old analyses and matches remain as history.

## Remaining limitation

The skill taxonomy still maps many technologies to `other`. That issue should be
handled separately after the eligibility contract is validated on a small,
hand-reviewed test set.


## Phase 2.2 contract updates

- Current job analysis contract: `job_analysis_v11`.
- Current backend matcher: `backend_match_v4`.
- Explicit `Werkstudent` / `Working Student` labels are treated as evidence that active student enrollment is required, unless the listing also offers a non-student part-time or full-time alternative.
- Explicit numeric or written minimum expressions such as `mind. 1-3 Jahre`, `mindestens drei Jahre`, `at least four years`, and `3+ years` are normalized into `minimum_years_experience`.
- Explicit CEFR minimums such as `German B2 minimum` are normalized into required language requirements.
- Soft experience preferences such as `ideally 1-3 years` are not treated as hard minimums.
- Unknown visa sponsorship policy is a non-blocking warning. It does not by itself produce `review_required`.
- Employment-type preference mismatches reduce fit and produce a warning, but do not by themselves make a candidate ineligible.
