from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Lock, get_ident

from fastapi.testclient import TestClient
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select

from foxden_music.enums import AcquisitionState, JobState, LibraryScanState
from foxden_music.models import (
    AcquisitionArtifact,
    AcquisitionEvent,
    AcquisitionJob,
    Job,
    JobEvent,
    LibraryAlbum,
    LibraryArtist,
    LibraryInventoryTrack,
    LibraryScanRun,
    StorageSnapshot,
)
from foxden_music.web import _cancel_acquisition_before_handoff, create_app


TRACK_URL = "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b"
ALBUM_URL = "https://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX"
PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_stage2_empty_pages_and_public_apis_render(settings, database) -> None:
    app = create_app(settings, database)
    with TestClient(app) as client:
        for path in (
            "/",
            "/add",
            "/acquisitions",
            "/review",
            "/history",
            "/library",
            "/library/artists",
            "/library/albums",
            "/library/health",
            "/partials/dashboard/metrics",
            "/partials/dashboard/active",
            "/partials/dashboard/recent",
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert "Traceback" not in response.text

        health = client.get("/api/health")
        status = client.get("/api/status")
        stats = client.get("/api/stats")
        assert health.status_code == status.status_code == stats.status_code == 200
        assert health.json()["checks"] == {"database": "ok"}
        assert stats.json()["library"]["tracks"] == 0
        assert status.json()["library_scan"]["state"] == "NOT_RUN"
        serialized = health.text + status.text + stats.text
        assert "/config" not in serialized
        assert "/staging" not in serialized
        assert settings.csrf_secret not in serialized


def test_spotify_batch_is_atomic_canonical_and_creates_stage3_directories(settings, database) -> None:
    app = create_app(settings, database)
    with TestClient(app) as client:
        add_page = client.get("/add")
        response = client.post(
            "/acquisitions",
            data={
                "csrf_token": _csrf(add_page.text),
                "preferred_format": "FLAC",
                "source_urls": f"{TRACK_URL}?si=discard-me\n{ALBUM_URL}\n{PLAYLIST_URL}",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/acquisitions?created=3"

        invalid = client.post(
            "/acquisitions",
            data={
                "csrf_token": _csrf(client.get("/add").text),
                "preferred_format": "FLAC",
                "source_urls": f"{TRACK_URL}\nhttps://open.spotify.com.evil/album/1ATL5GLyefJaxhQzSPVrLX",
            },
        )
        assert invalid.status_code == 400
        assert "Line 2" in invalid.text
        assert "Nothing was queued" in invalid.text

        rejected_csrf = client.post(
            "/acquisitions",
            data={
                "csrf_token": "invalid",
                "preferred_format": "FLAC",
                "source_urls": TRACK_URL,
            },
        )
        assert rejected_csrf.status_code == 403

    with database.session() as session:
        acquisitions = session.scalars(
            select(AcquisitionJob).order_by(AcquisitionJob.source_type)
        ).all()
        assert len(acquisitions) == 3
        assert {item.state for item in acquisitions} == {AcquisitionState.WAITING_FOR_USER.value}
        assert all("?" not in item.source_url for item in acquisitions)
        for item in acquisitions:
            assert (settings.acquisitions_dir / item.id / "incoming").is_dir()


def test_acquisition_upload_links_the_stage1_import_and_reuses_intake(settings, database) -> None:
    app = create_app(settings, database)
    with TestClient(app) as client:
        token = _csrf(client.get("/add").text)
        queued = client.post(
            "/acquisitions",
            data={"csrf_token": token, "preferred_format": "FLAC", "source_urls": ALBUM_URL},
            follow_redirects=False,
        )
        assert queued.status_code == 303

        with database.session() as session:
            acquisition_id = session.scalar(select(AcquisitionJob.id))
        assert acquisition_id
        detail = client.get(f"/acquisitions/{acquisition_id}")
        assert detail.status_code == 200
        assert 'target="_blank"' not in detail.text
        assert "Open SpotiDownloader" in detail.text
        assert "data-upload-form" in detail.text
        assert "A loose audio file is treated as a one-track release" in detail.text

        failed_upload = client.post(
            f"/acquisitions/{acquisition_id}/upload",
            data={"csrf_token": _csrf(detail.text)},
            files={"album": ("unsafe.exe", b"not-audio", "application/octet-stream")},
        )
        assert failed_upload.status_code == 400
        with database.session() as session:
            unchanged = session.get(AcquisitionJob, acquisition_id)
            assert unchanged is not None
            assert unchanged.state == AcquisitionState.WAITING_FOR_USER.value
            assert unchanged.associated_import_job_id is None
            assert session.scalar(select(func.count(Job.id))) == 0
            assert session.scalar(select(func.count(AcquisitionArtifact.id))) == 0
        assert list(settings.jobs_dir.iterdir()) == []

        uploaded = client.post(
            f"/acquisitions/{acquisition_id}/upload",
            data={"csrf_token": _csrf(detail.text)},
            files={"album": ("download.zip", b"synthetic-stage2-zip", "application/zip")},
            follow_redirects=False,
        )
        assert uploaded.status_code == 303
        assert uploaded.headers["location"] == f"/acquisitions/{acquisition_id}"
        assert client.get("/api/stats").json()["activity"]["processing"] == 1

        duplicate = client.post(
            f"/acquisitions/{acquisition_id}/upload",
            data={"csrf_token": _csrf(client.get("/add").text)},
            files={"album": ("again.zip", b"bytes", "application/zip")},
        )
        assert duplicate.status_code == 409

    with database.session() as session:
        acquisition = session.get(AcquisitionJob, acquisition_id)
        assert acquisition is not None
        assert acquisition.state == AcquisitionState.IMPORT_STARTED.value
        assert acquisition.associated_import_job_id
        import_job = session.get(Job, acquisition.associated_import_job_id)
        assert import_job is not None

        assert import_job.state == JobState.QUEUED.value
        assert import_job.source_reference == ALBUM_URL
        artifact = session.scalar(
            select(AcquisitionArtifact).where(
                AcquisitionArtifact.acquisition_job_id == acquisition_id
            )
        )
        assert artifact is not None
        assert artifact.received_via == "UPLOAD"
        assert artifact.state == "HANDED_OFF"
        assert artifact.import_job_id == import_job.id
        assert artifact.stored_relative_path.startswith(f"jobs/{import_job.id}/incoming/")
        assert artifact.handed_off_at is not None
        staged = settings.jobs_dir / import_job.id / import_job.source_relative_path
        assert staged.read_bytes() == b"synthetic-stage2-zip"


def test_acquisition_detail_starts_watched_server_download(
    settings, database, tmp_path
) -> None:
    settings.remote_browser_url = "http://10.0.30.20:5800"
    settings.download_inbox_dir = tmp_path / "downloads"
    settings.download_inbox_dir.mkdir()
    app = create_app(settings, database)
    with TestClient(app) as client:
        token = _csrf(client.get("/add").text)
        queued = client.post(
            "/acquisitions",
            data={"csrf_token": token, "preferred_format": "FLAC", "source_urls": ALBUM_URL},
            follow_redirects=False,
        )
        assert queued.status_code == 303
        with database.session() as session:
            acquisition_id = session.scalar(select(AcquisitionJob.id))
        assert acquisition_id

        detail = client.get(f"/acquisitions/{acquisition_id}")
        assert detail.status_code == 200
        assert f'action="/acquisitions/{acquisition_id}/begin-download"' in detail.text
        assert "Copy URL &amp; start watched download" in detail.text
        assert "data-copy-open-form" in detail.text
        assert 'target="_blank"' not in detail.text
        assert "Open SpotiDownloader" not in detail.text

        token = _csrf(detail.text)
        started = client.post(
            f"/acquisitions/{acquisition_id}/begin-download",
            data={"csrf_token": token},
            follow_redirects=False,
        )

        assert started.status_code == 303
        assert started.headers["location"] == "http://10.0.30.20:5800"
    with database.session() as session:
        acquisition = session.get(AcquisitionJob, acquisition_id)
        assert acquisition is not None
        assert acquisition.state == AcquisitionState.WAITING_FOR_DOWNLOAD.value


def test_waiting_acquisition_can_be_cancelled_safely_and_idempotently(
    settings, database
) -> None:
    app = create_app(settings, database)
    with TestClient(app) as client:
        queued = client.post(
            "/acquisitions",
            data={
                "csrf_token": _csrf(client.get("/add").text),
                "preferred_format": "FLAC",
                "source_urls": ALBUM_URL,
            },
            follow_redirects=False,
        )
        assert queued.status_code == 303
        with database.session() as session:
            acquisition_id = session.scalar(select(AcquisitionJob.id))
        assert acquisition_id
        acquisition_root = settings.acquisitions_dir / acquisition_id

        detail = client.get(f"/acquisitions/{acquisition_id}")
        assert detail.status_code == 200
        assert "Cancel request" in detail.text
        rejected = client.post(
            f"/acquisitions/{acquisition_id}/cancel",
            data={"csrf_token": "invalid"},
        )
        assert rejected.status_code == 403
        with database.session() as session:
            unchanged = session.get(AcquisitionJob, acquisition_id)
            assert unchanged is not None
            assert unchanged.state == AcquisitionState.WAITING_FOR_USER.value

        cancelled = client.post(
            f"/acquisitions/{acquisition_id}/cancel",
            data={"csrf_token": _csrf(detail.text)},
            follow_redirects=False,
        )
        assert cancelled.status_code == 303
        assert cancelled.headers["location"] == f"/acquisitions/{acquisition_id}"
        cancelled_detail = client.get(cancelled.headers["location"])
        assert "Request cancelled" in cancelled_detail.text
        assert "Import was not started" in cancelled_detail.text
        assert "Upload and start import" not in cancelled_detail.text
        status_partial = client.get(f"/acquisitions/{acquisition_id}/status")
        assert 'data-poll-terminal="true"' in status_partial.text
        assert "request was cancelled before handoff" in status_partial.text
        assert acquisition_root.is_dir()
        assert (acquisition_root / "incoming").is_dir()

        with database.session() as session:
            persisted = session.get(AcquisitionJob, acquisition_id)
            assert persisted is not None
            assert persisted.state == AcquisitionState.CANCELLED.value
            assert persisted.finished_at is not None
            assert persisted.associated_import_job_id is None
            events_before_retry = session.scalar(
                select(func.count(AcquisitionEvent.id)).where(
                    AcquisitionEvent.acquisition_job_id == acquisition_id
                )
            )
            last_event = session.scalar(
                select(AcquisitionEvent)
                .where(AcquisitionEvent.acquisition_job_id == acquisition_id)
                .order_by(AcquisitionEvent.id.desc())
            )
            assert last_event is not None
            assert last_event.state == AcquisitionState.CANCELLED.value
            assert last_event.message == "Acquisition cancelled by user before import handoff"

        repeated = client.post(
            f"/acquisitions/{acquisition_id}/cancel",
            data={"csrf_token": _csrf(client.get("/add").text)},
            follow_redirects=False,
        )
        assert repeated.status_code == 303
        with database.session() as session:
            assert session.scalar(
                select(func.count(AcquisitionEvent.id)).where(
                    AcquisitionEvent.acquisition_job_id == acquisition_id
                )
            ) == events_before_retry

        assert "1ATL5GLyefJaxhQzSPVrLX" not in client.get(
            "/acquisitions?state=WAITING_FOR_USER"
        ).text
        cancelled_queue = client.get("/acquisitions?state=CANCELLED")
        assert cancelled_queue.status_code == 200
        assert "1ATL5GLyefJaxhQzSPVrLX" in cancelled_queue.text
        history = client.get("/history")
        assert f'/acquisitions/{acquisition_id}' in history.text
        assert "CANCELLED" in history.text


def test_failed_import_can_be_dismissed_without_deleting_history(settings, database) -> None:
    job_id = "12000000-0000-0000-0000-000000000001"
    acquisition_id = "12000000-0000-0000-0000-000000000002"
    processing_id = "12000000-0000-0000-0000-000000000003"
    with database.session() as session:
        failed = Job(
            id=job_id,
            state=JobState.FAILED.value,
            display_name="Failed test album",
            source_filename="failed.zip",
            source_relative_path="incoming/failed.zip",
            retryable=True,
            error_message="Synthetic failure",
        )
        processing = Job(
            id=processing_id,
            state=JobState.STAGING.value,
            display_name="Active test album",
            source_filename="active.zip",
            source_relative_path="incoming/active.zip",
        )
        session.add_all((failed, processing))
        session.flush()
        session.add(
            AcquisitionJob(
                id=acquisition_id,
                provider="SPOTIDOWNLOADER_MANUAL",
                source_url=ALBUM_URL,
                source_type="ALBUM",
                source_identifier="1ATL5GLyefJaxhQzSPVrLX",
                display_title="Linked failed acquisition",
                state=AcquisitionState.FAILED.value,
                preferred_format="FLAC",
                acquisition_relative_directory=f"acquisitions/{acquisition_id}",
                associated_import_job_id=job_id,
            )
        )

    app = create_app(settings, database)
    with TestClient(app) as client:
        page = client.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert "Dismiss failed import" in page.text
        token = _csrf(page.text)

        rejected = client.post(
            f"/jobs/{job_id}/cancel", data={"csrf_token": "invalid"}
        )
        assert rejected.status_code == 403

        dismissed = client.post(
            f"/jobs/{job_id}/cancel",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert dismissed.status_code == 303
        assert dismissed.headers["location"] == f"/jobs/{job_id}"
        cancelled_page = client.get(f"/jobs/{job_id}")
        assert "Import cancelled" in cancelled_page.text
        assert "No music was deleted" in cancelled_page.text

        repeated = client.post(
            f"/jobs/{job_id}/cancel",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert repeated.status_code == 303

        active_rejected = client.post(
            f"/jobs/{processing_id}/cancel", data={"csrf_token": token}
        )
        assert active_rejected.status_code == 409

    with database.session() as session:
        job = session.get(Job, job_id)
        acquisition = session.get(AcquisitionJob, acquisition_id)
        assert job is not None
        assert job.state == JobState.CANCELLED.value
        assert job.retryable is False
        assert job.finished_at is not None
        assert acquisition is not None
        assert acquisition.state == AcquisitionState.CANCELLED.value
        events = session.scalars(
            select(JobEvent).where(JobEvent.job_id == job_id)
        ).all()
        assert [event.message for event in events].count(
            "Import cancelled by user; staged evidence was preserved"
        ) == 1


def test_cancel_rejects_linked_or_other_terminal_acquisitions(settings, database) -> None:
    linked_id = "40000000-0000-0000-0000-000000000001"
    failed_id = "40000000-0000-0000-0000-000000000002"
    import_id = "40000000-0000-0000-0000-000000000003"
    with database.session() as session:
        session.add(
            Job(
                id=import_id,
                state=JobState.QUEUED.value,
                display_name="Already handed off",
                source_filename="linked.zip",
                source_relative_path="incoming/linked.zip",
            )
        )
        session.flush()
        session.add_all(
            [
                AcquisitionJob(
                    id=linked_id,
                    source_url=ALBUM_URL,
                    source_type="ALBUM",
                    source_identifier="1ATL5GLyefJaxhQzSPVrLX",
                    display_title="Linked acquisition",
                    state=AcquisitionState.WAITING_FOR_USER.value,
                    preferred_format="FLAC",
                    associated_import_job_id=import_id,
                    acquisition_relative_directory=f"acquisitions/{linked_id}",
                ),
                AcquisitionJob(
                    id=failed_id,
                    source_url=TRACK_URL,
                    source_type="TRACK",
                    source_identifier="0VjIjW4GlUZAMYd2vXMi3b",
                    display_title="Failed acquisition",
                    state=AcquisitionState.FAILED.value,
                    preferred_format="FLAC",
                    acquisition_relative_directory=f"acquisitions/{failed_id}",
                ),
            ]
        )

    app = create_app(settings, database)
    with TestClient(app) as client:
        token = _csrf(client.get("/add").text)
        linked = client.post(
            f"/acquisitions/{linked_id}/cancel", data={"csrf_token": token}
        )
        assert linked.status_code == 409
        assert "already been handed to the importer" in linked.text
        failed = client.post(
            f"/acquisitions/{failed_id}/cancel", data={"csrf_token": token}
        )
        assert failed.status_code == 409
        assert "still waiting for its download" in failed.text

    with database.session() as session:
        assert session.get(AcquisitionJob, linked_id).state == AcquisitionState.WAITING_FOR_USER.value
        assert session.get(AcquisitionJob, failed_id).state == AcquisitionState.FAILED.value
        assert session.scalar(select(func.count(AcquisitionEvent.id))) == 0


def test_concurrent_cancellations_are_idempotent_without_sqlite_lock_upgrade(
    settings,
    database,
) -> None:
    acquisition_id = "40000000-0000-0000-0000-000000000004"
    with database.session() as session:
        session.add(
            AcquisitionJob(
                id=acquisition_id,
                source_url=ALBUM_URL,
                source_type="ALBUM",
                source_identifier="1ATL5GLyefJaxhQzSPVrLX",
                display_title="Concurrent cancellation",
                state=AcquisitionState.WAITING_FOR_USER.value,
                preferred_format="FLAC",
                acquisition_relative_directory=f"acquisitions/{acquisition_id}",
            )
        )

    acquisition_root = settings.acquisitions_dir / acquisition_id
    incoming = acquisition_root / "incoming"
    incoming.mkdir(parents=True)
    marker = incoming / "keep-me.txt"
    marker.write_text("unchanged", encoding="utf-8")

    initial_reads = Barrier(2)
    observed_threads: set[int] = set()
    observed_lock = Lock()

    def synchronize_initial_acquisition_reads(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if not statement.lstrip().upper().startswith("SELECT") or "FROM acquisition_jobs" not in statement:
            return
        thread_id = get_ident()
        with observed_lock:
            if thread_id in observed_threads:
                return
            observed_threads.add(thread_id)
        initial_reads.wait(timeout=5)

    sqlalchemy_event.listen(
        database.engine,
        "after_cursor_execute",
        synchronize_initial_acquisition_reads,
    )
    try:
        def cancel() -> str:
            with database.session() as session:
                return _cancel_acquisition_before_handoff(session, acquisition_id).state

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [
                future.result(timeout=10)
                for future in (executor.submit(cancel), executor.submit(cancel))
            ]
    finally:
        sqlalchemy_event.remove(
            database.engine,
            "after_cursor_execute",
            synchronize_initial_acquisition_reads,
        )

    assert outcomes == [AcquisitionState.CANCELLED.value] * 2
    assert marker.read_text(encoding="utf-8") == "unchanged"
    with database.session() as session:
        persisted = session.get(AcquisitionJob, acquisition_id)
        assert persisted is not None
        assert persisted.state == AcquisitionState.CANCELLED.value
        assert persisted.associated_import_job_id is None
        assert session.scalar(
            select(func.count(AcquisitionEvent.id)).where(
                AcquisitionEvent.acquisition_job_id == acquisition_id,
                AcquisitionEvent.state == AcquisitionState.CANCELLED.value,
            )
        ) == 1


def test_library_scan_request_inventory_views_and_stats_api(settings, database) -> None:
    with database.session() as session:
        artist = LibraryArtist(
            canonical_name="아티스트 & Friends",
            normalized_name="아티스트 & friends",
            album_count=1,
            track_count=2,
            total_bytes=3_000,
            flac_track_count=1,
        )
        session.add(artist)
        session.flush()
        album = LibraryAlbum(
            artist_id=artist.id,
            album_artist=artist.canonical_name,
            title="<Signals> & Echoes",
            normalized_title="signals & echoes",
            year=2026,
            relative_path="Artist/Signals",
            track_count=2,
            total_bytes=3_000,
            artwork_present=False,
            metadata_complete=False,
            codec_summary="FLAC, MP3",
            scan_generation=1,
        )
        session.add(album)
        session.flush()
        session.add_all(
            [
                LibraryInventoryTrack(
                    album_id=album.id,
                    relative_path="Artist/Signals/01.flac",
                    title="First",
                    artist=artist.canonical_name,
                    album_artist=artist.canonical_name,
                    album_title=album.title,
                    track_number=1,
                    disc_number=1,
                    codec="flac",
                    sample_rate=44_100,
                    bit_depth=16,
                    duration_seconds=180,
                    file_size=2_000,
                    mtime_ns=1,
                    sha256="a" * 64,
                    musicbrainz_recording_id="11111111-1111-4111-8111-111111111111",
                    metadata_complete=True,
                    scan_generation=1,
                ),
                LibraryInventoryTrack(
                    album_id=album.id,
                    relative_path="Artist/Signals/02.mp3",
                    title=None,
                    artist=artist.canonical_name,
                    album_artist=artist.canonical_name,
                    album_title=album.title,
                    track_number=2,
                    disc_number=1,
                    codec="mp3",
                    bitrate=192_000,
                    duration_seconds=170,
                    file_size=1_000,
                    mtime_ns=2,
                    sha256="a" * 64,
                    musicbrainz_recording_id="11111111-1111-4111-8111-111111111111",
                    metadata_complete=False,
                    missing_title=True,
                    scan_generation=1,
                ),
            ]
        )
        session.add(
            StorageSnapshot(
                id=1,
                library_bytes=3_000,
                staging_bytes=1_000,
                music_filesystem_total_bytes=10_000,
                music_filesystem_free_bytes=7_000,
                staging_filesystem_total_bytes=10_000,
                staging_filesystem_free_bytes=9_000,
            )
        )
        artist_id = artist.id
        album_id = album.id

    app = create_app(settings, database)
    with TestClient(app) as client:
        for path, expected in (
            ("/library", "2"),
            ("/library/artists", "아티스트 &amp; Friends"),
            (f"/library/artists/{artist_id}", "&lt;Signals&gt; &amp; Echoes"),
            ("/library/albums", "&lt;Signals&gt; &amp; Echoes"),
            (f"/library/albums/{album_id}", "Untitled track"),
            ("/library/health", "Low bitrate MP3"),
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert expected in response.text
            assert "<Signals>" not in response.text

        stats = client.get("/api/stats")
        assert stats.status_code == 200
        payload = stats.json()
        assert payload["library"]["artists"] == 1
        assert payload["library"]["albums"] == 1
        assert payload["library"]["tracks"] == 2
        assert payload["quality"]["flac"] == 1
        assert payload["quality"]["mp3_below_320"] == 1
        assert payload["health"]["missing_title"] == 1
        assert payload["health"]["duplicate_hash_groups"] == 1
        assert payload["health"]["possible_duplicates"] == 2
        assert payload["health"]["duplicate_recording_groups"] == 1
        assert payload["storage"]["free_bytes"] == 7_000
        assert "relative_path" not in stats.text

        dashboard_metrics = client.get("/partials/dashboard/metrics")
        assert "<strong>1</strong> Duplicate groups" in dashboard_metrics.text
        assert "<strong>2</strong> Duplicate groups" not in dashboard_metrics.text
        health_page = client.get("/library/health")
        assert "Repeated recordings" in health_page.text
        assert "Recording ID 11111111-1111-4111-8111-111111111111" in health_page.text
        assert "2 indexed copies" in health_page.text

        library_page = client.get("/library")
        assert "Refresh library" in library_page.text
        dashboard_page = client.get("/")
        assert 'action="/library/scans"' in dashboard_page.text
        assert "Refresh library" in dashboard_page.text
        scan_request = client.post(
            "/library/scans",
            data={"csrf_token": _csrf(library_page.text)},
            follow_redirects=False,
        )
        assert scan_request.status_code == 303
        assert scan_request.headers["location"].startswith("/library?scan=")
        scan_id = scan_request.headers["location"].split("=", 1)[1]
        scan_status = client.get(f"/library/scans/{scan_id}/status")
        assert scan_status.status_code == 200
        assert "QUEUED" in scan_status.text

    with database.session() as session:
        scan = session.get(LibraryScanRun, scan_id)
        assert scan is not None
        assert scan.state == LibraryScanState.QUEUED.value
        assert session.scalar(select(func.count(LibraryScanRun.id))) == 1


def test_linked_queued_import_overrides_persisted_review_state_and_timestamp(
    settings, database
) -> None:
    acquisition_id = "20000000-0000-0000-0000-000000000001"
    linked_job_id = "20000000-0000-0000-0000-000000000002"
    standalone_job_id = "20000000-0000-0000-0000-000000000003"
    acquisition_updated_at = datetime(2026, 8, 10, 8, 0, 0)
    standalone_updated_at = datetime(2026, 8, 10, 10, 0, 0)
    linked_updated_at = datetime(2026, 8, 10, 12, 0, 0)

    with database.session() as session:
        linked_job = Job(
            id=linked_job_id,
            state=JobState.QUEUED.value,
            display_name="Linked queued import",
            source_filename="linked.zip",
            source_relative_path="incoming/linked.zip",
            created_at=acquisition_updated_at,
            updated_at=linked_updated_at,
        )
        standalone_job = Job(
            id=standalone_job_id,
            state=JobState.QUEUED.value,
            display_name="Standalone queued import",
            source_filename="standalone.zip",
            source_relative_path="incoming/standalone.zip",
            created_at=standalone_updated_at,
            updated_at=standalone_updated_at,
        )
        session.add_all([linked_job, standalone_job])
        session.flush()
        session.add(
            AcquisitionJob(
                id=acquisition_id,
                source_url=ALBUM_URL,
                source_type="ALBUM",
                source_identifier="1ATL5GLyefJaxhQzSPVrLX",
                display_title="Persisted review with queued import",
                state=AcquisitionState.NEEDS_REVIEW.value,
                preferred_format="FLAC",
                associated_import_job_id=linked_job.id,
                acquisition_relative_directory=f"acquisitions/{acquisition_id}",
                created_at=acquisition_updated_at,
                updated_at=acquisition_updated_at,
            )
        )

    app = create_app(settings, database)
    with TestClient(app) as client:
        active = client.get("/partials/dashboard/active")
        assert active.status_code == 200
        assert "Persisted review with queued import" in active.text
        assert "IMPORT STARTED" in active.text
        assert active.text.index("Persisted review with queued import") < active.text.index(
            "Standalone queued import"
        )

        import_started = client.get("/acquisitions?state=IMPORT_STARTED")
        assert import_started.status_code == 200
        assert "Persisted review with queued import" in import_started.text
        assert "IMPORT STARTED" in import_started.text
        needs_review = client.get("/acquisitions?state=NEEDS_REVIEW")
        assert needs_review.status_code == 200
        assert "Persisted review with queued import" not in needs_review.text

        status_payload = client.get("/api/status").json()
        linked_item = next(
            item for item in status_payload["active"] if item["id"] == acquisition_id
        )
        assert linked_item["state"] == AcquisitionState.IMPORT_STARTED.value
        assert linked_item["updated_at"] == "2026-08-10T12:00:00+00:00"
        assert status_payload["active"][0]["id"] == acquisition_id


def test_status_uses_latest_scan_but_last_completed_timestamp_and_utc_offsets(
    settings, database
) -> None:
    completed_at = datetime(2026, 8, 9, 10, 30, 0)
    failed_at = datetime(2026, 8, 10, 11, 5, 0)
    active_at = datetime(2026, 8, 10, 12, 0, 0)
    with database.session() as session:
        session.add_all(
            [
                LibraryScanRun(
                    id="30000000-0000-0000-0000-000000000001",
                    state=LibraryScanState.COMPLETE.value,
                    reason="manual",
                    generation=1,
                    created_at=datetime(2026, 8, 9, 10, 0, 0),
                    started_at=datetime(2026, 8, 9, 10, 1, 0),
                    finished_at=completed_at,
                ),
                LibraryScanRun(
                    id="30000000-0000-0000-0000-000000000002",
                    state=LibraryScanState.FAILED.value,
                    reason="manual",
                    generation=2,
                    created_at=datetime(2026, 8, 10, 11, 0, 0),
                    started_at=datetime(2026, 8, 10, 11, 1, 0),
                    finished_at=failed_at,
                    error_message="Synthetic scan failure",
                ),
                Job(
                    id="30000000-0000-0000-0000-000000000003",
                    state=JobState.QUEUED.value,
                    display_name="Naive timestamp import",
                    source_filename="timestamp.zip",
                    source_relative_path="incoming/timestamp.zip",
                    created_at=active_at,
                    updated_at=active_at,
                ),
                StorageSnapshot(id=1, observed_at=active_at),
            ]
        )

    app = create_app(settings, database)
    with TestClient(app) as client:
        status_payload = client.get("/api/status").json()
        assert status_payload["generated_at"].endswith("+00:00")
        assert status_payload["library_scan"]["state"] == LibraryScanState.FAILED.value
        assert status_payload["library_scan"]["last_completed_at"] == (
            "2026-08-09T10:30:00+00:00"
        )
        active_item = next(
            item
            for item in status_payload["active"]
            if item["id"] == "30000000-0000-0000-0000-000000000003"
        )
        assert active_item["updated_at"] == "2026-08-10T12:00:00+00:00"

        stats_payload = client.get("/api/stats").json()
        assert stats_payload["generated_at"].endswith("+00:00")
        assert stats_payload["storage"]["observed_at"] == "2026-08-10T12:00:00+00:00"

        health_page = client.get("/library/health")
        assert "Latest rescan failed" in health_page.text
        assert "last successful inventory snapshot" in health_page.text


def test_health_page_does_not_treat_failed_initial_scan_as_clean(settings, database) -> None:
    with database.session() as session:
        session.add(
            LibraryScanRun(
                id="30000000-0000-0000-0000-000000000004",
                state=LibraryScanState.FAILED.value,
                reason="initial",
                generation=1,
                finished_at=datetime(2026, 8, 10, 11, 5, 0),
                error_message="Synthetic scan failure",
            )
        )

    app = create_app(settings, database)
    with TestClient(app) as client:
        health_page = client.get("/library/health")

    assert health_page.status_code == 200
    assert "Inventory unavailable" in health_page.text
    assert "latest scan failed" in health_page.text


def test_recent_imports_exclude_completed_exact_duplicate_skips(settings, database) -> None:
    with database.session() as session:
        session.add_all(
            [
                Job(
                    id="40000000-0000-0000-0000-000000000001",
                    state=JobState.COMPLETE.value,
                    display_name="Actually published album",
                    source_filename="published.zip",
                    source_relative_path="incoming/published.zip",
                    track_count=1,
                    imported_count=1,
                    finished_at=datetime(2026, 8, 10, 10, 0, 0),
                    created_at=datetime(2026, 8, 10, 9, 0, 0),
                    updated_at=datetime(2026, 8, 10, 10, 0, 0),
                ),
                Job(
                    id="40000000-0000-0000-0000-000000000002",
                    state=JobState.COMPLETE.value,
                    display_name="Exact duplicate skipped album",
                    source_filename="duplicate.zip",
                    source_relative_path="incoming/duplicate.zip",
                    track_count=1,
                    imported_count=0,
                    skipped_count=1,
                    finished_at=datetime(2026, 8, 10, 11, 0, 0),
                    created_at=datetime(2026, 8, 10, 10, 30, 0),
                    updated_at=datetime(2026, 8, 10, 11, 0, 0),
                ),
            ]
        )

    app = create_app(settings, database)
    with TestClient(app) as client:
        recent = client.get("/partials/dashboard/recent")
        assert recent.status_code == 200
        assert "Actually published album" in recent.text
        assert "Exact duplicate skipped album" not in recent.text
