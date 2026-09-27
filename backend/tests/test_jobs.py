from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from sqlalchemy import select

from foxden_music.config import Settings
from foxden_music.database import Database
from foxden_music.enums import JobState
from foxden_music.models import Job
from foxden_music.state import (
    InvalidStateTransition,
    claim_next_job,
    recover_interrupted_jobs,
    retry_job,
    transition_job,
)


def _job(name: str = "album.zip") -> Job:
    return Job(
        display_name=name,
        source_filename=name,
        source_relative_path="incoming/source.zip",
    )


def test_job_state_transitions_are_validated_and_persisted(database) -> None:
    with database.session() as session:
        job = _job()
        session.add(job)
        session.flush()
        job_id = job.id
        transition_job(session, job, JobState.STAGING, "claimed")
        transition_job(session, job, JobState.EXTRACTING, "extracting")

    with database.session() as session:
        persisted = session.get(Job, job_id)
        assert persisted is not None
        assert persisted.state == JobState.EXTRACTING.value
        assert [event.message for event in persisted.events] == ["claimed", "extracting"]

        try:
            transition_job(session, persisted, JobState.COMPLETE, "invalid")
        except InvalidStateTransition:
            pass
        else:
            raise AssertionError("invalid transition was accepted")


def test_atomic_claim_only_claims_one_queued_job(database) -> None:
    with database.session() as session:
        first = _job("one.zip")
        second = _job("two.zip")
        session.add_all([first, second])
    with database.session() as session:
        claimed = claim_next_job(session)
    assert claimed is not None
    with database.session() as session:
        states = session.execute(select(Job.state)).scalars().all()
        assert states.count(JobState.STAGING.value) == 1
        assert states.count(JobState.QUEUED.value) == 1


def test_interrupted_job_is_marked_failed_and_retryable(database) -> None:
    with database.session() as session:
        job = _job()
        job.state = JobState.TAGGING.value
        session.add(job)
        session.flush()
        job_id = job.id
    with database.session() as session:
        assert recover_interrupted_jobs(session) == 1
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.state == JobState.FAILED.value
        assert job.retryable is True
        assert job.error_code == "WORKER_INTERRUPTED"
        retry_job(session, job)
    with database.session() as session:
        assert session.get(Job, job_id).state == JobState.QUEUED.value


def test_concurrent_first_run_schema_initialization_is_serialized(tmp_path: Path) -> None:
    config = tmp_path / "concurrent-config"
    settings = Settings(
        _env_file=None,
        app_environment="test",
        config_dir=config,
        staging_dir=tmp_path / "staging",
        music_dir=tmp_path / "music",
        database_url=f"sqlite:///{(config / 'concurrent.db').as_posix()}",
        csrf_secret="test-secret-that-is-deliberately-longer-than-thirty-two-characters",
    )
    first = Database(settings)
    second = Database(settings)
    barrier = Barrier(2)

    def initialize(database: Database) -> None:
        barrier.wait(timeout=5)
        database.initialize()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(initialize, database) for database in (first, second)]
            for future in futures:
                future.result(timeout=10)
        with first.session() as session:
            assert session.execute(select(Job)).all() == []
    finally:
        first.engine.dispose()
        second.engine.dispose()
