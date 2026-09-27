from __future__ import annotations

import argparse
import logging
import signal
import threading
from time import monotonic

from sqlalchemy import select

from .acquisition import AcquisitionError, sync_acquisition_from_import
from .config import get_settings
from .database import Database
from .enums import JellyfinState, JobState
from .healthcheck import write_worker_heartbeat
from .inventory import (
    claim_next_scan,
    ensure_initial_or_scheduled_scan,
    recover_interrupted_scans,
    refresh_storage_snapshot,
    run_library_scan,
)
from .logging_config import configure_logging
from .models import AcquisitionJob, Job
from .pipeline import process_job, retry_jellyfin_refresh
from .state import claim_next_job, recover_interrupted_jobs


logger = logging.getLogger(__name__)


class HeartbeatThread(threading.Thread):
    def __init__(self, stop_event: threading.Event):
        super().__init__(name="worker-heartbeat", daemon=True)
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.wait(10):
            write_worker_heartbeat()


def worker_loop(*, once: bool = False) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    database = Database(settings)
    database.initialize()
    with database.session() as session:
        recovered = recover_interrupted_jobs(session)
        recovered_scans = recover_interrupted_scans(session)
        acquisitions = session.scalars(
            select(AcquisitionJob).where(
                AcquisitionJob.associated_import_job_id.is_not(None)
            )
        ).all()
        for acquisition in acquisitions:
            try:
                sync_acquisition_from_import(session, acquisition)
            except AcquisitionError:
                logger.exception(
                    "Could not synchronize an acquisition during recovery",
                    extra={"acquisition_id": acquisition.id},
                )
    if recovered:
        logger.warning("Recovered interrupted jobs", extra={"count": recovered})
    if recovered_scans:
        logger.warning(
            "Recovered interrupted library scans", extra={"count": recovered_scans}
        )

    stop_event = threading.Event()

    def stop(_signal: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    write_worker_heartbeat()
    heartbeat = HeartbeatThread(stop_event)
    heartbeat.start()
    last_storage_refresh = 0.0
    logger.info("Worker started")
    try:
        while not stop_event.is_set():
            job_id: str | None
            with database.session() as session:
                job_id = claim_next_job(session)
            if job_id:
                logger.info("Processing job", extra={"job_id": job_id})
                process_job(database, settings, job_id)
                with database.session() as session:
                    acquisition = session.scalar(
                        select(AcquisitionJob).where(
                            AcquisitionJob.associated_import_job_id == job_id
                        )
                    )
                    if acquisition is not None:
                        try:
                            sync_acquisition_from_import(session, acquisition)
                        except AcquisitionError:
                            logger.exception(
                                "Could not synchronize the associated acquisition",
                                extra={"acquisition_id": acquisition.id, "job_id": job_id},
                            )
            with database.session() as session:
                retry_id = session.scalar(
                    select(Job.id)
                    .where(
                        Job.state == JobState.COMPLETE.value,
                        Job.jellyfin_state == JellyfinState.FAILED.value,
                        Job.jellyfin_retry_requested.is_(True),
                    )
                    .order_by(Job.updated_at)
                    .limit(1)
                )
            if retry_id:
                retry_jellyfin_refresh(database, settings, retry_id)

            scan_id: str | None = None
            if not job_id and not retry_id:
                ensure_initial_or_scheduled_scan(database, settings)
                with database.session() as session:
                    scan_id = claim_next_scan(session)
                if scan_id:
                    logger.info("Scanning music library", extra={"scan_id": scan_id})
                    result = run_library_scan(database, settings, scan_id)
                    logger.info(
                        "Library scan finished",
                        extra={"scan_id": scan_id, "state": result.state},
                    )
                    last_storage_refresh = monotonic()

            storage_due = (
                settings.storage_snapshot_interval_seconds > 0
                and monotonic() - last_storage_refresh
                >= settings.storage_snapshot_interval_seconds
            )
            if not job_id and not retry_id and not scan_id and storage_due:
                refresh_storage_snapshot(database, settings)
                last_storage_refresh = monotonic()
            write_worker_heartbeat()
            if once:
                return 0
            if not job_id and not retry_id and not scan_id:
                stop_event.wait(settings.worker_poll_seconds)
    finally:
        stop_event.set()
        heartbeat.join(timeout=2)
        write_worker_heartbeat()
        logger.info("Worker stopped")
    return 0


def run() -> None:
    parser = argparse.ArgumentParser(description="Fox Den Music worker")
    parser.add_argument(
        "--once", action="store_true", help="Process at most one import or library task and exit"
    )
    args = parser.parse_args()
    raise SystemExit(worker_loop(once=args.once))


if __name__ == "__main__":
    run()
