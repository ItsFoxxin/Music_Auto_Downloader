from __future__ import annotations

import sqlite3
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import Base


SCHEMA_VERSION = "2"
_STAGE_2_TABLES = frozenset(
    {
        "acquisition_jobs",
        "acquisition_events",
        "acquisition_artifacts",
        "library_artists",
        "library_albums",
        "library_inventory_tracks",
        "library_scan_runs",
        "storage_snapshots",
    }
)


@contextmanager
def _schema_initialization_lock(path: Path) -> Iterator[None]:
    """Serialize first-run DDL between the web and worker processes."""

    if path.is_symlink():
        raise RuntimeError("Database schema lock path must not be a symlink")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError("Could not open the database schema lock") from exc
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(path)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or opened.st_dev != named.st_dev
            or opened.st_ino != named.st_ino
            or opened.st_nlink != 1
            or getattr(named, "st_file_attributes", 0) & reparse_attribute
        ):
            raise RuntimeError("Database schema lock path is unsafe")

        if os.name == "nt":
            import msvcrt

            if opened.st_size == 0:
                os.write(descriptor, b"1")
                os.fsync(descriptor)
            deadline = time.monotonic() + 30
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Timed out waiting for database schema initialization") from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class Database:
    """SQLAlchemy/SQLite access with short, explicit transactions.

    Rollback-journal mode is intentional. Fox Den Music has a web process and a
    worker process; WAL is only safe once the host SQLite includes the 2026 WAL
    reset fix. A busy timeout and one worker keep this design predictable.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        connect_args: dict[str, object] = {}
        if settings.resolved_database_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 30, "autocommit": False}
        self.engine: Engine = create_engine(
            settings.resolved_database_url,
            connect_args=connect_args,
            pool_pre_ping=True,
            future=True,
        )
        if settings.resolved_database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session)

    @staticmethod
    def _configure_sqlite(connection: sqlite3.Connection, _record: object) -> None:
        previous_autocommit = getattr(connection, "autocommit", None)
        if previous_autocommit is not None:
            connection.autocommit = True
        try:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=DELETE")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.close()
        finally:
            if previous_autocommit is not None:
                connection.autocommit = previous_autocommit

    def initialize(self) -> None:
        self.settings.ensure_directories()
        with _schema_initialization_lock(self.settings.config_dir / ".foxden-schema.lock"):
            with self.engine.begin() as connection:
                tables = set(inspect(connection).get_table_names())
                if "database_metadata" not in tables:
                    if tables:
                        raise RuntimeError(
                            "Database contains tables but no Fox Den schema metadata. "
                            "Back up /config and restore a consistent database."
                        )
                    Base.metadata.create_all(connection)
                    connection.execute(
                        text(
                            "INSERT INTO database_metadata (key, value) "
                            "VALUES ('schema_version', :version)"
                        ),
                        {"version": SCHEMA_VERSION},
                    )
                    return

                stored_version = connection.scalar(
                    text("SELECT value FROM database_metadata WHERE key = 'schema_version'")
                )
                try:
                    version = int(str(stored_version))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Database schema version is missing or invalid") from exc
                latest = int(SCHEMA_VERSION)
                if version < 1 or version > latest:
                    raise RuntimeError(
                        f"Unsupported database schema {stored_version}; expected 1 through {SCHEMA_VERSION}. "
                        "Back up /config before upgrading."
                    )

                while version < latest:
                    if version == 1:
                        # Stage 2 is deliberately additive. The Stage 1 job, track,
                        # duplicate-registry, and cache tables remain byte-for-byte
                        # compatible while acquisition and scanned inventory get
                        # separate domains.
                        Base.metadata.create_all(connection)
                        present = set(inspect(connection).get_table_names())
                        missing = _STAGE_2_TABLES - present
                        if missing:
                            raise RuntimeError(
                                "Stage 2 database migration did not create: "
                                + ", ".join(sorted(missing))
                            )
                        version = 2
                        connection.execute(
                            text(
                                "UPDATE database_metadata SET value = :version "
                                "WHERE key = 'schema_version'"
                            ),
                            {"version": str(version)},
                        )
                        continue
                    raise RuntimeError(f"No database migration is available from schema {version}")

                # Recreate a manually omitted index/table only within the current
                # additive schema, while never altering Stage 1 columns in place.
                Base.metadata.create_all(connection)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def is_healthy(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
