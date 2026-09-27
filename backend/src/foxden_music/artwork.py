from __future__ import annotations

import io
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Literal, Protocol
from uuid import UUID

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings


_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ArtworkError(RuntimeError):
    """Downloaded or embedded artwork could not be used safely."""


class ArtworkNotFoundError(ArtworkError):
    """The Cover Art Archive has no front image for this release."""


class BinaryCacheHooks(Protocol):
    """Optional adapter for a file-backed or object-backed artwork cache."""

    def get_bytes(self, namespace: str, key: str) -> bytes | None: ...

    def put_bytes(
        self, namespace: str, key: str, payload: bytes, ttl: timedelta
    ) -> None: ...


EmbeddedArtworkLoader = Callable[[], bytes | None]


@dataclass(frozen=True, slots=True)
class ArtworkResult:
    source: Literal["cover_art_archive", "embedded", "none"]
    jpeg_bytes: bytes | None
    warning: str | None = None

    @property
    def found(self) -> bool:
        return self.jpeg_bytes is not None


class ArtworkService:
    """Fetch safe release front art, then fall back to embedded artwork."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        cache: BinaryCacheHooks | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._settings = settings
        self._cache = cache
        self._max_retries = max_retries
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._sleeper = sleeper
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=settings.metadata_http_timeout_seconds,
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ArtworkService:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def fetch_release_artwork(
        self,
        release_id: str,
        *,
        embedded_loader: EmbeddedArtworkLoader | None = None,
    ) -> ArtworkResult:
        canonical_id = str(UUID(release_id))
        cached = self._cache_get(canonical_id)
        if cached is not None:
            try:
                self._validate_cached_jpeg(cached)
                return ArtworkResult(
                    source="cover_art_archive",
                    jpeg_bytes=cached,
                )
            except ArtworkError:
                # Treat a corrupt optional cache entry as a miss.
                pass

        cover_warning: str | None = None
        try:
            downloaded = self._download_front(canonical_id)
            jpeg_bytes = self._validate_and_encode(downloaded)
        except ArtworkNotFoundError:
            cover_warning = "Cover Art Archive has no front artwork for this release"
        except ArtworkError:
            cover_warning = "Cover Art Archive artwork was unavailable or invalid"
        else:
            self._cache_put(canonical_id, jpeg_bytes)
            return ArtworkResult(
                source="cover_art_archive",
                jpeg_bytes=jpeg_bytes,
            )

        if embedded_loader is not None:
            try:
                embedded = embedded_loader()
                if embedded:
                    jpeg_bytes = self._validate_and_encode(embedded)
                    return ArtworkResult(
                        source="embedded",
                        jpeg_bytes=jpeg_bytes,
                        warning=cover_warning,
                    )
            except Exception:
                # Hook exceptions and parser errors are intentionally converted to
                # a safe diagnostic; a path or tag value must not reach the UI.
                embedded_warning = "Embedded artwork was unavailable or invalid"
                cover_warning = (
                    f"{cover_warning}; {embedded_warning}"
                    if cover_warning
                    else embedded_warning
                )

        return ArtworkResult(source="none", jpeg_bytes=None, warning=cover_warning)

    def _download_front(self, release_id: str) -> bytes:
        url = (
            f"{self._settings.cover_art_base_url.rstrip('/')}"
            f"/release/{release_id}/front"
        )
        last_transport_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with self._client.stream(
                    "GET",
                    url,
                    headers={
                        "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8",
                        "User-Agent": self._settings.musicbrainz_user_agent
                        or "FoxDenMusic/2.3.1",
                    },
                    timeout=self._settings.metadata_http_timeout_seconds,
                    follow_redirects=True,
                ) as response:
                    if response.status_code == 404:
                        raise ArtworkNotFoundError(
                            "Cover Art Archive front artwork was not found"
                        )
                    if response.status_code in _RETRYABLE_STATUS_CODES:
                        if attempt >= self._max_retries:
                            raise ArtworkError(
                                "Cover Art Archive remained unavailable"
                            )
                        self._sleeper(self._backoff_seconds(attempt, response))
                        continue
                    if response.is_error:
                        raise ArtworkError(
                            f"Cover Art Archive request failed (HTTP {response.status_code})"
                        )

                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > self._settings.max_artwork_bytes:
                                raise ArtworkError("Artwork exceeds the configured byte limit")
                        except ValueError:
                            pass

                    chunks: list[bytes] = []
                    downloaded_bytes = 0
                    for chunk in response.iter_bytes():
                        downloaded_bytes += len(chunk)
                        if downloaded_bytes > self._settings.max_artwork_bytes:
                            raise ArtworkError("Artwork exceeds the configured byte limit")
                        chunks.append(chunk)
                    if not chunks:
                        raise ArtworkError("Cover Art Archive returned an empty image")
                    return b"".join(chunks)
            except httpx.TransportError as exc:
                last_transport_error = exc
                if attempt >= self._max_retries:
                    break
                self._sleeper(self._backoff_seconds(attempt, None))

        raise ArtworkError("Could not connect to Cover Art Archive") from last_transport_error

    def _backoff_seconds(
        self, attempt: int, response: httpx.Response | None
    ) -> float:
        exponential = min(8.0, self._retry_backoff_seconds * (2**attempt))
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(30.0, max(exponential, float(retry_after)))
                except ValueError:
                    pass
        return exponential

    def _validate_and_encode(self, raw: bytes) -> bytes:
        if not raw:
            raise ArtworkError("Artwork is empty")
        if len(raw) > self._settings.max_artwork_bytes:
            raise ArtworkError("Artwork exceeds the configured byte limit")

        try:
            with Image.open(io.BytesIO(raw)) as probe:
                width, height = probe.size
                _validate_dimensions(width, height, self._settings.max_artwork_pixels)
                probe.verify()

            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                _validate_dimensions(width, height, self._settings.max_artwork_pixels)
                image.seek(0)
                image = ImageOps.exif_transpose(image)
                rgb_image = _flatten_to_rgb(image)
                rgb_image.load()
                jpeg_bytes = _encode_jpeg_with_limit(
                    rgb_image, self._settings.max_artwork_bytes
                )
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ) as exc:
            raise ArtworkError("Artwork is not a valid supported image") from exc
        return jpeg_bytes

    def _validate_cached_jpeg(self, raw: bytes) -> None:
        if not raw or len(raw) > self._settings.max_artwork_bytes:
            raise ArtworkError("Cached artwork exceeds the configured byte limit")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != "JPEG":
                    raise ArtworkError("Cached artwork is not normalized JPEG")
                width, height = image.size
                _validate_dimensions(
                    width, height, self._settings.max_artwork_pixels
                )
                image.verify()
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ) as exc:
            raise ArtworkError("Cached artwork is invalid") from exc

    def _cache_get(self, release_id: str) -> bytes | None:
        if self._cache is None:
            return None
        try:
            cached = self._cache.get_bytes("cover-art", release_id)
        except Exception:
            return None
        return bytes(cached) if isinstance(cached, (bytes, bytearray)) else None

    def _cache_put(self, release_id: str, payload: bytes) -> None:
        if self._cache is None:
            return
        try:
            self._cache.put_bytes(
                "cover-art",
                release_id,
                payload,
                timedelta(days=self._settings.metadata_cache_days),
            )
        except Exception:
            return


def _validate_dimensions(width: int, height: int, max_pixels: int) -> None:
    if width <= 0 or height <= 0:
        raise ArtworkError("Artwork has invalid dimensions")
    if width * height > max_pixels:
        raise ArtworkError("Artwork exceeds the configured pixel limit")


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return image.convert("RGB")


def _encode_jpeg_with_limit(image: Image.Image, max_bytes: int) -> bytes:
    for quality in (92, 85, 75, 65):
        output = io.BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
        )
        encoded = output.getvalue()
        if len(encoded) <= max_bytes:
            return encoded
    raise ArtworkError("Re-encoded artwork exceeds the configured byte limit")
