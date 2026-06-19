# Phase 2.2 installation

No database migration is required.

1. Replace the files from this patch.
2. Run `python -m unittest discover backend/tests`.
3. Reanalyze selected jobs with `force_reanalyze=true`; they must return `job_analysis_v9`.
4. Rematch with `force_rematch=true`; results must return `backend_match_v4`.

Expected behavior:
- A `Werkstudent` job is ineligible for a profile with `student_status=not_enrolled`, using reason code `student_enrollment_required`.
- Unknown sponsorship policy remains eligible when no other unresolved explicit hard requirement exists, but produces a warning and a lower visa fit score.
- Employment-type mismatch alone is not a hard failure.
