from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import UUID

import httpx

from .config import Settings


MUSICBRAINZ_RELEASE_INCLUDES = (
    "recordings+artist-credits+release-groups+media+isrcs"
)
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_LUCENE_SPECIAL_CHARACTERS = frozenset(r'+-&|!(){}[]^"~*?:\\/')


class MusicBrainzError(RuntimeError):
    """A safe, application-facing MusicBrainz failure."""


class MusicBrainzConfigurationError(MusicBrainzError):
    """MusicBrainz cannot be used until its required configuration is present."""


class MusicBrainzNotFoundError(MusicBrainzError):
    """A MusicBrainz entity disappeared between search and lookup."""


class JsonCacheHooks(Protocol):
    """Small adapter surface for the app's persistent metadata cache."""

    def get_json(self, namespace: str, key: str) -> Mapping[str, Any] | None: ...

    def put_json(
        self,
        namespace: str,
        key: str,
        payload: Mapping[str, Any],
        ttl: timedelta,
    ) -> None: ...


class RequestLimiter(Protocol):
    def wait(self) -> None: ...


class ProcessRateLimiter:
    """Serialize request starts across every MusicBrainz client in this process."""

    def __init__(
        self,
        min_interval_seconds: float = 1.05,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval_seconds < 1.0:
            raise ValueError("MusicBrainz request interval must be at least one second")
        self.min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait(self) -> None:
        # Holding the lock during the short sleep is intentional: it prevents two
        # worker threads from waking together and exceeding the service limit.
        with self._lock:
            now = self._clock()
            while now < self._next_request_at:
                self._sleeper(self._next_request_at - now)
                now = self._clock()
            self._next_request_at = now + self.min_interval_seconds


# All production clients share this one limiter. Tests can inject a no-wait fake.
DEFAULT_MUSICBRAINZ_LIMITER = ProcessRateLimiter()


@dataclass(frozen=True, slots=True)
class TrackHint:
    title: str | None
    artist: str | None = None
    duration_seconds: float | None = None
    isrc: str | None = None
    disc_number: int | None = None
    track_number: int | None = None


@dataclass(frozen=True, slots=True)
class AlbumHints:
    album: str
    album_artist: str | None = None
    tracks: tuple[TrackHint, ...] = ()
    release_date: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateTrack:
    title: str
    artist_credit: str
    duration_seconds: float | None
    recording_id: str | None
    isrcs: tuple[str, ...]
    disc_number: int
    track_number: int


@dataclass(frozen=True, slots=True)
class ReleaseMatch:
    musicbrainz_release_id: str
    title: str
    artist_credit: str
    release_date: str | None
    country: str | None
    status: str | None
    disambiguation: str | None
    media_summary: str | None
    track_count: int | None
    source_score: float
    score: float
    tracks: tuple[CandidateTrack, ...]
    payload: Mapping[str, Any] = field(repr=False)
    score_components: Mapping[str, float] = field(default_factory=dict)

    def to_model_values(self, job_id: str) -> dict[str, Any]:
        """Return fields accepted by :class:`models.ReleaseCandidate`."""

        return {
            "job_id": job_id,
            "musicbrainz_release_id": self.musicbrainz_release_id,
            "title": self.title,
            "artist_credit": self.artist_credit,
            "release_date": self.release_date,
            "country": self.country,
            "status": self.status,
            "disambiguation": self.disambiguation,
            "media_summary": self.media_summary,
            "track_count": self.track_count,
            "source_score": self.source_score,
            "score": self.score,
            "payload_json": json.dumps(
                self.payload, ensure_ascii=False, separators=(",", ":")
            ),
        }


def album_hints_from_tracks(tracks: Sequence[Any]) -> AlbumHints:
    """Build stable matching input from the app's ORM ``Track`` objects."""

    if not tracks:
        raise ValueError("At least one track is required for release matching")

    def most_common(attribute: str) -> Any:
        values = [
            getattr(track, attribute, None)
            for track in tracks
            if getattr(track, attribute, None) not in (None, "")
        ]
        return Counter(values).most_common(1)[0][0] if values else None

    album = most_common("album")
    if not isinstance(album, str) or not album.strip():
        raise ValueError("An album tag is required for MusicBrainz release search")

    album_artist = most_common("album_artist") or most_common("artist")
    release_date = most_common("release_date")
    if release_date is None:
        year = most_common("year")
        release_date = str(year) if year is not None else None

    indexed_tracks = list(enumerate(tracks))
    indexed_tracks.sort(
        key=lambda pair: (
            getattr(pair[1], "disc_number", None) or 1,
            getattr(pair[1], "track_number", None) or 1_000_000,
            pair[0],
        )
    )
    hints = tuple(
        TrackHint(
            title=getattr(track, "title", None),
            artist=getattr(track, "artist", None),
            duration_seconds=getattr(track, "duration_seconds", None),
            isrc=getattr(track, "isrc", None),
            disc_number=getattr(track, "disc_number", None),
            track_number=getattr(track, "track_number", None),
        )
        for _, track in indexed_tracks
    )
    return AlbumHints(
        album=album.strip(),
        album_artist=str(album_artist).strip() if album_artist else None,
        tracks=hints,
        release_date=str(release_date).strip() if release_date else None,
    )


class MusicBrainzClient:
    """MusicBrainz release search, full lookup, caching, and local scoring."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        cache: JsonCacheHooks | None = None,
        limiter: RequestLimiter | None = None,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        user_agent = settings.musicbrainz_user_agent
        if not user_agent or not _is_meaningful_user_agent(user_agent):
            raise MusicBrainzConfigurationError(
                "MUSICBRAINZ_CONTACT is required and must identify an email address "
                "or website for the Fox Den Music User-Agent"
            )
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")

        self._settings = settings
        self._user_agent = user_agent
        self._cache = cache
        self._limiter = limiter or DEFAULT_MUSICBRAINZ_LIMITER
        self._max_retries = max_retries
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._sleeper = sleeper
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=settings.metadata_http_timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> MusicBrainzClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def search_releases(
        self,
        hints: AlbumHints,
        *,
        limit: int | None = None,
    ) -> list[ReleaseMatch]:
        if not hints.album.strip():
            raise ValueError("Album title cannot be empty")
        result_limit = (
            self._settings.max_release_candidates if limit is None else limit
        )
        if result_limit < 1:
            raise ValueError("Release candidate limit must be positive")
        result_limit = min(result_limit, 100)
        # MusicBrainz's search score is only a retrieval hint. Pull a modestly
        # wider pool so local track evidence can promote the right edition, then
        # retain only the configured number of review candidates.
        search_limit = min(100, result_limit * 2)

        query_parts = [f"release:{_lucene_phrase(hints.album)}"]
        if hints.album_artist:
            query_parts.append(f"artist:{_lucene_phrase(hints.album_artist)}")
        search_payload = self._request_json(
            "release/",
            params={
                "query": " AND ".join(query_parts),
                "fmt": "json",
                "limit": str(search_limit),
            },
            namespace="musicbrainz-search",
        )

        raw_releases = search_payload.get("releases")
        if not isinstance(raw_releases, list):
            return []

        matches: list[ReleaseMatch] = []
        seen_ids: set[str] = set()
        for search_release in raw_releases:
            if not isinstance(search_release, Mapping):
                continue
            raw_id = search_release.get("id")
            if not isinstance(raw_id, str):
                continue
            try:
                release_id = _canonical_mbid(raw_id)
            except ValueError:
                continue
            if release_id in seen_ids:
                continue
            seen_ids.add(release_id)
            try:
                payload = self.lookup_release(release_id)
            except MusicBrainzNotFoundError:
                # Search indexes and entity lookups are not updated atomically.
                continue
            source_score = _as_float(search_release.get("score")) or 0.0
            matches.append(_release_match(payload, hints, source_score))

        matches.sort(key=lambda item: (item.score, item.source_score), reverse=True)
        return matches[:result_limit]

    def lookup_release(self, release_id: str) -> Mapping[str, Any]:
        canonical_id = _canonical_mbid(release_id)
        return self._request_json(
            f"release/{canonical_id}",
            params={"fmt": "json", "inc": MUSICBRAINZ_RELEASE_INCLUDES},
            namespace="musicbrainz-release",
        )

    def _request_json(
        self,
        path: str,
        *,
        params: Mapping[str, str],
        namespace: str,
    ) -> Mapping[str, Any]:
        cache_key = _request_cache_key(path, params)
        cached = self._cache_get(namespace, cache_key)
        if cached is not None:
            return cached

        url = f"{self._settings.musicbrainz_base_url.rstrip('/')}/{path.lstrip('/')}"
        last_transport_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._limiter.wait()
            try:
                response = self._client.get(
                    url,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": self._user_agent,
                    },
                    timeout=self._settings.metadata_http_timeout_seconds,
                    follow_redirects=False,
                )
            except httpx.TransportError as exc:
                last_transport_error = exc
                if attempt >= self._max_retries:
                    break
                self._sleeper(self._backoff_seconds(attempt, None))
                continue

            if response.status_code == 404:
                raise MusicBrainzNotFoundError("MusicBrainz release was not found")
            if response.status_code in _RETRYABLE_STATUS_CODES:
                if attempt >= self._max_retries:
                    raise MusicBrainzError(
                        f"MusicBrainz remained unavailable (HTTP {response.status_code})"
                    )
                self._sleeper(self._backoff_seconds(attempt, response))
                continue
            if response.is_redirect:
                raise MusicBrainzError(
                    "MusicBrainz returned an unexpected redirect; check its base URL"
                )
            if response.is_error:
                raise MusicBrainzError(
                    f"MusicBrainz request failed (HTTP {response.status_code})"
                )
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise MusicBrainzError("MusicBrainz returned invalid JSON") from exc
            if not isinstance(payload, Mapping):
                raise MusicBrainzError("MusicBrainz returned an unexpected response")
            self._cache_put(namespace, cache_key, payload)
            return payload

        raise MusicBrainzError("Could not connect to MusicBrainz") from last_transport_error

    def _backoff_seconds(
        self, attempt: int, response: httpx.Response | None
    ) -> float:
        exponential = min(8.0, self._retry_backoff_seconds * (2**attempt))
        if response is None:
            return exponential
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(30.0, max(exponential, float(retry_after)))
            except ValueError:
                pass
        return exponential

    def _cache_get(self, namespace: str, cache_key: str) -> Mapping[str, Any] | None:
        if self._cache is None:
            return None
        try:
            payload = self._cache.get_json(namespace, cache_key)
        except Exception:
            # Metadata availability should not depend on an optional cache.
            return None
        return payload if isinstance(payload, Mapping) else None

    def _cache_put(
        self, namespace: str, cache_key: str, payload: Mapping[str, Any]
    ) -> None:
        if self._cache is None:
            return
        try:
            self._cache.put_json(
                namespace,
                cache_key,
                payload,
                timedelta(days=self._settings.metadata_cache_days),
            )
        except Exception:
            # A failed cache write must not turn a successful API request into a job failure.
            return


def _is_meaningful_user_agent(user_agent: str) -> bool:
    if not re.fullmatch(r"[^/\s]+/[^\s]+\s+\([^()\s][^()]*\)", user_agent.strip()):
        return False
    contact = user_agent[user_agent.find("(") + 1 : user_agent.rfind(")")].strip()
    return "@" in contact or contact.startswith(("https://", "http://"))


def _lucene_phrase(value: str) -> str:
    escaped = "".join(
        f"\\{character}" if character in _LUCENE_SPECIAL_CHARACTERS else character
        for character in value.strip()
    )
    return f'"{escaped}"'


def _request_cache_key(path: str, params: Mapping[str, str]) -> str:
    canonical = json.dumps(
        {"path": path, "params": sorted(params.items())},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_mbid(value: str) -> str:
    return str(UUID(value))


def _artist_credit(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            artist = item.get("artist")
            name = artist.get("name") if isinstance(artist, Mapping) else None
        if isinstance(name, str):
            parts.append(name)
        join_phrase = item.get("joinphrase")
        if isinstance(join_phrase, str):
            parts.append(join_phrase)
    return "".join(parts).strip()


def _candidate_tracks(
    payload: Mapping[str, Any], *, fallback_artist: str = ""
) -> tuple[CandidateTrack, ...]:
    result: list[CandidateTrack] = []
    media = payload.get("media")
    if not isinstance(media, list):
        return ()
    for medium_index, medium in enumerate(media, start=1):
        if not isinstance(medium, Mapping):
            continue
        disc_number = _as_int(medium.get("position")) or medium_index
        raw_tracks = medium.get("tracks")
        if not isinstance(raw_tracks, list):
            continue
        for track_index, track in enumerate(raw_tracks, start=1):
            if not isinstance(track, Mapping):
                continue
            recording = track.get("recording")
            recording = recording if isinstance(recording, Mapping) else {}
            title = track.get("title") or recording.get("title") or ""
            length_ms = _as_float(track.get("length"))
            if length_ms is None:
                length_ms = _as_float(recording.get("length"))
            raw_isrcs = recording.get("isrcs")
            isrcs = tuple(
                isrc
                for isrc in raw_isrcs
                if isinstance(isrc, str) and isrc.strip()
            ) if isinstance(raw_isrcs, list) else ()
            artist = _artist_credit(track.get("artist-credit"))
            if not artist:
                artist = _artist_credit(recording.get("artist-credit"))
            if not artist:
                artist = fallback_artist
            recording_id = recording.get("id")
            result.append(
                CandidateTrack(
                    title=str(title),
                    artist_credit=artist,
                    duration_seconds=(length_ms / 1000.0) if length_ms is not None else None,
                    recording_id=recording_id if isinstance(recording_id, str) else None,
                    isrcs=isrcs,
                    disc_number=disc_number,
                    track_number=_as_int(track.get("position")) or track_index,
                )
            )
    return tuple(result)


def _release_match(
    payload: Mapping[str, Any], hints: AlbumHints, source_score: float
) -> ReleaseMatch:
    release_id = _canonical_mbid(str(payload.get("id", "")))
    title = str(payload.get("title") or "")
    artist = _artist_credit(payload.get("artist-credit"))
    tracks = _candidate_tracks(payload, fallback_artist=artist)
    score, components = _score_release(hints, title, artist, tracks, payload.get("date"))

    media_summary_parts: list[str] = []
    raw_media = payload.get("media")
    if isinstance(raw_media, list):
        for index, medium in enumerate(raw_media, start=1):
            if not isinstance(medium, Mapping):
                continue
            position = _as_int(medium.get("position")) or index
            medium_format = medium.get("format") or "Unknown format"
            track_count = _as_int(medium.get("track-count"))
            if track_count is None and isinstance(medium.get("tracks"), list):
                track_count = len(medium["tracks"])
            count_label = f", {track_count} tracks" if track_count is not None else ""
            media_summary_parts.append(f"Disc {position}: {medium_format}{count_label}")

    raw_track_count = (
        sum(_medium_track_count(medium) for medium in raw_media)
        if isinstance(raw_media, list)
        else None
    )
    if raw_track_count == 0 and tracks:
        raw_track_count = len(tracks)

    def optional_string(key: str) -> str | None:
        value = payload.get(key)
        return str(value) if value not in (None, "") else None

    return ReleaseMatch(
        musicbrainz_release_id=release_id,
        title=title,
        artist_credit=artist,
        release_date=optional_string("date"),
        country=optional_string("country"),
        status=optional_string("status"),
        disambiguation=optional_string("disambiguation"),
        media_summary="; ".join(media_summary_parts) or None,
        track_count=raw_track_count,
        source_score=source_score,
        score=score,
        tracks=tracks,
        payload=payload,
        score_components=components,
    )


def _score_release(
    hints: AlbumHints,
    candidate_title: str,
    candidate_artist: str,
    candidate_tracks: Sequence[CandidateTrack],
    candidate_date: Any,
) -> tuple[float, dict[str, float]]:
    weighted: list[tuple[str, float, float]] = []
    weighted.append(("album", 20.0, _text_similarity(hints.album, candidate_title)))
    source_artist = hints.album_artist
    if not source_artist:
        track_artists = [track.artist for track in hints.tracks if track.artist]
        source_artist = Counter(track_artists).most_common(1)[0][0] if track_artists else None
    if source_artist:
        weighted.append(
            ("artist", 18.0, _text_similarity(source_artist, candidate_artist))
        )
    has_track_titles = any(track.title for track in hints.tracks)
    has_durations = any(
        track.duration_seconds is not None for track in hints.tracks
    )
    if hints.tracks:
        input_count = len(hints.tracks)
        candidate_count = len(candidate_tracks)
        count_similarity = (
            1.0 - abs(input_count - candidate_count) / max(input_count, candidate_count)
            if candidate_count
            else 0.0
        )
        weighted.append(("track_count", 12.0, max(0.0, count_similarity)))

        source_titles = [
            (index, source.title)
            for index, source in enumerate(hints.tracks)
            if source.title
        ]
        if source_titles:
            title_score = sum(
                _text_similarity(title, candidate_tracks[index].title)
                if index < candidate_count
                else 0.0
                for index, title in source_titles
            ) / len(source_titles)
            weighted.append(("track_titles", 24.0, title_score))

        source_durations = [
            (index, source.duration_seconds)
            for index, source in enumerate(hints.tracks)
            if source.duration_seconds is not None
        ]
        if source_durations:
            duration_score = sum(
                _duration_similarity(
                    float(duration),
                    float(candidate_tracks[index].duration_seconds),
                )
                if index < candidate_count
                and candidate_tracks[index].duration_seconds is not None
                else 0.0
                for index, duration in source_durations
            ) / len(source_durations)
            weighted.append(("durations", 14.0, duration_score))

        source_isrcs = {
            normalized
            for track in hints.tracks
            if track.isrc and (normalized := _normalize_isrc(track.isrc))
        }
        if source_isrcs:
            candidate_isrcs = {
                normalized
                for track in candidate_tracks
                for isrc in track.isrcs
                if (normalized := _normalize_isrc(isrc))
            }
            weighted.append(
                (
                    "isrcs",
                    8.0,
                    len(source_isrcs & candidate_isrcs) / len(source_isrcs),
                )
            )

    if hints.release_date:
        weighted.append(
            (
                "release_date",
                4.0,
                _date_similarity(hints.release_date, candidate_date)
                if isinstance(candidate_date, str) and candidate_date
                else 0.0,
            )
        )

    total_weight = sum(weight for _, weight, _ in weighted)
    raw_score = (
        sum(weight * component for _, weight, component in weighted) / total_weight
        if total_weight
        else 0.0
    )
    evidence_cap = 100.0
    if not hints.tracks:
        evidence_cap = 70.0
    else:
        if not has_track_titles or not has_durations:
            evidence_cap = min(evidence_cap, 90.0)
        if not source_artist:
            evidence_cap = min(evidence_cap, 92.0)
    components = {name: round(value * 100.0, 2) for name, _, value in weighted}
    return round(max(0.0, min(evidence_cap, raw_score * 100.0)), 2), components


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _text_similarity(left: str, right: str) -> float:
    normalized_left = _normalize_text(left)
    normalized_right = _normalize_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _duration_similarity(left: float, right: float) -> float:
    delta = abs(left - right)
    if delta <= 2.0:
        return 1.0
    if delta >= 12.0:
        return 0.0
    return 1.0 - ((delta - 2.0) / 10.0)


def _date_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if left.startswith(right) or right.startswith(left):
        return 0.95
    left_year = re.match(r"^(\d{4})", left)
    right_year = re.match(r"^(\d{4})", right)
    if not left_year or not right_year:
        return 0.0
    difference = abs(int(left_year.group(1)) - int(right_year.group(1)))
    if difference == 0:
        return 0.8
    return 0.25 if difference == 1 else 0.0


def _normalize_isrc(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _medium_track_count(medium: Any) -> int:
    if not isinstance(medium, Mapping):
        return 0
    explicit_count = _as_int(medium.get("track-count"))
    if explicit_count is not None:
        return explicit_count
    tracks = medium.get("tracks")
    return len(tracks) if isinstance(tracks, list) else 0


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
