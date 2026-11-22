"""Durable storage for Cloud Run: work on local disk, archive to a mounted bucket.

EnergyPlus and SQLite both need a real filesystem (random writes, file locking), which
a Cloud Storage FUSE mount doesn't provide. So on Cloud Run:

- runs execute in BEM_RUNS_DIR on local disk (fast, but lost when the instance stops)
- each finished run folder and chart is copied to BEM_ARCHIVE_DIR (the bucket mount)
- results.db lives locally; after every change a consistent snapshot (SQLite backup
  API) is copied to the archive, and on startup the last snapshot is restored

With BEM_ARCHIVE_DIR unset (local use) every function here is a no-op.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path

log = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()
DB_SNAPSHOT = "results.db"


def archive_dir() -> Path | None:
    value = os.environ.get("BEM_ARCHIVE_DIR")
    return Path(value) if value else None


def archive_run(run_dir: Path, remove_local: bool = True, subdir: str = "runs") -> Path | None:
    """Copy a finished run (or study) folder to the archive and free the local disk."""
    archive = archive_dir()
    if archive is None or not run_dir.is_dir():
        return None
    target = archive / subdir / run_dir.name
    target.mkdir(parents=True, exist_ok=True)
    for item in run_dir.iterdir():
        if item.is_file():
            shutil.copyfile(item, target / item.name)  # plain sequential writes
    if remove_local:
        shutil.rmtree(run_dir, ignore_errors=True)
    return target


def archive_file(path: Path, subdir: str) -> Path | None:
    archive = archive_dir()
    if archive is None or not path.is_file():
        return None
    target = archive / subdir / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    return target


def find_run_dir(base_dir: Path, run_id: str) -> Path | None:
    """A run folder, local first, then archived."""
    for candidate in (base_dir / run_id, (archive_dir() or Path("/nonexistent")) / "runs" / run_id):
        if (candidate / "run.json").exists():
            return candidate
    return None


def snapshot_db(db_path: Path) -> Path | None:
    """Copy a consistent snapshot of results.db to the archive."""
    archive = archive_dir()
    if archive is None or not db_path.exists():
        return None
    with _DB_LOCK:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / DB_SNAPSHOT
            with (
                closing(sqlite3.connect(db_path, timeout=30)) as src,
                closing(sqlite3.connect(local)) as dst,
            ):
                src.backup(dst)
            target = archive / DB_SNAPSHOT
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".db.partial")
            shutil.copyfile(local, partial)
            partial.replace(target)
    return target


def restore_db(db_path: Path) -> bool:
    """On startup: bring back the last snapshot if there is no local database yet."""
    archive = archive_dir()
    if archive is None or db_path.exists():
        return False
    snapshot = archive / DB_SNAPSHOT
    if not snapshot.is_file():
        return False
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, db_path)
    log.info("Restored results database from %s", snapshot)
    return True
