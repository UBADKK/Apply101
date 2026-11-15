"""Retire the fake provider's analyses and the matches built on them.

    python -m backend.scripts.retire_fake_analyses          # dry run
    python -m backend.scripts.retire_fake_analyses --yes    # apply

On the Lightsail deployment (back up first; run the two commands after it
right after the provider switch and ctl.sh up, see "Order" below):

    sudo deploy/lightsail/ctl.sh backup
    sudo deploy/lightsail/ctl.sh exec python -m backend.scripts.retire_fake_analyses
    sudo deploy/lightsail/ctl.sh exec python -m backend.scripts.retire_fake_analyses --yes

The fake provider (APPLY101_ANALYSIS_PROVIDER=fake, backend/app/fake_analysis.py)
stores its analyses with the same analysis_model and analysis_prompt_version
as the real one, so after a switch to the real provider on the same database
they would still be served as cached analyses and used for matching. It does
not switch the provider; that is a separate configuration change. Order:

1. Back up (sudo deploy/lightsail/ctl.sh backup). A dry run beforehand
   shows the counts and what will need re-analysis.
2. Switch the provider configuration and run ctl.sh up; right after that,
   in a quiet window, run the dry run and then --yes. ctl.sh exec needs the
   running app, so a --yes before the switch is harmless but not enough:
   anything the fake provider analyzes or matches afterwards stays current.
   Until --yes has run after the switch, cached fake analyses can still be
   served.

Afterwards the affected profiles need a new profile analysis, and the
affected catalog jobs drop out of recommendations ("Jobs for me",
catalog._current_job_analysis_filters) until analyzed again (admin
POST /jobs/analyze-missing); profiles_needing_reanalysis and
jobs_needing_reanalysis in the report list them.

Rules (classification in fake_analysis.classify_profile_analysis,
classify_job_analysis and is_fake_cover_letter_grounding):

- A profile analysis is fake when it is completed, its candidate_summary
  column and the candidate_summary in analysis_json are FAKE_ANALYSIS_SUMMARY
  and match_notes in analysis_json is exactly [FAKE_ANALYSIS_NOTE].
- A job analysis is fake when it is completed, the summary in analysis_json
  is FAKE_ANALYSIS_SUMMARY, and role_family (column and JSON),
  role_subfamily and role_tags are "other", "other" and ["other"].
- An analysis that is not fake but contains either marker text is
  ambiguous: never changed, only reported.
- With --yes, in one transaction: current fake profile and job analyses get
  is_current = 0, and so does every current match whose profile_analysis_id
  or job_analysis_id is any fake analysis (current or not). Each UPDATE only
  touches rows that are still current, and the reported counts are its
  rowcounts.
- Nothing is deleted and no other column or table changes: users, profiles
  (CV text and files), jobs, cover letters and their versions, guards,
  quotas, fetch runs and the catalog stay as they are, and the upload
  directory is never read. Current matches on an ambiguous analysis or with
  a missing analysis id (and no fake one) stay current and are reported.
  Cover letter versions written by the fake provider are only counted.
- A second run with --yes retires nothing.

The report holds ids and counts only. It works on APPLY101_DATABASE_URL
from the process environment only (never .env), refuses the repository's
real apply101.db and ./apply101.db in the working directory, and never
creates a database. Without --yes the database is opened read-only.
Exit code 0 after a dry run or a successful apply (also when ambiguous rows
exist), 1 on a refusal or a database error; a failed apply is rolled back.
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from backend.app import config
from backend.app.fake_analysis import (
    ANALYSIS_AMBIGUOUS,
    ANALYSIS_FAKE,
    classify_job_analysis,
    classify_profile_analysis,
    is_fake_cover_letter_grounding,
)


EXIT_SUCCESS = 0
EXIT_FAILURE = 1
# Ids per IN (...) list in an UPDATE; below SQLite's variable limit.
UPDATE_CHUNK_SIZE = 500


class RetireError(RuntimeError):
    pass


def _target(environ) -> Path:
    url = (environ.get(config.DATABASE_URL_ENV) or "").strip()
    if not url:
        raise RetireError(f"Set {config.DATABASE_URL_ENV}; the default is the real local data.")
    config._reject_sqlite_uri(url)
    database = config._sqlite_file(url)
    if database is None:
        raise RetireError(f"{config.DATABASE_URL_ENV} must be a file-backed SQLite URL.")
    database = Path(os.path.abspath(database))
    print(f"Database: {database}")
    if config._same_path(database, config.REPO_ROOT / config.DEFAULT_DATABASE_FILE) or config._same_path(
        database, config.DEFAULT_DATABASE_FILE
    ):
        raise RetireError("Refusing the repository's real apply101.db or ./apply101.db in the working directory.")
    if not database.is_file():
        raise RetireError(f"The database {database} does not exist.")
    return database


def _engine(database: Path, *, writable: bool):
    # mode=ro cannot write; mode=rw cannot create a missing file. The
    # driver's own transaction handling is off, so the explicit BEGIN below
    # makes the reads and the updates one transaction.
    uri = f"{database.as_uri()}?mode={'rw' if writable else 'ro'}"
    return create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(uri, uri=True, isolation_level=None),
        poolclass=NullPool,
    )


def _chunks(ids):
    ids = sorted(ids)
    for start in range(0, len(ids), UPDATE_CHUNK_SIZE):
        yield ids[start:start + UPDATE_CHUNK_SIZE]


def _retire(conn, table, id_column, ids) -> int:
    count = 0
    for chunk in _chunks(ids):
        result = conn.execute(
            update(table)
            .where(id_column.in_(chunk), table.c.is_current == True)  # noqa: E712
            .values(is_current=False)
        )
        count += result.rowcount
    return count


def _analyses(conn, table, owner, classify, columns):
    """The classification and owner (profile_id / job_id) of every row by
    analysis_id, and the ids of the current and current completed rows."""
    kinds, current, owners, completed = {}, set(), {}, set()
    rows = conn.execute(select(
        table.c.analysis_id, table.c[owner], table.c.is_current, table.c.analysis_status,
        *(table.c[name] for name in columns),
    ))
    for row in rows:
        analysis_id, owner_id, is_current, status = row[0], row[1], row[2], row[3]
        kinds[analysis_id] = classify(status, *row[4:])
        owners[analysis_id] = owner_id
        if is_current:
            current.add(analysis_id)
            if status == "completed":
                completed.add(analysis_id)
    return kinds, current, owners, completed


def _analysis_report(kinds, current, owners, completed):
    fake = {aid for aid, kind in kinds.items() if kind == ANALYSIS_FAKE}
    to_retire = fake & current
    remaining_owners = {owners[aid] for aid in completed - fake}
    return fake, to_retire, {
        "current_fake_to_retire": len(to_retire),
        "ambiguous_ids": sorted(aid for aid, kind in kinds.items() if kind == ANALYSIS_AMBIGUOUS),
        "other_current_untouched": len(current - fake),
    }, sorted({owners[aid] for aid in fake} - remaining_owners)


def run(database: Path, *, apply: bool) -> dict:
    # Imported here, after the target was checked: backend.app.database
    # reads the process environment on import.
    from backend.app import models

    profile_table = models.ProfileAnalysis.__table__
    job_table = models.JobAnalysis.__table__
    match_table = models.JobMatch.__table__
    version_table = models.CoverLetterVersion.__table__

    engine = _engine(database, writable=apply)
    try:
        with engine.connect() as conn:
            # IMMEDIATE takes the write lock up front, so nothing changes
            # between the classification and the updates.
            conn.exec_driver_sql("BEGIN IMMEDIATE" if apply else "BEGIN")
            try:
                p_fake, p_retire, p_report, profiles_needing = _analysis_report(*_analyses(
                    conn, profile_table, "profile_id", classify_profile_analysis, ("candidate_summary", "analysis_json"),
                ))
                j_fake, j_retire, j_report, jobs_needing = _analysis_report(*_analyses(
                    conn, job_table, "job_id", classify_job_analysis, ("analysis_json", "role_family"),
                ))
                p_ambiguous, j_ambiguous = set(p_report["ambiguous_ids"]), set(j_report["ambiguous_ids"])

                m_retire, m_ambiguous, m_missing = set(), [], []
                for match_id, profile_analysis_id, job_analysis_id in conn.execute(
                    select(match_table.c.match_id, match_table.c.profile_analysis_id, match_table.c.job_analysis_id)
                    .where(match_table.c.is_current == True)  # noqa: E712
                ):
                    if profile_analysis_id in p_fake or job_analysis_id in j_fake:
                        m_retire.add(match_id)
                        continue
                    if profile_analysis_id in p_ambiguous or job_analysis_id in j_ambiguous:
                        m_ambiguous.append(match_id)
                    if profile_analysis_id is None or job_analysis_id is None:
                        m_missing.append(match_id)

                fake_letters = sum(
                    1 for (grounding,) in conn.execute(select(version_table.c.grounding_json))
                    if is_fake_cover_letter_grounding(grounding)
                )

                report = {
                    "mode": "apply" if apply else "dry_run",
                    "target": str(database),
                    "profile_analyses": p_report,
                    "job_analyses": j_report,
                    "matches": {
                        "current_to_retire": len(m_retire),
                        "ambiguous_untouched_ids": sorted(m_ambiguous),
                        "missing_analysis_id_untouched_ids": sorted(m_missing),
                    },
                    "cover_letter_versions_with_fake_marker": fake_letters,
                    # Have a fake analysis and no current completed analysis
                    # left after retirement.
                    "profiles_needing_reanalysis": profiles_needing,
                    "jobs_needing_reanalysis": jobs_needing,
                }
                if apply:
                    report["retired"] = {
                        "profile_analyses": _retire(conn, profile_table, profile_table.c.analysis_id, p_retire),
                        "job_analyses": _retire(conn, job_table, job_table.c.analysis_id, j_retire),
                        "matches": _retire(conn, match_table, match_table.c.match_id, m_retire),
                    }
                    conn.commit()
                else:
                    conn.rollback()
            except BaseException:
                conn.rollback()
                raise
    finally:
        engine.dispose()
    return report


def main(argv=None, environ=None) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Retire the fake provider's analyses and their matches")
    parser.add_argument("--yes", action="store_true", help="Apply; without it this is a read-only dry run.")
    args = parser.parse_args(argv)
    try:
        database = _target(environ)
        report = run(database, apply=args.yes)
    except (RetireError, config.ConfigError, sqlite3.Error, SQLAlchemyError, OSError) as exc:
        print(f"Refusing to continue: {exc}")
        return EXIT_FAILURE
    print("APPLIED" if args.yes else "DRY RUN: nothing was written")
    print(json.dumps(report, indent=2))
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
