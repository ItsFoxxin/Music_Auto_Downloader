from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from foxden_music.enums import JellyfinState, JobKind, JobState, SourceType
from foxden_music.models import Job, ReleaseCandidate
from foxden_music.web import create_app


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_dashboard_health_and_bounded_upload(settings, database) -> None:
    app = create_app(settings, database)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["database"] is True

        form = client.get("/imports")
        assert form.status_code == 200
        assert "data-upload-form" in form.text
        assert "ZIP for a full album" in form.text
        assert "data-upload-progress" in form.text
        response = client.post(
            "/imports",
            data={"csrf_token": _csrf(form.text)},
            files={"album": ("한글 Album.zip", b"synthetic zip bytes", "application/zip")},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/jobs/")
        assert response.headers["x-content-type-options"] == "nosniff"

    with database.session() as session:
        job = session.scalar(select(Job))
        assert job is not None
        assert job.state == JobState.QUEUED.value
        assert job.display_name == "한글 Album.zip"
        assert (settings.jobs_dir / job.id / job.source_relative_path).read_bytes() == b"synthetic zip bytes"


def test_upload_requires_valid_csrf(settings, database) -> None:
    app = create_app(settings, database)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/imports",
            data={"csrf_token": "invalid"},
            files={"album": ("album.zip", b"bytes", "application/zip")},
        )
        assert response.status_code == 403
        assert response.headers["content-type"].startswith("text/html")
        assert "Request rejected" in response.text
        assert "Form expired or CSRF validation failed" in response.text
        assert "Fox Den Music" in response.text


def test_oversized_uploads_are_rejected_without_staging_residue(settings, database) -> None:
    app = create_app(settings, database)
    with TestClient(app, raise_server_exceptions=False) as client:
        form = client.get("/imports")
        token = _csrf(form.text)
        within_multipart_limit = client.post(
            "/imports",
            data={"csrf_token": token},
            files={
                "album": (
                    "too-large.zip",
                    b"x" * (settings.max_upload_bytes + 1),
                    "application/zip",
                )
            },
        )
        assert within_multipart_limit.status_code == 400

        token = _csrf(client.get("/imports").text)
        above_request_limit = client.post(
            "/imports",
            data={"csrf_token": token},
            files={
                "album": (
                    "far-too-large.zip",
                    b"x" * (settings.max_upload_bytes + 2 * 1024**2),
                    "application/zip",
                )
            },
        )
        assert above_request_limit.status_code == 413

    with database.session() as session:
        assert session.query(Job).count() == 0
    assert list(settings.jobs_dir.iterdir()) == []


def test_chunked_oversized_upload_returns_413(settings, database) -> None:
    app = create_app(settings, database)
    with TestClient(app, raise_server_exceptions=False) as client:
        token = _csrf(client.get("/imports").text)
        boundary = "fox-den-music-boundary"
        prefix = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
            f"{token}\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="album"; filename="too-large.zip"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()
        body = iter(
            (
                prefix,
                b"x" * (settings.max_upload_bytes + 1024**2 + 1),
                suffix,
            )
        )
        response = client.post(
            "/imports",
            content=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )

        assert "content-length" not in response.request.headers
        assert response.status_code == 413
        assert response.json() == {"detail": "Request body is too large"}
        assert response.headers["x-content-type-options"] == "nosniff"

    with database.session() as session:
        assert session.query(Job).count() == 0
    assert list(settings.jobs_dir.iterdir()) == []


@pytest.mark.parametrize("failing_method", ["flush", "commit"])
def test_database_failure_cleans_staged_upload(
    settings, database, monkeypatch, failing_method: str
) -> None:
    app = create_app(settings, database)
    with TestClient(app, raise_server_exceptions=False) as client:
        token = _csrf(client.get("/imports").text)

        def fail_database_write(*_args, **_kwargs) -> None:
            raise RuntimeError(f"synthetic {failing_method} failure")

        with monkeypatch.context() as patch:
            patch.setattr(Session, failing_method, fail_database_write)
            response = client.post(
                "/imports",
                data={"csrf_token": token},
                files={"album": ("album.zip", b"synthetic bytes", "application/zip")},
            )

        assert response.status_code == 500
        assert "synthetic" not in response.text
        assert "Traceback" not in response.text
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]

    with database.session() as session:
        assert session.query(Job).count() == 0
    assert list(settings.jobs_dir.iterdir()) == []


def test_jellyfin_retry_polls_until_result_is_persisted(settings, database) -> None:
    job_id = "11111111-1111-4111-8111-111111111111"
    with database.session() as session:
        session.add(
            Job(
                id=job_id,
                kind=JobKind.ALBUM_IMPORT.value,
                source_type=SourceType.UPLOAD.value,
                state=JobState.COMPLETE.value,
                display_name="Finished album.zip",
                source_filename="Finished album.zip",
                source_relative_path="incoming/source.zip",
                jellyfin_state=JellyfinState.FAILED.value,
                jellyfin_error="Synthetic Jellyfin outage",
                jellyfin_retryable=True,
            )
        )

    app = create_app(settings, database)
    with TestClient(app) as client:
        initial = client.get(f"/jobs/{job_id}")
        assert 'data-terminal="true"' in initial.text
        assert "Retry refresh" in initial.text

        retry = client.post(
            f"/jobs/{job_id}/retry-jellyfin",
            data={"csrf_token": _csrf(initial.text)},
            follow_redirects=False,
        )
        assert retry.status_code == 303

        pending = client.get(f"/jobs/{job_id}")
        assert 'data-terminal="false"' in pending.text
        assert "Jellyfin refresh retry queued" in pending.text
        pending_api = client.get(f"/api/jobs/{job_id}").json()
        assert pending_api["jellyfin_retryable"] is True
        assert pending_api["jellyfin_retry_requested"] is True

        script = client.get("/static/app.js").text
        assert 'job.state === "COMPLETE" && !job.jellyfin_retry_requested' in script
        assert "data-job-state-badge" in initial.text
        assert 'aria-live="polite"' in initial.text
        assert "setInterval" not in script
        assert "setTimeout(refresh, 2500)" in script
        assert "badge.textContent" in script

        with database.session() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.jellyfin_retry_requested = False
            job.jellyfin_state = JellyfinState.SUCCEEDED.value
            job.jellyfin_retryable = False
            job.jellyfin_error = None

        finished = client.get(f"/jobs/{job_id}")
        assert 'data-terminal="true"' in finished.text
        assert "Jellyfin refresh retry queued" not in finished.text
        finished_api = client.get(f"/api/jobs/{job_id}").json()
        assert finished_api["jellyfin_retryable"] is False
        assert finished_api["jellyfin_retry_requested"] is False


def test_non_retryable_jellyfin_failure_has_no_retry_action(settings, database) -> None:
    job_id = "22222222-2222-4222-8222-222222222222"
    with database.session() as session:
        session.add(
            Job(
                id=job_id,
                kind=JobKind.ALBUM_IMPORT.value,
                source_type=SourceType.UPLOAD.value,
                state=JobState.COMPLETE.value,
                display_name="Imported album.zip",
                source_filename="Imported album.zip",
                source_relative_path="incoming/source.zip",
                jellyfin_state=JellyfinState.FAILED.value,
                jellyfin_error="Jellyfin rejected the configured API key",
                jellyfin_retryable=False,
            )
        )

    app = create_app(settings, database)
    with TestClient(app) as client:
        page = client.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert "Retry refresh" not in page.text
        assert "configuration or authentication problem" in page.text

        rejected = client.post(
            f"/jobs/{job_id}/retry-jellyfin",
            data={"csrf_token": _csrf(client.get("/imports").text)},
        )
        assert rejected.status_code == 409
        assert rejected.headers["content-type"].startswith("text/html")
        assert "This Jellyfin refresh is not retryable" in rejected.text

        payload = client.get(f"/api/jobs/{job_id}").json()
        assert payload["jellyfin_retryable"] is False


def test_metadata_review_shows_safe_release_and_track_evidence(settings, database) -> None:
    job_id = "33333333-3333-4333-8333-333333333333"
    release_id = "12345678-1234-4123-8123-123456789abc"
    candidate_payload = {
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "position": 1,
                        "title": '<img src=x onerror="alert(1)">',
                        "length": 123000,
                    },
                    {
                        "position": 2,
                        "recording": {"title": "Safe second track", "length": 65000},
                    },
                ],
            }
        ]
    }
    with database.session() as session:
        session.add(
            Job(
                id=job_id,
                kind=JobKind.ALBUM_IMPORT.value,
                source_type=SourceType.UPLOAD.value,
                state=JobState.NEEDS_REVIEW.value,
                display_name="Ambiguous album.zip",
                source_filename="Ambiguous album.zip",
                source_relative_path="incoming/source.zip",
                review_kind="METADATA",
                review_reason="Two releases scored similarly.",
            )
        )
        session.add(
            ReleaseCandidate(
                job_id=job_id,
                musicbrainz_release_id=release_id,
                title="Candidate album",
                artist_credit="Candidate artist",
                release_date="2025-02-03",
                country="US",
                status="Official",
                media_summary="Disc 1: CD, 2 tracks",
                track_count=2,
                source_score=87.0,
                score=92.5,
                payload_json=json.dumps(candidate_payload),
            )
        )
        session.add(
            ReleaseCandidate(
                job_id=job_id,
                musicbrainz_release_id="not-a-valid-release-id",
                title="Malformed candidate payload",
                artist_credit="Candidate artist",
                status="<script>alert(2)</script>",
                track_count=1,
                source_score=5.0,
                score=5.0,
                payload_json="{not-json",
            )
        )

    app = create_app(settings, database)
    with TestClient(app) as client:
        page = client.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert "Release status: Official" in page.text
        assert f"https://musicbrainz.org/release/{release_id}" in page.text
        assert f"MBID: <code class=\"mono wrap\">{release_id}</code>" in page.text
        assert "Track evidence" in page.text
        assert "1.1" in page.text and "2:03" in page.text
        assert "Safe second track" in page.text and "1:05" in page.text
        assert '<img src=x onerror="alert(1)">' not in page.text
        assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in page.text
        assert "<script>alert(2)</script>" not in page.text
        assert "&lt;script&gt;alert(2)&lt;/script&gt;" in page.text
        assert "https://musicbrainz.org/release/not-a-valid-release-id" not in page.text

        invalid = client.post(
            f"/jobs/{job_id}/review",
            data={"csrf_token": _csrf(page.text), "selection": "99999"},
        )
        assert invalid.status_code == 400
        assert invalid.headers["content-type"].startswith("text/html")
        assert "Invalid release selection" in invalid.text


def test_page_validation_errors_are_html_but_api_errors_remain_json(settings, database) -> None:
    app = create_app(settings, database)
    with TestClient(app) as client:
        invalid_form = client.post("/imports", data={})
        assert invalid_form.status_code == 422
        assert invalid_form.headers["content-type"].startswith("text/html")
        assert "The submitted form was incomplete or invalid" in invalid_form.text

        missing_api_job = client.get("/api/jobs/does-not-exist")
        assert missing_api_job.status_code == 404
        assert missing_api_job.headers["content-type"].startswith("application/json")
        assert missing_api_job.json() == {"detail": "Job not found"}
