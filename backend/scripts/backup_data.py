"""Backup and restore of a deployment's SQLite database and CV uploads.

    python -m backend.scripts.backup_data backup --yes [--keep 7]
    python -m backend.scripts.backup_data list
    python -m backend.scripts.backup_data restore --from <backup name> --yes

It works on APPLY101_DATABASE_URL and APPLY101_UPLOAD_DIR from the process
environment only (never .env), like the other scripts, and refuses the
repository's real apply101.db and uploads/. Backups go to
APPLY101_BACKUP_DIR, default <database directory>/backups, one directory per
backup:

- database.db: a consistent copy made with SQLite's online backup API, so
  the app may keep running;
- uploads.tar.gz: the upload directory.

restore copies database.db back over the live database with the same API
and puts the uploads back; the current uploads are moved aside first, and
the current database is backed up first. Run it while nobody uses the
service.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from backend.app import config


EXIT_SUCCESS = 0
EXIT_FAILURE = 1
BACKUP_DIR_ENV = "APPLY101_BACKUP_DIR"
DATABASE_NAME = "database.db"
UPLOADS_NAME = "uploads.tar.gz"


class BackupError(RuntimeError):
    pass


def _paths(environ) -> tuple[Path, Path, Path]:
    url = (environ.get(config.DATABASE_URL_ENV) or "").strip()
    upload = (environ.get(config.UPLOAD_DIR_ENV) or "").strip()
    if not url or not upload:
        raise BackupError(f"Set {config.DATABASE_URL_ENV} and {config.UPLOAD_DIR_ENV}; the defaults are the real local data.")
    config._reject_sqlite_uri(url)
    database = config._sqlite_file(url)
    if database is None:
        raise BackupError(f"{config.DATABASE_URL_ENV} must be a file-backed SQLite URL.")
    database, upload_dir = Path(os.path.abspath(database)), Path(os.path.abspath(upload))
    real_db = config.REPO_ROOT / config.DEFAULT_DATABASE_FILE
    real_uploads = config.REPO_ROOT / config.DEFAULT_UPLOAD_DIR
    if config._same_path(database, real_db) or config._same_path(upload_dir, real_uploads):
        raise BackupError("Refusing the repository's real apply101.db or uploads/.")
    backups = (environ.get(BACKUP_DIR_ENV) or "").strip()
    backups = Path(os.path.abspath(backups)) if backups else database.parent / "backups"
    # The upload directory is archived and swapped whole on restore, so it
    # must contain neither the database nor the backups.
    if _within(database, upload_dir) or _within(backups, upload_dir) or _within(upload_dir, backups):
        raise BackupError(
            f"{config.UPLOAD_DIR_ENV} ({upload_dir}) must be separate from the database ({database}) "
            f"and the backups ({backups})."
        )
    return database, upload_dir, backups


def _within(path: Path, root: Path) -> bool:
    path, root = Path(os.path.realpath(path)), Path(os.path.realpath(root))
    return path == root or root in path.parents


def _copy_sqlite(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def backup(environ, *, keep: int, now: datetime | None = None) -> Path:
    database, upload_dir, backups = _paths(environ)
    if not database.is_file():
        raise BackupError(f"The database {database} does not exist.")
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    target = backups / f"apply101-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    _copy_sqlite(database, target / DATABASE_NAME)
    with tarfile.open(target / UPLOADS_NAME, "w:gz") as tar:
        if upload_dir.is_dir():
            tar.add(upload_dir, arcname="uploads")
    (target / "manifest.json").write_text(json.dumps({
        "created_at": stamp,
        "database": str(database),
        "upload_dir": str(upload_dir),
    }, indent=2))
    for old in list_backups(backups)[:-keep] if keep > 0 else []:
        shutil.rmtree(backups / old)
    return target


def list_backups(backups: Path) -> list[str]:
    if not backups.is_dir():
        return []
    return sorted(p.name for p in backups.iterdir() if p.is_dir() and (p / DATABASE_NAME).is_file())


def _safe_members(tar: tarfile.TarFile):
    for member in tar.getmembers():
        name = Path(member.name)
        if name.is_absolute() or ".." in name.parts or name.parts[:1] != ("uploads",):
            raise BackupError(f"Unexpected path in the uploads archive: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise BackupError(f"Unexpected entry type in the uploads archive: {member.name}")
        yield member


def restore(environ, name: str, *, now: datetime | None = None) -> dict:
    database, upload_dir, backups = _paths(environ)
    source = backups / name
    if Path(name).name != name or not (source / DATABASE_NAME).is_file():
        raise BackupError(f"No backup named {name!r} in {backups}.")
    with tarfile.open(source / UPLOADS_NAME, "r:gz") as tar:
        members = list(_safe_members(tar))  # checked before anything changes
        safety = backup(environ, keep=0, now=now) if database.is_file() else None
        staging = upload_dir.parent / f".restore-{upload_dir.name}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            tar.extractall(staging, members=members, filter="data")
            restored_uploads = staging / "uploads"
            if not restored_uploads.is_dir():
                restored_uploads.mkdir()
            # Uploads first, then the database; a failed database copy puts
            # the previous uploads back.
            moved_aside = None
            if upload_dir.exists():
                stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
                moved_aside = upload_dir.parent / f"{upload_dir.name}.before-restore-{stamp}"
                upload_dir.rename(moved_aside)
            restored_uploads.rename(upload_dir)
            try:
                database.parent.mkdir(parents=True, exist_ok=True)
                _copy_sqlite(source / DATABASE_NAME, database)
            except Exception:
                upload_dir.rename(restored_uploads)
                if moved_aside is not None:
                    moved_aside.rename(upload_dir)
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "restored": name,
        "backup_of_previous_state": str(safety) if safety else None,
        "previous_uploads_moved_to": str(moved_aside) if moved_aside else None,
    }


def main(argv=None, environ=None) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Back up or restore the deployment's data")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--keep", type=int, default=7, help="Backups to keep (0 keeps all).")
    b.add_argument("--yes", action="store_true")
    sub.add_parser("list")
    r = sub.add_parser("restore")
    r.add_argument("--from", dest="name", required=True)
    r.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        database, upload_dir, backups = _paths(environ)
        print(f"Database: {database}\nUploads: {upload_dir}\nBackups: {backups}")
        if args.command == "list":
            print(json.dumps(list_backups(backups), indent=2))
            return EXIT_SUCCESS
        if not args.yes:
            print("Refusing to write without --yes.")
            return EXIT_FAILURE
        if args.command == "backup":
            print(f"Backup written to {backup(environ, keep=args.keep)}")
        else:
            print(json.dumps(restore(environ, args.name), indent=2))
    except (BackupError, config.ConfigError, sqlite3.Error, OSError, tarfile.TarError) as exc:
        print(f"Refusing to continue: {exc}")
        return EXIT_FAILURE
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
