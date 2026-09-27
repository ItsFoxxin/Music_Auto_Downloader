from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from .acquisition import transition_acquisition
from .config import Settings
from .database import Database
from .enums import AcquisitionState, JobKind, JobState, SourceType
from .intake import IntakeError, cleanup_staged_job, list_download_inbox, stage_inbox_file
from .models import AcquisitionArtifact, AcquisitionJob, Job, utcnow
from .state import add_event


logger = logging.getLogger(__name__)


def _epoch(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _restore_staged_file(
    settings: Settings,
    job_id: str,
    staged_path: Path,
    display_name: str,
) -> None:
    """Return a moved file to the inbox after a database handoff race/failure."""

    if settings.download_inbox_move_files and staged_path.exists() and settings.download_inbox_dir:
        root = settings.download_inbox_dir.resolve(strict=True)
        destination = root / f"recovered-{uuid.uuid4().hex[:8]}-{display_name}"
        shutil.move(str(staged_path), str(destination))
    cleanup_staged_job(settings, job_id)


def stage_next_waiting_download(database: Database, settings: Settings) -> str | None:
    """Attach one settled inbox file to the single acquisition awaiting a download.

    The browser flow intentionally permits only one WAITING_FOR_DOWNLOAD request at
    a time. Files older than that request are ignored so an unrelated inbox item is
    never silently assigned to a new acquisition.
    """

    if not settings.download_inbox_auto_import or settings.download_inbox_dir is None:
        return None

    with database.session() as session:
        waiting = session.scalars(
            select(AcquisitionJob)
            .where(
                AcquisitionJob.state == AcquisitionState.WAITING_FOR_DOWNLOAD.value,
                AcquisitionJob.associated_import_job_id.is_(None),
            )
            .order_by(AcquisitionJob.updated_at, AcquisitionJob.created_at)
            .limit(2)
        ).all()
        if len(waiting) != 1:
            return None
        acquisition = waiting[0]
        acquisition_id = acquisition.id
        download_started_epoch = _epoch(acquisition.updated_at)

    try:
        candidates = [
            item
            for item in list_download_inbox(settings)
            if item.ready and item.modified_at_epoch >= download_started_epoch - 2
        ]
    except IntakeError:
        logger.exception("Automatic acquisition inbox scan failed")
        return None
    if not candidates:
        return None
    candidate = min(candidates, key=lambda item: (item.modified_at_epoch, item.display_name.lower()))

    try:
        job_id, staged_path, display_name = stage_inbox_file(candidate.relative_path, settings)
    except IntakeError:
        return None

    try:
        with database.session() as session:
            acquisition = session.get(AcquisitionJob, acquisition_id)
            if (
                acquisition is None
                or acquisition.state != AcquisitionState.WAITING_FOR_DOWNLOAD.value
                or acquisition.associated_import_job_id is not None
            ):
                raise RuntimeError("Acquisition changed before the inbox file could be attached")

            job_root = settings.jobs_dir / job_id
            import_job = Job(
                id=job_id,
                kind=JobKind.ALBUM_IMPORT.value,
                source_type=SourceType.INCOMING.value,
                state=JobState.QUEUED.value,
                display_name=display_name,
                source_filename=display_name,
                source_relative_path=staged_path.relative_to(job_root).as_posix(),
                source_reference=f"inbox:{candidate.relative_path}",
            )
            session.add(import_job)
            session.flush()
            now = utcnow()
            acquisition.associated_import_job_id = job_id
            session.add(
                AcquisitionArtifact(
                    acquisition_job_id=acquisition.id,
                    import_job_id=job_id,
                    received_via="SERVER_INBOX_AUTO",
                    state="HANDED_OFF",
                    display_filename=display_name,
                    stored_relative_path=staged_path.relative_to(settings.staging_dir).as_posix(),
                    byte_size=staged_path.stat().st_size,
                    handed_off_at=now,
                )
            )
            transition_acquisition(
                session,
                acquisition,
                AcquisitionState.FILE_RECEIVED,
                "Completed server download detected and safely staged",
            )
            transition_acquisition(
                session,
                acquisition,
                AcquisitionState.IMPORT_STARTED,
                "Automatic server-inbox import queued",
            )
            add_event(
                session,
                import_job,
                "Server download automatically matched to its waiting acquisition",
                data={
                    "acquisition_job_id": acquisition.id,
                    "inbox_file": candidate.relative_path,
                    "size": staged_path.stat().st_size,
                },
            )
        return job_id
    except BaseException:
        _restore_staged_file(settings, job_id, staged_path, display_name)
        raise
