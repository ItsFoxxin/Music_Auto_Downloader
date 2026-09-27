from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from .archive import ArchiveError, ArchiveLimits, ExtractionManifest, secure_extract_zip
from .artwork import ArtworkError, ArtworkService
from .audio import AudioInspectionError, SUPPORTED_AUDIO_EXTENSIONS, inspect_audio, sha256_file
from .cache import ArtworkFileCache, DatabaseJsonCache
from .config import Settings
from .database import Database
from .duplicates import assess_duplicates, normalized_track_identity, normalized_track_slot
from .enums import JellyfinState, JobState
from .filenames import build_track_filename, sanitize_component
from .jellyfin import JellyfinRefreshResult, refresh_jellyfin_library
from .library import (
    LibraryConflictError,
    LibraryImportError,
    PreparedTrack,
    commit_album,
    prepare_album,
    verify_prepared_album,
)
from .metadata import (
    MetadataError,
    NormalizedTrackMetadata,
    TagHints,
    UnsupportedTaggingError,
    deserialize_hints,
    extract_embedded_artwork,
    metadata_from_hints,
    read_tag_hints,
    serialize_hints,
    write_tags,
)
from .models import Job, LibraryTrack, ReleaseCandidate, Track
from .musicbrainz import (
    MusicBrainzClient,
    MusicBrainzConfigurationError,
    MusicBrainzError,
    album_hints_from_tracks,
)
from .state import add_event, transition_job


logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    code = "PIPELINE_ERROR"
    retryable = False


class RetryablePipelineError(PipelineError):
    retryable = True


class CommittedImportPending(RetryablePipelineError):
    code = "COMMITTED_IMPORT_PENDING_RECONCILIATION"


class ReviewRequired(RuntimeError):
    def __init__(self, reason: str, *, kind: str = "METADATA"):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


@dataclass(frozen=True, slots=True)
class SourceAudio:
    path: Path
    original_path: str
    expected_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class WorkingPlan:
    track_id: int
    source_path: Path
    expected_sha256: str
    extension: str
    metadata: NormalizedTrackMetadata


def _job_root(settings: Settings, job_id: str) -> Path:
    return settings.jobs_dir / job_id


def _safe_job_source(settings: Settings, job: Job) -> Path:
    root = _job_root(settings, job.id).resolve(strict=True)
    path = root / Path(*job.source_relative_path.replace("\\", "/").split("/"))
    try:
        path.resolve(strict=True).relative_to(root)
    except (ValueError, OSError) as exc:
        raise PipelineError("Staged source path escaped its job directory") from exc
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise PipelineError("Staged source must be a regular file")
    return path


def _transition(database: Database, job_id: str, new_state: JobState, message: str, **kwargs: Any) -> None:
    with database.session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise PipelineError("Job disappeared while processing")
        transition_job(session, job, new_state, message, **kwargs)


def _event(database: Database, job_id: str, message: str, **kwargs: Any) -> None:
    with database.session() as session:
        job = session.get(Job, job_id)
        if job is not None:
            add_event(session, job, message, **kwargs)


def _remove_generated_directory(root: Path, target: Path) -> None:
    if not target.exists() and not target.is_symlink():
        return
    resolved_root = root.resolve(strict=True)
    try:
        target.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise PipelineError("Generated cleanup target escaped its trusted root") from exc
    if target.is_symlink():
        raise PipelineError("Generated cleanup target was replaced by a symlink")
    shutil.rmtree(target)


def _archive_limits(settings: Settings) -> ArchiveLimits:
    return ArchiveLimits(
        max_archive_bytes=settings.max_upload_bytes,
        max_entries=settings.archive_max_files,
        max_files=settings.archive_max_files,
        max_entry_bytes=settings.archive_max_entry_bytes,
        max_total_bytes=settings.archive_max_total_bytes,
        max_compression_ratio=settings.archive_max_compression_ratio,
    )


def _source_audio_from_archive(extracted: Path, manifest: ExtractionManifest) -> list[SourceAudio]:
    result = [
        SourceAudio(
            path=extracted / item.stored_name,
            original_path=item.original_path,
            expected_sha256=item.sha256,
        )
        for item in manifest.files
        if item.media_kind == "audio"
    ]
    if not result:
        raise PipelineError("The archive contained no supported audio files")
    return sorted(result, key=lambda item: _natural_path_key(item.original_path))


def _natural_path_key(value: str) -> tuple[tuple[int, object], ...]:
    """Sort human track paths so 2 precedes 10 without trusting the names as paths."""

    normalized = unicodedata.normalize("NFC", value).casefold()
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token)
        for token in re.split(r"(\d+)", normalized)
        if token
    )


def _infer_path_hints(sources: list[SourceAudio], display_name: str) -> tuple[str, str | None]:
    split_paths = [source.original_path.replace("\\", "/").split("/") for source in sources]
    album_fallback = Path(display_name).stem
    artist_fallback: str | None = None
    if split_paths:
        first_parts = [parts[0] for parts in split_paths if len(parts) >= 2]
        if first_parts and len(set(first_parts)) == 1:
            album_fallback = first_parts[0]
        three_part = [parts for parts in split_paths if len(parts) >= 3]
        if three_part and len(three_part) == len(split_paths):
            artists = {parts[0] for parts in three_part}
            albums = {parts[1] for parts in three_part}
            if len(artists) == 1 and len(albums) == 1:
                artist_fallback = next(iter(artists))
                album_fallback = next(iter(albums))
    return album_fallback, artist_fallback


TRACK_PREFIX = re.compile(r"^\s*(?:disc\s*\d+\s*[-_. ]*)?\d+\s*[-_. ]+", re.IGNORECASE)


def _title_from_original(original_path: str) -> str:
    stem = Path(original_path.replace("\\", "/")).stem
    return TRACK_PREFIX.sub("", stem).strip() or stem or "Unknown Track"


def _inspect_sources(
    settings: Settings,
    sources: list[SourceAudio],
    display_name: str,
) -> list[dict[str, Any]]:
    album_fallback, artist_fallback = _infer_path_hints(sources, display_name)
    inspected: list[dict[str, Any]] = []
    for index, source in enumerate(sources, start=1):
        details = inspect_audio(source.path, settings)
        if source.expected_sha256 and details.sha256 != source.expected_sha256:
            raise PipelineError("An extracted audio file changed before inspection")
        original_hints = read_tag_hints(source.path)
        hints = replace(
            original_hints,
            title=original_hints.title or _title_from_original(source.original_path),
            artist=original_hints.artist or artist_fallback,
            album_artist=original_hints.album_artist or original_hints.artist or artist_fallback,
            album=original_hints.album or album_fallback,
            track_number=original_hints.track_number or index,
            track_total=original_hints.track_total or len(sources),
            disc_number=original_hints.disc_number or 1,
            disc_total=original_hints.disc_total or 1,
        )
        inspected.append(
            {
                "source": source,
                "details": details,
                "hints": hints,
                "original_hints": original_hints,
            }
        )
    return inspected


def _store_inspection(database: Database, job_id: str, inspected: list[dict[str, Any]]) -> None:
    root = _job_root(database.settings, job_id).resolve(strict=True)
    with database.session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise PipelineError("Job disappeared during inspection")
        session.execute(delete(Track).where(Track.job_id == job_id))
        session.flush()
        for item in inspected:
            source: SourceAudio = item["source"]
            details = item["details"]
            hints: TagHints = item["hints"]
            original_hints: TagHints = item["original_hints"]
            source_relative = source.path.resolve(strict=True).relative_to(root).as_posix()
            session.add(
                Track(
                    job_id=job_id,
                    source_relative_path=source_relative,
                    original_filename=source.original_path[:1000],
                    container=details.container,
                    codec=details.codec,
                    duration_seconds=details.duration_seconds,
                    bitrate=details.bitrate,
                    sample_rate=details.sample_rate,
                    bit_depth=details.bit_depth,
                    channels=details.channels,
                    file_size=details.file_size,
                    sha256=details.sha256,
                    # Preserve which values truly came from tags. Resolved fallbacks live
                    # in the dedicated columns and must not masquerade as trusted positions.
                    original_tags_json=serialize_hints(original_hints),
                    embedded_artwork=hints.embedded_artwork,
                    title=hints.title,
                    artist=hints.artist,
                    album_artist=hints.album_artist,
                    album=hints.album,
                    track_number=hints.track_number,
                    track_total=hints.track_total,
                    disc_number=hints.disc_number,
                    disc_total=hints.disc_total,
                    release_date=hints.release_date,
                    year=hints.year,
                    isrc=hints.isrc,
                    musicbrainz_recording_id=hints.musicbrainz_recording_id,
                    musicbrainz_release_id=hints.musicbrainz_release_id,
                )
            )
        job.track_count = len(inspected)
        add_event(
            session,
            job,
            f"Audio inspected: {len(inspected)} track{'s' if len(inspected) != 1 else ''}",
            data={"codecs": dict(Counter(item["details"].codec for item in inspected))},
        )


def _load_job_with_tracks(database: Database, job_id: str) -> Job:
    with database.session() as session:
        job = session.scalar(
            select(Job)
            .where(Job.id == job_id)
            .options(selectinload(Job.tracks), selectinload(Job.candidates))
        )
        if job is None:
            raise PipelineError("Job was not found")
        # Relationships and scalar state remain usable because sessions do not expire on commit.
        return job


def _pause_for_review(database: Database, job_id: str, review: ReviewRequired) -> None:
    with database.session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.review_kind = review.kind
        job.review_reason = review.reason
        transition_job(session, job, JobState.NEEDS_REVIEW, review.reason, level="WARNING")


def _choose_release(database: Database, settings: Settings, job_id: str) -> ReleaseCandidate | None:
    job = _load_job_with_tracks(database, job_id)
    if job.selected_release_id == "__incoming__":
        return None
    if job.selected_release_id:
        candidate = next(
            (
                item
                for item in job.candidates
                if item.musicbrainz_release_id == job.selected_release_id
            ),
            None,
        )
        if candidate is None:
            raise ReviewRequired("The selected MusicBrainz release is no longer available. Choose again.")
        return candidate

    try:
        hints = album_hints_from_tracks(job.tracks)
        with MusicBrainzClient(settings, cache=DatabaseJsonCache(database)) as client:
            matches = client.search_releases(hints)
    except MusicBrainzConfigurationError as exc:
        raise ReviewRequired(
            f"{exc}. Configure it and retry, or explicitly use the incoming tags."
        ) from exc
    except (MusicBrainzError, ValueError) as exc:
        raise ReviewRequired(
            f"MusicBrainz matching could not complete: {exc}. Retry later or use incoming tags."
        ) from exc

    selected_candidate: ReleaseCandidate | None = None
    candidate_count = 0
    with database.session() as session:
        session.execute(delete(ReleaseCandidate).where(ReleaseCandidate.job_id == job_id))
        for match in matches:
            session.add(ReleaseCandidate(**match.to_model_values(job_id)))
        session.flush()
        stored = session.scalars(
            select(ReleaseCandidate)
            .where(ReleaseCandidate.job_id == job_id)
            .order_by(ReleaseCandidate.score.desc())
        ).all()
        current = session.get(Job, job_id)
        assert current is not None
        candidate_count = len(stored)
        add_event(session, current, f"MusicBrainz returned {len(stored)} release candidate(s)")

        if stored:
            top = stored[0]
            runner_up_score = stored[1].score if len(stored) > 1 else 0.0
            count_matches = top.track_count == current.track_count
            if (
                top.score >= settings.automatic_match_threshold
                and top.score - runner_up_score >= settings.automatic_match_margin
                and count_matches
            ):
                top.selected = True
                current.selected_release_id = top.musicbrainz_release_id
                current.match_confidence = top.score
                current.review_kind = None
                current.review_reason = None
                add_event(
                    session,
                    current,
                    f"High-confidence MusicBrainz match accepted ({top.score:.1f}%)",
                )
                selected_candidate = top
    if selected_candidate is not None:
        return selected_candidate
    if candidate_count == 0:
        raise ReviewRequired("No MusicBrainz release candidate was found. Use incoming tags or retry with better tags.")
    raise ReviewRequired(
        "MusicBrainz results were ambiguous. Choose the exact release, edition, and country.",
        kind="METADATA",
    )


def _artist_credit(value: object) -> str:
    if not isinstance(value, list):
        return ""
    result: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not name and isinstance(item.get("artist"), Mapping):
            name = item["artist"].get("name")
        if name:
            result.append(str(name))
        if item.get("joinphrase"):
            result.append(str(item["joinphrase"]))
    return "".join(result).strip()


def _release_track_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    media = payload.get("media")
    if not isinstance(media, list):
        return []
    result: list[dict[str, Any]] = []
    disc_total = len(media)
    for medium_index, medium in enumerate(media, start=1):
        if not isinstance(medium, Mapping):
            continue
        disc_number = int(medium.get("position") or medium_index)
        tracks = medium.get("tracks")
        if not isinstance(tracks, list):
            continue
        track_total = int(medium.get("track-count") or len(tracks))
        for track_index, raw_track in enumerate(tracks, start=1):
            if not isinstance(raw_track, Mapping):
                continue
            recording = raw_track.get("recording")
            recording = recording if isinstance(recording, Mapping) else {}
            artist = _artist_credit(raw_track.get("artist-credit")) or _artist_credit(
                recording.get("artist-credit")
            )
            isrcs = recording.get("isrcs") if isinstance(recording.get("isrcs"), list) else []
            result.append(
                {
                    "disc_number": disc_number,
                    "disc_total": disc_total,
                    "track_number": int(raw_track.get("position") or track_index),
                    "track_total": track_total,
                    "title": str(raw_track.get("title") or recording.get("title") or "").strip(),
                    "artist": artist,
                    "recording_id": recording.get("id"),
                    "isrc": next((str(value) for value in isrcs if value), None),
                    "duration": float(raw_track.get("length") or recording.get("length") or 0) / 1000.0,
                }
            )
    return result


def _match_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def _title_match_variants(value: object) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    variants = {_match_text(text)}
    without_parentheticals = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", text).strip()
    variants.add(_match_text(without_parentheticals))
    without_feature_suffix = re.sub(
        r"\s+(?:feat|ft|featuring)\.?\s+.+$",
        "",
        without_parentheticals,
    ).strip()
    variants.add(_match_text(without_feature_suffix))
    return {variant for variant in variants if variant}


def _release_titles_compatible(source_title: object, release_title: object) -> bool:
    source_variants = _title_match_variants(source_title)
    release_variants = _title_match_variants(release_title)
    if not source_variants or not release_variants:
        return True
    if source_variants & release_variants:
        return True
    source = next(iter(source_variants))
    release = next(iter(release_variants))
    return SequenceMatcher(None, source, release).ratio() >= 0.55


def _resolved_hints(track: Track, original: TagHints) -> TagHints:
    """Merge path-derived fallbacks without pretending they were embedded tags."""

    return replace(
        original,
        title=track.title or original.title,
        artist=track.artist or original.artist,
        album_artist=track.album_artist or original.album_artist,
        album=track.album or original.album,
        track_number=track.track_number or original.track_number,
        track_total=track.track_total or original.track_total,
        disc_number=track.disc_number or original.disc_number,
        disc_total=track.disc_total or original.disc_total,
        release_date=track.release_date or original.release_date,
        year=track.year or original.year,
        isrc=track.isrc or original.isrc,
        musicbrainz_recording_id=track.musicbrainz_recording_id or original.musicbrainz_recording_id,
        musicbrainz_release_id=track.musicbrainz_release_id or original.musicbrainz_release_id,
        embedded_artwork=track.embedded_artwork or original.embedded_artwork,
    )


def _validate_incoming_completeness(
    tracks: list[Track],
    original_by_id: Mapping[int, TagHints],
) -> None:
    """Fail closed when incoming tags describe only part of an album."""

    if not tracks:
        raise ReviewRequired("The import contains no tracks.", kind="METADATA")

    if len(tracks) == 1:
        original = original_by_id[tracks[0].id]
        claims_an_incomplete_album = any(
            (
                original.track_number not in {None, 1},
                original.track_total not in {None, 1},
                original.disc_number not in {None, 1},
                original.disc_total not in {None, 1},
            )
        )
        if claims_an_incomplete_album:
            raise ReviewRequired(
                "Incoming tags identify this as only part of an album. Upload the complete album or choose a matching release.",
                kind="METADATA",
            )
        return

    if any(original_by_id[track.id].track_number is None for track in tracks):
        raise ReviewRequired(
            "Incoming multi-track albums need explicit track numbers so completeness can be verified.",
            kind="METADATA",
        )

    by_disc: dict[int, list[tuple[Track, TagHints]]] = {}
    for track in tracks:
        original = original_by_id[track.id]
        disc_number = original.disc_number or 1
        by_disc.setdefault(disc_number, []).append((track, original))

    disc_numbers = sorted(by_disc)
    if disc_numbers != list(range(1, max(disc_numbers) + 1)):
        raise ReviewRequired("Incoming disc numbers are not contiguous from disc 1.", kind="METADATA")

    for disc_number, members in by_disc.items():
        positions = sorted(int(original.track_number or 0) for _, original in members)
        if positions != list(range(1, len(members) + 1)):
            raise ReviewRequired(
                f"Incoming disc {disc_number} has missing or duplicate track positions.",
                kind="METADATA",
            )
        declared_totals = {
            original.track_total
            for _, original in members
            if original.track_total is not None
        }
        # Downloaders commonly provide correct 1..N positions but omit the
        # optional total. That is still enough to prove the uploaded disc is
        # internally complete. An explicit, contradictory total remains a
        # review condition because it can indicate a partial album download.
        if len(declared_totals) > 1 or (
            declared_totals and next(iter(declared_totals)) != len(members)
        ):
            raise ReviewRequired(
                f"Incoming disc {disc_number} does not declare a consistent complete track total.",
                kind="METADATA",
            )

    declared_disc_totals = {
        original.disc_total
        for original in original_by_id.values()
        if original.disc_total is not None
    }
    if len(by_disc) > 1 or declared_disc_totals:
        if len(declared_disc_totals) != 1 or next(iter(declared_disc_totals)) != len(by_disc):
            raise ReviewRequired(
                "Incoming tags do not declare a consistent complete disc total.",
                kind="METADATA",
            )


def _release_assignment_compatible(track: Track, release_track: Mapping[str, Any]) -> bool:
    source_isrc = _match_text(track.isrc)
    release_isrc = _match_text(release_track.get("isrc"))
    if source_isrc and release_isrc and source_isrc != release_isrc:
        return False
    if not _release_titles_compatible(track.title, release_track.get("title")):
        return False
    duration = float(release_track.get("duration") or 0)
    return not duration or abs(duration - track.duration_seconds) <= 20


def _assign_release_tracks(
    tracks: list[Track],
    release_tracks: list[dict[str, Any]],
    original_by_id: Mapping[int, TagHints],
) -> list[dict[str, Any]]:
    """Assign edition tracks only from explicit positions or unique strong evidence."""

    keyed = {(item["disc_number"], item["track_number"]): item for item in release_tracks}
    explicit_keys = [
        ((original_by_id[track.id].disc_number or 1), original_by_id[track.id].track_number)
        for track in tracks
    ]
    positions_are_explicit = all(key[1] is not None for key in explicit_keys)
    if positions_are_explicit and len(set(explicit_keys)) == len(tracks) and all(key in keyed for key in explicit_keys):
        assigned = [keyed[key] for key in explicit_keys]
        if all(_release_assignment_compatible(track, row) for track, row in zip(tracks, assigned)):
            return assigned
        raise ReviewRequired(
            "Incoming track positions conflict with titles, ISRCs, or durations in the selected release. Choose another edition.",
            kind="METADATA",
        )

    options: dict[int, set[int]] = {}
    for track in tracks:
        source_isrc = _match_text(track.isrc)
        if source_isrc:
            choices = {
                index
                for index, row in enumerate(release_tracks)
                if _match_text(row.get("isrc")) == source_isrc
                and _release_assignment_compatible(track, row)
            }
        else:
            source_title = _match_text(track.title)
            choices = {
                index
                for index, row in enumerate(release_tracks)
                if source_title
                and _match_text(row.get("title")) == source_title
                and _release_assignment_compatible(track, row)
            }
        if not choices:
            raise ReviewRequired(
                "Tracks could not be mapped safely to the selected release by position, ISRC, title, and duration.",
                kind="METADATA",
            )
        options[track.id] = choices

    chosen: dict[int, int] = {}
    used: set[int] = set()
    while len(chosen) < len(tracks):
        reduced = {
            track.id: options[track.id] - used
            for track in tracks
            if track.id not in chosen
        }
        if any(not choices for choices in reduced.values()):
            raise ReviewRequired("The selected release cannot be mapped one-to-one to these files.", kind="METADATA")
        singletons = {track_id: next(iter(choices)) for track_id, choices in reduced.items() if len(choices) == 1}
        if not singletons or len(set(singletons.values())) != len(singletons):
            raise ReviewRequired(
                "Track mapping for the selected release is ambiguous. Add correct tags or use the incoming metadata.",
                kind="METADATA",
            )
        chosen.update(singletons)
        used.update(singletons.values())
    return [release_tracks[chosen[track.id]] for track in tracks]


def _metadata_plans(
    settings: Settings,
    job: Job,
    candidate: ReleaseCandidate | None,
) -> list[WorkingPlan]:
    root = _job_root(settings, job.id).resolve(strict=True)
    ordered_tracks = sorted(
        job.tracks,
        key=lambda item: (
            item.disc_number or 1,
            item.track_number or 1_000_000,
            _natural_path_key(item.original_filename),
        ),
    )
    original_by_id = {
        track.id: deserialize_hints(track.original_tags_json)
        for track in ordered_tracks
    }
    metadata_by_id: dict[int, NormalizedTrackMetadata] = {}
    if candidate is None:
        _validate_incoming_completeness(ordered_tracks, original_by_id)
        incoming_track_totals: dict[int, int] = {}
        for track in ordered_tracks:
            disc_number = original_by_id[track.id].disc_number or 1
            incoming_track_totals[disc_number] = incoming_track_totals.get(disc_number, 0) + 1
        incoming_disc_total = len(incoming_track_totals)
        for track in ordered_tracks:
            hints = _resolved_hints(track, original_by_id[track.id])
            disc_number = hints.disc_number or 1
            try:
                metadata_by_id[track.id] = metadata_from_hints(
                    hints,
                    fallback_title=_title_from_original(track.original_filename),
                    track_total=incoming_track_totals[disc_number],
                    disc_total=incoming_disc_total,
                )
            except MetadataError as exc:
                raise ReviewRequired(
                    f"Incoming metadata is not complete enough to import safely: {exc}",
                    kind="METADATA",
                ) from exc
    else:
        try:
            payload = json.loads(candidate.payload_json)
        except json.JSONDecodeError as exc:
            raise ReviewRequired("The cached MusicBrainz release data was invalid. Choose the release again.") from exc
        release_tracks = _release_track_rows(payload)
        if len(release_tracks) != len(ordered_tracks):
            raise ReviewRequired(
                f"The selected release has {len(release_tracks)} tracks but the import has {len(ordered_tracks)}. Choose another edition."
            )
        assignments = _assign_release_tracks(ordered_tracks, release_tracks, original_by_id)
        release_artist = _artist_credit(payload.get("artist-credit")) or candidate.artist_credit
        candidate_date = str(payload.get("date") or candidate.release_date or "") or None
        for track, release_track in zip(ordered_tracks, assignments):
            duration = release_track.get("duration") or 0
            if duration and abs(float(duration) - track.duration_seconds) > 20:
                raise ReviewRequired(
                    "Track durations do not align with the selected release. Choose another edition."
                )
            original = original_by_id[track.id]
            release_date = (
                candidate_date
                or track.release_date
                or original.release_date
                or (str(track.year or original.year) if (track.year or original.year) else None)
            )
            year_match = re.match(r"^(\d{4})", release_date or "")
            metadata_by_id[track.id] = NormalizedTrackMetadata(
                title=release_track["title"] or track.title or _title_from_original(track.original_filename),
                artist=release_track["artist"] or release_artist or track.artist or "Unknown Artist",
                album_artist=release_artist or track.album_artist or track.artist or "Unknown Artist",
                album=str(payload.get("title") or candidate.title),
                track_number=release_track["track_number"],
                track_total=release_track["track_total"],
                disc_number=release_track["disc_number"],
                disc_total=release_track["disc_total"],
                release_date=release_date,
                year=int(year_match.group(1)) if year_match else None,
                isrc=release_track["isrc"] or track.isrc,
                musicbrainz_recording_id=release_track["recording_id"],
                musicbrainz_release_id=candidate.musicbrainz_release_id,
            )
    plans: list[WorkingPlan] = []
    for track in ordered_tracks:
        source = root / Path(*track.source_relative_path.split("/"))
        try:
            source.resolve(strict=True).relative_to(root)
        except (ValueError, OSError) as exc:
            raise PipelineError("Inspected track source escaped its job directory") from exc
        plans.append(
            WorkingPlan(
                track_id=track.id,
                source_path=source,
                expected_sha256=track.sha256,
                extension=source.suffix.lower(),
                metadata=metadata_by_id[track.id],
            )
        )
    return plans


def _first_embedded(plans: Iterable[WorkingPlan]) -> bytes | None:
    for plan in plans:
        artwork = extract_embedded_artwork(plan.source_path)
        if artwork:
            return artwork
    return None


def _prepare_artwork(
    settings: Settings,
    plans: list[WorkingPlan],
    candidate: ReleaseCandidate | None,
) -> tuple[bytes | None, str | None, str]:
    cache = ArtworkFileCache(settings.config_dir / "cache" / "artwork")
    with ArtworkService(settings, cache=cache) as service:
        if candidate is not None:
            result = service.fetch_release_artwork(
                candidate.musicbrainz_release_id,
                embedded_loader=lambda: _first_embedded(plans),
            )
            return result.jpeg_bytes, result.warning, result.source
        embedded = _first_embedded(plans)
        if not embedded:
            return None, "No embedded artwork was available", "none"
        try:
            # The same strict decoder/encoder is used for network and embedded bytes.
            jpeg = service._validate_and_encode(embedded)
        except ArtworkError:
            return None, "Embedded artwork was invalid", "none"
        return jpeg, None, "embedded"


def _copy_verified_source(plan: WorkingPlan, destination: Path) -> None:
    """Copy the exact inspected regular file through a no-follow descriptor."""

    if plan.source_path.is_symlink():
        raise RetryablePipelineError("An inspected source was replaced by a symlink")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(plan.source_path, flags)
    except OSError as exc:
        raise RetryablePipelineError("An inspected source could not be reopened safely") from exc
    digest = hashlib.sha256()
    try:
        with os.fdopen(source_fd, "rb") as source, destination.open("xb") as output:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise RetryablePipelineError("An inspected source is no longer a regular file")
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != plan.expected_sha256:
            raise RetryablePipelineError("An inspected source changed before tagging; retry from a stable upload")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _remux_raw_aac(settings: Settings, source: Path, destination: Path) -> None:
    """Losslessly wrap a verified ADTS AAC stream in M4A so it can be tagged."""

    command = [
        settings.ffmpeg_path,
        "-nostdin",
        "-v",
        "error",
        "-n",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=settings.ffmpeg_timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise PipelineError("ffmpeg is not installed or FFMPEG_PATH is incorrect") from exc
    except subprocess.TimeoutExpired as exc:
        raise RetryablePipelineError("ffmpeg timed out while losslessly wrapping raw AAC") from exc
    if result.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        raise PipelineError("ffmpeg could not losslessly wrap the raw AAC stream as M4A")


def _tag_working_files(
    database: Database,
    settings: Settings,
    job_id: str,
    candidate: ReleaseCandidate | None,
) -> bytes | None:
    job = _load_job_with_tracks(database, job_id)
    plans = _metadata_plans(settings, job, candidate)
    working_dir = _job_root(settings, job_id) / "working"
    _remove_generated_directory(_job_root(settings, job_id), working_dir)
    working_dir.mkdir(mode=0o750)
    artwork, artwork_warning, artwork_source = _prepare_artwork(settings, plans, candidate)
    if artwork:
        with (working_dir / "cover.jpg").open("xb") as output:
            output.write(artwork)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(working_dir / "cover.jpg", 0o640)

    updates: list[tuple[WorkingPlan, str, str]] = []
    for plan in plans:
        if plan.extension == ".aac":
            verified_source = working_dir / f"{plan.track_id:06d}.source.aac"
            destination = working_dir / f"{plan.track_id:06d}.m4a"
            _copy_verified_source(plan, verified_source)
            try:
                _remux_raw_aac(settings, verified_source, destination)
            finally:
                verified_source.unlink(missing_ok=True)
        else:
            destination = working_dir / f"{plan.track_id:06d}{plan.extension}"
            _copy_verified_source(plan, destination)
        os.chmod(destination, 0o640)
        write_tags(destination, plan.metadata, artwork)
        final_hash = sha256_file(destination)
        updates.append((plan, destination.relative_to(_job_root(settings, job_id)).as_posix(), final_hash))

    with database.session() as session:
        current = session.get(Job, job_id)
        assert current is not None
        for plan, working_relative, final_hash in updates:
            track = session.get(Track, plan.track_id)
            assert track is not None and track.job_id == job_id
            metadata = plan.metadata
            track.working_relative_path = working_relative
            track.final_sha256 = final_hash
            track.title = metadata.title
            track.artist = metadata.artist
            track.album_artist = metadata.album_artist
            track.album = metadata.album
            track.track_number = metadata.track_number
            track.track_total = metadata.track_total
            track.disc_number = metadata.disc_number
            track.disc_total = metadata.disc_total
            track.release_date = metadata.release_date
            track.year = metadata.year
            track.isrc = metadata.isrc
            track.musicbrainz_recording_id = metadata.musicbrainz_recording_id
            track.musicbrainz_release_id = metadata.musicbrainz_release_id
        add_event(session, current, f"Tags written to {len(updates)} working file(s)")
        if artwork:
            add_event(session, current, f"Album artwork prepared from {artwork_source.replace('_', ' ')}")
        elif artwork_warning:
            add_event(session, current, artwork_warning, level="WARNING")
    return artwork


def _duplicate_check(database: Database, job_id: str) -> tuple[bool, int]:
    review: ReviewRequired | None = None
    all_exact = False
    exact_count = 0
    with database.session() as session:
        job = session.scalar(select(Job).where(Job.id == job_id).options(selectinload(Job.tracks)))
        assert job is not None
        assessment = assess_duplicates(
            session,
            job.tracks,
            music_root=database.settings.music_dir,
        )
        job.skipped_count = assessment.exact_count
        job.conflict_count = (
            len(assessment.conflicting_track_ids)
            + len(assessment.within_job_track_ids)
            + len(assessment.inventory_drift_track_ids)
        )
        exact_count = assessment.exact_count
        add_event(
            session,
            job,
            "Duplicate check complete",
            data={
                "exact": assessment.exact_count,
                "conflicts": job.conflict_count,
                "inventory_drift": len(assessment.inventory_drift_track_ids),
            },
        )
        if assessment.has_conflicts or (0 < assessment.exact_count < len(job.tracks)):
            review = ReviewRequired(
                "DUPLICATE_CONFLICT: one or more tracks collide with existing or in-job audio. No files were overwritten.",
                kind="DUPLICATE",
            )
        all_exact = assessment.exact_count == len(job.tracks)
    if review is not None:
        raise review
    return all_exact, exact_count


def _album_identity(job: Job) -> tuple[str, str, int | None]:
    if job.selected_release_id and job.selected_release_id != "__incoming__":
        selected = next(
            (
                candidate
                for candidate in job.candidates
                if candidate.musicbrainz_release_id == job.selected_release_id
            ),
            None,
        )
        if selected is not None:
            year_match = re.match(r"^(\d{4})", selected.release_date or "")
            return selected.artist_credit, selected.title, int(year_match.group(1)) if year_match else None

    artists = {track.album_artist or track.artist for track in job.tracks if track.album_artist or track.artist}
    albums = {track.album for track in job.tracks if track.album}
    years = {track.year for track in job.tracks if track.year}
    if len(artists) != 1 or len(albums) != 1 or len(years) > 1:
        raise ReviewRequired(
            "Tracks do not agree on album artist, album title, or year. Review the selected metadata.",
            kind="METADATA",
        )
    if not artists or not albums:
        raise ReviewRequired("Album artist and album title are required before organizing.", kind="METADATA")
    return next(iter(artists)), next(iter(albums)), next(iter(years)) if years else None


def _prepare_library_album(
    database: Database,
    settings: Settings,
    job_id: str,
    artwork: bytes | None,
) -> tuple[Any, dict[int, str]]:
    job = _load_job_with_tracks(database, job_id)
    artist, album_name, year = _album_identity(job)
    artist_component = sanitize_component(artist)
    album_component = sanitize_component(f"{album_name} ({year})" if year else album_name)
    destination_relative = job.output_relative_path or f"{artist_component}/{album_component}"
    destination_path = settings.music_dir / Path(*destination_relative.split("/"))
    if (
        not job.output_relative_path
        and (destination_path.exists() or destination_path.is_symlink())
        and job.selected_release_id
        and job.selected_release_id != "__incoming__"
    ):
        selected = next(
            (
                item
                for item in job.candidates
                if item.musicbrainz_release_id == job.selected_release_id
            ),
            None,
        )
        edition_bits = [
            selected.country if selected else None,
            selected.disambiguation[:80] if selected and selected.disambiguation else None,
            job.selected_release_id[:8],
        ]
        qualifier = " ".join(str(value) for value in edition_bits if value)
        edition_component = sanitize_component(
            f"{album_name} ({year}) [{qualifier}]" if year else f"{album_name} [{qualifier}]"
        )
        destination_relative = f"{artist_component}/{edition_component}"
    multi_disc = len({track.disc_number or 1 for track in job.tracks}) > 1 or any(
        (track.disc_total or 1) > 1 for track in job.tracks
    )
    filename_by_id: dict[int, str] = {}
    prepared_tracks: list[PreparedTrack] = []
    used: set[str] = set()
    root = _job_root(settings, job_id)
    for track in sorted(job.tracks, key=lambda item: (item.disc_number or 1, item.track_number or 0, item.id)):
        if not track.working_relative_path or not track.final_sha256:
            raise PipelineError("A tagged working file was missing")
        extension = Path(track.working_relative_path).suffix.removeprefix(".")
        filename = build_track_filename(
            track.title or "Unknown Track",
            track_number=track.track_number or 1,
            disc_number=track.disc_number or 1,
            multi_disc=multi_disc,
            extension=extension,
        )
        key = filename.casefold()
        if key in used:
            stem, suffix = Path(filename).stem, Path(filename).suffix
            filename = f"{stem}~{(track.musicbrainz_recording_id or track.final_sha256)[:8]}{suffix}"
            key = filename.casefold()
        if key in used:
            raise ReviewRequired("Two tracks still resolve to one filename after disambiguation.", kind="METADATA")
        used.add(key)
        filename_by_id[track.id] = filename
        prepared_tracks.append(
            PreparedTrack(
                source_path=root / Path(*track.working_relative_path.split("/")),
                relative_path=filename,
                source_sha256=track.sha256,
                final_sha256=track.final_sha256,
            )
        )
    prepared = prepare_album(
        music_root=settings.music_dir,
        job_id=job_id,
        destination_relative_path=destination_relative,
        tracks=prepared_tracks,
        cover_jpeg=artwork,
    )
    with database.session() as session:
        current = session.get(Job, job_id)
        assert current is not None
        current.output_relative_path = destination_relative
        add_event(session, current, "Album assembled outside the live library")
    return prepared, filename_by_id


def _register_import(
    database: Database,
    job_id: str,
    destination_relative: str,
    filename_by_id: dict[int, str],
) -> None:
    with database.session() as session:
        job = session.scalar(select(Job).where(Job.id == job_id).options(selectinload(Job.tracks)))
        assert job is not None
        for track in job.tracks:
            final_relative = f"{destination_relative}/{filename_by_id[track.id]}"
            track.final_relative_path = final_relative
            existing = session.scalar(
                select(LibraryTrack).where(LibraryTrack.final_relative_path == final_relative)
            )
            if existing is None:
                session.add(
                    LibraryTrack(
                        final_relative_path=final_relative,
                        sha256=track.sha256,
                        final_sha256=track.final_sha256 or track.sha256,
                        normalized_identity=normalized_track_identity(track),
                        normalized_slot=normalized_track_slot(track),
                        musicbrainz_recording_id=track.musicbrainz_recording_id,
                        musicbrainz_release_id=track.musicbrainz_release_id,
                        codec=track.codec,
                        bitrate=track.bitrate,
                        sample_rate=track.sample_rate,
                        bit_depth=track.bit_depth,
                        source_job_id=job_id,
                    )
                )
            elif existing.source_job_id != job_id or existing.final_sha256 != track.final_sha256:
                raise LibraryConflictError("Library inventory conflicts with the atomically imported album")
        job.imported_count = len(job.tracks)
        add_event(session, job, f"Album inventory registered: {len(job.tracks)} tracks")


def _record_jellyfin_result(database: Database, job_id: str, result: JellyfinRefreshResult) -> None:
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.jellyfin_state = result.state.value
        job.jellyfin_error = result.error
        job.jellyfin_retryable = result.retryable
        job.jellyfin_attempts += 1 if result.state != JellyfinState.NOT_CONFIGURED else 0
        job.jellyfin_last_attempt_at = datetime.now(timezone.utc)
        job.jellyfin_retry_requested = False
        if result.state == JellyfinState.SUCCEEDED:
            message = "Jellyfin library refresh requested"
            level = "INFO"
        elif result.state == JellyfinState.NOT_CONFIGURED:
            message = "Jellyfin refresh skipped because it is not configured"
            level = "INFO"
        else:
            message = result.error or "Jellyfin refresh failed; the imported album remains intact"
            level = "WARNING"
        add_event(session, job, message, level=level)


def _is_registered_import_for_same_job(database: Database, job_id: str, expected_tracks: int) -> bool:
    with database.session() as session:
        registered = session.scalars(
            select(LibraryTrack.id).where(LibraryTrack.source_job_id == job_id)
        ).all()
        return len(registered) == expected_tracks and expected_tracks > 0


def _fail_job(database: Database, settings: Settings, job_id: str, exc: Exception) -> None:
    code = getattr(exc, "code", exc.__class__.__name__.upper())[:100]
    retryable = bool(getattr(exc, "retryable", isinstance(exc, (OSError, RetryablePipelineError))))
    safe_types = (
        PipelineError,
        ArchiveError,
        AudioInspectionError,
        MetadataError,
        LibraryImportError,
        MusicBrainzError,
    )
    message = str(exc)[:1000] if isinstance(exc, safe_types) else "Unexpected processing error. See the worker logs for details."
    logger.exception("Job processing failed", extra={"job_id": job_id, "error_code": code})
    with database.session() as session:
        job = session.get(Job, job_id)
        if job is None or job.state in {JobState.COMPLETE.value, JobState.NEEDS_REVIEW.value, JobState.FAILED.value}:
            return
        job.error_code = code
        job.error_message = message
        job.retryable = retryable
        transition_job(session, job, JobState.FAILED, message, level="ERROR")
    build_root = settings.imports_dir / job_id
    if build_root.exists() and not build_root.is_symlink():
        try:
            build_root.resolve(strict=False).relative_to(settings.imports_dir.resolve(strict=True))
            shutil.rmtree(build_root, ignore_errors=True)
        except ValueError:
            pass


def process_job(database: Database, settings: Settings, job_id: str) -> None:
    """Run one persisted import job through every Milestone 1 stage."""

    try:
        job = _load_job_with_tracks(database, job_id)
        if job.state != JobState.STAGING.value:
            raise PipelineError(f"Worker received job in unexpected state {job.state}")
        source_path = _safe_job_source(settings, job)
        job_root = _job_root(settings, job_id)
        if source_path.suffix.lower() == ".zip":
            _transition(database, job_id, JobState.EXTRACTING, "Validating and extracting archive")
            extracted = job_root / "extracted"
            _remove_generated_directory(job_root, extracted)
            manifest = secure_extract_zip(source_path, extracted, limits=_archive_limits(settings))
            sources = _source_audio_from_archive(extracted, manifest)
            _event(
                database,
                job_id,
                f"Archive validated: {manifest.file_count} files, {manifest.total_bytes} bytes",
            )
            _transition(database, job_id, JobState.INSPECTING, f"Inspecting {len(sources)} audio tracks")
        elif source_path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
            sources = [SourceAudio(path=source_path, original_path=job.source_filename)]
            _transition(database, job_id, JobState.INSPECTING, "Inspecting audio file")
        else:
            raise PipelineError("Staged source type is unsupported")

        inspected = _inspect_sources(settings, sources, job.display_name)
        _store_inspection(database, job_id, inspected)
        _transition(database, job_id, JobState.MATCHING_METADATA, "Searching MusicBrainz for release metadata")
        try:
            candidate = _choose_release(database, settings, job_id)
        except ReviewRequired as review:
            _pause_for_review(database, job_id, review)
            return

        _transition(database, job_id, JobState.TAGGING, "Writing normalized metadata to working copies")
        try:
            artwork = _tag_working_files(database, settings, job_id, candidate)
            all_exact, exact_count = _duplicate_check(database, job_id)
        except ReviewRequired as review:
            _pause_for_review(database, job_id, review)
            return
        except UnsupportedTaggingError as exc:
            _pause_for_review(database, job_id, ReviewRequired(str(exc), kind="FORMAT"))
            return

        _transition(database, job_id, JobState.ORGANIZING, "Normalizing album and track filenames")
        if all_exact:
            own_recovery = _is_registered_import_for_same_job(database, job_id, exact_count)
            _transition(
                database,
                job_id,
                JobState.VALIDATING,
                "Previously imported album confirmed" if own_recovery else "Exact duplicate album confirmed",
            )
            _transition(
                database,
                job_id,
                JobState.IMPORTING,
                "Reconciling prior import" if own_recovery else "Skipping exact duplicate without modifying the library",
            )
            if own_recovery:
                _transition(database, job_id, JobState.JELLYFIN_SCAN, "Retrying Jellyfin refresh after import recovery")
                result = refresh_jellyfin_library(settings)
                _record_jellyfin_result(database, job_id, result)
                with database.session() as session:
                    recovered_job = session.get(Job, job_id)
                    assert recovered_job is not None
                    transition_job(session, recovered_job, JobState.COMPLETE, "Recovered import complete")
                return
            with database.session() as session:
                duplicate_job = session.get(Job, job_id)
                assert duplicate_job is not None
                duplicate_job.skipped_count = exact_count
                duplicate_job.imported_count = 0
                transition_job(session, duplicate_job, JobState.COMPLETE, "Exact duplicate skipped")
            return

        try:
            prepared, filename_by_id = _prepare_library_album(database, settings, job_id, artwork)
        except ReviewRequired as review:
            _pause_for_review(database, job_id, review)
            return
        _transition(database, job_id, JobState.VALIDATING, "Verifying complete prepared album")
        verify_prepared_album(prepared)
        _transition(database, job_id, JobState.IMPORTING, "Atomically publishing album to the music library")
        destination, reconciled = commit_album(music_root=settings.music_dir, album=prepared)
        try:
            _register_import(database, job_id, prepared.destination_relative_path, filename_by_id)
        except Exception as exc:
            raise CommittedImportPending(
                "The complete album is published but inventory registration was interrupted. Retry to verify its manifest and finish reconciliation."
            ) from exc
        _event(
            database,
            job_id,
            "Album atomically imported" if not reconciled else "Previously committed album reconciled after interruption",
            data={"destination": prepared.destination_relative_path},
        )
        _transition(database, job_id, JobState.JELLYFIN_SCAN, "Requesting Jellyfin library refresh")
        result = refresh_jellyfin_library(settings)
        _record_jellyfin_result(database, job_id, result)
        with database.session() as session:
            completed = session.get(Job, job_id)
            assert completed is not None
            transition_job(session, completed, JobState.COMPLETE, "Import complete")
    except Exception as exc:
        _fail_job(database, settings, job_id, exc)


def retry_jellyfin_refresh(database: Database, settings: Settings, job_id: str) -> None:
    job = _load_job_with_tracks(database, job_id)
    if job.state != JobState.COMPLETE.value or not job.jellyfin_retry_requested:
        return
    result = refresh_jellyfin_library(settings)
    _record_jellyfin_result(database, job_id, result)
