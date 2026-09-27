from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    QUEUED = "QUEUED"
    STAGING = "STAGING"
    EXTRACTING = "EXTRACTING"
    INSPECTING = "INSPECTING"
    MATCHING_METADATA = "MATCHING_METADATA"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    TAGGING = "TAGGING"
    ORGANIZING = "ORGANIZING"
    VALIDATING = "VALIDATING"
    IMPORTING = "IMPORTING"
    JELLYFIN_SCAN = "JELLYFIN_SCAN"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobKind(StrEnum):
    ALBUM_IMPORT = "ALBUM_IMPORT"
    ACQUISITION = "ACQUISITION"


class SourceType(StrEnum):
    UPLOAD = "UPLOAD"
    INCOMING = "INCOMING"
    SPOTIFY = "SPOTIFY"


class JellyfinState(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DuplicateStatus(StrEnum):
    NONE = "NONE"
    EXACT_IN_JOB = "EXACT_IN_JOB"
    EXACT_IN_LIBRARY = "EXACT_IN_LIBRARY"
    RECORDING_CONFLICT = "RECORDING_CONFLICT"


class AcquisitionProvider(StrEnum):
    SPOTIDOWNLOADER_MANUAL = "SPOTIDOWNLOADER_MANUAL"


class AcquisitionSourceType(StrEnum):
    TRACK = "TRACK"
    ALBUM = "ALBUM"
    PLAYLIST = "PLAYLIST"


class AcquisitionState(StrEnum):
    QUEUED = "QUEUED"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_DOWNLOAD = "WAITING_FOR_DOWNLOAD"
    FILE_RECEIVED = "FILE_RECEIVED"
    IMPORT_STARTED = "IMPORT_STARTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PreferredFormat(StrEnum):
    FLAC = "FLAC"
    MP3_320 = "MP3_320"


class LibraryScanState(StrEnum):
    QUEUED = "QUEUED"
    SCANNING = "SCANNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


ACTIVE_JOB_STATES = frozenset(
    {
        JobState.STAGING,
        JobState.EXTRACTING,
        JobState.INSPECTING,
        JobState.MATCHING_METADATA,
        JobState.TAGGING,
        JobState.ORGANIZING,
        JobState.VALIDATING,
        JobState.IMPORTING,
        JobState.JELLYFIN_SCAN,
    }
)
