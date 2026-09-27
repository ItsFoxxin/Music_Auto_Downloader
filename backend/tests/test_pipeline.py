from __future__ import annotations

import os
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from foxden_music.audio import AudioInspection, sha256_file
from foxden_music.duplicates import assess_duplicates, normalized_track_identity, normalized_track_slot
from foxden_music.enums import JellyfinState, JobState
from foxden_music.metadata import NormalizedTrackMetadata, TagHints, serialize_hints
from foxden_music.models import Job, LibraryTrack, ReleaseCandidate, Track
from foxden_music.pipeline import (
    ReviewRequired,
    RetryablePipelineError,
    WorkingPlan,
    _assign_release_tracks,
    _choose_release,
    _copy_verified_source,
    _release_duration_compatible,
    _metadata_plans,
    _natural_path_key,
    _prepare_library_album,
    _release_titles_compatible,
    _remux_raw_aac,
    process_job,
)


def _queue_single_track(database, settings, *, job_id: str, title: str = "MOUNTAINS") -> Path:
    incoming = settings.jobs_dir / job_id / "incoming"
    incoming.mkdir(parents=True)
    source = incoming / "source.flac"
    source.write_bytes(b"fLaC\x00synthetic-generated-test-audio")
    with database.session() as session:
        session.add(
            Job(
                id=job_id,
                state=JobState.STAGING.value,
                display_name="ATE.flac",
                source_filename="ATE.flac",
                source_relative_path="incoming/source.flac",
                selected_release_id="__incoming__",
            )
        )
    return source


def _mock_pipeline(monkeypatch, source: Path, title: str = "MOUNTAINS") -> None:
    inspection = AudioInspection(
        container="flac",
        codec="flac",
        duration_seconds=182.0,
        bitrate=1_000_000,
        sample_rate=48_000,
        bit_depth=24,
        channels=2,
        file_size=source.stat().st_size,
        sha256=sha256_file(source),
    )
    monkeypatch.setattr("foxden_music.pipeline.inspect_audio", lambda path, settings: inspection)
    monkeypatch.setattr(
        "foxden_music.pipeline.read_tag_hints",
        lambda path: TagHints(
            title=title,
            artist="Stray Kids",
            album_artist="Stray Kids",
            album="ATE",
            track_number=1,
            track_total=1,
            disc_number=1,
            disc_total=1,
            release_date="2024-07-19",
            year=2024,
        ),
    )
    monkeypatch.setattr("foxden_music.pipeline.write_tags", lambda path, metadata, artwork: None)
    monkeypatch.setattr("foxden_music.pipeline.extract_embedded_artwork", lambda path: None)


def test_synthetic_end_to_end_import_and_exact_duplicate_skip(
    settings, database, monkeypatch
) -> None:
    first_id = "10000000-0000-0000-0000-000000000001"
    first_source = _queue_single_track(database, settings, job_id=first_id)
    _mock_pipeline(monkeypatch, first_source)
    process_job(database, settings, first_id)

    with database.session() as session:
        first = session.get(Job, first_id)
        assert first is not None
        assert first.state == JobState.COMPLETE.value
        assert first.jellyfin_state == JellyfinState.NOT_CONFIGURED.value
        assert first.output_relative_path == "Stray Kids/ATE (2024)"
        assert first.imported_count == 1
        library_track = session.query(LibraryTrack).one()
        final_path = settings.music_dir / Path(*library_track.final_relative_path.split("/"))
        assert final_path.is_file()
        assert final_path.name == "01 - MOUNTAINS.flac"

    second_id = "10000000-0000-0000-0000-000000000002"
    second_source = _queue_single_track(database, settings, job_id=second_id)
    _mock_pipeline(monkeypatch, second_source)
    process_job(database, settings, second_id)
    with database.session() as session:
        second = session.get(Job, second_id)
        assert second is not None
        assert second.state == JobState.COMPLETE.value
        assert second.skipped_count == 1
        assert second.imported_count == 0
        assert session.query(LibraryTrack).count() == 1


def test_processing_failure_never_creates_final_album(settings, database, monkeypatch) -> None:
    job_id = "20000000-0000-0000-0000-000000000001"
    source = _queue_single_track(database, settings, job_id=job_id)
    monkeypatch.setattr(
        "foxden_music.pipeline.inspect_audio",
        lambda path, settings: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )
    process_job(database, settings, job_id)
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.state == JobState.FAILED.value
        assert "synthetic failure" not in (job.error_message or "")
    live_entries = [path for path in settings.music_dir.iterdir() if path.name != ".imports"]
    assert live_entries == []


def test_registration_failure_preserves_published_album_for_retry(
    settings, database, monkeypatch
) -> None:
    job_id = "20000000-0000-0000-0000-000000000002"
    source = _queue_single_track(database, settings, job_id=job_id)
    _mock_pipeline(monkeypatch, source)
    monkeypatch.setattr(
        "foxden_music.pipeline._register_import",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    process_job(database, settings, job_id)
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.state == JobState.FAILED.value
        assert job.retryable is True
        assert job.error_code == "COMMITTED_IMPORT_PENDING_RECONCILIATION"
        assert "database unavailable" not in (job.error_message or "")
        assert job.output_relative_path is not None
        assert (settings.music_dir / Path(*job.output_relative_path.split("/"))).is_dir()


def _track(job_id: str, *, sha: str, title: str, recording_id: str | None = None) -> Track:
    return Track(
        job_id=job_id,
        source_relative_path=f"incoming/{title}.flac",
        original_filename=f"{title}.flac",
        container="flac",
        codec="flac",
        duration_seconds=120,
        file_size=100,
        sha256=sha,
        final_sha256=sha,
        original_tags_json="{}",
        title=title,
        artist="Artist",
        album_artist="Artist",
        album="Album",
        track_number=1,
        track_total=1,
        disc_number=1,
        disc_total=1,
        release_date="2026",
        year=2026,
        musicbrainz_recording_id=recording_id,
    )


def test_duplicate_signals_include_global_hash_and_recording_id(database) -> None:
    existing_job_id = "30000000-0000-0000-0000-000000000001"
    new_job_id = "30000000-0000-0000-0000-000000000002"
    with database.session() as session:
        session.add_all(
            [
                Job(
                    id=existing_job_id,
                    state=JobState.COMPLETE.value,
                    display_name="old",
                    source_filename="old.flac",
                    source_relative_path="incoming/old.flac",
                ),
                Job(
                    id=new_job_id,
                    state=JobState.TAGGING.value,
                    display_name="new",
                    source_filename="new.flac",
                    source_relative_path="incoming/new.flac",
                ),
            ]
        )
        session.flush()
        session.add(
            LibraryTrack(
                final_relative_path="Other/Release/01.flac",
                sha256="a" * 64,
                final_sha256="a" * 64,
                normalized_identity="unrelated identity",
                musicbrainz_recording_id="recording-one",
                musicbrainz_release_id="release-one",
                codec="flac",
                source_job_id=existing_job_id,
            )
        )
        exact = _track(new_job_id, sha="a" * 64, title="Different Slot")
        session.add(exact)
        session.flush()
        exact_result = assess_duplicates(session, [exact])
        assert exact_result.exact_count == 0
        assert exact_result.has_conflicts is True

        conflict = _track(
            new_job_id,
            sha="b" * 64,
            title="Another Release",
            recording_id="recording-one",
        )
        conflict.source_relative_path = "incoming/conflict.flac"
        session.add(conflict)
        session.flush()
        conflict_result = assess_duplicates(session, [conflict])
        assert conflict_result.has_conflicts is True


def test_within_job_duplicate_detects_shared_raw_hash_even_after_different_tags(database) -> None:
    job_id = "30000000-0000-0000-0000-000000000003"
    with database.session() as session:
        session.add(
            Job(
                id=job_id,
                state=JobState.TAGGING.value,
                display_name="same source twice",
                source_filename="album.zip",
                source_relative_path="incoming/album.zip",
            )
        )
        session.flush()
        first = _track(job_id, sha="d" * 64, title="One")
        first.final_sha256 = "1" * 64
        second = _track(job_id, sha="d" * 64, title="Two")
        second.source_relative_path = "incoming/two.flac"
        second.final_sha256 = "2" * 64
        session.add_all([first, second])
        session.flush()
        result = assess_duplicates(session, [first, second])
        assert result.has_conflicts is True
        assert result.within_job_track_ids == [second.id]


def test_exact_duplicate_requires_live_file_hash(settings, database) -> None:
    existing_job_id = "30000000-0000-0000-0000-000000000004"
    new_job_id = "30000000-0000-0000-0000-000000000005"
    incoming = _track(new_job_id, sha="e" * 64, title="Same bytes")
    with database.session() as session:
        session.add_all(
            [
                Job(
                    id=existing_job_id,
                    state=JobState.COMPLETE.value,
                    display_name="old",
                    source_filename="old.flac",
                    source_relative_path="incoming/old.flac",
                ),
                Job(
                    id=new_job_id,
                    state=JobState.TAGGING.value,
                    display_name="new",
                    source_filename="new.flac",
                    source_relative_path="incoming/new.flac",
                ),
            ]
        )
        session.flush()
        session.add(
            LibraryTrack(
                final_relative_path="Missing/Album/01.flac",
                sha256="e" * 64,
                final_sha256="e" * 64,
                normalized_identity=normalized_track_identity(incoming),
                normalized_slot=normalized_track_slot(incoming),
                codec="flac",
                source_job_id=existing_job_id,
            )
        )
        session.add(incoming)
        session.flush()
        result = assess_duplicates(session, [incoming], music_root=settings.music_dir)
        assert result.exact_count == 0
        assert result.inventory_drift_track_ids == [incoming.id]
        assert result.has_conflicts is True


def test_slot_collision_is_reviewed_when_existing_track_lacks_release_mbid(database) -> None:
    existing_job_id = "30000000-0000-0000-0000-000000000008"
    new_job_id = "30000000-0000-0000-0000-000000000009"
    incoming = _track(new_job_id, sha="9" * 64, title="Track")
    incoming.musicbrainz_release_id = "release-with-mbid"
    slot = normalized_track_slot(incoming)
    with database.session() as session:
        session.add_all(
            [
                Job(
                    id=existing_job_id,
                    state=JobState.COMPLETE.value,
                    display_name="old",
                    source_filename="old.flac",
                    source_relative_path="incoming/old.flac",
                ),
                Job(
                    id=new_job_id,
                    state=JobState.TAGGING.value,
                    display_name="new",
                    source_filename="new.flac",
                    source_relative_path="incoming/new.flac",
                ),
            ]
        )
        session.flush()
        session.add(
            LibraryTrack(
                final_relative_path="Artist/Album (2026)/01.flac",
                sha256="8" * 64,
                final_sha256="8" * 64,
                normalized_identity="incoming-without-release-id",
                normalized_slot=slot,
                musicbrainz_release_id=None,
                codec="flac",
                source_job_id=existing_job_id,
            )
        )
        session.add(incoming)
        session.flush()
        result = assess_duplicates(session, [incoming])
        assert result.has_conflicts is True
        assert result.conflicting_track_ids == [incoming.id]


def test_natural_track_order_places_two_before_ten() -> None:
    names = ["Disc 1/10 - Ten.flac", "Disc 1/2 - Two.flac", "Disc 1/1 - One.flac"]
    assert sorted(names, key=_natural_path_key) == [
        "Disc 1/1 - One.flac",
        "Disc 1/2 - Two.flac",
        "Disc 1/10 - Ten.flac",
    ]


def test_release_title_matching_accepts_parenthetical_feature_credit() -> None:
    assert _release_titles_compatible("Saki (feat. Aliyah's Interlude)", "Saki")
    assert _release_titles_compatible("Song [Feat. Guest]", "Song")
    assert not _release_titles_compatible("Saki (feat. Aliyah's Interlude)", "Irony")


def test_release_duration_matching_allows_clear_title_match_with_drift() -> None:
    track = _track("30000000-0000-0000-0000-000000000006", sha="6" * 64, title="Pureflow")
    track.duration_seconds = 176.6
    assert _release_duration_compatible(track, {"title": "Pureflow", "duration": 109.0})
    assert not _release_duration_compatible(track, {"title": "Different Song", "duration": 109.0})


def test_incoming_partial_album_claim_requires_review(settings, database) -> None:
    job_id = "30000000-0000-0000-0000-000000000006"
    (settings.jobs_dir / job_id).mkdir(parents=True)
    with database.session() as session:
        job = Job(
            id=job_id,
            state=JobState.TAGGING.value,
            display_name="partial",
            source_filename="partial.zip",
            source_relative_path="incoming/partial.zip",
        )
        session.add(job)
        session.flush()
        for number in range(1, 9):
            track = _track(job_id, sha=f"{number:064x}", title=f"Track {number}")
            track.source_relative_path = f"extracted/{number}.flac"
            track.track_number = number
            track.track_total = 10
            track.original_tags_json = serialize_hints(
                TagHints(
                    title=f"Track {number}",
                    artist="Artist",
                    album_artist="Artist",
                    album="Album",
                    track_number=number,
                    track_total=10,
                    disc_number=1,
                    disc_total=1,
                )
            )
            session.add(track)
        session.flush()
        with pytest.raises(ReviewRequired, match="complete track total"):
            _metadata_plans(settings, job, None)


def test_incoming_missing_totals_are_inferred_per_disc(settings, database) -> None:
    job_id = "30000000-0000-0000-0000-000000000007"
    extracted = settings.jobs_dir / job_id / "extracted"
    extracted.mkdir(parents=True)
    with database.session() as session:
        job = Job(
            id=job_id,
            state=JobState.TAGGING.value,
            display_name="complete-without-totals",
            source_filename="complete-without-totals.zip",
            source_relative_path="incoming/source.zip",
        )
        session.add(job)
        session.flush()
        for disc_number in (1, 2):
            for track_number in (1, 2):
                filename = f"disc-{disc_number}-track-{track_number}.flac"
                (extracted / filename).write_bytes(b"synthetic")
                track = _track(
                    job_id,
                    sha=f"{disc_number}{track_number}".zfill(64),
                    title=f"Disc {disc_number} Track {track_number}",
                )
                track.source_relative_path = f"extracted/{filename}"
                track.track_number = track_number
                track.track_total = None
                track.disc_number = disc_number
                track.disc_total = 2
                track.original_tags_json = serialize_hints(
                    TagHints(
                        title=track.title,
                        artist="Artist",
                        album_artist="Artist",
                        album="Album",
                        track_number=track_number,
                        track_total=None,
                        disc_number=disc_number,
                        disc_total=2,
                    )
                )
                session.add(track)
        session.flush()

        plans = _metadata_plans(settings, job, None)

    assert len(plans) == 4
    assert {plan.metadata.track_total for plan in plans} == {2}
    assert {plan.metadata.disc_total for plan in plans} == {2}


def test_verified_copy_rejects_source_changed_after_inspection(tmp_path: Path) -> None:
    source = tmp_path / "source.flac"
    destination = tmp_path / "working.flac"
    source.write_bytes(b"changed")
    plan = WorkingPlan(
        track_id=1,
        source_path=source,
        expected_sha256="0" * 64,
        extension=".flac",
        metadata=NormalizedTrackMetadata(
            title="Track",
            artist="Artist",
            album_artist="Artist",
            album="Album",
            track_number=1,
            track_total=1,
        ),
    )
    with pytest.raises(RetryablePipelineError, match="changed before tagging"):
        _copy_verified_source(plan, destination)
    assert not destination.exists()


def test_raw_aac_is_losslessly_wrapped_as_m4a(settings, tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "verified.aac"
    destination = tmp_path / "track.m4a"
    source.write_bytes(b"aac")
    captured: list[str] = []

    def fake_run(command, **kwargs):
        captured.extend(command)
        destination.write_bytes(b"m4a")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("foxden_music.pipeline.subprocess.run", fake_run)
    _remux_raw_aac(settings, source, destination)
    assert destination.read_bytes() == b"m4a"
    assert captured[captured.index("-c:a") + 1] == "copy"
    assert "-map_metadata" in captured


def test_selected_edition_gets_deterministic_qualifier_when_base_path_exists(
    settings,
    database,
) -> None:
    job_id = "30000000-0000-0000-0000-000000000007"
    release_id = "12345678-1234-1234-1234-123456789abc"
    base = settings.music_dir / "Artist" / "Album (2026)"
    base.mkdir(parents=True)
    working = settings.jobs_dir / job_id / "working" / "000001.flac"
    working.parent.mkdir(parents=True)
    working.write_bytes(b"tagged audio")
    digest = sha256_file(working)
    with database.session() as session:
        session.add(
            Job(
                id=job_id,
                state=JobState.ORGANIZING.value,
                display_name="edition",
                source_filename="edition.flac",
                source_relative_path="incoming/edition.flac",
                selected_release_id=release_id,
            )
        )
        session.flush()
        session.add(
            ReleaseCandidate(
                job_id=job_id,
                musicbrainz_release_id=release_id,
                title="Album",
                artist_credit="Artist",
                release_date="2026",
                country="JP",
                status="Official",
                disambiguation="Japanese edition",
                media_summary="1 track",
                track_count=1,
                source_score=100,
                score=100,
                payload_json="{}",
                selected=True,
            )
        )
        track = _track(job_id, sha="f" * 64, title="Track")
        track.working_relative_path = "working/000001.flac"
        track.final_sha256 = digest
        track.musicbrainz_release_id = release_id
        session.add(track)

    prepared, _ = _prepare_library_album(database, settings, job_id, None)
    assert prepared.destination_relative_path.startswith("Artist/Album (2026) [JP Japanese edition 12345678]")


def test_selected_edition_album_identity_comes_from_musicbrainz_candidate(
    settings,
    database,
) -> None:
    job_id = "30000000-0000-0000-0000-000000000010"
    release_id = "22345678-1234-1234-1234-123456789abc"
    first_working = settings.jobs_dir / job_id / "working" / "000001.flac"
    second_working = settings.jobs_dir / job_id / "working" / "000002.flac"
    first_working.parent.mkdir(parents=True)
    first_working.write_bytes(b"tagged track one")
    second_working.write_bytes(b"tagged track two")
    with database.session() as session:
        session.add(
            Job(
                id=job_id,
                state=JobState.ORGANIZING.value,
                display_name="messy tags",
                source_filename="messy.zip",
                source_relative_path="incoming/messy.zip",
                selected_release_id=release_id,
            )
        )
        session.flush()
        session.add(
            ReleaseCandidate(
                job_id=job_id,
                musicbrainz_release_id=release_id,
                title="Correct Album",
                artist_credit="Correct Artist",
                release_date="2025-04-03",
                country="US",
                status="Official",
                disambiguation="",
                media_summary="2 tracks",
                track_count=2,
                source_score=100,
                score=100,
                payload_json="{}",
                selected=True,
            )
        )
        first = _track(job_id, sha="1" * 64, title="Track One")
        second = _track(job_id, sha="2" * 64, title="Track Two")
        first.working_relative_path = "working/000001.flac"
        first.final_sha256 = sha256_file(first_working)
        second.working_relative_path = "working/000002.flac"
        second.final_sha256 = sha256_file(second_working)
        first.album_artist = "Wrong Artist"
        first.album = "Wrong Album"
        first.year = 1970
        second.album_artist = "Other Wrong Artist"
        second.album = "Other Wrong Album"
        second.year = 2026
        session.add_all([first, second])

    prepared, _ = _prepare_library_album(database, settings, job_id, None)
    assert prepared.destination_relative_path == "Correct Artist/Correct Album (2025)"


def test_ambiguous_release_candidates_are_persisted_for_review(
    settings, database, monkeypatch
) -> None:
    job_id = "40000000-0000-0000-0000-000000000001"
    with database.session() as session:
        session.add(
            Job(
                id=job_id,
                state=JobState.MATCHING_METADATA.value,
                display_name="album",
                source_filename="album.flac",
                source_relative_path="incoming/source.flac",
            )
        )
        session.flush()
        session.add(_track(job_id, sha="c" * 64, title="Track"))

    class FakeMatch:
        def __init__(self, release_id: str, score: float):
            self.release_id = release_id
            self.score = score

        def to_model_values(self, selected_job_id: str):
            return {
                "job_id": selected_job_id,
                "musicbrainz_release_id": self.release_id,
                "title": "Album",
                "artist_credit": "Artist",
                "release_date": "2026",
                "country": "US",
                "status": "Official",
                "disambiguation": None,
                "media_summary": "Disc 1: Digital Media, 1 tracks",
                "track_count": 1,
                "source_score": self.score,
                "score": self.score,
                "payload_json": json.dumps({"id": self.release_id, "media": []}),
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def search_releases(self, hints):
            return [
                FakeMatch("00000000-0000-0000-0000-000000000011", 88.0),
                FakeMatch("00000000-0000-0000-0000-000000000012", 86.0),
            ]

    monkeypatch.setattr("foxden_music.pipeline.MusicBrainzClient", FakeClient)
    with pytest.raises(ReviewRequired):
        _choose_release(database, settings, job_id)
    with database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert len(job.candidates) == 2
        assert any("2 release candidate" in event.message for event in job.events)


def test_release_assignment_trusts_musicbrainz_titles_when_positions_and_durations_fit() -> None:
    first = SimpleNamespace(id=1, isrc=None, title="download junk one", duration_seconds=180.0)
    second = SimpleNamespace(id=2, isrc=None, title="download junk two", duration_seconds=201.0)
    originals = {
        1: TagHints(track_number=1, disc_number=1),
        2: TagHints(track_number=2, disc_number=1),
    }
    release_tracks = [
        {
            "disc_number": 1,
            "disc_total": 1,
            "track_number": 1,
            "track_total": 2,
            "title": "Real Intro",
            "artist": "Artist",
            "duration": 181.0,
            "isrc": None,
        },
        {
            "disc_number": 1,
            "disc_total": 1,
            "track_number": 2,
            "track_total": 2,
            "title": "Real Finale",
            "artist": "Artist",
            "duration": 199.5,
            "isrc": None,
        },
    ]

    assigned = _assign_release_tracks([first, second], release_tracks, originals)

    assert [track["title"] for track in assigned] == ["Real Intro", "Real Finale"]
