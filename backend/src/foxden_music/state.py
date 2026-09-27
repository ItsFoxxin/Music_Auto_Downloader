from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .enums import ACTIVE_JOB_STATES, JobKind, JobState
from .models import Job, JobEvent


class InvalidStateTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.STAGING, JobState.FAILED, JobState.CANCELLED}),
    JobState.STAGING: frozenset({JobState.EXTRACTING, JobState.INSPECTING, JobState.FAILED}),
    JobState.EXTRACTING: frozenset({JobState.INSPECTING, JobState.FAILED}),
    JobState.INSPECTING: frozenset({JobState.MATCHING_METADATA, JobState.FAILED}),
    JobState.MATCHING_METADATA: frozenset({JobState.NEEDS_REVIEW, JobState.TAGGING, JobState.FAILED}),
    JobState.NEEDS_REVIEW: frozenset(
        {JobState.QUEUED, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.TAGGING: frozenset({JobState.ORGANIZING, JobState.NEEDS_REVIEW, JobState.FAILED}),
    JobState.ORGANIZING: frozenset({JobState.VALIDATING, JobState.NEEDS_REVIEW, JobState.FAILED}),
    JobState.VALIDATING: frozenset({JobState.IMPORTING, JobState.NEEDS_REVIEW, JobState.FAILED}),
    JobState.IMPORTING: frozenset(
        {JobState.JELLYFIN_SCAN, JobState.COMPLETE, JobState.NEEDS_REVIEW, JobState.FAILED}
    ),
    JobState.JELLYFIN_SCAN: frozenset({JobState.COMPLETE, JobState.FAILED}),
    JobState.COMPLETE: frozenset(),
    JobState.FAILED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.CANCELLED: frozenset(),
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def add_event(
    session: Session,
    job: Job,
    message: str,
    *,
    level: str = "INFO",
    data: dict[str, Any] | None = None,
) -> JobEvent:
    event = JobEvent(
        job_id=job.id,
        state=job.state,
        level=level,
        message=message[:1000],
        data_json=json.dumps(data, ensure_ascii=False, sort_keys=True) if data else None,
    )
    session.add(event)
    job.updated_at = now_utc()
    return event


def transition_job(
    session: Session,
    job: Job,
    new_state: JobState,
    message: str,
    *,
    level: str = "INFO",
    data: dict[str, Any] | None = None,
) -> None:
    old_state = JobState(job.state)
    if new_state not in ALLOWED_TRANSITIONS[old_state]:
        raise InvalidStateTransition(f"Cannot transition {old_state.value} to {new_state.value}")
    job.state = new_state.value
    job.updated_at = now_utc()
    if new_state in {JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED}:
        job.finished_at = now_utc()
    add_event(session, job, message, level=level, data=data)


def claim_next_job(session: Session) -> str | None:
    candidate_id = session.scalar(
        select(Job.id)
        .where(
            Job.state == JobState.QUEUED.value,
            Job.kind == JobKind.ALBUM_IMPORT.value,
        )
        .order_by(Job.created_at)
        .limit(1)
    )
    if not candidate_id:
        return None
    claimed_at = now_utc()
    result = session.execute(
        update(Job)
        .where(
            Job.id == candidate_id,
            Job.state == JobState.QUEUED.value,
            Job.kind == JobKind.ALBUM_IMPORT.value,
        )
        .values(
            state=JobState.STAGING.value,
            started_at=claimed_at,
            finished_at=None,
            updated_at=claimed_at,
            attempt=Job.attempt + 1,
            retryable=False,
            error_code=None,
            error_message=None,
        )
    )
    if result.rowcount != 1:
        return None
    job = session.get(Job, candidate_id)
    assert job is not None
    add_event(session, job, "Worker claimed job")
    return candidate_id


def recover_interrupted_jobs(session: Session) -> int:
    interrupted = session.scalars(
        select(Job).where(Job.state.in_([state.value for state in ACTIVE_JOB_STATES]))
    ).all()
    for job in interrupted:
        previous = job.state
        job.state = JobState.FAILED.value
        job.retryable = True
        job.error_code = "WORKER_INTERRUPTED"
        job.error_message = "Processing was interrupted. Review the job and retry it safely."
        job.finished_at = now_utc()
        add_event(
            session,
            job,
            f"Recovered interrupted job from {previous}; manual retry is available",
            level="WARNING",
        )
    return len(interrupted)


def retry_job(session: Session, job: Job, *, message: str = "Job queued for retry") -> None:
    state = JobState(job.state)
    if state not in {JobState.FAILED, JobState.NEEDS_REVIEW}:
        raise InvalidStateTransition(f"Job in {state.value} cannot be retried")
    transition_job(session, job, JobState.QUEUED, message)
