from __future__ import annotations

from types import SimpleNamespace

from foxden_music import worker
from foxden_music.enums import (
    AcquisitionProvider,
    AcquisitionSourceType,
    AcquisitionState,
    JobState,
    PreferredFormat,
)
from foxden_music.inventory import enqueue_library_scan
from foxden_music.models import AcquisitionJob, Job


def _isolate_worker(monkeypatch, settings) -> None:
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "write_worker_heartbeat", lambda: None)
    monkeypatch.setattr(worker.signal, "signal", lambda *_args: None)


def test_worker_once_claims_a_persisted_library_scan(monkeypatch, settings, database) -> None:
    _isolate_worker(monkeypatch, settings)
    with database.session() as session:
        scan_id = enqueue_library_scan(session, reason="test").id

    observed: list[str] = []

    def run_scan(_database, _settings, claimed_id: str):
        observed.append(claimed_id)
        return SimpleNamespace(state="COMPLETE")

    monkeypatch.setattr(worker, "Database", lambda _settings: database)
    monkeypatch.setattr(worker, "run_library_scan", run_scan)

    assert worker.worker_loop(once=True) == 0
    assert observed == [scan_id]


def test_worker_projects_completed_import_onto_acquisition(
    monkeypatch, settings, database
) -> None:
    _isolate_worker(monkeypatch, settings)
    with database.session() as session:
        import_job = Job(
            display_name="received.zip",
            source_filename="received.zip",
            source_relative_path="incoming/source.zip",
        )
        session.add(import_job)
        session.flush()
        import_id = import_job.id
        acquisition = AcquisitionJob(
            provider=AcquisitionProvider.SPOTIDOWNLOADER_MANUAL.value,
            source_url="https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy",
            source_type=AcquisitionSourceType.ALBUM.value,
            source_identifier="4aawyAB9vmqN3uQ7FjRGTy",
            display_title="Stage 2 test",
            state=AcquisitionState.IMPORT_STARTED.value,
            preferred_format=PreferredFormat.FLAC.value,
            associated_import_job_id=import_id,
            acquisition_relative_directory="acquisitions/test",
        )
        session.add(acquisition)
        session.flush()
        acquisition_id = acquisition.id

    def finish_import(target_database, _settings, job_id: str) -> None:
        with target_database.session() as session:
            job = session.get(Job, job_id)
            assert job is not None
            assert job.state == JobState.STAGING.value
            job.state = JobState.COMPLETE.value

    monkeypatch.setattr(worker, "Database", lambda _settings: database)
    monkeypatch.setattr(worker, "process_job", finish_import)

    assert worker.worker_loop(once=True) == 0
    with database.session() as session:
        acquisition = session.get(AcquisitionJob, acquisition_id)
        assert acquisition is not None
        assert acquisition.state == AcquisitionState.COMPLETE.value
        assert acquisition.finished_at is not None
        assert any(event.message == "Associated import completed" for event in acquisition.events)
