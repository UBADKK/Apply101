import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app import models
from backend.app.database import Base, get_db
from backend.app.security import create_access_token, hash_password

# profiles.py constructs an OpenAI client at import time (`client = OpenAI()`),
# which raises immediately if no API key is configured. OPENAI_API_KEY is
# synthetic only for the duration of this import -- patch.dict restores the
# real environment immediately afterward, matching the pattern used in
# test_authorization_users_profiles.py.
SYNTHETIC_OPENAI_API_KEY = "sk-synthetic-test-key-not-a-real-key"

with patch.dict(
    os.environ,
    {"OPENAI_API_KEY": SYNTHETIC_OPENAI_API_KEY},
    clear=False,
):
    from backend.routers import profiles, users

SYNTHETIC_SECRET = "synthetic-test-secret-for-upload-security-0123456789"
VALID_PASSWORD = "a-valid-synthetic-password"


def build_test_app(engine):
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(users.router)
    app.include_router(profiles.router)
    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def make_minimal_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# Captured at module import time, before any test patches tempfile, so the
# write-failure proxy below can still create a real underlying temp file.
_ORIGINAL_NAMED_TEMPORARY_FILE = tempfile.NamedTemporaryFile


class _WriteFailsTempFileProxy:
    """Stands in for the object tempfile.NamedTemporaryFile(...) normally
    returns. Forwards everything except write(), which raises to simulate
    an unexpected disk/IO failure partway through streaming an upload.
    """

    def __init__(self, real_file):
        self._real_file = real_file
        self.name = real_file.name
        self.closed = False

    def write(self, data):
        raise OSError("simulated disk write failure during CV upload")

    def close(self):
        self.closed = True
        self._real_file.close()


def _make_write_failing_temp_file(*args, **kwargs):
    real_file = _ORIGINAL_NAMED_TEMPORARY_FILE(*args, **kwargs)
    return _WriteFailsTempFileProxy(real_file)


# Captured at module import time, before any test patches os.replace.
_ORIGINAL_OS_REPLACE = os.replace


def _make_install_step_failing_os_replace():
    """Lets the "move existing final CV -> backup" and "restore backup ->
    final" replace() calls through untouched, but raises OSError on the
    "move temp -> final" install step specifically -- identified by the
    source path's ".cv_upload_" prefix, which only the upload temp file
    uses (the backup file uses ".cv_backup_").
    """

    def _replace(src, dst, *args, **kwargs):
        if ".cv_upload_" in os.path.basename(src):
            raise OSError("simulated install failure")
        return _ORIGINAL_OS_REPLACE(src, dst, *args, **kwargs)

    return _replace


class _BaseUploadSecurityTestCase(unittest.TestCase):
    """Shared setup: a throwaway synthetic SQLite database (never
    apply101.db), only users.router + profiles.router mounted, a synthetic
    JWT secret, and a temporary chdir so the router's relative "uploads"
    directory never touches the real repo's uploads/.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="apply101_upload_security_test_")
        db_path = os.path.join(self.tmp_dir, "synthetic_test.db")
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)

        self.app, self.session_factory = build_test_app(self.engine)
        self.client = TestClient(self.app)

        env_patcher = patch.dict(
            os.environ, {"JWT_SECRET_KEY": SYNTHETIC_SECRET}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        # The router uses a relative "uploads" directory -- chdir into a
        # fresh temp workdir (nested, so ../.. escapes stay observable and
        # contained) so no request in this test file can ever touch the
        # real repository's uploads/ directory.
        self.workdir = os.path.join(self.tmp_dir, "workdir_root", "app_run_dir")
        os.makedirs(self.workdir, exist_ok=True)
        self._original_cwd = os.getcwd()
        os.chdir(self.workdir)

    def tearDown(self):
        os.chdir(self._original_cwd)
        self.engine.dispose()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_user(self, mail, password=VALID_PASSWORD, name="Synthetic User"):
        session = self.session_factory()
        try:
            user = models.User(
                name=name,
                mail=mail,
                password_hash=hash_password(password) if password else None,
                is_admin=False,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user.user_id
        finally:
            session.close()

    def _create_profile(self, user_id, self_description="synthetic profile"):
        session = self.session_factory()
        try:
            profile = models.CandidateProfile(
                user_id=user_id, self_description=self_description
            )
            session.add(profile)
            session.commit()
            session.refresh(profile)
            return profile.profile_id
        finally:
            session.close()

    def _get_profile_row(self, profile_id):
        session = self.session_factory()
        try:
            return (
                session.query(models.CandidateProfile)
                .filter(models.CandidateProfile.profile_id == profile_id)
                .first()
            )
        finally:
            session.close()

    def _auth_headers(self, user_id):
        token = create_access_token(user_id)
        return {"Authorization": f"Bearer {token}"}

    def _all_files_under_tmp_dir(self):
        found = []
        for dirpath, _dirnames, filenames in os.walk(self.tmp_dir):
            for name in filenames:
                found.append(os.path.relpath(os.path.join(dirpath, name), self.tmp_dir))
        return found

    def _uploads_dir(self):
        # Absolute path, safe for actual filesystem operations in the test
        # (cwd is fixed at self.workdir for the whole test lifetime).
        return os.path.join(self.workdir, "uploads")

    def _expected_final_path(self, user_id, profile_id):
        # Absolute, for opening/checking the file on disk from the test.
        return os.path.join(self._uploads_dir(), f"user_{user_id}_profile_{profile_id}_cv.pdf")

    def _expected_relative_final_path(self, user_id, profile_id):
        # The route stores a relative "uploads/..." path (unchanged, pre-
        # existing behavior) -- this is what the JSON response / DB row
        # should actually contain.
        return os.path.join("uploads", f"user_{user_id}_profile_{profile_id}_cv.pdf")

    def _upload(self, user_id, profile_id, filename, content, headers=None):
        return self.client.post(
            f"/users/{user_id}/profiles/{profile_id}/upload-cv",
            files={"file": (filename, content, "application/pdf")},
            headers=headers if headers is not None else self._auth_headers(user_id),
        )


class PathTraversalRejectionTests(_BaseUploadSecurityTestCase):
    TRAVERSAL_PAYLOADS = [
        "../../../outside.pdf",
        "/../../outside.pdf",
        "..\\..\\..\\outside.pdf",
        "\\..\\..\\outside.pdf",
        "subdir/../../outside.pdf",
        "subdir\\..\\..\\outside.pdf",
    ]

    def test_traversal_payloads_are_rejected_and_create_no_file_anywhere(self):
        user_id = self._create_user("traversal.owner@example.com")
        profile_id = self._create_profile(user_id)

        for payload in self.TRAVERSAL_PAYLOADS:
            with self.subTest(payload=payload):
                db_files_before = self._all_files_under_tmp_dir()

                response = self._upload(user_id, profile_id, payload, b"irrelevant bytes")

                self.assertEqual(response.status_code, 400, response.text)
                files_after = self._all_files_under_tmp_dir()
                # Only the synthetic db file(s) may exist -- no upload
                # artifact anywhere under the whole temp tree.
                self.assertEqual(
                    set(files_after) - set(db_files_before),
                    set(),
                    f"payload {payload!r} created unexpected files: "
                    f"{set(files_after) - set(db_files_before)}",
                )
                if os.path.isdir(self._uploads_dir()):
                    self.assertEqual(os.listdir(self._uploads_dir()), [])


class ValidUploadTests(_BaseUploadSecurityTestCase):
    def test_valid_filename_succeeds_and_uses_server_generated_path_only(self):
        user_id = self._create_user("valid.upload@example.com")
        profile_id = self._create_profile(user_id)
        pdf_bytes = make_minimal_pdf_bytes()

        response = self._upload(user_id, profile_id, "cv.pdf", pdf_bytes)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["cv_filename"], "cv.pdf")

        expected_final_path = self._expected_final_path(user_id, profile_id)
        expected_relative_path = self._expected_relative_final_path(user_id, profile_id)
        self.assertEqual(os.path.normpath(body["cv_file_path"]), os.path.normpath(expected_relative_path))
        self.assertTrue(os.path.exists(expected_final_path))

        # The client filename ("cv.pdf") must not appear anywhere in the
        # actual on-disk path segments beyond being identical to the fixed
        # "_cv.pdf" suffix the server always generates.
        uploads_dir = self._uploads_dir()
        entries = os.listdir(uploads_dir)
        self.assertEqual(entries, [f"user_{user_id}_profile_{profile_id}_cv.pdf"])

        profile_row = self._get_profile_row(profile_id)
        self.assertEqual(profile_row.cv_filename, "cv.pdf")
        self.assertEqual(os.path.normpath(profile_row.cv_file_path), os.path.normpath(expected_relative_path))


class OversizedUploadTests(_BaseUploadSecurityTestCase):
    def test_oversized_upload_is_rejected_with_413_and_leaves_no_trace(self):
        user_id = self._create_user("oversized.owner@example.com")
        profile_id = self._create_profile(user_id)

        oversized_body = b"%PDF-1.4 " + (b"A" * (profiles.MAX_CV_UPLOAD_BYTES + 1))

        response = self._upload(user_id, profile_id, "big.pdf", oversized_body)

        self.assertEqual(response.status_code, 413, response.text)
        self.assertFalse(os.path.exists(self._expected_final_path(user_id, profile_id)))
        uploads_dir = self._uploads_dir()
        if os.path.isdir(uploads_dir):
            self.assertEqual(os.listdir(uploads_dir), [])

        profile_row = self._get_profile_row(profile_id)
        self.assertIsNone(profile_row.cv_filename)
        self.assertIsNone(profile_row.cv_file_path)
        self.assertIsNone(profile_row.cv_text)


class MalformedPdfTests(_BaseUploadSecurityTestCase):
    def test_malformed_pdf_is_rejected_with_400_and_leaves_no_trace(self):
        user_id = self._create_user("malformed.owner@example.com")
        profile_id = self._create_profile(user_id)

        response = self._upload(
            user_id, profile_id, "garbage.pdf", b"this is not a real pdf file at all"
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"], "Invalid or unreadable PDF file.")
        self.assertFalse(os.path.exists(self._expected_final_path(user_id, profile_id)))
        uploads_dir = self._uploads_dir()
        if os.path.isdir(uploads_dir):
            self.assertEqual(os.listdir(uploads_dir), [])

        profile_row = self._get_profile_row(profile_id)
        self.assertIsNone(profile_row.cv_filename)
        self.assertIsNone(profile_row.cv_file_path)
        self.assertIsNone(profile_row.cv_text)


class FailedReplacementPreservesPreviousCvTests(_BaseUploadSecurityTestCase):
    def test_failed_second_upload_preserves_original_valid_cv(self):
        user_id = self._create_user("preserve.owner@example.com")
        profile_id = self._create_profile(user_id)
        good_pdf_bytes = make_minimal_pdf_bytes()

        first_response = self._upload(user_id, profile_id, "resume.pdf", good_pdf_bytes)
        self.assertEqual(first_response.status_code, 200, first_response.text)

        final_path = self._expected_final_path(user_id, profile_id)
        with open(final_path, "rb") as fh:
            original_bytes_on_disk = fh.read()
        profile_row_before = self._get_profile_row(profile_id)
        original_cv_filename = profile_row_before.cv_filename
        original_cv_file_path = profile_row_before.cv_file_path
        original_cv_text = profile_row_before.cv_text

        second_response = self._upload(
            user_id, profile_id, "garbage.pdf", b"not a real pdf"
        )
        self.assertEqual(second_response.status_code, 400, second_response.text)

        with open(final_path, "rb") as fh:
            bytes_after_failed_upload = fh.read()
        self.assertEqual(bytes_after_failed_upload, original_bytes_on_disk)

        profile_row_after = self._get_profile_row(profile_id)
        self.assertEqual(profile_row_after.cv_filename, original_cv_filename)
        self.assertEqual(profile_row_after.cv_file_path, original_cv_file_path)
        self.assertEqual(profile_row_after.cv_text, original_cv_text)

        # No leftover temp file.
        uploads_dir = self._uploads_dir()
        self.assertEqual(
            os.listdir(uploads_dir), [f"user_{user_id}_profile_{profile_id}_cv.pdf"]
        )


class SuccessfulReplacementTests(_BaseUploadSecurityTestCase):
    def test_second_valid_upload_cleanly_replaces_the_first(self):
        user_id = self._create_user("replace.owner@example.com")
        profile_id = self._create_profile(user_id)

        first_pdf_bytes = make_minimal_pdf_bytes()
        first_response = self._upload(user_id, profile_id, "first.pdf", first_pdf_bytes)
        self.assertEqual(first_response.status_code, 200, first_response.text)
        final_path = self._expected_final_path(user_id, profile_id)
        with open(final_path, "rb") as fh:
            first_bytes_on_disk = fh.read()

        second_pdf_bytes = make_minimal_pdf_bytes() + b"\n% distinguishing trailer padding for test\n"
        # PdfWriter output is deterministic-ish per call; make the second
        # payload byte-distinguishable regardless by appending harmless
        # trailing bytes many PDF readers tolerate being ignored/appended.
        second_response = self._upload(user_id, profile_id, "second.pdf", second_pdf_bytes)
        self.assertEqual(second_response.status_code, 200, second_response.text)

        self.assertEqual(
            os.path.normpath(second_response.json()["cv_file_path"]),
            os.path.normpath(self._expected_relative_final_path(user_id, profile_id)),
        )
        self.assertEqual(second_response.json()["cv_filename"], "second.pdf")

        with open(final_path, "rb") as fh:
            second_bytes_on_disk = fh.read()
        self.assertNotEqual(second_bytes_on_disk, first_bytes_on_disk)

        uploads_dir = self._uploads_dir()
        self.assertEqual(
            os.listdir(uploads_dir), [f"user_{user_id}_profile_{profile_id}_cv.pdf"]
        )

        profile_row = self._get_profile_row(profile_id)
        self.assertEqual(profile_row.cv_filename, "second.pdf")


class CrossUserUploadStillDeniedTests(_BaseUploadSecurityTestCase):
    def test_cross_user_upload_is_still_denied_and_creates_no_file(self):
        attacker_id = self._create_user("upload.attacker@example.com")
        victim_id = self._create_user("upload.victim@example.com")
        victim_profile_id = self._create_profile(victim_id)

        response = self._upload(
            victim_id,
            victim_profile_id,
            "resume.pdf",
            make_minimal_pdf_bytes(),
            headers=self._auth_headers(attacker_id),
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(os.path.isdir(self._uploads_dir()))


class CommitFailureTests(_BaseUploadSecurityTestCase):
    def test_commit_failure_with_existing_valid_cv_preserves_original(self):
        user_id = self._create_user("commit.failure.existing@example.com")
        profile_id = self._create_profile(user_id)

        first_response = self._upload(user_id, profile_id, "first.pdf", make_minimal_pdf_bytes())
        self.assertEqual(first_response.status_code, 200, first_response.text)

        final_path = self._expected_final_path(user_id, profile_id)
        with open(final_path, "rb") as fh:
            original_bytes_on_disk = fh.read()
        profile_row_before = self._get_profile_row(profile_id)
        original_cv_filename = profile_row_before.cv_filename
        original_cv_file_path = profile_row_before.cv_file_path
        original_cv_text = profile_row_before.cv_text

        second_pdf_bytes = make_minimal_pdf_bytes() + b"\n% distinguishing trailer padding\n"
        with patch.object(Session, "commit", side_effect=RuntimeError("forced commit failure")):
            second_response = self._upload(user_id, profile_id, "second.pdf", second_pdf_bytes)

        self.assertEqual(second_response.status_code, 500, second_response.text)
        self.assertEqual(
            second_response.json()["detail"],
            "Failed to save the uploaded CV. Please try again.",
        )
        # No raw exception text ("forced commit failure") leaked to the client.
        self.assertNotIn("forced commit failure", second_response.text)

        with open(final_path, "rb") as fh:
            bytes_after_failed_commit = fh.read()
        self.assertEqual(bytes_after_failed_commit, original_bytes_on_disk)

        profile_row_after = self._get_profile_row(profile_id)
        self.assertEqual(profile_row_after.cv_filename, original_cv_filename)
        self.assertEqual(profile_row_after.cv_file_path, original_cv_file_path)
        self.assertEqual(profile_row_after.cv_text, original_cv_text)

        # Only the restored final CV remains -- no temp/backup file left over.
        uploads_dir = self._uploads_dir()
        self.assertEqual(
            os.listdir(uploads_dir), [f"user_{user_id}_profile_{profile_id}_cv.pdf"]
        )

    def test_commit_failure_with_no_previous_cv_leaves_nothing_behind(self):
        user_id = self._create_user("commit.failure.none@example.com")
        profile_id = self._create_profile(user_id)

        with patch.object(Session, "commit", side_effect=RuntimeError("forced commit failure")):
            response = self._upload(user_id, profile_id, "cv.pdf", make_minimal_pdf_bytes())

        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotIn("forced commit failure", response.text)

        self.assertFalse(os.path.exists(self._expected_final_path(user_id, profile_id)))
        uploads_dir = self._uploads_dir()
        if os.path.isdir(uploads_dir):
            self.assertEqual(os.listdir(uploads_dir), [])

        profile_row = self._get_profile_row(profile_id)
        self.assertIsNone(profile_row.cv_filename)
        self.assertIsNone(profile_row.cv_file_path)
        self.assertIsNone(profile_row.cv_text)


class StreamingWriteFailureTests(_BaseUploadSecurityTestCase):
    def test_unexpected_write_failure_closes_temp_handle_and_leaves_no_files(self):
        user_id = self._create_user("stream.write.failure@example.com")
        profile_id = self._create_profile(user_id)

        with patch(
            "backend.routers.profiles.tempfile.NamedTemporaryFile",
            side_effect=_make_write_failing_temp_file,
        ):
            with self.assertRaises(OSError):
                self._upload(user_id, profile_id, "cv.pdf", make_minimal_pdf_bytes())

        uploads_dir = self._uploads_dir()
        if os.path.isdir(uploads_dir):
            self.assertEqual(os.listdir(uploads_dir), [])
        self.assertFalse(os.path.exists(self._expected_final_path(user_id, profile_id)))

        profile_row = self._get_profile_row(profile_id)
        self.assertIsNone(profile_row.cv_filename)
        self.assertIsNone(profile_row.cv_file_path)
        self.assertIsNone(profile_row.cv_text)


class InstallStepFailureAfterBackupTests(_BaseUploadSecurityTestCase):
    def test_install_failure_after_backup_created_restores_original_cv(self):
        user_id = self._create_user("install.failure.after.backup@example.com")
        profile_id = self._create_profile(user_id)

        first_response = self._upload(user_id, profile_id, "first.pdf", make_minimal_pdf_bytes())
        self.assertEqual(first_response.status_code, 200, first_response.text)

        final_path = self._expected_final_path(user_id, profile_id)
        with open(final_path, "rb") as fh:
            original_bytes_on_disk = fh.read()
        profile_row_before = self._get_profile_row(profile_id)
        original_cv_filename = profile_row_before.cv_filename
        original_cv_file_path = profile_row_before.cv_file_path
        original_cv_text = profile_row_before.cv_text

        second_pdf_bytes = make_minimal_pdf_bytes() + b"\n% distinguishing trailer padding\n"
        with patch(
            "backend.routers.profiles.os.replace",
            side_effect=_make_install_step_failing_os_replace(),
        ):
            second_response = self._upload(user_id, profile_id, "second.pdf", second_pdf_bytes)

        self.assertEqual(second_response.status_code, 500, second_response.text)
        self.assertEqual(
            second_response.json()["detail"],
            "Failed to save the uploaded CV. Please try again.",
        )

        # Original CV A is still present, at the normal final path, with
        # identical bytes and unchanged DB fields.
        self.assertTrue(os.path.exists(final_path))
        with open(final_path, "rb") as fh:
            bytes_after_failed_install = fh.read()
        self.assertEqual(bytes_after_failed_install, original_bytes_on_disk)

        profile_row_after = self._get_profile_row(profile_id)
        self.assertEqual(profile_row_after.cv_filename, original_cv_filename)
        self.assertEqual(profile_row_after.cv_file_path, original_cv_file_path)
        self.assertEqual(profile_row_after.cv_text, original_cv_text)

        # No .cv_upload_* or .cv_backup_* leftovers -- only the restored
        # final CV remains.
        uploads_dir = self._uploads_dir()
        self.assertEqual(
            os.listdir(uploads_dir), [f"user_{user_id}_profile_{profile_id}_cv.pdf"]
        )


if __name__ == "__main__":
    unittest.main()
