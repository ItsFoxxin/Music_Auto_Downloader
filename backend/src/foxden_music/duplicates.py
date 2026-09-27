from __future__ import annotations

import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .enums import DuplicateStatus
from .models import LibraryTrack, Track
from .audio import sha256_file


def normalized_track_identity(track: Track) -> str:
    def part(value: object) -> str:
        text = unicodedata.normalize("NFC", str(value or ""))
        return re.sub(r"\s+", " ", text).strip().casefold()

    return "|".join(
        (
            part(track.album_artist or track.artist),
            part(track.album),
            part(track.musicbrainz_release_id or track.release_date or track.year),
            str(track.disc_number or 1),
            str(track.track_number or 0),
            part(track.title),
        )
    )


def normalized_track_slot(track: Track) -> str:
    """Edition-neutral slot used only when either side lacks a release MBID."""

    def part(value: object) -> str:
        text = unicodedata.normalize("NFC", str(value or ""))
        return re.sub(r"\s+", " ", text).strip().casefold()

    year_match = re.match(r"^(\d{4})", str(track.release_date or ""))
    release_year = track.year or (year_match.group(1) if year_match else track.release_date)
    return "|".join(
        (
            part(track.album_artist or track.artist),
            part(track.album),
            part(release_year),
            str(track.disc_number or 1),
            str(track.track_number or 0),
            part(track.title),
        )
    )


@dataclass(slots=True)
class DuplicateAssessment:
    exact_track_ids: list[int] = field(default_factory=list)
    conflicting_track_ids: list[int] = field(default_factory=list)
    within_job_track_ids: list[int] = field(default_factory=list)
    inventory_drift_track_ids: list[int] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return bool(
            self.conflicting_track_ids
            or self.within_job_track_ids
            or self.inventory_drift_track_ids
        )

    @property
    def exact_count(self) -> int:
        return len(self.exact_track_ids)


def _inventory_file_matches(music_root: Path, candidate: LibraryTrack) -> bool:
    """Verify that an inventory row still names the exact regular file it recorded."""

    relative = PurePosixPath(candidate.final_relative_path)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return False
    try:
        root = music_root.resolve(strict=True)
        current = root
        for part in relative.parts:
            current = current / part
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                return False
        if not stat.S_ISREG(current.lstat().st_mode):
            return False
        current.resolve(strict=True).relative_to(root)
        return sha256_file(current) == candidate.final_sha256
    except (OSError, ValueError):
        return False


def assess_duplicates(
    session: Session,
    tracks: list[Track],
    *,
    music_root: Path | None = None,
) -> DuplicateAssessment:
    """Classify duplicates without treating legitimate compilation reuse as identical."""

    assessment = DuplicateAssessment()
    seen_slots: dict[str, Track] = {}
    seen_neutral_slots: dict[str, Track] = {}
    seen_source_hashes: dict[str, Track] = {}
    seen_final_hashes: dict[str, Track] = {}
    for track in tracks:
        identity = normalized_track_identity(track)
        slot = normalized_track_slot(track)
        previous = seen_slots.get(identity) or seen_neutral_slots.get(slot)
        repeated_source = track.sha256 in seen_source_hashes
        repeated_final = bool(track.final_sha256 and track.final_sha256 in seen_final_hashes)
        if previous is not None or repeated_source or repeated_final:
            track.duplicate_status = DuplicateStatus.EXACT_IN_JOB.value
            assessment.within_job_track_ids.append(track.id)
            continue
        seen_slots[identity] = track
        seen_neutral_slots[slot] = track
        seen_source_hashes[track.sha256] = track
        if track.final_sha256:
            seen_final_hashes[track.final_sha256] = track

        clauses = [
            LibraryTrack.normalized_identity == identity,
            LibraryTrack.normalized_slot == slot,
            LibraryTrack.sha256 == track.sha256,
        ]
        if track.final_sha256:
            clauses.append(LibraryTrack.final_sha256 == track.final_sha256)
        if track.musicbrainz_recording_id:
            clauses.append(LibraryTrack.musicbrainz_recording_id == track.musicbrainz_recording_id)
        candidates = session.scalars(select(LibraryTrack).where(or_(*clauses))).all()
        candidates = [
            candidate
            for candidate in candidates
            if (
                candidate.normalized_identity == identity
                or candidate.sha256 == track.sha256
                or (track.final_sha256 and candidate.final_sha256 == track.final_sha256)
                or (
                    track.musicbrainz_recording_id
                    and candidate.musicbrainz_recording_id == track.musicbrainz_recording_id
                )
                or (
                    candidate.normalized_slot == slot
                    and not (
                        candidate.musicbrainz_release_id
                        and track.musicbrainz_release_id
                        and candidate.musicbrainz_release_id != track.musicbrainz_release_id
                    )
                )
            )
        ]
        if not candidates:
            track.duplicate_status = DuplicateStatus.NONE.value
            continue
        def same_logical_slot(candidate: LibraryTrack) -> bool:
            if candidate.normalized_identity == identity:
                return True
            return bool(
                candidate.normalized_slot == slot
                and not (
                    candidate.musicbrainz_release_id
                    and track.musicbrainz_release_id
                    and candidate.musicbrainz_release_id != track.musicbrainz_release_id
                )
            )

        exact_candidates = [
            candidate
            for candidate in candidates
            if (
                track.final_sha256 == candidate.final_sha256
                or track.sha256 == candidate.sha256
            )
            and same_logical_slot(candidate)
        ]
        if music_root is not None and exact_candidates:
            verified = next(
                (candidate for candidate in exact_candidates if _inventory_file_matches(music_root, candidate)),
                None,
            )
            if verified is None:
                existing = exact_candidates[0]
                track.duplicate_library_track_id = existing.id
                track.duplicate_status = DuplicateStatus.RECORDING_CONFLICT.value
                assessment.inventory_drift_track_ids.append(track.id)
                continue
            existing = verified
        else:
            existing = exact_candidates[0] if exact_candidates else candidates[0]
        same_audio = bool(exact_candidates)
        track.duplicate_library_track_id = existing.id
        if same_audio:
            track.duplicate_status = DuplicateStatus.EXACT_IN_LIBRARY.value
            assessment.exact_track_ids.append(track.id)
        else:
            track.duplicate_status = DuplicateStatus.RECORDING_CONFLICT.value
            assessment.conflicting_track_ids.append(track.id)
    return assessment
