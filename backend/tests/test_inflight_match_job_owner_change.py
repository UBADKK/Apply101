"""Matching must not write a match against a different job row that took over
the matched job's job_id AND url but has a different owner
(created_by_user_id) that is still visible to the profile owner.

jobs.url is only unique per owner / among catalog jobs (uq_jobs_catalog_url,
uq_jobs_owner_url), job_id has no AUTOINCREMENT and SQLite foreign keys are
not enforced, so job_id + url alone does not identify the row: a catalog
job (owner NULL) can be replaced by the profile owner's own job with the
same job_id and url, or vice versa. Both directions are visible to the
profile owner, so only the owner comparison detects the replacement.

The replacement is made deterministic, without threads, from a separate,
independently committed session: inside the patched calculate_backend_match
(single-match route) or right before the replaced job's per-job call (batch
route). Only the jobs row is replaced; job_matches/job_analyses rows are
left in place so the previously current match can be checked.
"""

import unittest
from unittest.mock import patch

from backend.app import models
from backend.tests.test_inflight_profile_delete_writes import (
    REAL_CALCULATE_BACKEND_MATCH,
    REAL_MATCH_IMPL,
    _BaseInflightDeleteTestCase,
)


BATCH_TOP_LEVEL_KEYS = {
    "status",
    "user_id",
    "profile_id",
    "requested_limit",
    "offset",
    "selected_job_count",
    "matched_count",
    "failed_count",
    "max_allowed_limit",
    "results",
}

BATCH_MATCHED_ENTRY_KEYS = {
    "job_id",
    "title",
    "company_name",
    "location",
    "url",
    "status",
    "match_id",
    "match_version",
    "eligibility_status",
    "hard_fail_reason_codes",
    "overall_score",
    "recommendation",
    "summary",
}

REPLACEMENT_TITLE = "REPLACEMENT-POSTING-TITLE"


class _JobOwnerChangeTestCase(_BaseInflightDeleteTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._create_user("owner.change.profile@example.com")
        self.profile_id = self._create_profile(self.user_id)
        self.profile_analysis_id = self._seed_profile_analysis(self.profile_id)
        self.headers = self._headers(self.user_id)

    # --- helpers -----------------------------------------------------------

    def _job_not_found_detail(self, job_id):
        return {
            "error_code": "ERR_JOB_NOT_FOUND",
            "message": f"Job with id {job_id} was not found.",
        }

    def _job_owner_and_url(self, job_id):
        session = self.session_factory()
        try:
            return session.query(
                models.Job.created_by_user_id, models.Job.url
            ).filter(models.Job.job_id == job_id).one()
        finally:
            session.close()

    def _replace_job_row_elsewhere(self, job_id, new_owner_id):
        """Deletes ONLY the jobs row and inserts a different job with the
        same job_id and url but another created_by_user_id, from its own
        committed session."""
        session = self.session_factory()
        try:
            original_owner_id, url = session.query(
                models.Job.created_by_user_id, models.Job.url
            ).filter(models.Job.job_id == job_id).one()
            self.assertNotEqual(original_owner_id, new_owner_id)
            session.query(models.Job).filter(
                models.Job.job_id == job_id
            ).delete(synchronize_session=False)
            session.add(models.Job(
                job_id=job_id,
                title=REPLACEMENT_TITLE,
                url=url,
                description_text="replacement description",
                created_by_user_id=new_owner_id,
            ))
            session.commit()
        finally:
            session.close()

    def _match_row_snapshot(self, match_id):
        session = self.session_factory()
        try:
            row = session.get(models.JobMatch, match_id)
            return (
                row.match_id,
                row.profile_id,
                row.job_id,
                row.profile_analysis_id,
                row.job_analysis_id,
                row.match_status,
                row.match_json,
                row.overall_score,
                row.recommendation,
                row.summary,
                row.match_model,
                row.match_prompt_version,
                row.is_current,
            )
        finally:
            session.close()

    def _match_single(self, job_id, side_effect):
        with patch(
            "backend.routers.matches.calculate_backend_match",
            side_effect=side_effect,
        ) as mock_calc:
            response = self.client.post(
                f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/{job_id}/match",
                params={"force_rematch": "true"},
                headers=self.headers,
            )
        mock_calc.assert_called_once()
        return response

    def _batch_with_hook_before_job(self, target_job_id, hook):
        calls = []

        def impl(**kwargs):
            calls.append(kwargs["job_id"])
            if kwargs["job_id"] == target_job_id:
                hook()
            return REAL_MATCH_IMPL(**kwargs)

        with patch(
            "backend.routers.matches._match_profile_with_job_impl", side_effect=impl
        ):
            response = self.client.post(
                f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/match-analyzed",
                params={"force_rematch": "true"},
                headers=self.headers,
            )
        return response, calls


class SingleMatchJobOwnerChangeTests(_JobOwnerChangeTestCase):
    def _setup_job(self, original_owner_id):
        self.job_id = self._create_job("owner-change", owner_id=original_owner_id)
        self.seeded_match_id = self._seed_match(
            self.profile_id, self.profile_analysis_id, self.job_id, summary="seeded"
        )
        self.seeded_snapshot = self._match_row_snapshot(self.seeded_match_id)

    def _replace_then_real(self, new_owner_id):
        def side_effect(**kwargs):
            self._replace_job_row_elsewhere(self.job_id, new_owner_id)
            return REAL_CALCULATE_BACKEND_MATCH(**kwargs)
        return side_effect

    def _replace_then_raise(self, new_owner_id):
        def side_effect(**kwargs):
            self._replace_job_row_elsewhere(self.job_id, new_owner_id)
            raise RuntimeError("synthetic match failure")
        return side_effect

    def _assert_job_404_and_nothing_written(self, response, new_owner_id):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json(), {"detail": self._job_not_found_detail(self.job_id)}
        )
        # Neither a completed nor a failed row was added; the seeded match
        # is still the current one and otherwise unchanged (the is_current
        # demotion was rolled back).
        self.assertEqual(
            self._match_rows(self.profile_id),
            [(self.seeded_match_id, self.job_id, "completed", True)],
        )
        self.assertEqual(
            self._match_row_snapshot(self.seeded_match_id), self.seeded_snapshot
        )
        # The replacement really happened (same url, new owner).
        owner, url = self._job_owner_and_url(self.job_id)
        self.assertEqual(owner, new_owner_id)
        self.assertEqual(url, "https://example.com/job/owner-change")

    def test_success_catalog_job_replaced_by_owners_job_returns_404(self):
        self._setup_job(original_owner_id=None)

        response = self._match_single(self.job_id, self._replace_then_real(self.user_id))

        self._assert_job_404_and_nothing_written(response, self.user_id)

    def test_success_owners_job_replaced_by_catalog_job_returns_404(self):
        self._setup_job(original_owner_id=self.user_id)

        response = self._match_single(self.job_id, self._replace_then_real(None))

        self._assert_job_404_and_nothing_written(response, None)

    def test_failure_catalog_job_replaced_by_owners_job_returns_404(self):
        self._setup_job(original_owner_id=None)

        response = self._match_single(self.job_id, self._replace_then_raise(self.user_id))

        self._assert_job_404_and_nothing_written(response, self.user_id)

    def test_failure_owners_job_replaced_by_catalog_job_returns_404(self):
        self._setup_job(original_owner_id=self.user_id)

        response = self._match_single(self.job_id, self._replace_then_raise(None))

        self._assert_job_404_and_nothing_written(response, None)


class BatchJobOwnerChangeTests(_JobOwnerChangeTestCase):
    def _run(self, original_owner_id, new_owner_id):
        # Processing order is job_id desc: other_job first, then the target.
        target_job_id = self._create_job("owner-change-target", owner_id=original_owner_id)
        other_job_id = self._create_job("owner-change-other", owner_id=original_owner_id)

        response, calls = self._batch_with_hook_before_job(
            target_job_id,
            lambda: self._replace_job_row_elsewhere(target_job_id, new_owner_id),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [other_job_id, target_job_id])
        body = response.json()
        self.assertEqual(set(body), BATCH_TOP_LEVEL_KEYS)
        self.assertEqual(body["selected_job_count"], 2)
        self.assertEqual(body["matched_count"], 1)
        self.assertEqual(body["failed_count"], 1)

        by_job = {item["job_id"]: item for item in body["results"]}
        self.assertEqual(set(by_job), {target_job_id, other_job_id})
        self.assertEqual(
            by_job[target_job_id],
            {
                "job_id": target_job_id,
                "title": "Synthetic Job owner-change-target",
                "company_name": None,
                "status": "failed",
                "error": self._job_not_found_detail(target_job_id),
            },
        )
        self.assertEqual(set(by_job[other_job_id]), BATCH_MATCHED_ENTRY_KEYS)
        self.assertEqual(by_job[other_job_id]["status"], "created")
        self.assertNotIn(REPLACEMENT_TITLE, response.text)

        rows = self._match_rows(self.profile_id)
        self.assertEqual(
            [(job_id, status, current) for _, job_id, status, current in rows],
            [(other_job_id, "completed", True)],
        )
        self.assertEqual(
            self._job_owner_and_url(target_job_id),
            (new_owner_id, "https://example.com/job/owner-change-target"),
        )

    def test_catalog_job_replaced_by_owners_job_is_per_job_404(self):
        self._run(original_owner_id=None, new_owner_id=self.user_id)

    def test_owners_job_replaced_by_catalog_job_is_per_job_404(self):
        self._run(original_owner_id=self.user_id, new_owner_id=None)


class JobOwnerUnchangedBehaviorTests(_JobOwnerChangeTestCase):
    def setUp(self):
        super().setUp()
        self.catalog_job_id = self._create_job("unchanged-catalog")
        self.personal_job_id = self._create_job("unchanged-personal", owner_id=self.user_id)

    def _assert_single_match_created_and_demotes(self, job_id):
        seeded_match_id = self._seed_match(
            self.profile_id, self.profile_analysis_id, job_id
        )

        response = self.client.post(
            f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/{job_id}/match",
            params={"force_rematch": "true"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "created")
        new_match_id = response.json()["match_id"]
        rows = [row for row in self._match_rows(self.profile_id) if row[1] == job_id]
        self.assertEqual(
            rows,
            [
                (seeded_match_id, job_id, "completed", False),
                (new_match_id, job_id, "completed", True),
            ],
        )

    def test_single_match_catalog_job_unchanged(self):
        self._assert_single_match_created_and_demotes(self.catalog_job_id)

    def test_single_match_owners_personal_job_unchanged(self):
        self._assert_single_match_created_and_demotes(self.personal_job_id)

    def test_single_match_failure_still_writes_failed_match(self):
        with patch(
            "backend.routers.matches.calculate_backend_match",
            side_effect=RuntimeError("synthetic match failure"),
        ):
            response = self.client.post(
                f"/users/{self.user_id}/profiles/{self.profile_id}"
                f"/jobs/{self.personal_job_id}/match",
                params={"force_rematch": "true"},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_BACKEND_MATCH_FAILED")
        self.assertEqual(
            [(job_id, status, current) for _, job_id, status, current
             in self._match_rows(self.profile_id)],
            [(self.personal_job_id, "failed", False)],
        )

    def test_batch_matches_catalog_and_personal_jobs(self):
        response = self.client.post(
            f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/match-analyzed",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), BATCH_TOP_LEVEL_KEYS)
        self.assertEqual(body["matched_count"], 2)
        self.assertEqual(body["failed_count"], 0)
        for item in body["results"]:
            self.assertEqual(set(item), BATCH_MATCHED_ENTRY_KEYS)
            self.assertEqual(item["status"], "created")
        self.assertEqual(
            {item["job_id"] for item in body["results"]},
            {self.catalog_job_id, self.personal_job_id},
        )


if __name__ == "__main__":
    unittest.main()
