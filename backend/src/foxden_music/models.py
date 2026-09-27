from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import (
    AcquisitionProvider,
    AcquisitionState,
    AcquisitionSourceType,
    DuplicateStatus,
    JellyfinState,
    JobKind,
    JobState,
    LibraryScanState,
    PreferredFormat,
    SourceType,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class DatabaseMetadata(Base):
    __tablename__ = "database_metadata"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=JobKind.ALBUM_IMPORT.value)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default=SourceType.UPLOAD.value)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=JobState.QUEUED.value, index=True)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source_relative_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(2000))

    selected_release_id: Mapped[str | None] = mapped_column(String(64), index=True)
    match_confidence: Mapped[float | None] = mapped_column(Float)
    review_kind: Mapped[str | None] = mapped_column(String(32))
    review_reason: Mapped[str | None] = mapped_column(Text)
    output_relative_path: Mapped[str | None] = mapped_column(String(1500))

    track_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)

    jellyfin_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JellyfinState.NOT_REQUESTED.value
    )
    jellyfin_error: Mapped[str | None] = mapped_column(Text)
    jellyfin_retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    jellyfin_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    jellyfin_last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    jellyfin_retry_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list[JobEvent]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobEvent.id"
    )
    tracks: Mapped[list[Track]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Track.id"
    )
    candidates: Mapped[list[ReleaseCandidate]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="ReleaseCandidate.score.desc()"
    )


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    data_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="events")


class Track(Base):
    __tablename__ = "tracks"
    __table_args__ = (UniqueConstraint("job_id", "source_relative_path", name="uq_track_job_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    source_relative_path: Mapped[str] = mapped_column(String(1200), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(1000), nullable=False)
    working_relative_path: Mapped[str | None] = mapped_column(String(1200))
    final_relative_path: Mapped[str | None] = mapped_column(String(1500))

    container: Mapped[str] = mapped_column(String(100), nullable=False)
    codec: Mapped[str] = mapped_column(String(100), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    bitrate: Mapped[int | None] = mapped_column(Integer)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    bit_depth: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(Integer)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    final_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    original_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    embedded_artwork: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    title: Mapped[str | None] = mapped_column(String(1000))
    artist: Mapped[str | None] = mapped_column(String(1000))
    album_artist: Mapped[str | None] = mapped_column(String(1000))
    album: Mapped[str | None] = mapped_column(String(1000))
    track_number: Mapped[int | None] = mapped_column(Integer)
    track_total: Mapped[int | None] = mapped_column(Integer)
    disc_number: Mapped[int | None] = mapped_column(Integer)
    disc_total: Mapped[int | None] = mapped_column(Integer)
    release_date: Mapped[str | None] = mapped_column(String(32))
    year: Mapped[int | None] = mapped_column(Integer)
    isrc: Mapped[str | None] = mapped_column(String(32), index=True)
    musicbrainz_recording_id: Mapped[str | None] = mapped_column(String(36), index=True)
    musicbrainz_release_id: Mapped[str | None] = mapped_column(String(36), index=True)

    duplicate_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DuplicateStatus.NONE.value
    )
    duplicate_library_track_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_tracks.id", ondelete="SET NULL")
    )

    job: Mapped[Job] = relationship(back_populates="tracks")


class ReleaseCandidate(Base):
    __tablename__ = "release_candidates"
    __table_args__ = (UniqueConstraint("job_id", "musicbrainz_release_id", name="uq_candidate_job_mbid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    musicbrainz_release_id: Mapped[str] = mapped_column(String(36), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    artist_credit: Mapped[str] = mapped_column(String(1000), nullable=False)
    release_date: Mapped[str | None] = mapped_column(String(32))
    country: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str | None] = mapped_column(String(100))
    disambiguation: Mapped[str | None] = mapped_column(String(1000))
    media_summary: Mapped[str | None] = mapped_column(String(1000))
    track_count: Mapped[int | None] = mapped_column(Integer)
    source_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="candidates")


class LibraryTrack(Base):
    __tablename__ = "library_tracks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    final_relative_path: Mapped[str] = mapped_column(String(1500), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    final_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    normalized_identity: Mapped[str] = mapped_column(String(2000), nullable=False, index=True)
    normalized_slot: Mapped[str | None] = mapped_column(String(2000), index=True)
    musicbrainz_recording_id: Mapped[str | None] = mapped_column(String(36), index=True)
    musicbrainz_release_id: Mapped[str | None] = mapped_column(String(36), index=True)
    codec: Mapped[str] = mapped_column(String(100), nullable=False)
    bitrate: Mapped[int | None] = mapped_column(Integer)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    bit_depth: Mapped[int | None] = mapped_column(Integer)
    source_job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MetadataCache(Base):
    __tablename__ = "metadata_cache"
    __table_args__ = (Index("ix_metadata_cache_expiry", "expires_at"),)

    cache_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(50), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AcquisitionJob(Base):
    """A request to obtain media, deliberately separate from import processing."""

    __tablename__ = "acquisition_jobs"
    __table_args__ = (
        Index("ix_acquisition_state_updated", "state", "updated_at"),
        Index("ix_acquisition_source", "provider", "source_type", "source_identifier"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider: Mapped[str] = mapped_column(
        String(48), nullable=False, default=AcquisitionProvider.SPOTIDOWNLOADER_MANUAL.value
    )
    source_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    display_title: Mapped[str] = mapped_column(String(500), nullable=False)
    artist: Mapped[str | None] = mapped_column(String(500))
    album: Mapped[str | None] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AcquisitionState.WAITING_FOR_USER.value, index=True
    )
    preferred_format: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PreferredFormat.FLAC.value
    )
    associated_import_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), unique=True, index=True
    )
    acquisition_relative_directory: Mapped[str] = mapped_column(String(500), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    file_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    import_job: Mapped[Job | None] = relationship(foreign_keys=[associated_import_job_id])
    events: Mapped[list[AcquisitionEvent]] = relationship(
        back_populates="acquisition_job",
        cascade="all, delete-orphan",
        order_by="AcquisitionEvent.id",
    )
    artifacts: Mapped[list[AcquisitionArtifact]] = relationship(
        back_populates="acquisition_job",
        cascade="all, delete-orphan",
        order_by="AcquisitionArtifact.id",
    )


class AcquisitionEvent(Base):
    __tablename__ = "acquisition_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    acquisition_job_id: Mapped[str] = mapped_column(
        ForeignKey("acquisition_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    event_code: Mapped[str | None] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    data_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    acquisition_job: Mapped[AcquisitionJob] = relationship(back_populates="events")


class AcquisitionArtifact(Base):
    __tablename__ = "acquisition_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    acquisition_job_id: Mapped[str] = mapped_column(
        ForeignKey("acquisition_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    import_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    received_via: Mapped[str] = mapped_column(String(32), nullable=False, default="UPLOAD")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="HANDED_OFF")
    display_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_relative_path: Mapped[str | None] = mapped_column(String(1200))
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    handed_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    acquisition_job: Mapped[AcquisitionJob] = relationship(back_populates="artifacts")


class LibraryArtist(Base):
    __tablename__ = "library_artists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True, index=True)
    album_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    track_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    flac_track_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    albums: Mapped[list[LibraryAlbum]] = relationship(back_populates="artist")


class LibraryAlbum(Base):
    __tablename__ = "library_albums"
    __table_args__ = (Index("ix_library_album_artist_title", "artist_id", "normalized_title"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artist_id: Mapped[int] = mapped_column(
        ForeignKey("library_artists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    album_artist: Mapped[str] = mapped_column(String(1000), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(1000), nullable=False, index=True)
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    relative_path: Mapped[str] = mapped_column(String(1500), nullable=False, unique=True)
    musicbrainz_release_id: Mapped[str | None] = mapped_column(String(36), index=True)
    track_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    artwork_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    codec_summary: Mapped[str | None] = mapped_column(String(500))
    scan_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    artist: Mapped[LibraryArtist] = relationship(back_populates="albums")
    tracks: Mapped[list[LibraryInventoryTrack]] = relationship(
        back_populates="album", cascade="all, delete-orphan"
    )


class LibraryInventoryTrack(Base):
    """Read-only index of every supported audio file observed under /music."""

    __tablename__ = "library_inventory_tracks"
    __table_args__ = (
        Index("ix_inventory_album_position", "album_id", "disc_number", "track_number"),
        Index("ix_inventory_quality", "codec", "bitrate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    album_id: Mapped[int] = mapped_column(
        ForeignKey("library_albums.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relative_path: Mapped[str] = mapped_column(String(1500), nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(String(1000))
    artist: Mapped[str | None] = mapped_column(String(1000))
    album_artist: Mapped[str | None] = mapped_column(String(1000))
    album_title: Mapped[str | None] = mapped_column(String(1000))
    year: Mapped[int | None] = mapped_column(Integer)
    track_number: Mapped[int | None] = mapped_column(Integer)
    track_total: Mapped[int | None] = mapped_column(Integer)
    disc_number: Mapped[int | None] = mapped_column(Integer)
    disc_total: Mapped[int | None] = mapped_column(Integer)
    container: Mapped[str | None] = mapped_column(String(100))
    codec: Mapped[str | None] = mapped_column(String(100), index=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, index=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    bit_depth: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    musicbrainz_recording_id: Mapped[str | None] = mapped_column(String(36), index=True)
    musicbrainz_release_id: Mapped[str | None] = mapped_column(String(36), index=True)
    embedded_artwork: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    missing_title: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    missing_artist: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    missing_album: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    missing_track_number: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    inspection_error: Mapped[str | None] = mapped_column(String(500))
    scan_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    album: Mapped[LibraryAlbum] = relationship(back_populates="tracks")


class LibraryScanRun(Base):
    __tablename__ = "library_scan_runs"
    __table_args__ = (Index("ix_library_scan_state_created", "state", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=LibraryScanState.QUEUED.value, index=True
    )
    reason: Mapped[str] = mapped_column(String(100), nullable=False, default="manual")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_inspected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_reused: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StorageSnapshot(Base):
    __tablename__ = "storage_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    library_bytes: Mapped[int | None] = mapped_column(BigInteger)
    staging_bytes: Mapped[int | None] = mapped_column(BigInteger)
    music_filesystem_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    music_filesystem_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    staging_filesystem_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    staging_filesystem_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
