"""Profile analysis and matching must not write anything for a profile whose
user was deleted while the operation was in flight -- neither an orphan row
for the deleted profile_id nor a row attached to a NEW user/profile that
reused the same ids (users/candidate_profiles have no AUTOINCREMENT and
SQLite foreign keys are not enforced).

The deletion is made deterministic, without threads: the slow seam that runs
between the route's initial reads and its writes (the OpenAI call for
profile analysis, calculate_backend_match for matching) is patched with a
side effect that deletes the user through the real users.delete_user in a
separate, independently committed session, optionally recreates the same
user_id/profile_id for a different person, and then continues.
"""

import contextlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from backend.app import models, profile_write_guard
from backend.app.analysis_contract import (
    JOB_ANALYSIS_MODEL,
    JOB_ANALYSIS_PROMPT_VERSION,
    MATCH_MODEL,
    MATCH_VERSION,
    PROFILE_ANALYSIS_MODEL,
    PROFILE_ANALYSIS_PROMPT_VERSION,
)
from backend.app.analysis_guard import PROFILE_OPERATION_TYPE, USER_OPERATION_TYPE
from backend.app.auth_dependencies import get_current_user, get_owned_profile
from backend.app.database import Base, get_db
from backend.app.security import create_access_token

# Synthetic key only while importing (profiles.py builds its client at
# import time); every OpenAI-reaching call below is patched.
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import matches, profiles, users


SYNTHETIC_SECRET = "synthetic-test-secret-for-inflight-profile-delete-0123456789"
PROFILE_NOT_FOUND = {"detail": "Profile not found."}
REAL_CALCULATE_BACKEND_MATCH = matches.calculate_backend_match
REAL_MATCH_IMPL = matches._match_profile_with_job_impl
# Put into the new account's (Y's) rows; must never appear in any response
# to the deleted account's (X's) in-flight request.
Y_MARKER = "Y-PRIVATE-MARKER"

# Exactly schemas.ProfileAnalysisStructured's fields (extra="forbid").
VALID_ANALYSIS_PAYLOAD = {
    "candidate_summary": "synthetic summary",
    "current_role_family": "other",
    "target_role_families": ["other"],
    "target_role_tags": ["other"],
    "target_roles": [],
    "excluded_roles": [],
    "strong_skills": [],
    "moderate_skills": [],
    "weak_or_basic_skills": [],
    "tools": [],
    "industries": [],
    "years_of_experience": None,
    "seniority_level": "unknown",
    "education_level": "unknown",
    "field_of_study": "unknown",
    "languages": [],
    "visa_sponsorship_needed": "unknown",
    "work_authorization_status": "unknown",
    "relocation_preference": "unknown",
    "current_residence_country": "unknown",
    "student_status": "unknown",
    "match_notes": [],
}


def _openai_response(output_text):
    response = MagicMock()
    response.output_text = output_text
    return response


class _BaseInflightDeleteTestCase(unittest.TestCase):
    """Throwaway synthetic SQLite database (never apply101.db); users,
    profiles and matches routers mounted; synthetic JWT secret."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_inflight_delete_test_")
        self.db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)

        self.session_factory = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app = FastAPI()
        app.include_router(users.router)
        app.include_router(profiles.router)
        app.include_router(matches.router)
        app.dependency_overrides[get_db] = override_get_db
        self.app = app
        self.client = TestClient(app)

        env_patcher = patch.dict(
            os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        # Never a real client; _create_profile_analysis_response is patched
        # in every test that reaches it.
        client_patcher = patch.object(profiles, "client", MagicMock())
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # --- seeding ---------------------------------------------------------

    def _add(self, obj):
        session = self.session_factory()
        try:
            session.add(obj)
            session.commit()
            session.refresh(obj)
            return obj
        finally:
            session.close()

    def _create_user(self, mail, user_id=None, is_admin=False):
        return self._add(models.User(
            user_id=user_id, name="Synthetic User", mail=mail, is_admin=is_admin
        )).user_id

    def _create_profile(self, user_id, profile_id=None, description="synthetic profile"):
        return self._add(models.CandidateProfile(
            profile_id=profile_id, user_id=user_id, self_description=description
        )).profile_id

    def _seed_profile_analysis(self, profile_id, is_current=True, summary=None):
        payload = dict(VALID_ANALYSIS_PAYLOAD)
        if summary is not None:
            payload["candidate_summary"] = summary
        return self._add(models.ProfileAnalysis(
            profile_id=profile_id,
            analysis_status="completed",
            analysis_json=json.dumps(payload),
            candidate_summary=summary,
            target_role_tags_json='["other"]',
            analysis_model=PROFILE_ANALYSIS_MODEL,
            analysis_prompt_version=PROFILE_ANALYSIS_PROMPT_VERSION,
            is_current=is_current,
        )).analysis_id

    def _create_job(self, slug, job_id=None, owner_id=None):
        job_id = self._add(models.Job(
            job_id=job_id,
            title=f"Synthetic Job {slug}",
            url=f"https://example.com/job/{slug}",
            description_text="synthetic description",
            created_by_user_id=owner_id,
        )).job_id
        self._add(models.JobAnalysis(
            job_id=job_id,
            analysis_status="completed",
            analysis_json=json.dumps({"hard_requirements": {}}),
            role_tags_json='["other"]',
            analysis_model=JOB_ANALYSIS_MODEL,
            analysis_prompt_version=JOB_ANALYSIS_PROMPT_VERSION,
            is_current=True,
        ))
        return job_id

    def _seed_match(self, profile_id, profile_analysis_id, job_id, summary=None, score=50):
        session = self.session_factory()
        try:
            job_analysis_id = session.query(models.JobAnalysis.analysis_id).filter(
                models.JobAnalysis.job_id == job_id,
                models.JobAnalysis.is_current == True,
            ).scalar()
        finally:
            session.close()
        return self._add(models.JobMatch(
            profile_id=profile_id,
            job_id=job_id,
            profile_analysis_id=profile_analysis_id,
            job_analysis_id=job_analysis_id,
            match_status="completed",
            match_json=json.dumps({"eligibility_status": "eligible", "summary": summary}),
            overall_score=score,
            recommendation="maybe",
            summary=summary,
            match_model=MATCH_MODEL,
            match_prompt_version=MATCH_VERSION,
            matched_at=datetime.now(timezone.utc),
            is_current=True,
        )).match_id

    def _headers(self, user_id):
        # Minted with the user's real stored token key, exactly as login does.
        session = self.session_factory()
        try:
            token_key = session.query(models.User.token_key).filter(
                models.User.user_id == user_id
            ).scalar()
        finally:
            session.close()
        return {"Authorization": f"Bearer {create_access_token(user_id, token_key)}"}

    # --- the concurrent request, in its own independently committed session

    def _delete_user_elsewhere(self, user_id, session_factory=None):
        session = (session_factory or self.session_factory)()
        try:
            user = session.query(models.User).filter(
                models.User.user_id == user_id
            ).one()
            result = users.delete_user(user_id=user_id, current_user=user, db=session)
            self.assertEqual(result["status"], "deleted")
        finally:
            session.close()

    def _recreate_same_ids_elsewhere(
        self, user_id, profile_id, mail, description="next person's own profile"
    ):
        # A different person who got the deleted user's user_id and
        # profile_id (SQLite reuses the max rowid without AUTOINCREMENT).
        # The ORM default gives this row its own fresh token_key.
        session = self.session_factory()
        try:
            session.add(models.User(user_id=user_id, name="Next Person", mail=mail))
            session.flush()
            session.add(models.CandidateProfile(
                profile_id=profile_id,
                user_id=user_id,
                self_description=description,
            ))
            session.commit()
        finally:
            session.close()

    def _delete_attempt_right_after_write_check(self, outcomes):
        """Patches the identity SELECT so that, right after it returns
        INSIDE the write check (i.e. anywhere between the check and our
        commit), the user's deletion is attempted from another connection
        with a short busy timeout. With the check issued after the flushed
        writes, this connection already holds SQLite's write lock, so the
        delete must fail with "database is locked"; a check issued before
        the first write would let it commit and leave an orphan row."""
        blocked_engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 0.1},
        )
        self.addCleanup(blocked_engine.dispose)
        blocked_factory = sessionmaker(autocommit=False, autoflush=False, bind=blocked_engine)
        real_row = profile_write_guard._current_owner_token_key_row
        in_write_check = []

        def mark_write_check(real):
            def wrapper(db, identity):
                in_write_check.append(True)
                try:
                    return real(db, identity)
                finally:
                    in_write_check.pop()
            return wrapper

        def row_then_try_delete(db, profile_id, user_id):
            row = real_row(db, profile_id, user_id)
            if in_write_check and not outcomes:
                try:
                    self._delete_user_elsewhere(self.user_id, blocked_factory)
                    outcomes.append("delete committed")
                except OperationalError as exc:
                    outcomes.append("locked" if "locked" in str(exc) else repr(exc))
            return row

        stack = contextlib.ExitStack()
        for module in (profiles, matches):
            stack.enter_context(patch.object(
                module,
                "verify_profile_owner_unchanged_or_404",
                mark_write_check(module.verify_profile_owner_unchanged_or_404),
            ))
        stack.enter_context(patch(
            "backend.app.profile_write_guard._current_owner_token_key_row",
            side_effect=row_then_try_delete,
        ))
        return stack

    def _inject_after_authorization(self, callback):
        """Runs callback (in its own committed sessions) right after the
        real get_owned_profile authorized and loaded the profile, before the
        route body starts -- the window between authorization and identity
        capture."""
        def get_owned_profile_then_callback(
            user_id: int,
            profile_id: int,
            db: Session = Depends(get_db),
            current_user: models.User = Depends(get_current_user),
        ):
            profile = get_owned_profile(
                user_id=user_id, profile_id=profile_id, db=db, current_user=current_user
            )
            callback()
            return profile

        self.app.dependency_overrides[get_owned_profile] = get_owned_profile_then_callback
        self.addCleanup(self.app.dependency_overrides.pop, get_owned_profile, None)

    def _assert_profile_survived(self):
        session = self.session_factory()
        try:
            self.assertIsNotNone(session.get(models.CandidateProfile, self.profile_id))
        finally:
            session.close()

    # --- reads -------------------------------------------------------------

    def _profile_analysis_rows(self, profile_id):
        session = self.session_factory()
        try:
            return sorted(
                (row.analysis_id, row.analysis_status, row.is_current)
                for row in session.query(models.ProfileAnalysis).filter(
                    models.ProfileAnalysis.profile_id == profile_id
                ).all()
            )
        finally:
            session.close()

    def _match_rows(self, profile_id):
        session = self.session_factory()
        try:
            return sorted(
                (row.match_id, row.job_id, row.match_status, row.is_current)
                for row in session.query(models.JobMatch).filter(
                    models.JobMatch.profile_id == profile_id
                ).all()
            )
        finally:
            session.close()

    def _all_match_rows(self):
        session = self.session_factory()
        try:
            return sorted(
                (row.match_id, row.profile_id, row.job_id, row.match_status, row.is_current)
                for row in session.query(models.JobMatch).all()
            )
        finally:
            session.close()

    def _guard_owner_token(self, operation_type, resource_id):
        session = self.session_factory()
        try:
            row = session.query(models.AnalysisGuard.owner_token).filter(
                models.AnalysisGuard.operation_type == operation_type,
                models.AnalysisGuard.resource_id == resource_id,
            ).first()
            return None if row is None else row[0]
        finally:
            session.close()


class ProfileAnalysisInflightDeleteTests(_BaseInflightDeleteTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._create_user("inflight.owner@example.com")
        self.profile_id = self._create_profile(self.user_id)
        # Deleted together with the user; forces the OpenAI path below.
        self._seed_profile_analysis(self.profile_id)

        # Unrelated live profile whose current analysis must stay current.
        self.other_user_id = self._create_user("inflight.other@example.com")
        self.other_profile_id = self._create_profile(self.other_user_id)
        self.other_analysis_id = self._seed_profile_analysis(self.other_profile_id)

        self.headers = self._headers(self.user_id)

    def _analyze(self, side_effect):
        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            side_effect=side_effect,
        ) as mock_call, self.assertNoLogs("backend.routers.profiles", level="WARNING"):
            response = self.client.post(
                f"/users/{self.user_id}/profiles/{self.profile_id}/analyze",
                params={"force_reanalyze": "true"},
                headers=self.headers,
            )
        mock_call.assert_called_once()
        return response

    def _assert_other_profile_untouched(self):
        self.assertEqual(
            self._profile_analysis_rows(self.other_profile_id),
            [(self.other_analysis_id, "completed", True)],
        )

    def _assert_guards_not_left_locked(self):
        # delete_user removed the guard rows; the route's release is a
        # token-gated no-op and must neither crash nor leave a lease behind.
        self.assertIsNone(self._guard_owner_token(PROFILE_OPERATION_TYPE, self.profile_id))
        self.assertIsNone(self._guard_owner_token(USER_OPERATION_TYPE, self.user_id))

    def _delete_then(self, outcome):
        def side_effect(*args, **kwargs):
            self._delete_user_elsewhere(self.user_id)
            return outcome()
        return side_effect

    def _delete_reuse_then(self, outcome, seed_new_current_analysis=False):
        def side_effect(*args, **kwargs):
            self._delete_user_elsewhere(self.user_id)
            self._recreate_same_ids_elsewhere(
                self.user_id, self.profile_id, "inflight.next.person@example.com"
            )
            if seed_new_current_analysis:
                self.new_profile_analysis_id = self._seed_profile_analysis(self.profile_id)
            return outcome()
        return side_effect

    @staticmethod
    def _valid():
        return _openai_response(json.dumps(VALID_ANALYSIS_PAYLOAD))

    @staticmethod
    def _invalid_json():
        return _openai_response("not valid json{{{")

    @staticmethod
    def _raise():
        raise RuntimeError("synthetic upstream failure")

    def test_success_after_delete_returns_404_and_writes_nothing(self):
        response = self._analyze(self._delete_then(self._valid))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self.assertEqual(self._profile_analysis_rows(self.profile_id), [])
        self._assert_other_profile_untouched()
        self._assert_guards_not_left_locked()

    def test_invalid_json_after_delete_returns_404_and_writes_no_failed_row(self):
        response = self._analyze(self._delete_then(self._invalid_json))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self.assertEqual(self._profile_analysis_rows(self.profile_id), [])
        self._assert_other_profile_untouched()
        self._assert_guards_not_left_locked()

    def test_exception_after_delete_returns_404_and_writes_no_failed_row(self):
        response = self._analyze(self._delete_then(self._raise))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self.assertEqual(self._profile_analysis_rows(self.profile_id), [])
        self._assert_other_profile_untouched()
        self._assert_guards_not_left_locked()

    def test_success_after_delete_and_id_reuse_attaches_nothing_to_new_profile(self):
        response = self._analyze(self._delete_reuse_then(self._valid))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self.assertEqual(self._profile_analysis_rows(self.profile_id), [])
        self._assert_other_profile_untouched()

        analyzed = self.client.get(
            f"/users/{self.user_id}/profiles/analyzed",
            headers=self._headers(self.user_id),
        )
        self.assertEqual(analyzed.status_code, 200)
        self.assertEqual(analyzed.json(), [])

    def test_success_after_id_reuse_keeps_new_profiles_current_analysis(self):
        response = self._analyze(
            self._delete_reuse_then(self._valid, seed_new_current_analysis=True)
        )

        self.assertEqual(response.status_code, 404)
        # The is_current flip ran for this profile_id before the check and
        # must have been rolled back with everything else.
        self.assertEqual(
            self._profile_analysis_rows(self.profile_id),
            [(self.new_profile_analysis_id, "completed", True)],
        )
        self._assert_other_profile_untouched()

    def test_invalid_json_after_id_reuse_attaches_no_failed_row(self):
        response = self._analyze(
            self._delete_reuse_then(self._invalid_json, seed_new_current_analysis=True)
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self.assertEqual(
            self._profile_analysis_rows(self.profile_id),
            [(self.new_profile_analysis_id, "completed", True)],
        )

    def test_exception_after_id_reuse_attaches_no_failed_row(self):
        response = self._analyze(self._delete_reuse_then(self._raise))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._profile_analysis_rows(self.profile_id), [])


class ProfileAnalysisUnchangedBehaviorTests(_BaseInflightDeleteTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._create_user("inflight.live@example.com")
        self.profile_id = self._create_profile(self.user_id)
        self.headers = self._headers(self.user_id)

    def _post(self, **params):
        return self.client.post(
            f"/users/{self.user_id}/profiles/{self.profile_id}/analyze",
            params=params,
            headers=self.headers,
        )

    def test_success_still_creates_and_flips_previous_current(self):
        prior_id = self._seed_profile_analysis(self.profile_id)

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_openai_response(json.dumps(VALID_ANALYSIS_PAYLOAD)),
        ):
            response = self._post(force_reanalyze="true")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "created")
        self.assertEqual(body["profile_id"], self.profile_id)
        self.assertEqual(
            self._profile_analysis_rows(self.profile_id),
            [(prior_id, "completed", False), (body["analysis_id"], "completed", True)],
        )
        self.assertIsNone(self._guard_owner_token(PROFILE_OPERATION_TYPE, self.profile_id))

    def test_failure_still_writes_failed_row_for_live_profile(self):
        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_openai_response("not valid json{{{"),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_PROFILE_ANALYSIS_FAILED")
        self.assertEqual(response.json()["detail"]["profile_id"], self.profile_id)
        rows = self._profile_analysis_rows(self.profile_id)
        self.assertEqual([(status, current) for _, status, current in rows], [("failed", False)])

    def test_cache_hit_unchanged(self):
        prior_id = self._seed_profile_analysis(self.profile_id)

        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cached")
        self.assertEqual(response.json()["analysis_id"], prior_id)
        mock_call.assert_not_called()

    def _post_with_delete_attempt_after_check(self, output_text):
        # Success path: the is_current UPDATE already took the write lock.
        # Failure path: everything was rolled back, so the failed row's
        # flush inside the check is the only thing that takes the lock --
        # this is the path that proves the flush-then-check order matters.
        outcomes = []
        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_openai_response(output_text),
        ), self._delete_attempt_right_after_write_check(outcomes):
            response = self._post()

        self.assertEqual(outcomes, ["locked"])
        # The user and profile survived, so whatever was committed is a
        # legitimate row of a live profile, not an orphan.
        self._assert_profile_survived()
        return response

    def test_concurrent_delete_cannot_commit_between_check_and_commit_on_success(self):
        response = self._post_with_delete_attempt_after_check(
            json.dumps(VALID_ANALYSIS_PAYLOAD)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "created")
        self.assertEqual(
            self._profile_analysis_rows(self.profile_id),
            [(response.json()["analysis_id"], "completed", True)],
        )

    def test_concurrent_delete_cannot_commit_between_check_and_commit_on_failure(self):
        response = self._post_with_delete_attempt_after_check("not valid json{{{")

        self.assertEqual(response.status_code, 500)
        rows = self._profile_analysis_rows(self.profile_id)
        self.assertEqual([(status, current) for _, status, current in rows], [("failed", False)])


class MatchingInflightDeleteTests(_BaseInflightDeleteTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._create_user("inflight.match.owner@example.com")
        self.profile_id = self._create_profile(self.user_id)
        self.profile_analysis_id = self._seed_profile_analysis(self.profile_id)
        self.job_id = self._create_job("shared")

        # Another live profile with a current match against the same job.
        self.other_user_id = self._create_user("inflight.match.other@example.com")
        self.other_profile_id = self._create_profile(self.other_user_id)
        other_analysis_id = self._seed_profile_analysis(self.other_profile_id)
        self.other_match_id = self._seed_match(
            self.other_profile_id, other_analysis_id, self.job_id
        )

        self.headers = self._headers(self.user_id)

    def _match(self, side_effect, job_id=None):
        with patch(
            "backend.routers.matches.calculate_backend_match",
            side_effect=side_effect,
        ) as mock_calc:
            response = self.client.post(
                f"/users/{self.user_id}/profiles/{self.profile_id}"
                f"/jobs/{job_id or self.job_id}/match",
                params={"force_rematch": "true"},
                headers=self.headers,
            )
        mock_calc.assert_called_once()
        return response

    def _assert_other_match_untouched(self):
        self.assertEqual(
            self._match_rows(self.other_profile_id),
            [(self.other_match_id, self.job_id, "completed", True)],
        )

    def _delete_then_real(self, reuse=False, seed_new_match=False):
        def side_effect(**kwargs):
            self._delete_user_elsewhere(self.user_id)
            if reuse:
                self._recreate_same_ids_elsewhere(
                    self.user_id, self.profile_id, "inflight.match.next@example.com"
                )
                if seed_new_match:
                    new_analysis_id = self._seed_profile_analysis(self.profile_id)
                    self.new_match_id = self._seed_match(
                        self.profile_id, new_analysis_id, self.job_id
                    )
            return REAL_CALCULATE_BACKEND_MATCH(**kwargs)
        return side_effect

    def _delete_then_raise(self, reuse=False):
        def side_effect(**kwargs):
            self._delete_user_elsewhere(self.user_id)
            if reuse:
                self._recreate_same_ids_elsewhere(
                    self.user_id, self.profile_id, "inflight.match.next@example.com"
                )
            raise RuntimeError("synthetic match failure")
        return side_effect

    def test_success_after_delete_returns_404_and_writes_no_match(self):
        response = self._match(self._delete_then_real())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self.assertEqual(self._match_rows(self.profile_id), [])
        self._assert_other_match_untouched()

    def test_failure_after_delete_returns_404_and_writes_no_failed_match(self):
        response = self._match(self._delete_then_raise())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self.assertEqual(self._match_rows(self.profile_id), [])
        self._assert_other_match_untouched()

    def test_success_after_id_reuse_attaches_no_match_to_new_profile(self):
        response = self._match(self._delete_then_real(reuse=True))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self.assertEqual(self._match_rows(self.profile_id), [])
        self._assert_other_match_untouched()

    def test_success_after_id_reuse_keeps_new_profiles_current_match(self):
        response = self._match(self._delete_then_real(reuse=True, seed_new_match=True))

        self.assertEqual(response.status_code, 404)
        # The is_current flip for (profile_id, job_id) must be rolled back.
        self.assertEqual(
            self._match_rows(self.profile_id),
            [(self.new_match_id, self.job_id, "completed", True)],
        )
        self._assert_other_match_untouched()

    def test_failure_after_id_reuse_attaches_no_failed_match(self):
        response = self._match(self._delete_then_raise(reuse=True))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._match_rows(self.profile_id), [])

        listed = self.client.get(
            f"/users/{self.user_id}/profiles/{self.profile_id}/matches",
            headers=self._headers(self.user_id),
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total_count"], 0)

    def _expected_job_not_found(self):
        return {
            "detail": {
                "error_code": "ERR_JOB_NOT_FOUND",
                "message": f"Job with id {self.job_id} was not found.",
            }
        }

    def _delete_job_elsewhere(self, replacement_url=None, replacement_owner=None):
        session = self.session_factory()
        try:
            session.query(models.JobMatch).filter(
                models.JobMatch.job_id == self.job_id
            ).delete(synchronize_session=False)
            session.query(models.JobAnalysis).filter(
                models.JobAnalysis.job_id == self.job_id
            ).delete(synchronize_session=False)
            session.query(models.Job).filter(
                models.Job.job_id == self.job_id
            ).delete(synchronize_session=False)
            if replacement_url is not None:
                session.add(models.Job(
                    job_id=self.job_id,
                    title="Different posting",
                    url=replacement_url,
                    created_by_user_id=replacement_owner,
                ))
            session.commit()
        finally:
            session.close()

    def test_job_deleted_mid_match_returns_job_404_and_writes_no_match(self):
        def side_effect(**kwargs):
            self._delete_job_elsewhere()
            return REAL_CALCULATE_BACKEND_MATCH(**kwargs)

        response = self._match(side_effect)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), self._expected_job_not_found())
        self.assertEqual(self._all_match_rows(), [])

    def test_job_replaced_with_same_id_mid_match_returns_job_404(self):
        def side_effect(**kwargs):
            self._delete_job_elsewhere(replacement_url="https://example.com/job/other")
            return REAL_CALCULATE_BACKEND_MATCH(**kwargs)

        response = self._match(side_effect)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), self._expected_job_not_found())
        self.assertEqual(self._all_match_rows(), [])

    def test_job_no_longer_visible_to_owner_mid_match_returns_job_404(self):
        def side_effect(**kwargs):
            session = self.session_factory()
            try:
                session.query(models.Job).filter(
                    models.Job.job_id == self.job_id
                ).update({"created_by_user_id": self.other_user_id})
                session.commit()
            finally:
                session.close()
            raise RuntimeError("synthetic match failure")

        response = self._match(side_effect)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), self._expected_job_not_found())
        self.assertEqual(self._match_rows(self.profile_id), [])
        self._assert_other_match_untouched()

    def test_batch_delete_mid_run_stops_with_profile_404_and_writes_no_match(self):
        second_job_id = self._create_job("second")
        calls = []

        def side_effect(**kwargs):
            if not calls:
                self._delete_user_elsewhere(self.user_id)
            calls.append(kwargs["job"].job_id)
            return REAL_CALCULATE_BACKEND_MATCH(**kwargs)

        with patch(
            "backend.routers.matches.calculate_backend_match", side_effect=side_effect
        ):
            response = self.client.post(
                f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/match-analyzed",
                params={"force_rematch": "true"},
                headers=self.headers,
            )

        # The first job processed (highest job_id) hit the write guard; the
        # whole request then ends like a missing profile and no further job
        # is processed.
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self.assertEqual(calls, [second_job_id])
        self.assertEqual(self._match_rows(self.profile_id), [])
        self._assert_other_match_untouched()


class MatchingUnchangedBehaviorTests(_BaseInflightDeleteTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._create_user("inflight.match.live@example.com")
        self.profile_id = self._create_profile(self.user_id)
        self.profile_analysis_id = self._seed_profile_analysis(self.profile_id)
        self.job_id = self._create_job("live")
        self.headers = self._headers(self.user_id)

    def _post(self, **params):
        return self.client.post(
            f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/{self.job_id}/match",
            params=params,
            headers=self.headers,
        )

    def test_success_then_cache_then_force_rematch(self):
        first = self._post()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "created")
        first_id = first.json()["match_id"]

        cached = self._post()
        self.assertEqual(cached.status_code, 200)
        self.assertEqual(cached.json()["status"], "cached")
        self.assertEqual(cached.json()["match_id"], first_id)

        forced = self._post(force_rematch="true")
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(forced.json()["status"], "created")
        self.assertEqual(
            self._match_rows(self.profile_id),
            [
                (first_id, self.job_id, "completed", False),
                (forced.json()["match_id"], self.job_id, "completed", True),
            ],
        )

    def test_failure_still_writes_failed_match_for_live_profile(self):
        with patch(
            "backend.routers.matches.calculate_backend_match",
            side_effect=RuntimeError("synthetic match failure"),
        ):
            response = self._post()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["error_code"], "ERR_BACKEND_MATCH_FAILED")
        rows = self._match_rows(self.profile_id)
        self.assertEqual([(status, current) for _, _, status, current in rows], [("failed", False)])

    def test_batch_success_unchanged(self):
        response = self.client.post(
            f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/match-analyzed",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["matched_count"], 1)
        self.assertEqual(response.json()["results"][0]["status"], "created")

    def test_concurrent_delete_cannot_commit_between_check_and_commit(self):
        # Failure path: after the rollback, the failed row's flush inside
        # the check is the first write of the transaction.
        outcomes = []
        with patch(
            "backend.routers.matches.calculate_backend_match",
            side_effect=RuntimeError("synthetic match failure"),
        ), self._delete_attempt_right_after_write_check(outcomes):
            response = self._post()

        self.assertEqual(outcomes, ["locked"])
        self.assertEqual(response.status_code, 500)
        self._assert_profile_survived()
        rows = self._match_rows(self.profile_id)
        self.assertEqual([(status, current) for _, _, status, current in rows], [("failed", False)])


class OwnerReuseReadPathTests(_BaseInflightDeleteTestCase):
    """X's request is in flight when X is deleted and a different person Y
    gets the same user_id and profile_id (distinct mail, distinct token_key)
    and their own analyses/matches. Nothing of Y's -- cached analysis,
    cached match, listing rows, analysis ids/versions in error details --
    may reach X, nothing may be computed from or written to Y on X's
    behalf, and a batch stops at the first mismatch."""

    X_DESCRIPTION = "X-PRIVATE-DESCRIPTION"
    X_MAIL = "reuse.x@example.com"
    Y_MAIL = "reuse.y.next@example.com"
    Y_DESCRIPTION = f"{Y_MARKER} description"

    def setUp(self):
        super().setUp()
        self.user_id = self._create_user(self.X_MAIL)
        self.profile_id = self._create_profile(self.user_id, description=self.X_DESCRIPTION)
        self.x_analysis_id = self._seed_profile_analysis(self.profile_id)
        self.job_id = self._create_job("reuse-a")
        self.headers = self._headers(self.user_id)
        self.y_match_ids = []

    # --- the reuse, from separate committed sessions -----------------------

    def _reuse_by_y(self, y_analysis=None, y_match_jobs=()):
        self._delete_user_elsewhere(self.user_id)
        self._recreate_same_ids_elsewhere(
            self.user_id, self.profile_id, self.Y_MAIL, description=self.Y_DESCRIPTION
        )
        if y_analysis == "current":
            self.y_analysis_id = self._seed_profile_analysis(self.profile_id, summary=Y_MARKER)
        elif y_analysis == "stale":
            # Completed but not current: matching would report it as
            # latest_existing_analysis_* in its 400 detail.
            self.y_analysis_id = self._seed_profile_analysis(
                self.profile_id, is_current=False, summary=Y_MARKER
            )
        for job_id in y_match_jobs:
            self.y_match_ids.append(
                self._seed_match(self.profile_id, self.y_analysis_id, job_id,
                                 summary=Y_MARKER, score=87)
            )

    def _assert_nothing_of_y(self, response):
        self.assertNotIn(Y_MARKER, response.text)
        self.assertNotIn(self.Y_MAIL, response.text)
        self.assertNotIn("latest_existing_analysis", response.text)

    def _assert_generic_404(self, response):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), PROFILE_NOT_FOUND)
        self._assert_nothing_of_y(response)

    # --- profile analysis ----------------------------------------------------

    def _analyze(self, headers=None, **params):
        return self.client.post(
            f"/users/{self.user_id}/profiles/{self.profile_id}/analyze",
            params=params,
            headers=headers or self.headers,
        )

    def test_analysis_cached_path_after_reuse_before_capture_returns_404(self):
        self._inject_after_authorization(lambda: self._reuse_by_y(y_analysis="current"))

        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            response = self._analyze()

        self._assert_generic_404(response)
        mock_call.assert_not_called()

    def test_analysis_after_reuse_before_capture_makes_no_openai_call_or_write(self):
        self._inject_after_authorization(lambda: self._reuse_by_y())

        with patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            response = self._analyze(force_reanalyze="true")

        self._assert_generic_404(response)
        mock_call.assert_not_called()
        self.assertEqual(self._profile_analysis_rows(self.profile_id), [])

    def test_analysis_reuse_after_capture_while_building_prompt_makes_no_openai_call(self):
        real_build = profiles.build_languages_text

        def reuse_then_build(languages):
            self._reuse_by_y()
            return real_build(languages)

        with patch(
            "backend.routers.profiles.build_languages_text", side_effect=reuse_then_build
        ), patch("backend.routers.profiles._create_profile_analysis_response") as mock_call:
            response = self._analyze(force_reanalyze="true")

        self._assert_generic_404(response)
        mock_call.assert_not_called()
        self.assertEqual(self._profile_analysis_rows(self.profile_id), [])

    def test_admin_analysis_after_reuse_uses_only_the_current_owners_data(self):
        # An admin may analyze any user's profile, so after the reuse the
        # admin's request analyzes whoever owns the profile at capture --
        # but strictly from that owner's own data, never X's stale data.
        admin_id = self._create_user("reuse.admin@example.com", is_admin=True)
        self._inject_after_authorization(lambda: self._reuse_by_y())

        with patch(
            "backend.routers.profiles._create_profile_analysis_response",
            return_value=_openai_response(json.dumps(VALID_ANALYSIS_PAYLOAD)),
        ) as mock_call:
            response = self._analyze(headers=self._headers(admin_id), force_reanalyze="true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "created")
        prompt = mock_call.call_args.args[0]
        self.assertIn(self.Y_DESCRIPTION, prompt)
        self.assertIn(self.Y_MAIL, prompt)
        self.assertNotIn(self.X_DESCRIPTION, prompt)
        self.assertNotIn(self.X_MAIL, prompt)
        self.assertEqual(
            self._profile_analysis_rows(self.profile_id),
            [(response.json()["analysis_id"], "completed", True)],
        )

    # --- single match ----------------------------------------------------------

    def _match(self, **params):
        return self.client.post(
            f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/{self.job_id}/match",
            params=params,
            headers=self.headers,
        )

    def test_single_match_cached_path_after_reuse_before_capture_returns_404(self):
        self._inject_after_authorization(
            lambda: self._reuse_by_y(y_analysis="current", y_match_jobs=[self.job_id])
        )

        with patch("backend.routers.matches.calculate_backend_match") as mock_calc:
            response = self._match()

        self._assert_generic_404(response)
        mock_calc.assert_not_called()

    def test_single_match_after_reuse_before_capture_computes_and_writes_nothing(self):
        self._inject_after_authorization(lambda: self._reuse_by_y(y_analysis="current"))

        with patch("backend.routers.matches.calculate_backend_match") as mock_calc:
            response = self._match(force_rematch="true")

        self._assert_generic_404(response)
        mock_calc.assert_not_called()
        self.assertEqual(self._match_rows(self.profile_id), [])

    def test_single_match_400_detail_never_reveals_new_owners_analysis(self):
        self._inject_after_authorization(lambda: self._reuse_by_y(y_analysis="stale"))

        response = self._match()

        self._assert_generic_404(response)

    # --- bulk match ----------------------------------------------------------

    def _bulk_with_hook_before_second_job(self, hook, **params):
        calls = []

        def impl(**kwargs):
            calls.append(kwargs["job_id"])
            if len(calls) == 2:
                hook()
            return REAL_MATCH_IMPL(**kwargs)

        with patch("backend.routers.matches._match_profile_with_job_impl", side_effect=impl):
            response = self.client.post(
                f"/users/{self.user_id}/profiles/{self.profile_id}/jobs/match-analyzed",
                params=params,
                headers=self.headers,
            )
        return response, calls

    def test_bulk_never_returns_new_owners_cached_match_and_stops(self):
        job_b = self._create_job("reuse-b")
        job_c = self._create_job("reuse-c")
        # Processing order is job_id desc: job_c (created for X), then job_b
        # (where Y now has a cached current match), then self.job_id.
        response, calls = self._bulk_with_hook_before_second_job(
            lambda: self._reuse_by_y(
                y_analysis="current", y_match_jobs=[job_c, job_b, self.job_id]
            )
        )

        self._assert_generic_404(response)
        self.assertEqual(calls, [job_c, job_b])
        # X's job_c match went with X; only Y's own rows remain, untouched.
        self.assertEqual(
            [match_id for match_id, _, _, _ in self._match_rows(self.profile_id)],
            sorted(self.y_match_ids),
        )
        self.assertTrue(all(current for _, _, _, current in self._match_rows(self.profile_id)))

    def test_bulk_400_detail_never_reveals_new_owners_analysis(self):
        job_b = self._create_job("reuse-b")

        response, calls = self._bulk_with_hook_before_second_job(
            lambda: self._reuse_by_y(y_analysis="stale")
        )

        self._assert_generic_404(response)
        self.assertEqual(calls, [job_b, self.job_id])
        self.assertEqual(self._match_rows(self.profile_id), [])

    def _replace_job_elsewhere(self, job_id, replacement=None):
        session = self.session_factory()
        try:
            session.query(models.JobMatch).filter(
                models.JobMatch.job_id == job_id
            ).delete(synchronize_session=False)
            session.query(models.JobAnalysis).filter(
                models.JobAnalysis.job_id == job_id
            ).delete(synchronize_session=False)
            session.query(models.Job).filter(
                models.Job.job_id == job_id
            ).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()
        if replacement is not None:
            self._create_job(replacement, job_id=job_id)

    def _job_not_found_detail(self, job_id):
        return {
            "error_code": "ERR_JOB_NOT_FOUND",
            "message": f"Job with id {job_id} was not found.",
        }

    def test_bulk_job_deleted_mid_batch_is_a_per_job_error_from_snapshot(self):
        job_b = self._create_job("reuse-b")

        response, calls = self._bulk_with_hook_before_second_job(
            lambda: self._replace_job_elsewhere(self.job_id)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [job_b, self.job_id])
        by_job = {item["job_id"]: item for item in response.json()["results"]}
        self.assertEqual(by_job[job_b]["status"], "created")
        self.assertEqual(by_job[self.job_id]["status"], "failed")
        self.assertEqual(by_job[self.job_id]["title"], "Synthetic Job reuse-a")
        self.assertEqual(by_job[self.job_id]["error"], self._job_not_found_detail(self.job_id))

    def test_bulk_job_id_reused_by_other_job_is_not_matched_or_shown(self):
        job_b = self._create_job("reuse-b")

        response, calls = self._bulk_with_hook_before_second_job(
            lambda: self._replace_job_elsewhere(self.job_id, replacement="replacement-posting")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [job_b, self.job_id])
        by_job = {item["job_id"]: item for item in response.json()["results"]}
        self.assertEqual(by_job[self.job_id]["status"], "failed")
        self.assertEqual(by_job[self.job_id]["error"], self._job_not_found_detail(self.job_id))
        self.assertEqual(by_job[self.job_id]["title"], "Synthetic Job reuse-a")
        self.assertNotIn("replacement-posting", response.text)
        self.assertEqual(
            [job_id for _, job_id, _, _ in self._match_rows(self.profile_id)], [job_b]
        )

    # --- match listing ---------------------------------------------------------

    def _list(self, headers=None):
        return self.client.get(
            f"/users/{self.user_id}/profiles/{self.profile_id}/matches",
            headers=headers or self.headers,
        )

    def test_listing_after_reuse_before_capture_returns_404(self):
        self._seed_match(self.profile_id, self.x_analysis_id, self.job_id)
        self._inject_after_authorization(
            lambda: self._reuse_by_y(y_analysis="current", y_match_jobs=[self.job_id])
        )

        response = self._list()

        self._assert_generic_404(response)

    def test_admin_listing_with_reuse_after_capture_returns_404(self):
        admin_id = self._create_user("reuse.admin.list@example.com", is_admin=True)
        admin_headers = self._headers(admin_id)
        real_clause = matches.visible_jobs_clause_for_owner
        reused = []

        def reuse_then_clause(owner_user_id):
            # Built after capture and before the rows are read.
            if not reused:
                reused.append(True)
                self._reuse_by_y(y_analysis="current", y_match_jobs=[self.job_id])
            return real_clause(owner_user_id)

        with patch(
            "backend.routers.matches.visible_jobs_clause_for_owner",
            side_effect=reuse_then_clause,
        ):
            response = self._list(headers=admin_headers)

        self.assertEqual(reused, [True])
        self._assert_generic_404(response)

    def test_listing_unchanged_for_live_owner_and_admin(self):
        match_id = self._seed_match(self.profile_id, self.x_analysis_id, self.job_id)
        admin_id = self._create_user("reuse.admin.live@example.com", is_admin=True)

        for headers in (self.headers, self._headers(admin_id)):
            response = self._list(headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total_count"], 1)
            self.assertEqual(response.json()["results"][0]["match_id"], match_id)


if __name__ == "__main__":
    unittest.main()
