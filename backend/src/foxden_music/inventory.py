from __future__ import annotations

import os
import re
import shutil
import stat
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from sqlalchemy import Integer, case, cast, delete, func, select, update
from sqlalchemy.orm import Session

from .audio import AudioInspection, SUPPORTED_AUDIO_EXTENSIONS, inspect_audio
from .config import Settings
from .database import Database
from .enums import LibraryScanState
from .metadata import TagHints, read_tag_hints
from .models import (
    DatabaseMetadata,
    LibraryAlbum,
    LibraryArtist,
    LibraryInventoryTrack,
    LibraryScanRun,
    StorageSnapshot,
    utcnow,
)


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_ARTWORK_NAMES = frozenset(
    {
        "cover.jpg",
        "cover.jpeg",
        "cover.png",
        "cover.webp",
        "folder.jpg",
        "folder.jpeg",
        "folder.png",
        "front.jpg",
        "front.jpeg",
        "front.png",
    }
)
_UNKNOWN_ARTIST = "Unknown Artist"
_UNKNOWN_ALBUM = "Unknown Album"
_MAX_QUERY_LIMIT = 500
_DISC_DIRECTORY = re.compile(r"^(?:cd|disc|disk)[\s._-]*0*\d+$", re.IGNORECASE)
_FAILED_SCAN_RETRY_SECONDS = 5 * 60


class InventoryScanError(RuntimeError):
    """A concise inventory error that is safe to persist and display."""


@dataclass(frozen=True, slots=True)
class _DiscoveredFile:
    path: Path
    relative_path: str
    file_size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _ExistingTrack:
    id: int
    file_size: int
    mtime_ns: int
    sha256: str | None
    inspection_error: str | None


@dataclass(frozen=True, slots=True)
class _InspectedFile:
    discovered: _DiscoveredFile
    audio: AudioInspection | None
    tags: TagHints
    inspection_error: str | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _display(value: str | None, fallback: str) -> str:
    if value:
        cleaned = re.sub(r"[\x00-\x1f\x7f]", "", unicodedata.normalize("NFC", value)).strip()
        if cleaned:
            return cleaned[:1000]
    return fallback


def _is_reparse_point(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & _REPARSE_POINT)


def _validate_library_root(root: Path) -> Path:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise InventoryScanError("The music library root is unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode) or _is_reparse_point(root_stat):
        raise InventoryScanError("The music library root must be a real directory, not a link")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise InventoryScanError("The music library root could not be resolved") from exc


def _safe_relative(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise InventoryScanError("A discovered music path escaped the library root") from exc
    return unicodedata.normalize("NFC", relative.as_posix())


def _discover_audio_files(root: Path) -> list[_DiscoveredFile]:
    """Walk a trusted root without following symlinks or reparse points."""

    resolved_root = _validate_library_root(root)
    discovered: list[_DiscoveredFile] = []
    pending = [resolved_root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError as exc:
            raise InventoryScanError("The music library contains an unreadable directory") from exc
        for entry in entries:
            if entry.name.casefold() == ".imports":
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise InventoryScanError("A music library entry could not be inspected safely") from exc
            if stat.S_ISLNK(entry_stat.st_mode) or _is_reparse_point(entry_stat):
                continue
            path = Path(entry.path)
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                continue
            if path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue
            # Every traversed ancestor was checked without following links. Resolve once
            # more before accepting the entry to catch a replacement during traversal.
            try:
                path.resolve(strict=True).relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                raise InventoryScanError("A discovered music file escaped the library root") from exc
            discovered.append(
                _DiscoveredFile(
                    path=path,
                    relative_path=_safe_relative(resolved_root, path),
                    file_size=entry_stat.st_size,
                    mtime_ns=entry_stat.st_mtime_ns,
                )
            )
    discovered.sort(key=lambda item: item.relative_path.casefold())
    return discovered


def _safe_error(exc: BaseException, *, category: str) -> str:
    if isinstance(exc, InventoryScanError):
        detail = str(exc)
    elif isinstance(exc, OSError):
        detail = "File access failed"
    else:
        # The audio and metadata layers deliberately expose concise errors. Unknown
        # exceptions are not persisted because they may contain local paths.
        module = exc.__class__.__module__
        if module.startswith("foxden_music"):
            detail = str(exc)
        else:
            detail = "Unexpected inspection error"
    detail = re.sub(r"[\x00-\x1f\x7f]", " ", detail).strip()
    return f"{category}: {detail or 'inspection failed'}"[:500]


def _verify_unchanged_file(root: Path, item: _DiscoveredFile) -> None:
    try:
        after = item.path.lstat()
        item.path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise InventoryScanError("A music file changed location during inspection") from exc
    if not stat.S_ISREG(after.st_mode) or stat.S_ISLNK(after.st_mode) or _is_reparse_point(after):
        raise InventoryScanError("A music file became unsafe during inspection")
    if after.st_size != item.file_size or after.st_mtime_ns != item.mtime_ns:
        raise InventoryScanError("A music file changed while it was being inspected")


def _inspect_file(root: Path, item: _DiscoveredFile, settings: Settings) -> _InspectedFile:
    errors: list[str] = []
    audio: AudioInspection | None = None
    tags = TagHints()
    try:
        audio = inspect_audio(item.path, settings)
    except Exception as exc:
        errors.append(_safe_error(exc, category="audio"))
    try:
        tags = read_tag_hints(item.path)
    except Exception as exc:
        errors.append(_safe_error(exc, category="metadata"))
    try:
        _verify_unchanged_file(root, item)
    except InventoryScanError as exc:
        errors.append(_safe_error(exc, category="file"))
        audio = None
    return _InspectedFile(
        discovered=item,
        audio=audio,
        tags=tags,
        inspection_error="; ".join(errors)[:500] if errors else None,
    )


def _album_path(relative_path: str) -> str:
    parent_path = PurePosixPath(relative_path).parent
    if _DISC_DIRECTORY.fullmatch(parent_path.name):
        parent_path = parent_path.parent
    parent = parent_path.as_posix()
    return "" if parent == "." else parent


def _path_fallbacks(relative_path: str) -> tuple[str | None, str]:
    parent = PurePosixPath(relative_path).parent
    if _DISC_DIRECTORY.fullmatch(parent.name):
        parent = parent.parent
    parts = parent.parts
    album = parts[-1] if parts else _UNKNOWN_ALBUM
    artist = parts[-2] if len(parts) >= 2 else None
    return artist, album


def _metadata_values(item: _InspectedFile) -> dict[str, Any]:
    tags = item.tags
    path_artist, path_album = _path_fallbacks(item.discovered.relative_path)
    album_artist_display = _display(tags.album_artist or tags.artist, path_artist or _UNKNOWN_ARTIST)
    album_title_display = _display(tags.album, path_album)
    missing_title = not bool(tags.title)
    missing_artist = not bool(tags.artist or tags.album_artist)
    missing_album = not bool(tags.album)
    missing_track_number = tags.track_number is None
    audio = item.audio
    return {
        "album_path": _album_path(item.discovered.relative_path),
        "album_artist_display": album_artist_display,
        "album_title_display": album_title_display,
        "title": tags.title,
        "artist": tags.artist,
        "album_artist": tags.album_artist,
        "album_title": tags.album,
        "year": tags.year,
        "track_number": tags.track_number,
        "track_total": tags.track_total,
        "disc_number": tags.disc_number,
        "disc_total": tags.disc_total,
        "container": audio.container if audio else None,
        "codec": audio.codec if audio else None,
        "bitrate": audio.bitrate if audio else None,
        "sample_rate": audio.sample_rate if audio else None,
        "bit_depth": audio.bit_depth if audio else None,
        "channels": audio.channels if audio else None,
        "duration_seconds": audio.duration_seconds if audio else None,
        "sha256": audio.sha256 if audio else None,
        "musicbrainz_recording_id": tags.musicbrainz_recording_id,
        "musicbrainz_release_id": tags.musicbrainz_release_id,
        "embedded_artwork": tags.embedded_artwork,
        "metadata_complete": not (
            missing_title or missing_artist or missing_album or missing_track_number
        ),
        "missing_title": missing_title,
        "missing_artist": missing_artist,
        "missing_album": missing_album,
        "missing_track_number": missing_track_number,
    }


def _safe_album_art(root: Path, relative_path: str) -> bool:
    directory = root / Path(*PurePosixPath(relative_path).parts) if relative_path else root
    try:
        directory.resolve(strict=True).relative_to(root)
        directory_stat = directory.lstat()
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_ISLNK(directory_stat.st_mode)
            or _is_reparse_point(directory_stat)
        ):
            return False
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.casefold() not in _ARTWORK_NAMES:
                    continue
                artwork_stat = entry.stat(follow_symlinks=False)
                if (
                    stat.S_ISREG(artwork_stat.st_mode)
                    and not stat.S_ISLNK(artwork_stat.st_mode)
                    and not _is_reparse_point(artwork_stat)
                ):
                    return True
    except OSError:
        return False
    return False


def _safe_directory_size(root: Path, *, skip_imports: bool = False) -> int | None:
    if not root.exists():
        return 0
    try:
        resolved = _validate_library_root(root)
        total = 0
        pending = [resolved]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if skip_imports and entry.name.casefold() == ".imports":
                        continue
                    entry_stat = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(entry_stat.st_mode) or _is_reparse_point(entry_stat):
                        continue
                    if stat.S_ISDIR(entry_stat.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(entry_stat.st_mode):
                        total += entry_stat.st_size
        return total
    except (OSError, InventoryScanError):
        return None


def _disk_usage(path: Path) -> tuple[int | None, int | None]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None, None
    return usage.total, usage.free


def _serialize_scan_enqueue(session: Session) -> None:
    """Take the SQLite writer lock before checking whether a scan is active.

    A plain SELECT-then-INSERT can let the web and worker processes both observe
    an empty queue, after which one request fails while upgrading its read
    transaction. This no-op write to the existing schema-version row makes the
    eligibility check and optional insert run behind one cross-process writer
    lock without adding another schema object.
    """

    locked = session.execute(
        update(DatabaseMetadata)
        .where(DatabaseMetadata.key == "schema_version")
        .values(value=DatabaseMetadata.value)
    )
    if locked.rowcount != 1:
        raise InventoryScanError("Database schema metadata is unavailable")


def _enqueue_library_scan_locked(session: Session, *, reason: str) -> LibraryScanRun:
    """Queue or return one scan while the enqueue writer lock is held."""

    existing = session.scalar(
        select(LibraryScanRun)
        .where(
            LibraryScanRun.state.in_(
                (LibraryScanState.QUEUED.value, LibraryScanState.SCANNING.value)
            )
        )
        .order_by(LibraryScanRun.created_at)
        .limit(1)
    )
    if existing is not None:
        return existing
    cleaned_reason = re.sub(r"[^a-zA-Z0-9_. -]", "", reason).strip()[:100] or "manual"
    scan = LibraryScanRun(reason=cleaned_reason)
    session.add(scan)
    session.flush()
    return scan


def enqueue_library_scan(session: Session, *, reason: str = "manual") -> LibraryScanRun:
    """Queue one scan, coalescing concurrent web and worker requests."""

    _serialize_scan_enqueue(session)
    return _enqueue_library_scan_locked(session, reason=reason)


def claim_next_scan(session: Session) -> str | None:
    candidate = session.scalar(
        select(LibraryScanRun.id)
        .where(LibraryScanRun.state == LibraryScanState.QUEUED.value)
        .order_by(LibraryScanRun.created_at)
        .limit(1)
    )
    if candidate is None:
        return None
    generation = (
        session.scalar(select(func.max(LibraryScanRun.generation))) or 0
    ) + 1
    claimed_at = _now()
    result = session.execute(
        update(LibraryScanRun)
        .where(
            LibraryScanRun.id == candidate,
            LibraryScanRun.state == LibraryScanState.QUEUED.value,
        )
        .values(
            state=LibraryScanState.SCANNING.value,
            generation=generation,
            started_at=claimed_at,
            finished_at=None,
            error_message=None,
        )
    )
    return candidate if result.rowcount == 1 else None


# Backward-compatible descriptive alias for callers that prefer the longer name.
claim_next_library_scan = claim_next_scan


def recover_interrupted_scans(session: Session) -> int:
    """Fail interrupted scans without deleting or invalidating inventory rows."""

    scans = session.scalars(
        select(LibraryScanRun).where(
            LibraryScanRun.state == LibraryScanState.SCANNING.value
        )
    ).all()
    finished_at = _now()
    for scan in scans:
        scan.state = LibraryScanState.FAILED.value
        scan.error_message = (
            "scan: Worker restarted during library scan; the previous inventory was left intact"
        )
        scan.finished_at = finished_at
    return len(scans)


def _scheduled_scan_decision(
    session: Session,
    settings: Settings,
) -> tuple[str | None, str | None]:
    """Return an active scan ID or the reason for a newly due scan."""

    active = session.scalar(
        select(LibraryScanRun.id)
        .where(
            LibraryScanRun.state.in_(
                (LibraryScanState.QUEUED.value, LibraryScanState.SCANNING.value)
            )
        )
        .order_by(LibraryScanRun.created_at)
        .limit(1)
    )
    if active is not None:
        return active, None
    latest = session.scalar(
        select(LibraryScanRun).order_by(LibraryScanRun.created_at.desc()).limit(1)
    )
    if latest is not None and latest.state == LibraryScanState.FAILED.value:
        failed_at = latest.finished_at or latest.created_at
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=timezone.utc)
        if (_now() - failed_at).total_seconds() < _FAILED_SCAN_RETRY_SECONDS:
            return None, None
        return None, "retry-after-failure"
    latest_complete = session.scalar(
        select(LibraryScanRun)
        .where(LibraryScanRun.state == LibraryScanState.COMPLETE.value)
        .order_by(LibraryScanRun.finished_at.desc())
        .limit(1)
    )
    due = latest_complete is None
    if latest_complete is not None and settings.library_scan_interval_seconds > 0:
        completed_at = latest_complete.finished_at or latest_complete.created_at
        # SQLite may return a naive datetime despite timezone=True. It still
        # represents UTC because every persisted timestamp is generated here.
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        due = (_now() - completed_at).total_seconds() >= settings.library_scan_interval_seconds
    if latest_complete is not None and settings.library_scan_interval_seconds <= 0:
        due = False
    if not due:
        return None, None
    return None, "scheduled" if latest_complete else "initial"


def ensure_initial_or_scheduled_scan(database: Database, settings: Settings) -> str | None:
    """Queue the initial scan or the next due periodic scan.

    The common not-due worker poll stays read-only. When a scan appears due, a
    second transaction takes the enqueue writer lock and repeats the decision so
    a concurrent manual request cannot create a duplicate or a lock-upgrade 500.
    """

    with database.session() as session:
        active, reason = _scheduled_scan_decision(session, settings)
    if active is not None or reason is None:
        return active

    with database.session() as session:
        _serialize_scan_enqueue(session)
        active, reason = _scheduled_scan_decision(session, settings)
        if active is not None or reason is None:
            return active
        return _enqueue_library_scan_locked(session, reason=reason).id


def _ensure_scan_started(database: Database, scan_id: str) -> int:
    with database.session() as session:
        scan = session.get(LibraryScanRun, scan_id)
        if scan is None:
            raise InventoryScanError("Library scan job was not found")
        if scan.state == LibraryScanState.QUEUED.value:
            generation = (session.scalar(select(func.max(LibraryScanRun.generation))) or 0) + 1
            scan.state = LibraryScanState.SCANNING.value
            scan.generation = generation
            scan.started_at = _now()
            scan.finished_at = None
            scan.error_message = None
        elif scan.state != LibraryScanState.SCANNING.value:
            raise InventoryScanError("Library scan job is not runnable")
        elif scan.generation <= 0:
            scan.generation = (session.scalar(select(func.max(LibraryScanRun.generation))) or 0) + 1
        return scan.generation


def _existing_tracks(database: Database) -> dict[str, _ExistingTrack]:
    with database.session() as session:
        rows = session.execute(
            select(
                LibraryInventoryTrack.id,
                LibraryInventoryTrack.relative_path,
                LibraryInventoryTrack.file_size,
                LibraryInventoryTrack.mtime_ns,
                LibraryInventoryTrack.sha256,
                LibraryInventoryTrack.inspection_error,
            )
        ).all()
    return {
        relative_path: _ExistingTrack(
            id=identifier,
            file_size=file_size,
            mtime_ns=mtime_ns,
            sha256=sha256,
            inspection_error=inspection_error,
        )
        for identifier, relative_path, file_size, mtime_ns, sha256, inspection_error in rows
    }


def _get_or_create_artist(
    session: Session,
    cache: dict[str, LibraryArtist],
    display_name: str,
) -> LibraryArtist:
    normalized_name = _normalized(display_name)
    artist = cache.get(normalized_name)
    if artist is None:
        artist = session.scalar(
            select(LibraryArtist).where(LibraryArtist.normalized_name == normalized_name)
        )
    if artist is None:
        artist = LibraryArtist(
            canonical_name=display_name,
            normalized_name=normalized_name,
        )
        session.add(artist)
        session.flush()
    elif artist.canonical_name == _UNKNOWN_ARTIST and display_name != _UNKNOWN_ARTIST:
        artist.canonical_name = display_name
    cache[normalized_name] = artist
    return artist


def _get_or_create_album(
    session: Session,
    cache: dict[str, LibraryAlbum],
    artist: LibraryArtist,
    *,
    relative_path: str,
    album_artist: str,
    title: str,
    year: int | None,
    generation: int,
) -> LibraryAlbum:
    album = cache.get(relative_path)
    if album is None:
        album = session.scalar(
            select(LibraryAlbum).where(LibraryAlbum.relative_path == relative_path)
        )
    if album is None:
        album = LibraryAlbum(
            artist_id=artist.id,
            album_artist=album_artist,
            title=title,
            normalized_title=_normalized(title),
            year=year,
            relative_path=relative_path,
            scan_generation=generation,
        )
        session.add(album)
        session.flush()
    else:
        album.artist_id = artist.id
        album.album_artist = album_artist
        album.title = title
        album.normalized_title = _normalized(title)
        album.year = year
        album.scan_generation = generation
    cache[relative_path] = album
    return album


def _apply_track_values(
    track: LibraryInventoryTrack,
    item: _InspectedFile,
    values: dict[str, Any],
    *,
    album_id: int,
    generation: int,
) -> None:
    track.album_id = album_id
    track.relative_path = item.discovered.relative_path
    track.title = values["title"]
    track.artist = values["artist"]
    track.album_artist = values["album_artist"]
    track.album_title = values["album_title"]
    track.year = values["year"]
    track.track_number = values["track_number"]
    track.track_total = values["track_total"]
    track.disc_number = values["disc_number"]
    track.disc_total = values["disc_total"]
    track.container = values["container"]
    track.codec = values["codec"]
    track.bitrate = values["bitrate"]
    track.sample_rate = values["sample_rate"]
    track.bit_depth = values["bit_depth"]
    track.channels = values["channels"]
    track.duration_seconds = values["duration_seconds"]
    track.file_size = item.discovered.file_size
    track.mtime_ns = item.discovered.mtime_ns
    track.sha256 = values["sha256"]
    track.musicbrainz_recording_id = values["musicbrainz_recording_id"]
    track.musicbrainz_release_id = values["musicbrainz_release_id"]
    track.embedded_artwork = values["embedded_artwork"]
    track.metadata_complete = values["metadata_complete"]
    track.missing_title = values["missing_title"]
    track.missing_artist = values["missing_artist"]
    track.missing_album = values["missing_album"]
    track.missing_track_number = values["missing_track_number"]
    track.inspection_error = item.inspection_error
    track.scan_generation = generation
    track.indexed_at = utcnow()


def _refresh_aggregates(
    session: Session,
    artwork_by_path: dict[str, bool],
    generation: int,
) -> None:
    session.flush()
    session.execute(
        delete(LibraryAlbum).where(
            ~LibraryAlbum.id.in_(select(LibraryInventoryTrack.album_id))
        )
    )
    session.flush()

    album_aggregates = {
        album_id: (track_count, total_bytes, metadata_complete, embedded_artwork)
        for album_id, track_count, total_bytes, metadata_complete, embedded_artwork in session.execute(
            select(
                LibraryInventoryTrack.album_id,
                func.count(LibraryInventoryTrack.id),
                func.coalesce(func.sum(LibraryInventoryTrack.file_size), 0),
                func.min(cast(LibraryInventoryTrack.metadata_complete, Integer)),
                func.max(cast(LibraryInventoryTrack.embedded_artwork, Integer)),
            ).group_by(LibraryInventoryTrack.album_id)
        ).all()
    }
    codecs_by_album: dict[int, Counter[str]] = defaultdict(Counter)
    for album_id, codec, count in session.execute(
        select(
            LibraryInventoryTrack.album_id,
            LibraryInventoryTrack.codec,
            func.count(LibraryInventoryTrack.id),
        ).group_by(LibraryInventoryTrack.album_id, LibraryInventoryTrack.codec)
    ).all():
        codecs_by_album[album_id][(codec or "unknown").casefold()] = int(count)
    releases_by_album: dict[int, set[str]] = defaultdict(set)
    for album_id, release_id in session.execute(
        select(
            LibraryInventoryTrack.album_id,
            LibraryInventoryTrack.musicbrainz_release_id,
        )
        .where(LibraryInventoryTrack.musicbrainz_release_id.is_not(None))
        .distinct()
    ).all():
        releases_by_album[album_id].add(release_id)

    albums = session.scalars(select(LibraryAlbum)).all()
    for album in albums:
        track_count, total_bytes, all_metadata, embedded_artwork = album_aggregates[album.id]
        codecs = codecs_by_album[album.id]
        release_ids = releases_by_album[album.id]
        album.track_count = int(track_count)
        album.total_bytes = int(total_bytes)
        album.metadata_complete = bool(track_count) and bool(all_metadata)
        album.codec_summary = ", ".join(
            f"{codec}:{count}" for codec, count in sorted(codecs.items())
        )[:500]
        album.musicbrainz_release_id = next(iter(release_ids)) if len(release_ids) == 1 else None
        album.artwork_present = artwork_by_path.get(album.relative_path, False) or bool(
            embedded_artwork
        )
        album.scan_generation = generation

    session.execute(
        delete(LibraryArtist).where(
            ~LibraryArtist.id.in_(select(LibraryAlbum.artist_id))
        )
    )
    session.flush()
    artist_aggregates = {
        artist_id: (album_count, track_count, total_bytes, flac_count)
        for artist_id, album_count, track_count, total_bytes, flac_count in session.execute(
            select(
                LibraryAlbum.artist_id,
                func.count(func.distinct(LibraryAlbum.id)),
                func.count(LibraryInventoryTrack.id),
                func.coalesce(func.sum(LibraryInventoryTrack.file_size), 0),
                func.coalesce(
                    func.sum(
                        case(
                            (func.lower(LibraryInventoryTrack.codec) == "flac", 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .join(
                LibraryInventoryTrack,
                LibraryInventoryTrack.album_id == LibraryAlbum.id,
            )
            .group_by(LibraryAlbum.artist_id)
        ).all()
    }
    for artist in session.scalars(select(LibraryArtist)).all():
        album_count, track_count, total_bytes, flac_count = artist_aggregates[artist.id]
        artist.album_count = int(album_count)
        artist.track_count = int(track_count)
        artist.total_bytes = int(total_bytes)
        artist.flac_track_count = int(flac_count)
        artist.updated_at = utcnow()


def _chunks[T](values: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _persist_reused_batch(
    session: Session,
    reused: Sequence[tuple[str, _ExistingTrack]],
    *,
    generation: int,
) -> None:
    for _relative_path, snapshot in reused:
        track = session.get(LibraryInventoryTrack, snapshot.id)
        if track is not None:
            track.scan_generation = generation
    session.flush()


def _persist_inspected_batch(
    session: Session,
    inspected: Sequence[_InspectedFile],
    *,
    generation: int,
) -> None:
    artist_cache: dict[str, LibraryArtist] = {}
    album_cache: dict[str, LibraryAlbum] = {}
    for item in inspected:
        values = _metadata_values(item)
        artist = _get_or_create_artist(
            session,
            artist_cache,
            values["album_artist_display"],
        )
        album = _get_or_create_album(
            session,
            album_cache,
            artist,
            relative_path=values["album_path"],
            album_artist=values["album_artist_display"],
            title=values["album_title_display"],
            year=values["year"],
            generation=generation,
        )
        track = session.scalar(
            select(LibraryInventoryTrack).where(
                LibraryInventoryTrack.relative_path == item.discovered.relative_path
            )
        )
        if track is None:
            track = LibraryInventoryTrack(
                album_id=album.id,
                relative_path=item.discovered.relative_path,
                file_size=item.discovered.file_size,
                mtime_ns=item.discovered.mtime_ns,
                scan_generation=generation,
            )
            session.add(track)
        _apply_track_values(
            track,
            item,
            values,
            album_id=album.id,
            generation=generation,
        )
    session.flush()


def _finalize_scan(
    session: Session,
    *,
    scan_id: str,
    generation: int,
    discovered: Sequence[_DiscoveredFile],
    reused: dict[str, _ExistingTrack],
    inspected: Sequence[_InspectedFile],
    artwork_by_path: dict[str, bool],
    library_bytes: int | None,
    staging_bytes: int | None,
    music_usage: tuple[int | None, int | None],
    staging_usage: tuple[int | None, int | None],
) -> None:
    # Stale rows are removed only after discovery, inspection, and every
    # bounded upsert batch succeeded. All inventory publication remains in this
    # transaction, so a failure at any point restores the last complete view.
    session.execute(
        delete(LibraryInventoryTrack).where(
            LibraryInventoryTrack.scan_generation != generation
        )
    )
    _refresh_aggregates(session, artwork_by_path, generation)

    snapshot = session.get(StorageSnapshot, 1)
    if snapshot is None:
        snapshot = StorageSnapshot(id=1)
        session.add(snapshot)
    snapshot.library_bytes = library_bytes
    snapshot.staging_bytes = staging_bytes
    snapshot.music_filesystem_total_bytes = music_usage[0]
    snapshot.music_filesystem_free_bytes = music_usage[1]
    snapshot.staging_filesystem_total_bytes = staging_usage[0]
    snapshot.staging_filesystem_free_bytes = staging_usage[1]
    snapshot.observed_at = utcnow()

    scan = session.get(LibraryScanRun, scan_id)
    if scan is None:
        raise InventoryScanError("Library scan job disappeared")
    failed = sum(1 for item in inspected if item.inspection_error)
    scan.state = LibraryScanState.COMPLETE.value
    scan.files_discovered = len(discovered)
    scan.files_reused = len(reused)
    scan.files_failed = failed
    scan.files_inspected = len(inspected) - failed
    scan.error_message = None
    scan.finished_at = _now()


def _persist_scan(
    database: Database,
    settings: Settings,
    *,
    scan_id: str,
    generation: int,
    discovered: Sequence[_DiscoveredFile],
    reused: dict[str, _ExistingTrack],
    inspected: Sequence[_InspectedFile],
    artwork_by_path: dict[str, bool],
    library_bytes: int | None,
    staging_bytes: int | None,
    music_usage: tuple[int | None, int | None],
    staging_usage: tuple[int | None, int | None],
) -> None:
    batch_size = max(1, settings.inventory_commit_batch_size)
    reused_items = list(reused.items())
    # Filesystem discovery, ffprobe/tag reads, hashing, artwork checks, and disk
    # queries have already completed. Keep only the bounded database publication
    # below in one transaction so readers see either the previous complete
    # generation or the new complete generation, never a mixture of both.
    with database.session() as session:
        for batch in _chunks(reused_items, batch_size):
            _persist_reused_batch(session, batch, generation=generation)
        for batch in _chunks(inspected, batch_size):
            _persist_inspected_batch(session, batch, generation=generation)
        _finalize_scan(
            session,
            scan_id=scan_id,
            generation=generation,
            discovered=discovered,
            reused=reused,
            inspected=inspected,
            artwork_by_path=artwork_by_path,
            library_bytes=library_bytes,
            staging_bytes=staging_bytes,
            music_usage=music_usage,
            staging_usage=staging_usage,
        )


def _mark_scan_failed(database: Database, scan_id: str, exc: BaseException) -> None:
    message = _safe_error(exc, category="scan")
    with database.session() as session:
        scan = session.get(LibraryScanRun, scan_id)
        if scan is None:
            return
        scan.state = LibraryScanState.FAILED.value
        scan.error_message = message
        scan.finished_at = _now()


def run_library_scan(
    database: Database,
    settings: Settings,
    scan_id: str,
) -> LibraryScanRun:
    """Execute one persisted scan without ever modifying files under /music."""

    try:
        generation = _ensure_scan_started(database, scan_id)
        root = _validate_library_root(settings.music_dir)
        discovered = _discover_audio_files(root)
        existing = _existing_tracks(database)
        reused: dict[str, _ExistingTrack] = {}
        inspected: list[_InspectedFile] = []
        for item in discovered:
            previous = existing.get(item.relative_path)
            if (
                previous is not None
                and previous.file_size == item.file_size
                and previous.mtime_ns == item.mtime_ns
                and previous.sha256
                and not previous.inspection_error
            ):
                reused[item.relative_path] = previous
            else:
                inspected.append(_inspect_file(root, item, settings))

        # Storage traversal and disk queries are intentionally outside the SQLite
        # transaction. The dashboard reads the persisted snapshot cheaply.
        album_paths = {_album_path(item.relative_path) for item in discovered}
        artwork_by_path = {
            relative_path: _safe_album_art(root, relative_path)
            for relative_path in album_paths
        }
        library_bytes = _safe_directory_size(root, skip_imports=True)
        staging_bytes = _safe_directory_size(settings.staging_dir)
        music_usage = _disk_usage(root)
        staging_usage = _disk_usage(settings.staging_dir)
        _persist_scan(
            database,
            settings,
            scan_id=scan_id,
            generation=generation,
            discovered=discovered,
            reused=reused,
            inspected=inspected,
            artwork_by_path=artwork_by_path,
            library_bytes=library_bytes,
            staging_bytes=staging_bytes,
            music_usage=music_usage,
            staging_usage=staging_usage,
        )
    except Exception as exc:
        _mark_scan_failed(database, scan_id, exc)
    with database.session() as session:
        result = session.get(LibraryScanRun, scan_id)
        if result is None:
            raise InventoryScanError("Library scan job disappeared")
        return result


def scan_library_now(
    database: Database,
    *,
    settings: Settings | None = None,
    reason: str = "manual",
) -> LibraryScanRun:
    with database.session() as session:
        scan = enqueue_library_scan(session, reason=reason)
        scan_id = scan.id
    return run_library_scan(database, settings or database.settings, scan_id)


def refresh_storage_snapshot(database: Database, settings: Settings) -> StorageSnapshot:
    """Refresh cached storage figures without initiating a library crawl."""

    library_bytes = _safe_directory_size(settings.music_dir, skip_imports=True)
    staging_bytes = _safe_directory_size(settings.staging_dir)
    music_usage = _disk_usage(settings.music_dir)
    staging_usage = _disk_usage(settings.staging_dir)
    with database.session() as session:
        snapshot = session.get(StorageSnapshot, 1)
        if snapshot is None:
            snapshot = StorageSnapshot(id=1)
            session.add(snapshot)
        # A failed walk is "unknown", not an empty library. Keep that
        # distinction so the dashboard never reports a fabricated 0 B value.
        snapshot.library_bytes = library_bytes
        snapshot.staging_bytes = staging_bytes
        snapshot.music_filesystem_total_bytes = music_usage[0]
        snapshot.music_filesystem_free_bytes = music_usage[1]
        snapshot.staging_filesystem_total_bytes = staging_usage[0]
        snapshot.staging_filesystem_free_bytes = staging_usage[1]
        snapshot.observed_at = utcnow()
        session.flush()
        return snapshot


def library_counts(session: Session) -> dict[str, int | float]:
    artists = session.scalar(select(func.count(LibraryArtist.id))) or 0
    albums = session.scalar(select(func.count(LibraryAlbum.id))) or 0
    tracks, total_bytes, duration = session.execute(
        select(
            func.count(LibraryInventoryTrack.id),
            func.coalesce(func.sum(LibraryInventoryTrack.file_size), 0),
            func.coalesce(func.sum(LibraryInventoryTrack.duration_seconds), 0.0),
        )
    ).one()
    return {
        "artists": int(artists),
        "albums": int(albums),
        "tracks": int(tracks),
        "bytes": int(total_bytes),
        "duration_seconds": float(duration),
    }


def quality_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(
            LibraryInventoryTrack.codec,
            LibraryInventoryTrack.bitrate,
            func.count(LibraryInventoryTrack.id),
        ).group_by(LibraryInventoryTrack.codec, LibraryInventoryTrack.bitrate)
    ).all()
    counts = {
        "flac": 0,
        "mp3_320": 0,
        "mp3_below_320": 0,
        "aac_m4a": 0,
        "opus_ogg": 0,
        "unknown": 0,
    }
    for raw_codec, bitrate, count in rows:
        codec = (raw_codec or "").casefold()
        amount = int(count)
        if codec == "flac":
            counts["flac"] += amount
        elif codec == "mp3" and bitrate is not None and bitrate >= 320_000:
            counts["mp3_320"] += amount
        elif codec == "mp3" and bitrate is not None:
            counts["mp3_below_320"] += amount
        elif codec in {"aac", "alac"}:
            counts["aac_m4a"] += amount
        elif codec in {"opus", "vorbis"}:
            counts["opus_ogg"] += amount
        else:
            counts["unknown"] += amount
    counts["lower_quality"] = counts["mp3_below_320"]
    return counts


def health_counts(session: Session) -> dict[str, int]:
    missing_artwork = session.scalar(
        select(func.count(LibraryAlbum.id)).where(LibraryAlbum.artwork_present.is_(False))
    ) or 0
    flag_counts = session.execute(
        select(
            func.count(LibraryInventoryTrack.id).filter(
                LibraryInventoryTrack.metadata_complete.is_(False)
            ),
            func.count(LibraryInventoryTrack.id).filter(LibraryInventoryTrack.missing_title.is_(True)),
            func.count(LibraryInventoryTrack.id).filter(LibraryInventoryTrack.missing_artist.is_(True)),
            func.count(LibraryInventoryTrack.id).filter(LibraryInventoryTrack.missing_album.is_(True)),
            func.count(LibraryInventoryTrack.id).filter(
                LibraryInventoryTrack.missing_track_number.is_(True)
            ),
            func.count(LibraryInventoryTrack.id).filter(
                LibraryInventoryTrack.inspection_error.is_not(None)
            ),
        )
    ).one()
    duplicate_hash_counts = session.scalars(
        select(func.count(LibraryInventoryTrack.id))
        .where(LibraryInventoryTrack.sha256.is_not(None))
        .group_by(LibraryInventoryTrack.sha256)
        .having(func.count(LibraryInventoryTrack.id) > 1)
    ).all()
    duplicate_recording_counts = session.scalars(
        select(func.count(LibraryInventoryTrack.id))
        .where(LibraryInventoryTrack.musicbrainz_recording_id.is_not(None))
        .group_by(LibraryInventoryTrack.musicbrainz_recording_id)
        .having(func.count(LibraryInventoryTrack.id) > 1)
    ).all()
    low_quality = session.scalar(
        select(func.count(LibraryInventoryTrack.id)).where(
            LibraryInventoryTrack.codec == "mp3",
            LibraryInventoryTrack.bitrate.is_not(None),
            LibraryInventoryTrack.bitrate < 320_000,
        )
    ) or 0
    return {
        "missing_artwork": int(missing_artwork),
        "missing_metadata": int(flag_counts[0]),
        "missing_title": int(flag_counts[1]),
        "missing_artist": int(flag_counts[2]),
        "missing_album": int(flag_counts[3]),
        "missing_track_number": int(flag_counts[4]),
        "failed_inspections": int(flag_counts[5]),
        "duplicate_hash_groups": len(duplicate_hash_counts),
        "duplicate_hash_tracks": sum(int(value) for value in duplicate_hash_counts),
        "duplicate_recording_groups": len(duplicate_recording_counts),
        "duplicate_recording_tracks": sum(int(value) for value in duplicate_recording_counts),
        "possible_duplicates": sum(int(value) for value in duplicate_hash_counts),
        "low_quality_files": int(low_quality),
    }


def storage_values(session: Session) -> dict[str, int | str | None]:
    snapshot = session.get(StorageSnapshot, 1)
    if snapshot is None:
        return {
            "library_bytes": None,
            "staging_bytes": None,
            "music_filesystem_total_bytes": None,
            "music_filesystem_free_bytes": None,
            "staging_filesystem_total_bytes": None,
            "staging_filesystem_free_bytes": None,
            "observed_at": None,
        }
    return {
        "library_bytes": snapshot.library_bytes,
        "staging_bytes": snapshot.staging_bytes,
        "music_filesystem_total_bytes": snapshot.music_filesystem_total_bytes,
        "music_filesystem_free_bytes": snapshot.music_filesystem_free_bytes,
        "staging_filesystem_total_bytes": snapshot.staging_filesystem_total_bytes,
        "staging_filesystem_free_bytes": snapshot.staging_filesystem_free_bytes,
        "observed_at": (
            snapshot.observed_at.replace(tzinfo=timezone.utc)
            if snapshot.observed_at.tzinfo is None
            else snapshot.observed_at.astimezone(timezone.utc)
        ).isoformat(),
    }


def inventory_statistics(session: Session) -> dict[str, Any]:
    return {
        "library": library_counts(session),
        "quality": quality_counts(session),
        "health": health_counts(session),
        "storage": storage_values(session),
    }


def latest_scan(session: Session) -> LibraryScanRun | None:
    return session.scalar(
        select(LibraryScanRun).order_by(LibraryScanRun.created_at.desc()).limit(1)
    )


def list_artists(
    session: Session,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[LibraryArtist]:
    bounded_limit = max(1, min(limit, _MAX_QUERY_LIMIT))
    bounded_offset = max(0, offset)
    return list(
        session.scalars(
            select(LibraryArtist)
            .order_by(LibraryArtist.normalized_name, LibraryArtist.id)
            .offset(bounded_offset)
            .limit(bounded_limit)
        ).all()
    )


def list_albums(
    session: Session,
    *,
    artist_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[LibraryAlbum]:
    query = select(LibraryAlbum)
    if artist_id is not None:
        query = query.where(LibraryAlbum.artist_id == artist_id)
    bounded_limit = max(1, min(limit, _MAX_QUERY_LIMIT))
    bounded_offset = max(0, offset)
    return list(
        session.scalars(
            query.order_by(
                LibraryAlbum.year.desc().nullslast(),
                LibraryAlbum.normalized_title,
                LibraryAlbum.id,
            )
            .offset(bounded_offset)
            .limit(bounded_limit)
        ).all()
    )


def album_tracks(session: Session, album_id: int) -> list[LibraryInventoryTrack]:
    return list(
        session.scalars(
            select(LibraryInventoryTrack)
            .where(LibraryInventoryTrack.album_id == album_id)
            .order_by(
                LibraryInventoryTrack.disc_number,
                LibraryInventoryTrack.track_number,
                LibraryInventoryTrack.relative_path,
            )
        ).all()
    )
