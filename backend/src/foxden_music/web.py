from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import case, exists, func, or_, select, update
from sqlalchemy.orm import Session, selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .acquisition import (
    AcquisitionBatchError,
    SpotifyReferenceError,
    add_acquisition_event,
    create_acquisition_batch,
    sync_acquisitions_for_import_job,
    transition_acquisition,
)
from .config import Settings, get_settings
from .database import Database
from .enums import (
    ACTIVE_JOB_STATES,
    AcquisitionState,
    JellyfinState,
    JobKind,
    JobState,
    LibraryScanState,
    PreferredFormat,
    SourceType,
)
from .intake import (
    IntakeError,
    cleanup_staged_job,
    list_download_inbox,
    stage_inbox_file,
    stage_upload,
)
from .inventory import (
    enqueue_library_scan,
    health_counts as inventory_health_counts,
    latest_scan as inventory_latest_scan,
    library_counts as inventory_library_counts,
    quality_counts as inventory_quality_counts,
    storage_values as inventory_storage_values,
)
from .logging_config import configure_logging
from .models import (
    AcquisitionArtifact,
    AcquisitionJob,
    Job,
    LibraryAlbum,
    LibraryArtist,
    LibraryInventoryTrack,
    LibraryScanRun,
    ReleaseCandidate,
    utcnow,
)
from .state import InvalidStateTransition, add_event, retry_job


logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).resolve().parent
_STAGED_JOB_CLEANUP_KEY = "foxden_music_staged_job_cleanup"
_SESSION_ALREADY_COMMITTED_KEY = "foxden_music_session_already_committed"
_MUSICBRAINZ_RELEASE_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class _RequestBodyTooLarge(BaseException):
    """Escape FastAPI's generic Exception-to-400 body parser wrapper."""


class RequestBodyLimitMiddleware:
    """Bound the actual ASGI request stream before multipart spooling."""

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        path_limits: dict[str, int] | None = None,
    ):
        self.app = app
        self.max_bytes = max_bytes
        self.path_limits = path_limits or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        request_limit = self.path_limits.get(scope.get("path", ""), self.max_bytes)
        declared = next(
            (value for key, value in scope.get("headers", []) if key.lower() == b"content-length"),
            None,
        )
        if declared:
            try:
                if int(declared) > request_limit:
                    await JSONResponse({"detail": "Request body is too large"}, status_code=413)(
                        scope, receive, send
                    )
                    return
            except ValueError:
                pass
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > request_limit:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await JSONResponse({"detail": "Request body is too large"}, status_code=413)(
                scope, receive, send
            )


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                security_names = {name.lower().encode("ascii") for name in _SECURITY_HEADERS}
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in security_names
                ]
                headers.extend(
                    (name.lower().encode("ascii"), value.encode("ascii"))
                    for name, value in _SECURITY_HEADERS.items()
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secure_send)


def _load_or_create_csrf_secret(settings: Settings) -> str:
    if settings.csrf_secret:
        return settings.csrf_secret
    path = settings.config_dir / "csrf-secret"
    try:
        secret = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        secret = secrets.token_urlsafe(48)
        temporary = settings.config_dir / f".csrf-secret-{os.getpid()}.part"
        try:
            with temporary.open("x", encoding="ascii") as handle:
                os.chmod(temporary, 0o600)
                handle.write(secret)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.rename(temporary, path)
            except FileExistsError:
                temporary.unlink(missing_ok=True)
                secret = path.read_text(encoding="ascii").strip()
        finally:
            temporary.unlink(missing_ok=True)
    if len(secret) < 32:
        raise RuntimeError("CSRF_SECRET must contain at least 32 characters")
    return secret


class CsrfManager:
    def __init__(self, secret: str):
        self.serializer = URLSafeTimedSerializer(secret, salt="foxden-music-csrf-v1")

    def issue(self) -> str:
        return self.serializer.dumps({"purpose": "form"})

    def verify(self, token: str) -> None:
        try:
            payload = self.serializer.loads(token, max_age=7200)
        except (BadSignature, SignatureExpired) as exc:
            raise HTTPException(status_code=403, detail="Form expired or CSRF validation failed") from exc
        if payload != {"purpose": "form"}:
            raise HTTPException(status_code=403, detail="CSRF validation failed")


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _format_duration(value: float | None) -> str:
    if value is None:
        return "—"
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes}:{seconds:02d}"


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return str(value)


def _positive_integer(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _duration_from_milliseconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(milliseconds)
        or milliseconds < 0
        or milliseconds > 24 * 60 * 60 * 1000
    ):
        return None
    return milliseconds / 1000


def _candidate_presentation(candidate: ReleaseCandidate) -> dict[str, Any]:
    """Build a small, escaped-by-Jinja review view from a stored MB payload."""

    release_id = candidate.musicbrainz_release_id
    release_url = (
        f"https://musicbrainz.org/release/{release_id.lower()}"
        if _MUSICBRAINZ_RELEASE_ID.fullmatch(release_id)
        else None
    )
    tracks: list[dict[str, Any]] = []
    try:
        payload = (
            json.loads(candidate.payload_json)
            if len(candidate.payload_json) <= 2 * 1024**2
            else None
        )
    except (json.JSONDecodeError, TypeError, RecursionError):
        payload = None
    media = payload.get("media") if isinstance(payload, dict) else None
    if isinstance(media, list):
        for medium_index, medium in enumerate(media, start=1):
            if not isinstance(medium, dict):
                continue
            disc_number = _positive_integer(medium.get("position"), medium_index)
            raw_tracks = medium.get("tracks")
            if not isinstance(raw_tracks, list):
                continue
            for track_index, track in enumerate(raw_tracks, start=1):
                if not isinstance(track, dict):
                    continue
                recording = track.get("recording")
                recording = recording if isinstance(recording, dict) else {}
                raw_title = track.get("title") or recording.get("title")
                title = (
                    str(raw_title).strip()
                    if raw_title not in (None, "")
                    else "Untitled track"
                )
                if len(title) > 240:
                    title = f"{title[:237]}..."
                length = track.get("length")
                if length in (None, ""):
                    length = recording.get("length")
                tracks.append(
                    {
                        "disc_number": disc_number,
                        "track_number": _positive_integer(track.get("position"), track_index),
                        "title": title,
                        "duration_seconds": _duration_from_milliseconds(length),
                    }
                )
                if len(tracks) == 5:
                    break
            if len(tracks) == 5:
                break
    remaining = max((candidate.track_count or len(tracks)) - len(tracks), 0)
    return {"release_url": release_url, "tracks": tracks, "remaining_tracks": remaining}


def _candidate_presentations(job: Job) -> dict[int, dict[str, Any]]:
    return {candidate.id: _candidate_presentation(candidate) for candidate in job.candidates}


_PAGE_SIZE = 50
_ACQUISITION_TERMINAL_STATES = frozenset(
    {
        AcquisitionState.COMPLETE.value,
        AcquisitionState.FAILED.value,
        AcquisitionState.CANCELLED.value,
        AcquisitionState.NEEDS_REVIEW.value,
    }
)


def _effective_acquisition_state(acquisition: AcquisitionJob) -> str:
    """Present the linked importer state even before its sync transaction runs."""

    import_job = acquisition.import_job
    if import_job is None:
        return acquisition.state
    if import_job.state == JobState.COMPLETE.value:
        return AcquisitionState.COMPLETE.value
    if import_job.state == JobState.FAILED.value:
        return AcquisitionState.FAILED.value
    if import_job.state == JobState.CANCELLED.value:
        return AcquisitionState.CANCELLED.value
    if import_job.state == JobState.NEEDS_REVIEW.value:
        return AcquisitionState.NEEDS_REVIEW.value
    if import_job.state in {state.value for state in ACTIVE_JOB_STATES} | {JobState.QUEUED.value}:
        return AcquisitionState.IMPORT_STARTED.value
    return acquisition.state


def _acquisition_timeline(acquisition: AcquisitionJob, limit: int = 200) -> list[dict[str, Any]]:
    events = [
        {
            "message": event.message,
            "level": event.level,
            "created_at": event.created_at,
            "domain": "acquisition",
            "state": event.state,
        }
        for event in acquisition.events
    ]
    if acquisition.import_job is not None:
        events.extend(
            {
                "message": event.message,
                "level": event.level,
                "created_at": event.created_at,
                "domain": "import",
                "state": event.state,
            }
            for event in acquisition.import_job.events
        )

    def sort_key(item: dict[str, Any]) -> datetime:
        created_at = item["created_at"]
        return (
            created_at.replace(tzinfo=timezone.utc)
            if created_at.tzinfo is None
            else created_at.astimezone(timezone.utc)
        )

    events.sort(key=sort_key)
    return events[-limit:]


def _quality_counts(session: Session) -> dict[str, int]:
    return inventory_quality_counts(session)


def _library_metrics(session: Session) -> dict[str, int | float]:
    return inventory_library_counts(session)


def _acquisition_state_expression() -> Any:
    return case(
        (Job.state == JobState.COMPLETE.value, AcquisitionState.COMPLETE.value),
        (Job.state == JobState.FAILED.value, AcquisitionState.FAILED.value),
        (Job.state == JobState.CANCELLED.value, AcquisitionState.CANCELLED.value),
        (Job.state == JobState.NEEDS_REVIEW.value, AcquisitionState.NEEDS_REVIEW.value),
        (
            Job.state.in_(
                tuple(state.value for state in ACTIVE_JOB_STATES) + (JobState.QUEUED.value,)
            ),
            AcquisitionState.IMPORT_STARTED.value,
        ),
        else_=AcquisitionJob.state,
    )


def _activity_metrics(session: Session) -> dict[str, int]:
    effective_acquisition_state = _acquisition_state_expression()
    acquisition_counts = dict(
        session.execute(
            select(effective_acquisition_state, func.count(AcquisitionJob.id))
            .select_from(AcquisitionJob)
            .outerjoin(Job, AcquisitionJob.associated_import_job_id == Job.id)
            .group_by(effective_acquisition_state)
        ).all()
    )
    associated = exists(
        select(AcquisitionJob.id).where(AcquisitionJob.associated_import_job_id == Job.id)
    )
    import_counts = dict(
        session.execute(
            select(Job.state, func.count(Job.id)).where(~associated).group_by(Job.state)
        ).all()
    )
    return {
        "queued": int(acquisition_counts.get(AcquisitionState.QUEUED.value, 0)),
        "waiting_for_user": int(
            acquisition_counts.get(AcquisitionState.WAITING_FOR_USER.value, 0)
            + acquisition_counts.get(AcquisitionState.WAITING_FOR_DOWNLOAD.value, 0)
        ),
        "processing": int(
            acquisition_counts.get(AcquisitionState.IMPORT_STARTED.value, 0)
            + sum(import_counts.get(state.value, 0) for state in ACTIVE_JOB_STATES)
            + import_counts.get(JobState.QUEUED.value, 0)
        ),
        "needs_review": int(
            acquisition_counts.get(AcquisitionState.NEEDS_REVIEW.value, 0)
            + import_counts.get(JobState.NEEDS_REVIEW.value, 0)
        ),
        "failed": int(
            acquisition_counts.get(AcquisitionState.FAILED.value, 0)
            + import_counts.get(JobState.FAILED.value, 0)
        ),
    }


def _latest_scan(session: Session) -> LibraryScanRun | None:
    return inventory_latest_scan(session)


def _latest_completed_scan(session: Session) -> LibraryScanRun | None:
    return session.scalar(
        select(LibraryScanRun)
        .where(LibraryScanRun.state == LibraryScanState.COMPLETE.value)
        .order_by(LibraryScanRun.finished_at.desc())
        .limit(1)
    )


def _cancel_acquisition_before_handoff(
    session: Session,
    acquisition_id: str,
) -> AcquisitionJob:
    """Cancel one waiting acquisition with a fresh-transaction CAS."""

    acquisition = session.get(AcquisitionJob, acquisition_id)
    if acquisition is None:
        raise HTTPException(status_code=404, detail="Acquisition job not found")
    if acquisition.associated_import_job_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This acquisition has already been handed to the importer. "
                "Open the linked import to review or retry it."
            ),
        )
    if acquisition.state == AcquisitionState.CANCELLED.value:
        return acquisition
    eligible_states = {
        AcquisitionState.QUEUED.value,
        AcquisitionState.WAITING_FOR_USER.value,
        AcquisitionState.WAITING_FOR_DOWNLOAD.value,
    }
    if acquisition.state not in eligible_states:
        raise HTTPException(
            status_code=409,
            detail="Only an acquisition still waiting for its download can be cancelled",
        )
    previous_state = acquisition.state

    # A concurrent upload stages outside SQLite and then performs its own CAS.
    # Release this eligibility read transaction before competing for the writer
    # lock so cancellation never fails while upgrading a stale SQLite snapshot.
    session.rollback()
    now = utcnow()
    claimed = session.execute(
        update(AcquisitionJob)
        .where(
            AcquisitionJob.id == acquisition_id,
            AcquisitionJob.associated_import_job_id.is_(None),
            AcquisitionJob.state == previous_state,
        )
        .values(
            state=AcquisitionState.CANCELLED.value,
            updated_at=now,
            finished_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        session.rollback()
        current = session.get(AcquisitionJob, acquisition_id)
        if (
            current is not None
            and current.associated_import_job_id is None
            and current.state == AcquisitionState.CANCELLED.value
        ):
            return current
        raise HTTPException(
            status_code=409,
            detail="This acquisition changed before cancellation could be applied",
        )

    session.expire_all()
    cancelled = session.get(AcquisitionJob, acquisition_id)
    if cancelled is None:
        raise HTTPException(status_code=409, detail="The acquisition is no longer available")
    add_acquisition_event(
        session,
        cancelled,
        "Acquisition cancelled by user before import handoff",
    )
    return cancelled


def _cancel_import_before_processing(session: Session, job_id: str) -> Job:
    """Atomically stop a queued/paused/failed import without deleting evidence."""

    eligible_states = (
        JobState.QUEUED.value,
        JobState.NEEDS_REVIEW.value,
        JobState.FAILED.value,
    )
    now = utcnow()
    claimed = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.state.in_(eligible_states))
        .values(
            state=JobState.CANCELLED.value,
            retryable=False,
            updated_at=now,
            finished_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        session.rollback()
        current = session.get(Job, job_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Import job not found")
        if current.state == JobState.CANCELLED.value:
            return current
        raise HTTPException(
            status_code=409,
            detail=(
                "Only a queued, review-paused, or failed import can be cancelled. "
                "A processing import must first reach a safe pause or terminal state."
            ),
        )

    session.expire_all()
    cancelled = session.get(Job, job_id)
    if cancelled is None:
        raise HTTPException(status_code=409, detail="The import is no longer available")
    add_event(session, cancelled, "Import cancelled by user; staged evidence was preserved")
    sync_acquisitions_for_import_job(session, cancelled)
    return cancelled


def _storage_metrics(session: Session) -> dict[str, int | str | None]:
    snapshot = inventory_storage_values(session)
    return {
        "music_bytes": snapshot["library_bytes"],
        "staging_bytes": snapshot["staging_bytes"],
        "free_bytes": snapshot["music_filesystem_free_bytes"],
        "observed_at": snapshot["observed_at"],
    }


def _active_presentations(session: Session, limit: int = 12) -> list[dict[str, Any]]:
    effective_state = _acquisition_state_expression()
    acquisitions = session.scalars(
        select(AcquisitionJob)
        .outerjoin(Job, AcquisitionJob.associated_import_job_id == Job.id)
        .where(~effective_state.in_(tuple(_ACQUISITION_TERMINAL_STATES)))
        .options(selectinload(AcquisitionJob.import_job))
        .order_by(func.coalesce(Job.updated_at, AcquisitionJob.updated_at).desc())
        .limit(limit)
    ).all()
    associated = exists(
        select(AcquisitionJob.id).where(AcquisitionJob.associated_import_job_id == Job.id)
    )
    imports = session.scalars(
        select(Job)
        .where(
            ~associated,
            or_(
                Job.state == JobState.QUEUED.value,
                Job.state.in_(tuple(state.value for state in ACTIVE_JOB_STATES)),
                Job.state == JobState.NEEDS_REVIEW.value,
            ),
        )
        .order_by(Job.updated_at.desc())
        .limit(limit)
    ).all()
    items = [
        {
            "kind": "acquisition",
            "id": item.id,
            "title": item.display_title,
            "state": _effective_acquisition_state(item),
            "updated_at": (
                item.import_job.updated_at
                if item.import_job is not None
                else item.updated_at
            ),
            "url": f"/acquisitions/{item.id}",
        }
        for item in acquisitions
    ]
    items.extend(
        {
            "kind": "import",
            "id": item.id,
            "title": item.display_name,
            "state": item.state,
            "updated_at": item.updated_at,
            "url": f"/jobs/{item.id}",
        }
        for item in imports
    )
    return sorted(items, key=lambda item: item["updated_at"], reverse=True)[:limit]


def _recent_imports(session: Session, limit: int = 10) -> list[dict[str, Any]]:
    jobs = session.scalars(
        select(Job)
        .where(
            Job.state == JobState.COMPLETE.value,
            Job.imported_count > 0,
        )
        .options(selectinload(Job.tracks))
        .order_by(Job.finished_at.desc(), Job.updated_at.desc())
        .limit(limit)
    ).all()
    presentations: list[dict[str, Any]] = []
    for job in jobs:
        first_track = job.tracks[0] if job.tracks else None
        codecs = sorted({track.codec.upper() for track in job.tracks if track.codec})
        presentations.append(
            {
                "id": job.id,
                "title": first_track.album if first_track and first_track.album else job.display_name,
                "artist": (
                    first_track.album_artist or first_track.artist
                    if first_track
                    else None
                ),
                "track_count": job.track_count,
                "format_summary": ", ".join(codecs) if codecs else "Format unavailable",
                "finished_at": job.finished_at,
                "jellyfin_state": job.jellyfin_state,
            }
        )
    return presentations


def _health_counts(session: Session) -> dict[str, int]:
    counts = inventory_health_counts(session)
    return {
        **counts,
        "low_quality": counts["low_quality_files"],
        "inspection_failed": counts["failed_inspections"],
    }


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    database = database or Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        database.initialize()
        app.state.csrf = CsrfManager(_load_or_create_csrf_secret(settings))
        yield

    app = FastAPI(title=settings.app_name, version=__version__, debug=False, lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=settings.max_upload_bytes + 1024**2,
        path_limits={
            "/acquisitions": settings.spotify_batch_max_characters + 64 * 1024,
            "/library/scans": 64 * 1024,
        },
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    template_environment = Environment(
        loader=FileSystemLoader(PACKAGE_DIR / "templates"),
        autoescape=select_autoescape(("html", "xml")),
        enable_async=False,
    )
    templates = Jinja2Templates(env=template_environment)
    templates.env.filters["datetime"] = _format_datetime
    templates.env.filters["duration"] = _format_duration
    templates.env.filters["filesize"] = _format_bytes

    def session_dependency() -> Any:
        session = database.session_factory()
        try:
            yield session
            if not session.info.pop(_SESSION_ALREADY_COMMITTED_KEY, False):
                session.commit()
        except BaseException:
            try:
                session.rollback()
            finally:
                staged_job_id = session.info.get(_STAGED_JOB_CLEANUP_KEY)
                if isinstance(staged_job_id, str):
                    cleanup_staged_job(settings, staged_job_id)
            raise
        finally:
            session.close()

    def common_context(request: Request, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "app_name": settings.app_name,
            "version": __version__,
            "csrf_token": request.app.state.csrf.issue(),
            "active_states": {state.value for state in ACTIVE_JOB_STATES},
            **values,
        }

    def render_error(
        request: Request,
        *,
        status_code: int,
        title: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            status_code=status_code,
            headers=headers,
            context=common_context(
                request,
                status_code=status_code,
                title=title,
                message=message,
            ),
        )

    def uses_json_errors(request: Request) -> bool:
        return request.url.path == "/health" or request.url.path.startswith("/api/")

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> HTMLResponse | JSONResponse:
        if uses_json_errors(request):
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
        titles = {
            400: "Invalid request",
            403: "Request rejected",
            404: "Page not found",
            409: "Action unavailable",
            413: "Upload too large",
            422: "Invalid form",
        }
        message = (
            exc.detail
            if isinstance(exc.detail, str)
            else "The request could not be completed."
        )
        return render_error(
            request,
            status_code=exc.status_code,
            title=titles.get(exc.status_code, "Request could not be completed"),
            message=message,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception(
        request: Request, exc: RequestValidationError
    ) -> HTMLResponse | JSONResponse:
        if uses_json_errors(request):
            return await request_validation_exception_handler(request, exc)
        return render_error(
            request,
            status_code=422,
            title="Invalid form",
            message="The submitted form was incomplete or invalid. Go back and try again.",
        )

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> HTMLResponse:
        incident = secrets.token_hex(6)
        logger.exception("Unhandled web request error", extra={"incident_id": incident})
        return render_error(
            request,
            status_code=500,
            title="Something went wrong",
            message=f"The request could not be completed. Incident {incident} was logged.",
            headers=_SECURITY_HEADERS,
        )

    @app.get("/health", include_in_schema=False)
    def health() -> JSONResponse:
        healthy = database.is_healthy()
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "database": healthy, "version": __version__},
            status_code=200 if healthy else 503,
        )

    @app.get("/api/health")
    def api_health() -> JSONResponse:
        healthy = database.is_healthy()
        return JSONResponse(
            {
                "schema_version": 1,
                "status": "ok" if healthy else "degraded",
                "version": __version__,
                "checks": {"database": "ok" if healthy else "failed"},
            },
            status_code=200 if healthy else 503,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, session: Session = Depends(session_dependency)) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=common_context(
                request,
                library_metrics=_library_metrics(session),
                quality_metrics=_quality_counts(session),
                activity_metrics=_activity_metrics(session),
                storage_metrics=_storage_metrics(session),
                health_metrics=_health_counts(session),
                active_items=_active_presentations(session),
                recent_imports=_recent_imports(session),
                latest_scan=_latest_scan(session),
            ),
        )

    @app.get("/partials/dashboard/metrics", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_metrics(
        request: Request, session: Session = Depends(session_dependency)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_metrics.html",
            context=common_context(
                request,
                library_metrics=_library_metrics(session),
                quality_metrics=_quality_counts(session),
                activity_metrics=_activity_metrics(session),
                storage_metrics=_storage_metrics(session),
                health_metrics=_health_counts(session),
                latest_scan=_latest_scan(session),
            ),
        )

    @app.get("/partials/dashboard/active", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_active(
        request: Request, session: Session = Depends(session_dependency)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_active.html",
            context=common_context(request, active_items=_active_presentations(session)),
        )

    @app.get("/partials/dashboard/recent", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_recent(
        request: Request, session: Session = Depends(session_dependency)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="partials/dashboard_recent.html",
            context=common_context(request, recent_imports=_recent_imports(session)),
        )

    @app.get("/add", response_class=HTMLResponse)
    def add_music(request: Request) -> HTMLResponse:
        inbox_error = None
        inbox_files = []
        if settings.download_inbox_dir is not None:
            try:
                inbox_files = list_download_inbox(settings)
            except IntakeError as exc:
                inbox_error = str(exc)
        return templates.TemplateResponse(
            request=request,
            name="add.html",
            context=common_context(
                request,
                max_upload_bytes=settings.max_upload_bytes,
                download_inbox_dir=settings.download_inbox_dir,
                download_inbox_min_bytes=settings.download_inbox_min_bytes,
                inbox_files=inbox_files,
                inbox_error=inbox_error,
                remote_browser_url=settings.remote_browser_url,
                source_urls="",
                preferred_format=PreferredFormat.FLAC.value,
                form_error=None,
            ),
        )

    @app.post("/acquisitions")
    def create_acquisitions(
        request: Request,
        source_urls: str = Form(...),
        preferred_format: str = Form(PreferredFormat.FLAC.value),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> Any:
        request.app.state.csrf.verify(csrf_token)
        try:
            if len(source_urls) > settings.spotify_batch_max_characters:
                raise ValueError(
                    "Spotify URL input exceeds the configured "
                    f"{settings.spotify_batch_max_characters:,} character limit"
                )
            requested_format = PreferredFormat(preferred_format)
            acquisitions = create_acquisition_batch(
                session,
                source_urls,
                preferred_format=requested_format,
                max_items=settings.spotify_batch_max_urls,
            )
        except (ValueError, SpotifyReferenceError, AcquisitionBatchError) as exc:
            return templates.TemplateResponse(
                request=request,
                name="add.html",
                status_code=400,
                context=common_context(
                    request,
                    max_upload_bytes=settings.max_upload_bytes,
                    source_urls=source_urls,
                    preferred_format=preferred_format,
                    form_error=str(exc),
                ),
            )
        created_roots: list[Path] = []
        try:
            for acquisition in acquisitions:
                root = settings.acquisitions_dir / acquisition.id
                created_roots.append(root)
                root.mkdir(mode=0o750, exist_ok=False)
                (root / "incoming").mkdir(mode=0o750, exist_ok=False)
            session.commit()
            session.info[_SESSION_ALREADY_COMMITTED_KEY] = True
        except BaseException:
            for root in reversed(created_roots):
                shutil.rmtree(root, ignore_errors=True)
            raise
        return RedirectResponse(
            url=f"/acquisitions?created={len(acquisitions)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    def load_acquisition(session: Session, acquisition_id: str) -> AcquisitionJob:
        acquisition = session.scalar(
            select(AcquisitionJob)
            .where(AcquisitionJob.id == acquisition_id)
            .options(
                selectinload(AcquisitionJob.events),
                selectinload(AcquisitionJob.artifacts),
                selectinload(AcquisitionJob.import_job).selectinload(Job.events),
            )
        )
        if acquisition is None:
            raise HTTPException(status_code=404, detail="Acquisition job not found")
        return acquisition

    @app.get("/acquisitions", response_class=HTMLResponse)
    def acquisition_queue(
        request: Request,
        state: str | None = None,
        page: int = 1,
        created: int | None = None,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        page = max(page, 1)
        statement = select(AcquisitionJob).options(selectinload(AcquisitionJob.import_job))
        if state:
            try:
                selected_state = AcquisitionState(state).value
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Unknown acquisition state") from exc
            statement = statement.outerjoin(
                Job, AcquisitionJob.associated_import_job_id == Job.id
            ).where(_acquisition_state_expression() == selected_state)
        total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
        acquisitions = session.scalars(
            statement.order_by(AcquisitionJob.updated_at.desc())
            .offset((page - 1) * _PAGE_SIZE)
            .limit(_PAGE_SIZE)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="acquisitions/index.html",
            context=common_context(
                request,
                acquisitions=acquisitions,
                effective_state=_effective_acquisition_state,
                selected_state=state or "",
                acquisition_states=[item.value for item in AcquisitionState],
                page=page,
                total=total,
                page_size=_PAGE_SIZE,
                created=created,
            ),
        )

    @app.get("/acquisitions/{acquisition_id}", response_class=HTMLResponse)
    def acquisition_detail(
        request: Request,
        acquisition_id: str,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        acquisition = load_acquisition(session, acquisition_id)
        return templates.TemplateResponse(
            request=request,
            name="acquisitions/detail.html",
            headers={"Cache-Control": "no-store"},
            context=common_context(
                request,
                acquisition=acquisition,
                effective_state=_effective_acquisition_state(acquisition),
                provider_url=settings.spotidownloader_url,
                remote_browser_url=settings.remote_browser_url,
                automatic_inbox_enabled=bool(
                    settings.download_inbox_auto_import and settings.download_inbox_dir
                ),
                timeline_events=_acquisition_timeline(acquisition),
                max_upload_bytes=settings.max_upload_bytes,
            ),
        )

    @app.get(
        "/acquisitions/{acquisition_id}/status",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def acquisition_status(
        request: Request,
        acquisition_id: str,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        acquisition = load_acquisition(session, acquisition_id)
        effective_state = _effective_acquisition_state(acquisition)
        return templates.TemplateResponse(
            request=request,
            name="partials/acquisition_status.html",
            context=common_context(
                request,
                acquisition=acquisition,
                effective_state=effective_state,
                poll_terminal=effective_state in _ACQUISITION_TERMINAL_STATES,
                timeline_events=_acquisition_timeline(acquisition),
            ),
        )

    @app.post("/acquisitions/{acquisition_id}/cancel")
    def cancel_acquisition(
        request: Request,
        acquisition_id: str,
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> RedirectResponse:
        request.app.state.csrf.verify(csrf_token)
        acquisition = _cancel_acquisition_before_handoff(session, acquisition_id)
        return RedirectResponse(
            url=f"/acquisitions/{acquisition.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/acquisitions/{acquisition_id}/browser", response_class=HTMLResponse)
    def acquisition_browser(
        request: Request,
        acquisition_id: str,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        acquisition = load_acquisition(session, acquisition_id)
        if not settings.remote_browser_url:
            raise HTTPException(status_code=409, detail="Server browser is not configured")
        return templates.TemplateResponse(
            request=request,
            name="acquisitions/browser.html",
            headers={"Cache-Control": "no-store"},
            context=common_context(
                request, acquisition=acquisition, browser_url="/server-browser/",
            ),
        )

    @app.post("/acquisitions/{acquisition_id}/begin-download")
    def begin_acquisition_download(
        request: Request,
        acquisition_id: str,
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        request.app.state.csrf.verify(csrf_token)
        acquisition = load_acquisition(session, acquisition_id)
        if not settings.download_inbox_auto_import or settings.download_inbox_dir is None:
            raise HTTPException(status_code=409, detail="Automatic download inbox is not configured")
        if acquisition.associated_import_job_id is not None:
            raise HTTPException(status_code=409, detail="This acquisition already has an import")
        if acquisition.state not in {
            AcquisitionState.QUEUED.value,
            AcquisitionState.WAITING_FOR_USER.value,
            AcquisitionState.WAITING_FOR_DOWNLOAD.value,
        }:
            raise HTTPException(status_code=409, detail="This acquisition cannot start a download")
        another_waiting = session.scalar(
            select(AcquisitionJob.id).where(
                AcquisitionJob.id != acquisition.id,
                AcquisitionJob.state == AcquisitionState.WAITING_FOR_DOWNLOAD.value,
                AcquisitionJob.associated_import_job_id.is_(None),
            )
        )
        if another_waiting:
            raise HTTPException(
                status_code=409,
                detail="Finish or cancel the current server download before starting another",
            )
        if acquisition.state != AcquisitionState.WAITING_FOR_DOWNLOAD.value:
            transition_acquisition(
                session,
                acquisition,
                AcquisitionState.WAITING_FOR_DOWNLOAD,
                "Server browser handoff started; watching the download inbox",
            )
        destination = f"/acquisitions/{acquisition.id}/browser"
        # Return an actual same-origin document before navigating away. This
        # works even when a client has an older cached app.js and avoids Firefox
        # treating an external redirect as a blocked cross-origin form action.
        return templates.TemplateResponse(
            request=request,
            name="acquisitions/browser_handoff.html",
            headers={"Cache-Control": "no-store"},
            context=common_context(
                request,
                acquisition=acquisition,
                browser_url=destination,
            ),
        )

    @app.post("/acquisitions/{acquisition_id}/upload")
    async def acquisition_upload(
        request: Request,
        acquisition_id: str,
        album: UploadFile = File(...),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> RedirectResponse:
        request.app.state.csrf.verify(csrf_token)
        acquisition = load_acquisition(session, acquisition_id)
        if acquisition.associated_import_job_id is not None:
            raise HTTPException(status_code=409, detail="This acquisition already has an import")
        if acquisition.state not in {
            AcquisitionState.QUEUED.value,
            AcquisitionState.WAITING_FOR_USER.value,
            AcquisitionState.WAITING_FOR_DOWNLOAD.value,
        }:
            raise HTTPException(status_code=409, detail="This acquisition no longer accepts uploads")
        source_reference = acquisition.source_url
        # Release the eligibility read transaction before copying a potentially
        # multi-gigabyte multipart file. A compare-and-swap after staging makes
        # exactly one concurrent upload the linked import.
        session.rollback()
        try:
            job_id, source_path, display_name = await stage_upload(album, settings)
        except IntakeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.info[_STAGED_JOB_CLEANUP_KEY] = job_id
        job_root = settings.jobs_dir / job_id
        import_job = Job(
            id=job_id,
            kind=JobKind.ALBUM_IMPORT.value,
            source_type=SourceType.SPOTIFY.value,
            state=JobState.QUEUED.value,
            display_name=display_name,
            source_filename=display_name,
            source_relative_path=source_path.relative_to(job_root).as_posix(),
            source_reference=source_reference,
        )
        session.add(import_job)
        session.flush()
        now = utcnow()
        claimed = session.execute(
            update(AcquisitionJob)
            .where(
                AcquisitionJob.id == acquisition_id,
                AcquisitionJob.associated_import_job_id.is_(None),
                AcquisitionJob.state.in_(
                    (
                        AcquisitionState.QUEUED.value,
                        AcquisitionState.WAITING_FOR_USER.value,
                        AcquisitionState.WAITING_FOR_DOWNLOAD.value,
                    )
                ),
            )
            .values(associated_import_job_id=job_id, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            session.rollback()
            cleanup_staged_job(settings, job_id)
            session.info.pop(_STAGED_JOB_CLEANUP_KEY, None)
            raise HTTPException(status_code=409, detail="This acquisition already has an import")

        session.expire_all()
        acquisition = session.get(AcquisitionJob, acquisition_id)
        if acquisition is None:
            raise HTTPException(status_code=409, detail="The acquisition is no longer available")
        session.add(
            AcquisitionArtifact(
                acquisition_job_id=acquisition.id,
                import_job_id=job_id,
                received_via="UPLOAD",
                state="HANDED_OFF",
                display_filename=display_name,
                stored_relative_path=source_path.relative_to(settings.staging_dir).as_posix(),
                byte_size=source_path.stat().st_size,
                handed_off_at=now,
            )
        )
        acquisition.associated_import_job_id = job_id
        transition_acquisition(
            session,
            acquisition,
            AcquisitionState.FILE_RECEIVED,
            "Downloaded file received and safely staged",
        )
        transition_acquisition(
            session,
            acquisition,
            AcquisitionState.IMPORT_STARTED,
            "Stage 1 import queued",
        )
        add_event(
            session,
            import_job,
            "Acquisition download safely staged and queued",
            data={"acquisition_job_id": acquisition.id, "size": source_path.stat().st_size},
        )
        session.flush()
        session.commit()
        session.info[_SESSION_ALREADY_COMMITTED_KEY] = True
        session.info.pop(_STAGED_JOB_CLEANUP_KEY, None)
        return RedirectResponse(
            url=f"/acquisitions/{acquisition.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/imports", response_class=HTMLResponse)
    def import_form(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="import.html",
            context=common_context(request, max_upload_bytes=settings.max_upload_bytes),
        )

    @app.post("/imports")
    async def create_import(
        request: Request,
        album: UploadFile = File(...),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> RedirectResponse:
        request.app.state.csrf.verify(csrf_token)
        try:
            job_id, source_path, display_name = await stage_upload(album, settings)
        except IntakeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.info[_STAGED_JOB_CLEANUP_KEY] = job_id
        job_root = settings.jobs_dir / job_id
        job = Job(
            id=job_id,
            kind=JobKind.ALBUM_IMPORT.value,
            source_type=SourceType.UPLOAD.value,
            state=JobState.QUEUED.value,
            display_name=display_name,
            source_filename=display_name,
            source_relative_path=source_path.relative_to(job_root).as_posix(),
        )
        session.add(job)
        add_event(session, job, "Upload safely staged and queued", data={"size": source_path.stat().st_size})
        session.flush()
        session.commit()
        session.info[_SESSION_ALREADY_COMMITTED_KEY] = True
        session.info.pop(_STAGED_JOB_CLEANUP_KEY, None)
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/imports/inbox")
    def create_import_from_inbox(
        request: Request,
        inbox_file: str = Form(...),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> RedirectResponse:
        request.app.state.csrf.verify(csrf_token)
        try:
            job_id, source_path, display_name = stage_inbox_file(inbox_file, settings)
        except IntakeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.info[_STAGED_JOB_CLEANUP_KEY] = job_id
        job_root = settings.jobs_dir / job_id
        job = Job(
            id=job_id,
            kind=JobKind.ALBUM_IMPORT.value,
            source_type=SourceType.INCOMING.value,
            state=JobState.QUEUED.value,
            display_name=display_name,
            source_filename=display_name,
            source_relative_path=source_path.relative_to(job_root).as_posix(),
            source_reference=f"inbox:{inbox_file}",
        )
        session.add(job)
        add_event(
            session,
            job,
            "Server download inbox file safely staged and queued",
            data={"inbox_file": inbox_file, "size": source_path.stat().st_size},
        )
        session.flush()
        session.commit()
        session.info[_SESSION_ALREADY_COMMITTED_KEY] = True
        session.info.pop(_STAGED_JOB_CLEANUP_KEY, None)
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/jobs/{job_id}/clear-inbox-download")
    def clear_inbox_download(
        request: Request,
        job_id: str,
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> RedirectResponse:
        request.app.state.csrf.verify(csrf_token)
        job = load_job(session, job_id)
        if job.source_type != SourceType.INCOMING.value:
            raise HTTPException(status_code=409, detail="Only server inbox imports can be cleared this way")
        if job.state not in {
            JobState.FAILED.value,
            JobState.NEEDS_REVIEW.value,
            JobState.CANCELLED.value,
        }:
            raise HTTPException(
                status_code=409,
                detail="Only failed, review-paused, or cancelled inbox imports can be cleared",
            )
        if job.state != JobState.CANCELLED.value:
            job.state = JobState.CANCELLED.value
            job.retryable = False
            job.finished_at = utcnow()
            add_event(session, job, "Inbox import cancelled before redownload")
            sync_acquisitions_for_import_job(session, job)
        session.flush()
        session.commit()
        session.info[_SESSION_ALREADY_COMMITTED_KEY] = True
        cleanup_staged_job(settings, job_id)
        return RedirectResponse(url="/add", status_code=status.HTTP_303_SEE_OTHER)

    def load_job(session: Session, job_id: str) -> Job:
        job = session.scalar(
            select(Job)
            .where(Job.id == job_id)
            .options(selectinload(Job.events), selectinload(Job.tracks), selectinload(Job.candidates))
        )
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str, session: Session = Depends(session_dependency)) -> HTMLResponse:
        job = load_job(session, job_id)
        return templates.TemplateResponse(
            request=request,
            name="job.html",
            context=common_context(
                request,
                job=job,
                candidate_views=_candidate_presentations(job),
            ),
        )

    @app.get("/jobs/{job_id}/status", response_class=HTMLResponse)
    def job_status(request: Request, job_id: str, session: Session = Depends(session_dependency)) -> HTMLResponse:
        job = load_job(session, job_id)
        return templates.TemplateResponse(
            request=request,
            name="partials/job_status.html",
            context=common_context(
                request,
                job=job,
                candidate_views=_candidate_presentations(job),
            ),
        )

    @app.post("/jobs/{job_id}/cancel")
    def cancel_import_job(
        request: Request,
        job_id: str,
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> RedirectResponse:
        request.app.state.csrf.verify(csrf_token)
        job = _cancel_import_before_processing(session, job_id)
        return RedirectResponse(
            url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post("/jobs/{job_id}/review")
    def select_release(
        request: Request,
        job_id: str,
        selection: str = Form(...),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> RedirectResponse:
        request.app.state.csrf.verify(csrf_token)
        job = load_job(session, job_id)
        if job.state != JobState.NEEDS_REVIEW.value:
            raise HTTPException(status_code=409, detail="This job is not waiting for metadata review")
        for candidate in job.candidates:
            candidate.selected = False
        if selection == "incoming":
            job.selected_release_id = "__incoming__"
            message = "Incoming metadata selected; job queued for processing"
        else:
            candidate = session.scalar(
                select(ReleaseCandidate).where(
                    ReleaseCandidate.id == int(selection), ReleaseCandidate.job_id == job.id
                )
            ) if selection.isdigit() else None
            if candidate is None:
                raise HTTPException(status_code=400, detail="Invalid release selection")
            candidate.selected = True
            job.selected_release_id = candidate.musicbrainz_release_id
            job.match_confidence = candidate.score
            message = f"MusicBrainz release {candidate.musicbrainz_release_id} selected"
        job.review_reason = None
        job.review_kind = None
        retry_job(session, job, message=message)
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/jobs/{job_id}/retry")
    def retry_failed_job(
        request: Request,
        job_id: str,
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> RedirectResponse:
        request.app.state.csrf.verify(csrf_token)
        job = load_job(session, job_id)
        try:
            retry_job(session, job)
        except InvalidStateTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/jobs/{job_id}/retry-jellyfin")
    def retry_jellyfin(
        request: Request,
        job_id: str,
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> RedirectResponse:
        request.app.state.csrf.verify(csrf_token)
        job = load_job(session, job_id)
        if (
            job.state != JobState.COMPLETE.value
            or job.jellyfin_state != JellyfinState.FAILED.value
            or not job.jellyfin_retryable
        ):
            raise HTTPException(status_code=409, detail="This Jellyfin refresh is not retryable")
        job.jellyfin_retry_requested = True
        add_event(session, job, "Jellyfin refresh retry requested")
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/review", response_class=HTMLResponse)
    def review_queue(request: Request, session: Session = Depends(session_dependency)) -> HTMLResponse:
        acquisitions = session.scalars(
            select(AcquisitionJob)
            .outerjoin(Job, AcquisitionJob.associated_import_job_id == Job.id)
            .where(
                or_(
                    AcquisitionJob.state == AcquisitionState.NEEDS_REVIEW.value,
                    Job.state == JobState.NEEDS_REVIEW.value,
                )
            )
            .options(selectinload(AcquisitionJob.import_job))
            .order_by(AcquisitionJob.updated_at.desc())
            .limit(100)
        ).all()
        associated = exists(
            select(AcquisitionJob.id).where(AcquisitionJob.associated_import_job_id == Job.id)
        )
        jobs = session.scalars(
            select(Job)
            .where(Job.state == JobState.NEEDS_REVIEW.value, ~associated)
            .order_by(Job.updated_at.desc())
            .limit(100)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="review/index.html",
            context=common_context(request, acquisitions=acquisitions, jobs=jobs),
        )

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, session: Session = Depends(session_dependency)) -> HTMLResponse:
        acquisitions = session.scalars(
            select(AcquisitionJob)
            .options(selectinload(AcquisitionJob.import_job))
            .order_by(AcquisitionJob.updated_at.desc())
            .limit(100)
        ).all()
        associated = exists(
            select(AcquisitionJob.id).where(AcquisitionJob.associated_import_job_id == Job.id)
        )
        jobs = session.scalars(
            select(Job).where(~associated).order_by(Job.updated_at.desc()).limit(100)
        ).all()
        items = [
            {
                "kind": "acquisition",
                "title": item.display_title,
                "state": _effective_acquisition_state(item),
                "track_count": item.import_job.track_count if item.import_job else 0,
                "jellyfin_state": item.import_job.jellyfin_state if item.import_job else None,
                "updated_at": item.updated_at,
                "url": f"/acquisitions/{item.id}",
                "source": item.source_type,
            }
            for item in acquisitions
        ]
        items.extend(
            {
                "kind": "import",
                "title": item.display_name,
                "state": item.state,
                "track_count": item.track_count,
                "jellyfin_state": item.jellyfin_state,
                "updated_at": item.updated_at,
                "url": f"/jobs/{item.id}",
                "source": item.source_type,
            }
            for item in jobs
        )
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return templates.TemplateResponse(
            request=request,
            name="history.html",
            context=common_context(request, history_items=items[:100], jobs=jobs),
        )

    @app.get("/library", response_class=HTMLResponse)
    def library(
        request: Request,
        scan: str | None = None,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        selected_scan = session.get(LibraryScanRun, scan) if scan else None
        return templates.TemplateResponse(
            request=request,
            name="library/index.html",
            context=common_context(
                request,
                library_metrics=_library_metrics(session),
                quality_metrics=_quality_counts(session),
                health_metrics=_health_counts(session),
                storage_metrics=_storage_metrics(session),
                latest_scan=_latest_scan(session),
                selected_scan=selected_scan,
            ),
        )

    @app.post("/library/scans")
    def request_library_scan(
        request: Request,
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ) -> RedirectResponse:
        request.app.state.csrf.verify(csrf_token)
        active_scan = enqueue_library_scan(session, reason="manual")
        return RedirectResponse(
            url=f"/library?scan={active_scan.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get(
        "/library/scans/{scan_id}/status",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def library_scan_status(
        request: Request,
        scan_id: str,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        scan_run = session.get(LibraryScanRun, scan_id)
        if scan_run is None:
            raise HTTPException(status_code=404, detail="Library scan not found")
        return templates.TemplateResponse(
            request=request,
            name="partials/library_scan_status.html",
            context=common_context(
                request,
                scan_run=scan_run,
                poll_terminal=scan_run.state
                in {LibraryScanState.COMPLETE.value, LibraryScanState.FAILED.value},
            ),
        )

    @app.get("/library/artists", response_class=HTMLResponse)
    def library_artists(
        request: Request,
        q: str = "",
        page: int = 1,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        page = max(page, 1)
        statement = select(LibraryArtist)
        normalized_query = q.strip()[:200]
        if normalized_query:
            statement = statement.where(LibraryArtist.canonical_name.ilike(f"%{normalized_query}%"))
        total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
        artists = session.scalars(
            statement.order_by(LibraryArtist.canonical_name)
            .offset((page - 1) * _PAGE_SIZE)
            .limit(_PAGE_SIZE)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="library/artists.html",
            context=common_context(
                request,
                artists=artists,
                q=normalized_query,
                page=page,
                total=total,
                page_size=_PAGE_SIZE,
            ),
        )

    @app.get("/library/artists/{artist_id}", response_class=HTMLResponse)
    def library_artist(
        request: Request,
        artist_id: int,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        artist = session.get(LibraryArtist, artist_id)
        if artist is None:
            raise HTTPException(status_code=404, detail="Artist not found")
        albums = session.scalars(
            select(LibraryAlbum)
            .where(LibraryAlbum.artist_id == artist.id)
            .order_by(LibraryAlbum.year.desc(), LibraryAlbum.title)
            .limit(500)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="library/artist.html",
            context=common_context(request, artist=artist, albums=albums),
        )

    @app.get("/library/albums", response_class=HTMLResponse)
    def library_albums(
        request: Request,
        q: str = "",
        page: int = 1,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        page = max(page, 1)
        normalized_query = q.strip()[:200]
        statement = select(LibraryAlbum).options(selectinload(LibraryAlbum.artist))
        if normalized_query:
            statement = statement.where(
                or_(
                    LibraryAlbum.title.ilike(f"%{normalized_query}%"),
                    LibraryAlbum.album_artist.ilike(f"%{normalized_query}%"),
                )
            )
        total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
        albums = session.scalars(
            statement.order_by(LibraryAlbum.album_artist, LibraryAlbum.year.desc(), LibraryAlbum.title)
            .offset((page - 1) * _PAGE_SIZE)
            .limit(_PAGE_SIZE)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="library/albums.html",
            context=common_context(
                request,
                albums=albums,
                q=normalized_query,
                page=page,
                total=total,
                page_size=_PAGE_SIZE,
            ),
        )

    @app.get("/library/albums/{album_id}", response_class=HTMLResponse)
    def library_album(
        request: Request,
        album_id: int,
        session: Session = Depends(session_dependency),
    ) -> HTMLResponse:
        album = session.scalar(
            select(LibraryAlbum)
            .where(LibraryAlbum.id == album_id)
            .options(selectinload(LibraryAlbum.artist))
        )
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found")
        tracks = session.scalars(
            select(LibraryInventoryTrack)
            .where(LibraryInventoryTrack.album_id == album.id)
            .order_by(
                LibraryInventoryTrack.disc_number,
                LibraryInventoryTrack.track_number,
                LibraryInventoryTrack.title,
            )
            .limit(1000)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="library/album.html",
            context=common_context(request, album=album, tracks=tracks),
        )

    @app.get("/library/health", response_class=HTMLResponse)
    def library_health(
        request: Request, session: Session = Depends(session_dependency)
    ) -> HTMLResponse:
        latest_scan = _latest_scan(session)
        latest_complete_scan = _latest_completed_scan(session)
        missing_artwork = session.scalars(
            select(LibraryAlbum)
            .where(LibraryAlbum.artwork_present.is_(False))
            .options(selectinload(LibraryAlbum.artist))
            .order_by(LibraryAlbum.album_artist, LibraryAlbum.title)
            .limit(50)
        ).all()
        metadata_issues = session.scalars(
            select(LibraryInventoryTrack)
            .where(LibraryInventoryTrack.metadata_complete.is_(False))
            .order_by(LibraryInventoryTrack.relative_path)
            .limit(100)
        ).all()
        low_quality = session.scalars(
            select(LibraryInventoryTrack)
            .where(
                func.lower(LibraryInventoryTrack.codec) == "mp3",
                LibraryInventoryTrack.bitrate.is_not(None),
                LibraryInventoryTrack.bitrate < 320_000,
            )
            .order_by(LibraryInventoryTrack.bitrate)
            .limit(100)
        ).all()
        duplicate_groups = session.execute(
            select(
                LibraryInventoryTrack.sha256,
                func.count(LibraryInventoryTrack.id),
            )
            .where(LibraryInventoryTrack.sha256.is_not(None))
            .group_by(LibraryInventoryTrack.sha256)
            .having(func.count(LibraryInventoryTrack.id) > 1)
            .limit(50)
        ).all()
        duplicate_recording_groups = session.execute(
            select(
                LibraryInventoryTrack.musicbrainz_recording_id,
                func.count(LibraryInventoryTrack.id),
            )
            .where(LibraryInventoryTrack.musicbrainz_recording_id.is_not(None))
            .group_by(LibraryInventoryTrack.musicbrainz_recording_id)
            .having(func.count(LibraryInventoryTrack.id) > 1)
            .limit(50)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="library/health.html",
            context=common_context(
                request,
                health_metrics=_health_counts(session),
                missing_artwork=missing_artwork,
                metadata_issues=metadata_issues,
                low_quality=low_quality,
                duplicate_groups=duplicate_groups,
                duplicate_recording_groups=duplicate_recording_groups,
                latest_scan=latest_scan,
                latest_complete_scan=latest_complete_scan,
            ),
        )

    @app.get("/api/status")
    def api_status(session: Session = Depends(session_dependency)) -> JSONResponse:
        latest_scan = _latest_scan(session)
        latest_complete = _latest_completed_scan(session)
        active = _active_presentations(session, limit=10)
        return JSONResponse(
            {
                "schema_version": 1,
                "generated_at": _iso_utc(datetime.now(timezone.utc)),
                "library_scan": {
                    "state": latest_scan.state if latest_scan else "NOT_RUN",
                    "last_completed_at": _iso_utc(
                        latest_complete.finished_at if latest_complete else None
                    ),
                },
                "activity": _activity_metrics(session),
                "active": [
                    {
                        "id": item["id"],
                        "kind": item["kind"],
                        "title": item["title"],
                        "state": item["state"],
                        "updated_at": _iso_utc(item["updated_at"]),
                        "detail_url": item["url"],
                    }
                    for item in active
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/stats")
    def api_stats(session: Session = Depends(session_dependency)) -> JSONResponse:
        return JSONResponse(
            {
                "schema_version": 1,
                "generated_at": _iso_utc(datetime.now(timezone.utc)),
                "library": _library_metrics(session),
                "quality": _quality_counts(session),
                "activity": _activity_metrics(session),
                "health": _health_counts(session),
                "storage": _storage_metrics(session),
            },
            headers={"Cache-Control": "public, max-age=15"},
        )

    @app.get("/api/jobs/{job_id}")
    def job_api(job_id: str, session: Session = Depends(session_dependency)) -> dict[str, Any]:
        job = load_job(session, job_id)
        return {
            "id": job.id,
            "kind": job.kind,
            "state": job.state,
            "display_name": job.display_name,
            "track_count": job.track_count,
            "output_relative_path": job.output_relative_path,
            "error": job.error_message,
            "review_reason": job.review_reason,
            "jellyfin_state": job.jellyfin_state,
            "jellyfin_retryable": job.jellyfin_retryable,
            "jellyfin_retry_requested": job.jellyfin_retry_requested,
            "created_at": _iso_utc(job.created_at),
            "updated_at": _iso_utc(job.updated_at),
        }

    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "foxden_music.web:app",
        host=settings.web_host,
        port=settings.web_port,
        workers=settings.web_workers,
        log_config=None,
    )
