from __future__ import annotations

from sqlalchemy import inspect, select, text

from foxden_music.database import Database, SCHEMA_VERSION
from foxden_music.enums import JobKind, JobState
from foxden_music.models import Base, Job
from foxden_music.state import claim_next_job


STAGE_1_TABLES = (
    "database_metadata",
    "jobs",
    "job_events",
    "library_tracks",
    "tracks",
    "release_candidates",
    "metadata_cache",
)


def test_schema_one_database_migrates_additively_without_losing_history(settings) -> None:
    settings.ensure_directories()
    database = Database(settings)
    tables = [Base.metadata.tables[name] for name in STAGE_1_TABLES]
    Base.metadata.create_all(database.engine, tables=tables)
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO database_metadata (key, value) "
                "VALUES ('schema_version', '1')"
            )
        )
    with database.session() as session:
        original = Job(
            display_name="Stage 1 history.zip",
            source_filename="Stage 1 history.zip",
            source_relative_path="incoming/source.zip",
            state=JobState.COMPLETE.value,
        )
        session.add(original)
        session.flush()
        original_id = original.id

    database.initialize()
    database.initialize()

    with database.engine.connect() as connection:
        assert connection.scalar(
            text("SELECT value FROM database_metadata WHERE key = 'schema_version'")
        ) == SCHEMA_VERSION
        names = set(inspect(connection).get_table_names())
        assert {
            "acquisition_jobs",
            "acquisition_events",
            "acquisition_artifacts",
            "library_artists",
            "library_albums",
            "library_inventory_tracks",
            "library_scan_runs",
            "storage_snapshots",
        } <= names
    with database.session() as session:
        persisted = session.get(Job, original_id)
        assert persisted is not None
        assert persisted.display_name == "Stage 1 history.zip"
        assert persisted.state == JobState.COMPLETE.value


def test_import_claim_ignores_legacy_acquisition_kind(database) -> None:
    with database.session() as session:
        legacy = Job(
            kind=JobKind.ACQUISITION.value,
            display_name="legacy acquisition",
            source_filename="not-an-import",
            source_relative_path="incoming/missing.zip",
        )
        album = Job(
            kind=JobKind.ALBUM_IMPORT.value,
            display_name="album.zip",
            source_filename="album.zip",
            source_relative_path="incoming/source.zip",
        )
        session.add_all([legacy, album])
        session.flush()
        legacy_id = legacy.id
        album_id = album.id

    with database.session() as session:
        assert claim_next_job(session) == album_id
    with database.session() as session:
        assert session.get(Job, legacy_id).state == JobState.QUEUED.value
        assert session.get(Job, album_id).state == JobState.STAGING.value


def test_future_schema_version_fails_closed(settings) -> None:
    database = Database(settings)
    database.initialize()
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE database_metadata SET value = '999' "
                "WHERE key = 'schema_version'"
            )
        )

    try:
        database.initialize()
    except RuntimeError as exc:
        assert "Unsupported database schema 999" in str(exc)
    else:
        raise AssertionError("A future database schema was accepted")
