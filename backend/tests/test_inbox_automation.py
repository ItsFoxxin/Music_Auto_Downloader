from __future__ import annotations

import os
import time
from pathlib import Path

from sqlalchemy import select

from foxden_music.acquisition import create_acquisition_batch, transition_acquisition
from foxden_music.config import Settings
from foxden_music.database import Database
from foxden_music.enums import AcquisitionState, JobState
from foxden_music.inbox_automation import stage_next_waiting_download
from foxden_music.models import AcquisitionArtifact, AcquisitionJob, Job


ALBUM_URL = "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy"


def configured_database(settings: Settings, tmp_path: Path) -> tuple[Settings, Database, Path]:
    inbox = tmp_path / "downloads"
    inbox.mkdir()
    configured = settings.model_copy(
        update={
            "download_inbox_dir": inbox,
            "download_inbox_settle_seconds": 0,
            "download_inbox_min_bytes": 1,
            "download_inbox_auto_import": True,
        }
    )
    database = Database(configured)
    database.initialize()
    return configured, database, inbox


def waiting_acquisition(database: Database) -> str:
    with database.session() as session:
        acquisition = create_acquisition_batch(session, ALBUM_URL)[0]
        transition_acquisition(
            session,
            acquisition,
            AcquisitionState.WAITING_FOR_DOWNLOAD,
            "Server browser opened; watching the download inbox",
        )
        return acquisition.id


def test_settled_download_is_attached_and_queued_automatically(
    settings: Settings, tmp_path: Path
) -> None:
    configured, database, inbox = configured_database(settings, tmp_path)
    acquisition_id = waiting_acquisition(database)
    downloaded = inbox / "album.zip"
    downloaded.write_bytes(b"completed-download")

    job_id = stage_next_waiting_download(database, configured)

    assert job_id is not None
    assert not downloaded.exists()
    with database.session() as session:
        acquisition = session.get(AcquisitionJob, acquisition_id)
        job = session.get(Job, job_id)
        artifact = session.scalar(
            select(AcquisitionArtifact).where(
                AcquisitionArtifact.acquisition_job_id == acquisition_id
            )
        )
        assert acquisition is not None
        assert acquisition.state == AcquisitionState.IMPORT_STARTED.value
        assert acquisition.associated_import_job_id == job_id
        assert job is not None
        assert job.state == JobState.QUEUED.value
        assert job.source_reference == "inbox:album.zip"
        assert artifact is not None
        assert artifact.received_via == "SERVER_INBOX_AUTO"


def test_download_older_than_watched_request_is_not_claimed(
    settings: Settings, tmp_path: Path
) -> None:
    configured, database, inbox = configured_database(settings, tmp_path)
    old_file = inbox / "old-album.zip"
    old_file.write_bytes(b"old-download")
    old_time = time.time() - 120
    os.utime(old_file, (old_time, old_time))
    acquisition_id = waiting_acquisition(database)

    assert stage_next_waiting_download(database, configured) is None
    assert old_file.exists()
    with database.session() as session:
        acquisition = session.get(AcquisitionJob, acquisition_id)
        assert acquisition is not None
        assert acquisition.state == AcquisitionState.WAITING_FOR_DOWNLOAD.value
