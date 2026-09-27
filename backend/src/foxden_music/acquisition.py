from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .enums import (
    ACTIVE_JOB_STATES,
    AcquisitionProvider,
    AcquisitionSourceType,
    AcquisitionState,
    JobKind,
    JobState,
    PreferredFormat,
)
from .models import AcquisitionEvent, AcquisitionJob, Job, utcnow


SPOTIFY_HOST = "open.spotify.com"
MAX_SPOTIFY_URL_CHARS = 2_048
DEFAULT_BATCH_MAX_ITEMS = 100
DEFAULT_BATCH_MAX_CHARS = 65_536

_SPOTIFY_PATH = re.compile(
    r"/(?P<source_type>track|album|playlist)/(?P<identifier>[A-Za-z0-9]{22})/?"
)
_EVENT_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


class AcquisitionError(ValueError):
    """Base class for safe, user-presentable acquisition validation errors."""


class SpotifyReferenceError(AcquisitionError):
    pass


class AcquisitionBatchError(AcquisitionError):
    pass


class ProviderConfigurationError(AcquisitionError):
    pass


class InvalidAcquisitionTransition(AcquisitionError):
    pass


@dataclass(frozen=True, slots=True)
class SpotifyReference:
    """A normalized Spotify reference that is safe to persist and display."""

    canonical_url: str
    source_type: AcquisitionSourceType
    identifier: str


@dataclass(frozen=True, slots=True)
class ManualProviderInstructions:
    """Provider-neutral instructions for a human-assisted acquisition."""

    provider: AcquisitionProvider
    provider_name: str
    public_url: str
    source_url: str
    preferred_format: PreferredFormat
    steps: tuple[str, ...]


class ManualAcquisitionProvider(Protocol):
    provider: AcquisitionProvider
    display_name: str

    @property
    def public_url(self) -> str: ...

    def instructions(self, acquisition: AcquisitionJob) -> ManualProviderInstructions: ...


def _has_unsafe_url_characters(value: str) -> bool:
    return "\\" in value or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _split_url(value: str, *, label: str) -> SplitResult:
    if not isinstance(value, str):
        raise SpotifyReferenceError(f"{label} must be text")
    if not value or len(value) > MAX_SPOTIFY_URL_CHARS:
        raise SpotifyReferenceError(f"{label} is empty or too long")
    if value != value.strip() or any(character.isspace() for character in value):
        raise SpotifyReferenceError(f"{label} must not contain whitespace")
    if _has_unsafe_url_characters(value):
        raise SpotifyReferenceError(f"{label} contains unsafe characters")
    try:
        parsed = urlsplit(value)
        # Accessing port performs urllib's port and bracket validation.
        parsed.port
    except ValueError as exc:
        raise SpotifyReferenceError(f"{label} is malformed") from exc
    return parsed


def parse_spotify_url(value: str) -> SpotifyReference:
    """Validate one Spotify URL without making any network request.

    Only canonical web links for tracks, albums, and playlists are accepted.
    Share/tracking query parameters and fragments are intentionally discarded so
    they are never logged, persisted, or forwarded to an acquisition provider.
    """

    parsed = _split_url(value, label="Spotify URL")
    if parsed.scheme.casefold() != "https":
        raise SpotifyReferenceError("Spotify URLs must use https")
    if parsed.username is not None or parsed.password is not None:
        raise SpotifyReferenceError("Spotify URLs must not contain credentials")
    if parsed.hostname is None or parsed.hostname.casefold() != SPOTIFY_HOST:
        raise SpotifyReferenceError("Only open.spotify.com URLs are supported")
    if parsed.port is not None or parsed.netloc.casefold() != SPOTIFY_HOST:
        raise SpotifyReferenceError("Spotify URLs must not contain a port or alternate authority")

    match = _SPOTIFY_PATH.fullmatch(parsed.path)
    if match is None:
        raise SpotifyReferenceError("Expected a Spotify track, album, or playlist URL")
    source_type = AcquisitionSourceType(match.group("source_type").upper())
    identifier = match.group("identifier")
    canonical_url = f"https://{SPOTIFY_HOST}/{match.group('source_type')}/{identifier}"
    return SpotifyReference(
        canonical_url=canonical_url,
        source_type=source_type,
        identifier=identifier,
    )


def parse_spotify_batch(
    value: str,
    *,
    max_items: int = DEFAULT_BATCH_MAX_ITEMS,
    max_input_chars: int = DEFAULT_BATCH_MAX_CHARS,
) -> list[SpotifyReference]:
    """Parse newline-separated Spotify URLs atomically and de-duplicate them."""

    if not isinstance(value, str):
        raise AcquisitionBatchError("Spotify URL batch must be text")
    if max_items < 1 or max_input_chars < 1:
        raise ValueError("Batch limits must be positive")
    if len(value) > max_input_chars:
        raise AcquisitionBatchError("Spotify URL batch is too large")

    numbered_lines = [
        (line_number, line.strip())
        for line_number, line in enumerate(value.splitlines(), start=1)
        if line.strip()
    ]
    if not numbered_lines:
        raise AcquisitionBatchError("Enter at least one Spotify URL")
    if len(numbered_lines) > max_items:
        raise AcquisitionBatchError(f"A batch may contain at most {max_items} URLs")

    references: list[SpotifyReference] = []
    seen: set[str] = set()
    for line_number, line in numbered_lines:
        try:
            reference = parse_spotify_url(line)
        except SpotifyReferenceError as exc:
            raise AcquisitionBatchError(f"Line {line_number}: {exc}") from exc
        if reference.canonical_url in seen:
            continue
        seen.add(reference.canonical_url)
        references.append(reference)
    return references


def _validated_provider_url(value: str) -> str:
    try:
        parsed = _split_url(value, label="Provider URL")
    except SpotifyReferenceError as exc:
        raise ProviderConfigurationError(str(exc)) from exc
    if parsed.scheme.casefold() != "https" or parsed.hostname is None:
        raise ProviderConfigurationError("Provider URL must be an absolute https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderConfigurationError("Provider URL must not contain credentials")
    if parsed.port not in {None, 443}:
        raise ProviderConfigurationError("Provider URL may only use the standard https port")
    if parsed.fragment:
        raise ProviderConfigurationError("Provider URL must not contain a fragment")
    # Normalize only the scheme and drop an explicit default port. The configured
    # path/query remain static operator-controlled values; no source URL is added.
    host = parsed.hostname.casefold()
    normalized = urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    return normalized


class SpotiDownloaderManualProvider:
    """Human-assisted provider adapter; it performs no browser or HTTP work."""

    provider = AcquisitionProvider.SPOTIDOWNLOADER_MANUAL
    display_name = "SpotiDownloader (manual)"

    def __init__(self, public_url: str):
        self._public_url = _validated_provider_url(public_url)

    @property
    def public_url(self) -> str:
        return self._public_url

    def instructions(self, acquisition: AcquisitionJob) -> ManualProviderInstructions:
        if acquisition.provider != self.provider.value:
            raise AcquisitionError("Acquisition job belongs to a different provider")
        try:
            preferred_format = PreferredFormat(acquisition.preferred_format)
        except ValueError as exc:
            raise AcquisitionError("Acquisition job has an unsupported preferred format") from exc
        return ManualProviderInstructions(
            provider=self.provider,
            provider_name=self.display_name,
            public_url=self.public_url,
            source_url=acquisition.source_url,
            preferred_format=preferred_format,
            steps=(
                "Copy the Spotify URL.",
                "Open the configured provider page.",
                "Paste the URL and complete any CAPTCHA yourself.",
                f"Download the requested {preferred_format.value.replace('_', ' ')} file or ZIP.",
                "Return to Fox Den Music and upload the completed download.",
            ),
        )


def manual_provider(
    provider: AcquisitionProvider | str,
    *,
    public_url: str,
) -> ManualAcquisitionProvider:
    """Build a manual provider from an explicitly configured static URL."""

    try:
        provider_value = AcquisitionProvider(provider)
    except ValueError as exc:
        raise ProviderConfigurationError("Unsupported acquisition provider") from exc
    if provider_value is AcquisitionProvider.SPOTIDOWNLOADER_MANUAL:
        return SpotiDownloaderManualProvider(public_url)
    raise ProviderConfigurationError("Unsupported acquisition provider")


ALLOWED_ACQUISITION_TRANSITIONS: dict[AcquisitionState, frozenset[AcquisitionState]] = {
    AcquisitionState.QUEUED: frozenset(
        {
            AcquisitionState.WAITING_FOR_USER,
            AcquisitionState.WAITING_FOR_DOWNLOAD,
            AcquisitionState.FILE_RECEIVED,
            AcquisitionState.FAILED,
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.WAITING_FOR_USER: frozenset(
        {
            AcquisitionState.WAITING_FOR_DOWNLOAD,
            AcquisitionState.FILE_RECEIVED,
            AcquisitionState.FAILED,
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.WAITING_FOR_DOWNLOAD: frozenset(
        {
            AcquisitionState.WAITING_FOR_USER,
            AcquisitionState.FILE_RECEIVED,
            AcquisitionState.FAILED,
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.FILE_RECEIVED: frozenset(
        {
            AcquisitionState.IMPORT_STARTED,
            AcquisitionState.FAILED,
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.IMPORT_STARTED: frozenset(
        {
            AcquisitionState.NEEDS_REVIEW,
            AcquisitionState.COMPLETE,
            AcquisitionState.FAILED,
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.NEEDS_REVIEW: frozenset(
        {
            AcquisitionState.IMPORT_STARTED,
            AcquisitionState.COMPLETE,
            AcquisitionState.FAILED,
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.COMPLETE: frozenset(),
    AcquisitionState.FAILED: frozenset(
        {
            AcquisitionState.QUEUED,
            AcquisitionState.WAITING_FOR_USER,
            AcquisitionState.IMPORT_STARTED,
            AcquisitionState.NEEDS_REVIEW,
            AcquisitionState.COMPLETE,
            AcquisitionState.CANCELLED,
        }
    ),
    AcquisitionState.CANCELLED: frozenset(
        {AcquisitionState.QUEUED, AcquisitionState.WAITING_FOR_USER}
    ),
}


def add_acquisition_event(
    session: Session,
    acquisition: AcquisitionJob,
    message: str,
    *,
    level: str = "INFO",
) -> AcquisitionEvent:
    normalized_level = level.strip().upper()
    if normalized_level not in _EVENT_LEVELS:
        raise AcquisitionError("Unsupported acquisition event level")
    safe_message = str(message).strip()
    if not safe_message:
        raise AcquisitionError("Acquisition event message must not be empty")
    event = AcquisitionEvent(
        acquisition_job_id=acquisition.id,
        state=acquisition.state,
        level=normalized_level,
        message=safe_message[:1000],
    )
    session.add(event)
    acquisition.updated_at = utcnow()
    return event


def transition_acquisition(
    session: Session,
    acquisition: AcquisitionJob,
    new_state: AcquisitionState | str,
    message: str,
    *,
    level: str = "INFO",
) -> None:
    try:
        old_state = AcquisitionState(acquisition.state)
        target_state = AcquisitionState(new_state)
    except ValueError as exc:
        raise InvalidAcquisitionTransition("Acquisition job contains an unknown state") from exc
    if target_state == old_state:
        return
    if target_state not in ALLOWED_ACQUISITION_TRANSITIONS[old_state]:
        raise InvalidAcquisitionTransition(
            f"Cannot transition acquisition from {old_state.value} to {target_state.value}"
        )

    now = utcnow()
    acquisition.state = target_state.value
    acquisition.updated_at = now
    if target_state is AcquisitionState.FILE_RECEIVED and acquisition.file_received_at is None:
        acquisition.file_received_at = now
    if target_state in {
        AcquisitionState.COMPLETE,
        AcquisitionState.FAILED,
        AcquisitionState.CANCELLED,
    }:
        acquisition.finished_at = now
    elif old_state in {AcquisitionState.FAILED, AcquisitionState.CANCELLED}:
        acquisition.finished_at = None
        acquisition.error_message = None
    add_acquisition_event(session, acquisition, message, level=level)


def _coerce_preferred_format(value: PreferredFormat | str) -> PreferredFormat:
    try:
        return PreferredFormat(value)
    except ValueError as exc:
        raise AcquisitionBatchError("Preferred format must be FLAC or MP3 320") from exc


def _coerce_provider(value: AcquisitionProvider | str) -> AcquisitionProvider:
    try:
        return AcquisitionProvider(value)
    except ValueError as exc:
        raise AcquisitionBatchError("Unsupported acquisition provider") from exc


def create_acquisition_batch(
    session: Session,
    value: str,
    *,
    preferred_format: PreferredFormat | str = PreferredFormat.FLAC,
    provider: AcquisitionProvider | str = AcquisitionProvider.SPOTIDOWNLOADER_MANUAL,
    max_items: int = DEFAULT_BATCH_MAX_ITEMS,
) -> list[AcquisitionJob]:
    """Validate an entire batch, create jobs, and flush without committing."""

    references = parse_spotify_batch(value, max_items=max_items)
    requested_format = _coerce_preferred_format(preferred_format)
    requested_provider = _coerce_provider(provider)
    acquisitions: list[AcquisitionJob] = []
    for reference in references:
        acquisition_id = str(uuid.uuid4())
        display_kind = reference.source_type.value.title()
        acquisition = AcquisitionJob(
            id=acquisition_id,
            provider=requested_provider.value,
            source_url=reference.canonical_url,
            source_type=reference.source_type.value,
            source_identifier=reference.identifier,
            display_title=f"Spotify {display_kind} · {reference.identifier}",
            state=AcquisitionState.QUEUED.value,
            preferred_format=requested_format.value,
            acquisition_relative_directory=f"acquisitions/{acquisition_id}",
        )
        session.add(acquisition)
        add_acquisition_event(session, acquisition, f"Spotify {display_kind.lower()} queued")
        transition_acquisition(
            session,
            acquisition,
            AcquisitionState.WAITING_FOR_USER,
            "Ready for human-assisted acquisition",
        )
        acquisitions.append(acquisition)
    session.flush()
    return acquisitions


_ACTIVE_IMPORT_STATE_VALUES = frozenset(state.value for state in ACTIVE_JOB_STATES) | {
    JobState.QUEUED.value
}


def _acquisition_target_for_import(import_job: Job) -> AcquisitionState | None:
    if import_job.state in _ACTIVE_IMPORT_STATE_VALUES:
        return AcquisitionState.IMPORT_STARTED
    if import_job.state == JobState.NEEDS_REVIEW.value:
        return AcquisitionState.NEEDS_REVIEW
    if import_job.state == JobState.COMPLETE.value:
        return AcquisitionState.COMPLETE
    if import_job.state == JobState.FAILED.value:
        return AcquisitionState.FAILED
    if import_job.state == JobState.CANCELLED.value:
        return AcquisitionState.CANCELLED
    return None


def sync_acquisition_from_import(session: Session, acquisition: AcquisitionJob) -> bool:
    """Project a linked Stage 1 import state onto one acquisition job.

    Returns True only when the acquisition state changed. This helper never
    changes the import job and never commits the caller's transaction.
    """

    if not acquisition.associated_import_job_id:
        return False
    import_job = session.get(Job, acquisition.associated_import_job_id)
    if import_job is None:
        return False
    if import_job.kind != JobKind.ALBUM_IMPORT.value:
        raise AcquisitionError("Acquisition may only be linked to an album import job")
    target = _acquisition_target_for_import(import_job)
    if target is None or acquisition.state == target.value:
        return False

    current = AcquisitionState(acquisition.state)
    if current in {
        AcquisitionState.QUEUED,
        AcquisitionState.WAITING_FOR_USER,
        AcquisitionState.WAITING_FOR_DOWNLOAD,
    }:
        transition_acquisition(
            session,
            acquisition,
            AcquisitionState.FILE_RECEIVED,
            "Associated download was received",
        )

    messages = {
        AcquisitionState.IMPORT_STARTED: "Associated import is processing",
        AcquisitionState.NEEDS_REVIEW: "Associated import needs review",
        AcquisitionState.COMPLETE: "Associated import completed",
        AcquisitionState.FAILED: "Associated import failed",
        AcquisitionState.CANCELLED: "Associated import was cancelled by the user",
    }
    if target is AcquisitionState.FAILED:
        acquisition.error_message = import_job.error_message or "Associated import failed"
    else:
        acquisition.error_message = None
    transition_acquisition(
        session,
        acquisition,
        target,
        messages[target],
        level="ERROR" if target is AcquisitionState.FAILED else "INFO",
    )
    return True


def sync_acquisitions_for_import_job(session: Session, import_job: Job) -> list[AcquisitionJob]:
    """Synchronize and return all acquisitions linked to one import job."""

    if import_job.id is None:
        session.flush()
    acquisitions = session.scalars(
        select(AcquisitionJob)
        .where(AcquisitionJob.associated_import_job_id == import_job.id)
        .order_by(AcquisitionJob.created_at, AcquisitionJob.id)
    ).all()
    for acquisition in acquisitions:
        sync_acquisition_from_import(session, acquisition)
    return acquisitions
