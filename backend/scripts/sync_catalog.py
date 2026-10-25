"""Operator command for the shared job catalog (admin-only by nature: it
needs server/shell access, like set_admin).

    python -m backend.scripts.sync_catalog seed
    python -m backend.scripts.sync_catalog check --board-id 3
    python -m backend.scripts.sync_catalog check --unverified --max-boards 10
    python -m backend.scripts.sync_catalog sync --max-boards 5 --max-pages 5 [--include-arbeitnow]
    python -m backend.scripts.sync_catalog summary

It works on APPLY101_DATABASE_URL (process environment only, never .env),
prints the target first, refuses a SQLite file that does not exist, and
writes only with --yes (seed, sync and check record results). Bounded by
the same limits as POST /admin/catalog/sync.
"""

import argparse
import json
import os
import sys

from sqlalchemy.orm import sessionmaker

from backend.app import catalog_sync, models
from backend.app.database import engine as default_engine
from backend.app.job_sources import HttpClient


EXIT_SUCCESS = 0
EXIT_FAILURE = 1


def _target_ok(engine) -> bool:
    print(f"Target database: {engine.url.render_as_string(hide_password=True)}")
    if engine.url.get_backend_name() == "sqlite":
        path = engine.url.database
        if not path or not os.path.isfile(os.path.abspath(path)):
            print("Refusing to continue: the SQLite database file does not exist. This tool never creates one.")
            return False
    return True


def main(argv=None, engine=default_engine, http_factory=HttpClient) -> int:
    parser = argparse.ArgumentParser(description="Shared job catalog sync")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="Add missing boards from company_boards.json (disabled).").add_argument("--yes", action="store_true")
    check = sub.add_parser("check", help="Read boards live and record whether they could be read.")
    check.add_argument("--board-id", type=int, action="append")
    check.add_argument("--unverified", action="store_true")
    check.add_argument("--max-boards", type=int, default=10)
    check.add_argument("--max-pages", type=int, default=5)
    check.add_argument("--yes", action="store_true")
    sync = sub.add_parser("sync", help="Sync enabled boards into the catalog.")
    sync.add_argument("--board-id", type=int, action="append")
    sync.add_argument("--source", choices=["greenhouse", "lever", "ashby"], action="append")
    sync.add_argument("--include-arbeitnow", action="store_true")
    sync.add_argument("--acknowledge-arbeitnow-terms", action="store_true",
                      help="Required with --include-arbeitnow: Arbeitnow is not published automatically.")
    sync.add_argument("--max-boards", type=int, default=catalog_sync.DEFAULT_MAX_BOARDS)
    sync.add_argument("--max-pages", type=int, default=catalog_sync.DEFAULT_MAX_PAGES)
    sync.add_argument("--yes", action="store_true")
    sub.add_parser("summary", help="Counts per source, country and board state.")

    args = parser.parse_args(argv)
    if not _target_ok(engine):
        return EXIT_FAILURE
    writes = args.command in ("seed", "check", "sync")
    if writes and not args.yes:
        print("Refusing to write without --yes.")
        return EXIT_FAILURE

    db = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        if args.command == "seed":
            result = catalog_sync.seed_boards(db, catalog_sync.utcnow())
            db.commit()
        elif args.command == "check":
            max_boards = max(1, min(args.max_boards, catalog_sync.MAX_BOARDS_LIMIT))
            max_pages = max(1, min(args.max_pages, catalog_sync.MAX_PAGES_LIMIT))
            query = db.query(models.JobSourceBoard)
            if args.board_id:
                query = query.filter(models.JobSourceBoard.board_id.in_(args.board_id))
            elif args.unverified:
                query = query.filter(models.JobSourceBoard.verification_status != "verified")
            else:
                print("Pass --board-id or --unverified.")
                return EXIT_FAILURE
            http = http_factory()
            result = []
            for board in query.order_by(models.JobSourceBoard.board_id).limit(max_boards).all():
                checked = catalog_sync.check_board(http, board.source, board.board_token, board.region, max_pages)
                now = catalog_sync.utcnow()
                if checked["complete"]:
                    board.verification_status = "verified"
                    board.verified_at = now
                    board.last_active_jobs_count = checked["active_postings"]
                elif board.verification_status != "verified":
                    board.verification_status = "failed"
                db.commit()
                result.append({"board_id": board.board_id, **checked})
        elif args.command == "sync":
            result = catalog_sync.run_sync(
                db,
                http_factory(),
                include_arbeitnow=args.include_arbeitnow,
                acknowledge_arbeitnow_terms=args.acknowledge_arbeitnow_terms,
                board_ids=args.board_id,
                sources=args.source,
                max_boards=args.max_boards,
                max_pages=args.max_pages,
            )
        else:
            from sqlalchemy import func

            result = {
                "boards": [
                    {"source": s, "verification_status": v, "is_enabled": e, "count": c}
                    for s, v, e, c in db.query(
                        models.JobSourceBoard.source,
                        models.JobSourceBoard.verification_status,
                        models.JobSourceBoard.is_enabled,
                        func.count(),
                    ).group_by(
                        models.JobSourceBoard.source,
                        models.JobSourceBoard.verification_status,
                        models.JobSourceBoard.is_enabled,
                    ).all()
                ],
                "active_catalog_jobs_by_source": dict(
                    db.query(models.Job.source, func.count(models.Job.job_id))
                    .filter(models.Job.created_by_user_id.is_(None), models.Job.job_status == catalog_sync.STATUS_ACTIVE)
                    .group_by(models.Job.source)
                    .all()
                ),
            }
    except (catalog_sync.SyncAlreadyRunning, catalog_sync.BoardNotSyncable) as exc:
        print(f"Refusing to continue: {exc}")
        return EXIT_FAILURE
    finally:
        db.close()
    print(json.dumps(result, indent=2, default=str))
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
