from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from foxden_music.acquisition import (
    AcquisitionBatchError,
    AcquisitionError,
    InvalidAcquisitionTransition,
    ProviderConfigurationError,
    SpotifyReferenceError,
    SpotiDownloaderManualProvider,
    add_acquisition_event,
    create_acquisition_batch,
    manual_provider,
    parse_spotify_batch,
    parse_spotify_url,
    sync_acquisition_from_import,
    sync_acquisitions_for_import_job,
    transition_acquisition,
)
from foxden_music.enums import (
    AcquisitionProvider,
    AcquisitionSourceType,
    AcquisitionState,
    JobKind,
    JobState,
    PreferredFormat,
    SourceType,
)
from foxden_music.models import AcquisitionEvent, AcquisitionJob, Job


TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"
ALBUM_ID = "1ATL5GLyefJaxhQzSPVrLX"
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"


@pytest.mark.parametrize(
    ("kind", "identifier", "source_type"),
    [
        ("track", TRACK_ID, AcquisitionSourceType.TRACK),
        ("album", ALBUM_ID, AcquisitionSourceType.ALBUM),
        ("playlist", PLAYLIST_ID, AcquisitionSourceType.PLAYLIST),
    ],
)
def test_spotify_track_album_and_playlist_urls_are_normalized(
    kind: str,
    identifier: str,
    source_type: AcquisitionSourceType,
) -> None:
    reference = parse_spotify_url(
        f"https://open.spotify.com/{kind}/{identifier}/?si=share-token&utm_source=test#ignored"
    )

    assert reference.canonical_url == f"https://open.spotify.com/{kind}/{identifier}"
    assert reference.source_type is source_type
    assert reference.identifier == identifier


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not a url",
        f"http://open.spotify.com/track/{TRACK_ID}",
        f"file://open.spotify.com/track/{TRACK_ID}",
        f"javascript://open.spotify.com/track/{TRACK_ID}",
        f"https://spotify.com/track/{TRACK_ID}",
        f"https://open.spotify.com.example/track/{TRACK_ID}",
        f"https://open.spotify.com@evil.example/track/{TRACK_ID}",
        f"https://evil.example@open.spotify.com/track/{TRACK_ID}",
        f"https://open.spotify.com:443/track/{TRACK_ID}",
        f"https://open.spotify.com/episode/{TRACK_ID}",
        "https://open.spotify.com/track/short",
        f"https://open.spotify.com/track/{TRACK_ID}/extra",
        f"https://open.spotify.com/track%2F{TRACK_ID}",
        f"https://open.spotify.com\\@evil.example/track/{TRACK_ID}",
        f" https://open.spotify.com/track/{TRACK_ID}",
        f"https://open.spotify.com/track/{TRACK_ID}?q=hello world",
        f"https://open.spotify.com/track/{TRACK_ID}\x00",
    ],
)
def test_spotify_url_rejects_malformed_unsupported_and_injection_like_input(value: str) -> None:
    with pytest.raises(SpotifyReferenceError):
        parse_spotify_url(value)


def test_spotify_query_and_fragment_input_is_not_persisted() -> None:
    reference = parse_spotify_url(
        f"https://open.spotify.com/album/{ALBUM_ID}?si=%3Cscript%3Ealert(1)%3C/script%3E"
        "#javascript:alert(2)"
    )

    assert reference.canonical_url == f"https://open.spotify.com/album/{ALBUM_ID}"
    assert "script" not in reference.canonical_url
    assert "javascript" not in reference.canonical_url


def test_spotify_batch_is_ordered_deduplicated_and_bounded() -> None:
    references = parse_spotify_batch(
        "\n".join(
            [
                f"https://open.spotify.com/album/{ALBUM_ID}?si=first",
                f"https://open.spotify.com/track/{TRACK_ID}",
                f"https://open.spotify.com/album/{ALBUM_ID}?si=duplicate",
            ]
        )
    )

    assert [item.identifier for item in references] == [ALBUM_ID, TRACK_ID]
    with pytest.raises(AcquisitionBatchError, match="at most 2"):
        parse_spotify_batch(
            "\n".join(
                [
                    f"https://open.spotify.com/album/{ALBUM_ID}",
                    f"https://open.spotify.com/track/{TRACK_ID}",
                    f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
                ]
            ),
            max_items=2,
        )
    with pytest.raises(AcquisitionBatchError, match="too large"):
        parse_spotify_batch(f"https://open.spotify.com/album/{ALBUM_ID}", max_input_chars=10)
    with pytest.raises(AcquisitionBatchError, match="at least one"):
        parse_spotify_batch(" \n \n")


def test_batch_error_identifies_the_invalid_line() -> None:
    value = "\n".join(
        [
            f"https://open.spotify.com/album/{ALBUM_ID}",
            "https://evil.example/album/not-safe",
        ]
    )

    with pytest.raises(AcquisitionBatchError, match="Line 2"):
        parse_spotify_batch(value)


def test_create_batch_flushes_persistable_jobs_and_events_without_duplicates(database) -> None:
    with database.session() as session:
        acquisitions = create_acquisition_batch(
            session,
            "\n".join(
                [
                    f"https://open.spotify.com/album/{ALBUM_ID}?si=one",
                    f"https://open.spotify.com/track/{TRACK_ID}",
                    f"https://open.spotify.com/album/{ALBUM_ID}?si=two",
                ]
            ),
            preferred_format=PreferredFormat.MP3_320,
        )
        acquisition_ids = [item.id for item in acquisitions]
        assert all(acquisition_ids)
        assert all(item.state == AcquisitionState.WAITING_FOR_USER.value for item in acquisitions)

    with database.session() as session:
        persisted = session.scalars(
            select(AcquisitionJob).order_by(AcquisitionJob.created_at, AcquisitionJob.id)
        ).all()
        assert {item.id for item in persisted} == set(acquisition_ids)
        assert {item.source_identifier for item in persisted} == {ALBUM_ID, TRACK_ID}
        for item in persisted:
            assert item.source_url == (
                f"https://open.spotify.com/{item.source_type.lower()}/{item.source_identifier}"
            )
            assert item.preferred_format == PreferredFormat.MP3_320.value
            assert item.acquisition_relative_directory == f"acquisitions/{item.id}"
            assert [(event.state, event.message) for event in item.events] == [
                (AcquisitionState.QUEUED.value, f"Spotify {item.source_type.lower()} queued"),
                (AcquisitionState.WAITING_FOR_USER.value, "Ready for human-assisted acquisition"),
            ]


def test_invalid_batch_creates_no_partial_database_rows(database) -> None:
    with database.session() as session:
        with pytest.raises(AcquisitionBatchError):
            create_acquisition_batch(
                session,
                "\n".join(
                    [
                        f"https://open.spotify.com/album/{ALBUM_ID}",
                        "https://evil.example/track/not-spotify",
                    ]
                ),
            )
        assert session.query(AcquisitionJob).count() == 0

    with database.session() as session:
        assert session.query(AcquisitionJob).count() == 0
        assert session.query(AcquisitionEvent).count() == 0


@pytest.mark.parametrize("preferred_format", ["MP3", "WAV", "javascript:alert(1)"])
def test_create_batch_rejects_unknown_formats(database, preferred_format: str) -> None:
    with database.session() as session:
        with pytest.raises(AcquisitionBatchError, match="Preferred format"):
            create_acquisition_batch(
                session,
                f"https://open.spotify.com/album/{ALBUM_ID}",
                preferred_format=preferred_format,
            )
        assert session.query(AcquisitionJob).count() == 0


def test_manual_provider_uses_only_static_configured_public_url() -> None:
    acquisition = AcquisitionJob(
        provider=AcquisitionProvider.SPOTIDOWNLOADER_MANUAL.value,
        source_url=f"https://open.spotify.com/album/{ALBUM_ID}",
        source_type=AcquisitionSourceType.ALBUM.value,
        source_identifier=ALBUM_ID,
        display_title="Spotify Album",
        preferred_format=PreferredFormat.FLAC.value,
        acquisition_relative_directory="acquisitions/example",
    )
    provider = manual_provider(
        AcquisitionProvider.SPOTIDOWNLOADER_MANUAL,
        public_url="https://downloads.example:443/manual?mode=music",
    )
    instructions = provider.instructions(acquisition)

    assert isinstance(provider, SpotiDownloaderManualProvider)
    assert instructions.public_url == "https://downloads.example/manual?mode=music"
    assert instructions.source_url == acquisition.source_url
    assert acquisition.source_url not in instructions.public_url
    assert instructions.provider is AcquisitionProvider.SPOTIDOWNLOADER_MANUAL
    assert instructions.preferred_format is PreferredFormat.FLAC
    assert any("CAPTCHA" in step for step in instructions.steps)


@pytest.mark.parametrize(
    "public_url",
    [
        "http://downloads.example/",
        "javascript:alert(1)",
        "https://user:password@downloads.example/",
        "https://downloads.example:8443/",
        "https://downloads.example/#fragment",
        "https://downloads.example/ bad",
    ],
)
def test_manual_provider_rejects_unsafe_static_configuration(public_url: str) -> None:
    with pytest.raises(ProviderConfigurationError):
        manual_provider(AcquisitionProvider.SPOTIDOWNLOADER_MANUAL, public_url=public_url)


def test_acquisition_transitions_and_event_validation_persist(database) -> None:
    with database.session() as session:
        acquisition = create_acquisition_batch(
            session, f"https://open.spotify.com/album/{ALBUM_ID}"
        )[0]
        transition_acquisition(
            session,
            acquisition,
            AcquisitionState.WAITING_FOR_DOWNLOAD,
            "User opened provider",
        )
        transition_acquisition(
            session,
            acquisition,
            AcquisitionState.FILE_RECEIVED,
            "Download received",
        )
        add_acquisition_event(session, acquisition, "x" * 1_100, level="warning")
        acquisition_id = acquisition.id
        assert acquisition.file_received_at is not None

        with pytest.raises(AcquisitionError, match="level"):
            add_acquisition_event(session, acquisition, "bad", level="<script>")
        with pytest.raises(InvalidAcquisitionTransition):
            transition_acquisition(
                session,
                acquisition,
                AcquisitionState.COMPLETE,
                "Importer was skipped",
            )

    with database.session() as session:
        acquisition = session.get(AcquisitionJob, acquisition_id)
        assert acquisition is not None
        assert acquisition.state == AcquisitionState.FILE_RECEIVED.value
        assert acquisition.events[-1].level == "WARNING"
        assert len(acquisition.events[-1].message) == 1000


def _import_job(*, state: JobState, name: str = "album.zip") -> Job:
    return Job(
        kind=JobKind.ALBUM_IMPORT.value,
        source_type=SourceType.INCOMING.value,
        state=state.value,
        display_name=name,
        source_filename=name,
        source_relative_path="incoming/source.zip",
    )


def test_linked_import_state_is_synchronized_through_review_and_completion(database) -> None:
    with database.session() as session:
        acquisition = create_acquisition_batch(
            session, f"https://open.spotify.com/album/{ALBUM_ID}"
        )[0]
        import_job = _import_job(state=JobState.QUEUED)
        session.add(import_job)
        session.flush()
        acquisition.associated_import_job_id = import_job.id
        acquisition_id = acquisition.id
        import_id = import_job.id

        assert sync_acquisition_from_import(session, acquisition) is True
        assert acquisition.state == AcquisitionState.IMPORT_STARTED.value
        assert acquisition.file_received_at is not None

    with database.session() as session:
        acquisition = session.get(AcquisitionJob, acquisition_id)
        import_job = session.get(Job, import_id)
        assert acquisition is not None and import_job is not None
        import_job.state = JobState.NEEDS_REVIEW.value
        assert sync_acquisition_from_import(session, acquisition) is True
        assert acquisition.state == AcquisitionState.NEEDS_REVIEW.value

        import_job.state = JobState.QUEUED.value
        linked = sync_acquisitions_for_import_job(session, import_job)
        assert [item.id for item in linked] == [acquisition.id]
        assert acquisition.state == AcquisitionState.IMPORT_STARTED.value

        import_job.state = JobState.COMPLETE.value
        assert sync_acquisition_from_import(session, acquisition) is True
        assert acquisition.state == AcquisitionState.COMPLETE.value
        assert acquisition.finished_at is not None
        assert sync_acquisition_from_import(session, acquisition) is False


def test_failed_linked_import_records_safe_error_and_can_resume(database) -> None:
    with database.session() as session:
        acquisition = create_acquisition_batch(
            session, f"https://open.spotify.com/track/{TRACK_ID}"
        )[0]
        import_job = _import_job(state=JobState.FAILED, name="track.flac")
        import_job.error_message = "Archive validation failed"
        session.add(import_job)
        session.flush()
        acquisition.associated_import_job_id = import_job.id

        assert sync_acquisition_from_import(session, acquisition) is True
        assert acquisition.state == AcquisitionState.FAILED.value
        assert acquisition.error_message == "Archive validation failed"
        assert acquisition.events[-1].level == "ERROR"

        import_job.state = JobState.QUEUED.value
        assert sync_acquisition_from_import(session, acquisition) is True
        assert acquisition.state == AcquisitionState.IMPORT_STARTED.value
        assert acquisition.error_message is None
        assert acquisition.finished_at is None


def test_unlinked_or_non_import_job_is_not_silently_projected(database) -> None:
    with database.session() as session:
        acquisition = create_acquisition_batch(
            session, f"https://open.spotify.com/playlist/{PLAYLIST_ID}"
        )[0]
        assert sync_acquisition_from_import(session, acquisition) is False

        wrong_job = _import_job(state=JobState.QUEUED)
        wrong_job.kind = JobKind.ACQUISITION.value
        session.add(wrong_job)
        session.flush()
        acquisition.associated_import_job_id = wrong_job.id
        with pytest.raises(AcquisitionError, match="album import"):
            sync_acquisition_from_import(session, acquisition)


def test_acquisition_events_store_plain_json_free_messages(database) -> None:
    """The core stores text only; templates remain responsible for auto-escaping it."""

    hostile = '<script type="application/json">' + json.dumps({"x": "</script>"})
    with database.session() as session:
        acquisition = create_acquisition_batch(
            session, f"https://open.spotify.com/album/{ALBUM_ID}"
        )[0]
        add_acquisition_event(session, acquisition, hostile)
        acquisition_id = acquisition.id

    with database.session() as session:
        acquisition = session.get(AcquisitionJob, acquisition_id)
        assert acquisition is not None
        assert acquisition.events[-1].message == hostile
