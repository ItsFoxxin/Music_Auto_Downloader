from __future__ import annotations

import io
from datetime import timedelta
from typing import Any, Mapping

import httpx
import pytest
from PIL import Image

from foxden_music.artwork import ArtworkService
from foxden_music.config import Settings
from foxden_music.enums import JellyfinState
from foxden_music.jellyfin import refresh_jellyfin_library
from foxden_music.musicbrainz import (
    DEFAULT_MUSICBRAINZ_LIMITER,
    AlbumHints,
    MusicBrainzClient,
    MusicBrainzConfigurationError,
    ProcessRateLimiter,
    TrackHint,
)


RELEASE_ID_MATCH = "11111111-1111-4111-8111-111111111111"
RELEASE_ID_WRONG = "22222222-2222-4222-8222-222222222222"


class NoWaitLimiter:
    def __init__(self) -> None:
        self.calls = 0

    def wait(self) -> None:
        self.calls += 1


class MemoryJsonCache:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Mapping[str, Any]] = {}
        self.put_ttls: list[timedelta] = []

    def get_json(self, namespace: str, key: str) -> Mapping[str, Any] | None:
        return self.values.get((namespace, key))

    def put_json(
        self,
        namespace: str,
        key: str,
        payload: Mapping[str, Any],
        ttl: timedelta,
    ) -> None:
        self.values[(namespace, key)] = payload
        self.put_ttls.append(ttl)


class MemoryBinaryCache:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], bytes] = {}

    def get_bytes(self, namespace: str, key: str) -> bytes | None:
        return self.values.get((namespace, key))

    def put_bytes(
        self, namespace: str, key: str, payload: bytes, ttl: timedelta
    ) -> None:
        assert ttl.days == 30
        self.values[(namespace, key)] = payload


def integration_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "musicbrainz_contact": "maintainer@example.test",
        "musicbrainz_base_url": "https://musicbrainz.test/ws/2",
        "cover_art_base_url": "https://coverart.test",
        "metadata_http_timeout_seconds": 2.0,
        "metadata_cache_days": 30,
        "max_release_candidates": 2,
        "max_artwork_bytes": 10_000,
        "max_artwork_pixels": 10_000,
    }
    values.update(overrides)
    return Settings(**values)


def artist_credit(name: str) -> list[dict[str, Any]]:
    return [{"name": name, "artist": {"name": name}, "joinphrase": ""}]


def full_release(
    release_id: str,
    *,
    title: str,
    artist: str,
    date: str,
    tracks: list[tuple[str, int, str]],
) -> dict[str, Any]:
    return {
        "id": release_id,
        "title": title,
        "artist-credit": artist_credit(artist),
        "date": date,
        "country": "US",
        "status": "Official",
        "disambiguation": "",
        "release-group": {"id": "33333333-3333-4333-8333-333333333333"},
        "media": [
            {
                "position": 1,
                "format": "Digital Media",
                "track-count": len(tracks),
                "tracks": [
                    {
                        "position": index,
                        "title": track_title,
                        "length": duration_ms,
                        "artist-credit": artist_credit(artist),
                        "recording": {
                            "id": f"44444444-4444-4444-8444-44444444444{index}",
                            "title": track_title,
                            "length": duration_ms,
                            "artist-credit": artist_credit(artist),
                            "isrcs": [isrc],
                        },
                    }
                    for index, (track_title, duration_ms, isrc) in enumerate(
                        tracks, start=1
                    )
                ],
            }
        ],
    }


def test_musicbrainz_search_looks_up_full_releases_scores_locally_and_caches() -> None:
    matching = full_release(
        RELEASE_ID_MATCH,
        title="Night / Day",
        artist="Fox & Hound",
        date="2024-03-01",
        tracks=[
            ("Den Lights", 180_000, "USABC2400001"),
            ("Morning Run", 201_000, "USABC2400002"),
        ],
    )
    wrong = full_release(
        RELEASE_ID_WRONG,
        title="Night and Day (Deluxe)",
        artist="Someone Else",
        date="1998",
        tracks=[("Unrelated", 320_000, "GBXYZ9800001")],
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["User-Agent"] == (
            "FoxDenMusic/2.3.0 (maintainer@example.test)"
        )
        if request.url.path == "/ws/2/release/":
            assert request.url.params["fmt"] == "json"
            assert request.url.params["limit"] == "4"
            assert request.url.params["query"] == (
                'release:"Night \\/ Day" AND artist:"Fox \\& Hound"'
            )
            return httpx.Response(
                200,
                json={
                    "releases": [
                        {"id": RELEASE_ID_WRONG, "score": "100"},
                        {"id": RELEASE_ID_MATCH, "score": "80"},
                    ]
                },
            )
        assert request.url.params["inc"] == (
            "recordings+artist-credits+release-groups+media+isrcs"
        )
        if request.url.path.endswith(RELEASE_ID_MATCH):
            return httpx.Response(200, json=matching)
        if request.url.path.endswith(RELEASE_ID_WRONG):
            return httpx.Response(200, json=wrong)
        raise AssertionError(f"Unexpected request: {request.url}")

    cache = MemoryJsonCache()
    limiter = NoWaitLimiter()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = MusicBrainzClient(
        integration_settings(),
        client=http_client,
        cache=cache,
        limiter=limiter,
    )
    hints = AlbumHints(
        album="Night / Day",
        album_artist="Fox & Hound",
        release_date="2024-03-01",
        tracks=(
            TrackHint("Den Lights", duration_seconds=180.4, isrc="US-ABC-24-00001"),
            TrackHint("Morning Run", duration_seconds=200.2, isrc="USABC2400002"),
        ),
    )

    matches = client.search_releases(hints)

    assert [match.musicbrainz_release_id for match in matches] == [
        RELEASE_ID_MATCH,
        RELEASE_ID_WRONG,
    ]
    assert matches[0].score == 100.0
    assert matches[0].source_score == 80.0
    assert set(matches[0].score_components) == {
        "album",
        "artist",
        "track_count",
        "track_titles",
        "durations",
        "isrcs",
        "release_date",
    }
    assert matches[0].tracks[0].recording_id is not None
    assert matches[0].to_model_values("job-id")["payload_json"].startswith("{")
    assert len(requests) == 3
    assert limiter.calls == 3
    assert all(ttl.days == 30 for ttl in cache.put_ttls)

    # Search and both full release responses are returned from the cache.
    cached_matches = client.search_releases(hints)
    assert cached_matches == matches
    assert len(requests) == 3
    assert limiter.calls == 3


def test_musicbrainz_search_falls_back_when_artist_query_returns_no_releases() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/ws/2/release/":
            query = request.url.params["query"]
            if "artist:" in query:
                return httpx.Response(200, json={"releases": []})
            return httpx.Response(
                200,
                json={"releases": [{"id": RELEASE_ID_MATCH, "score": 74}]},
            )
        if request.url.path.endswith(RELEASE_ID_MATCH):
            return httpx.Response(
                200,
                json=full_release(
                    RELEASE_ID_MATCH,
                    title="Pureflow, Pt. 1",
                    artist="Fox Den",
                    date="2026-09-27",
                    tracks=[
                        ("Intro", 180_000, "USABC2600001"),
                        ("Run", 201_000, "USABC2600002"),
                    ],
                ),
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = MusicBrainzClient(
        integration_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        limiter=NoWaitLimiter(),
    )

    matches = client.search_releases(
        AlbumHints(
            album="Pureflow, Pt. 1",
            album_artist="Wrong Download Artist",
            tracks=(
                TrackHint("Intro", duration_seconds=180.0),
                TrackHint("Run", duration_seconds=201.0),
            ),
        )
    )

    assert [match.musicbrainz_release_id for match in matches] == [RELEASE_ID_MATCH]
    assert "artist:" in requests[0].url.params["query"]
    assert "artist:" not in requests[1].url.params["query"]


def test_musicbrainz_retries_retryable_status_without_skipping_limiter() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"Retry-After": "0.5"})
        return httpx.Response(200, json={"releases": []})

    limiter = NoWaitLimiter()
    client = MusicBrainzClient(
        integration_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        limiter=limiter,
        max_retries=1,
        sleeper=sleeps.append,
    )

    assert client.search_releases(AlbumHints(album="Album")) == []
    assert attempts == 2
    assert limiter.calls == 2
    assert sleeps == [0.5]


def test_musicbrainz_requires_a_meaningful_contact_and_global_interval() -> None:
    with pytest.raises(MusicBrainzConfigurationError):
        MusicBrainzClient(
            integration_settings(musicbrainz_contact=None),
            client=httpx.Client(transport=httpx.MockTransport(lambda _: None)),
        )

    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = ProcessRateLimiter(
        min_interval_seconds=1.0,
        clock=lambda: now[0],
        sleeper=sleep,
    )
    limiter.wait()
    limiter.wait()
    assert sleeps == [1.0]
    assert DEFAULT_MUSICBRAINZ_LIMITER.min_interval_seconds >= 1.0


def png_bytes(size: tuple[int, int] = (12, 12)) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", size, (18, 52, 86, 128)).save(output, format="PNG")
    return output.getvalue()


def test_artwork_follows_redirect_validates_reencodes_and_caches() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "coverart.test":
            return httpx.Response(
                307,
                headers={"Location": "https://archive.test/front.png"},
            )
        return httpx.Response(200, content=png_bytes())

    cache = MemoryBinaryCache()
    service = ArtworkService(
        integration_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        cache=cache,
    )

    result = service.fetch_release_artwork(RELEASE_ID_MATCH)

    assert result.source == "cover_art_archive"
    assert result.warning is None
    assert result.jpeg_bytes is not None
    assert result.jpeg_bytes.startswith(b"\xff\xd8")
    with Image.open(io.BytesIO(result.jpeg_bytes)) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert image.size == (12, 12)
    assert len(calls) == 2

    second = service.fetch_release_artwork(RELEASE_ID_MATCH)
    assert second.source == "cover_art_archive"
    assert second.jpeg_bytes == result.jpeg_bytes
    assert len(calls) == 2


def test_artwork_enforces_byte_limit_then_uses_embedded_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "5000"},
            content=b"not-read",
        )

    service = ArtworkService(
        integration_settings(max_artwork_bytes=1_000),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = service.fetch_release_artwork(
        RELEASE_ID_MATCH,
        embedded_loader=lambda: png_bytes((5, 5)),
    )

    assert result.source == "embedded"
    assert result.jpeg_bytes is not None
    assert result.warning == "Cover Art Archive artwork was unavailable or invalid"


def test_artwork_enforces_pixel_limit_then_uses_embedded_fallback() -> None:
    service = ArtworkService(
        integration_settings(max_artwork_pixels=100),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=png_bytes((20, 20)))
            )
        ),
    )
    result = service.fetch_release_artwork(
        RELEASE_ID_MATCH,
        embedded_loader=lambda: png_bytes((5, 5)),
    )

    assert result.source == "embedded"
    assert result.jpeg_bytes is not None


def test_jellyfin_refresh_uses_mediabrowser_header_and_preserves_base_path() -> None:
    secret = "super-secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://jellyfin.test/jellyfin/Library/Refresh"
        assert request.headers["Authorization"] == (
            f'MediaBrowser Token="{secret}"'
        )
        assert request.content == b""
        return httpx.Response(204)

    result = refresh_jellyfin_library(
        integration_settings(
            jellyfin_url="http://jellyfin.test/jellyfin/",
            jellyfin_api_key=secret,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.state is JellyfinState.SUCCEEDED
    assert result.status_code == 204
    assert secret not in repr(result)


def test_jellyfin_failure_is_separate_retryable_and_never_leaks_key() -> None:
    secret = "key-that-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed while using {secret}", request=request)

    result = refresh_jellyfin_library(
        integration_settings(
            jellyfin_url="https://jellyfin.test",
            jellyfin_api_key=secret,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.state is JellyfinState.FAILED
    assert result.retryable is True
    assert result.error == "Could not connect to Jellyfin"
    assert secret not in repr(result)


def test_jellyfin_unconfigured_returns_without_network() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    result = refresh_jellyfin_library(
        integration_settings(jellyfin_url=None, jellyfin_api_key=None),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.state is JellyfinState.NOT_CONFIGURED
    assert called is False


def test_jellyfin_invalid_port_is_a_safe_separate_failure() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(204)

    result = refresh_jellyfin_library(
        integration_settings(
            jellyfin_url="http://jellyfin.test:not-a-port",
            jellyfin_api_key="secret-key",
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.state is JellyfinState.FAILED
    assert result.retryable is False
    assert result.error == "Jellyfin URL contains an invalid port"
    assert called is False
