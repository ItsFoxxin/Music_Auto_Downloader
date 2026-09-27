from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings
from .enums import JellyfinState


@dataclass(frozen=True, slots=True)
class JellyfinRefreshResult:
    """Independent outcome for the best-effort post-import library refresh."""

    state: JellyfinState
    status_code: int | None = None
    error: str | None = None
    retryable: bool = False

    @property
    def succeeded(self) -> bool:
        return self.state is JellyfinState.SUCCEEDED


class JellyfinClient:
    """Minimal Jellyfin API client that never stores credentials in results."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=settings.jellyfin_timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> JellyfinClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def refresh_library(self) -> JellyfinRefreshResult:
        if not self._settings.jellyfin_configured:
            return JellyfinRefreshResult(state=JellyfinState.NOT_CONFIGURED)

        assert self._settings.jellyfin_url is not None
        assert self._settings.jellyfin_api_key is not None
        try:
            refresh_url = _refresh_url(self._settings.jellyfin_url)
            authorization = _authorization_header(self._settings.jellyfin_api_key)
        except ValueError as exc:
            return JellyfinRefreshResult(
                state=JellyfinState.FAILED,
                error=str(exc),
                retryable=False,
            )

        try:
            response = self._client.post(
                refresh_url,
                content=b"",
                headers={
                    "Accept": "application/json",
                    "Authorization": authorization,
                },
                timeout=self._settings.jellyfin_timeout_seconds,
                follow_redirects=False,
            )
        except (httpx.InvalidURL, httpx.TransportError):
            return JellyfinRefreshResult(
                state=JellyfinState.FAILED,
                error="Could not connect to Jellyfin",
                retryable=True,
            )

        if 200 <= response.status_code < 300:
            return JellyfinRefreshResult(
                state=JellyfinState.SUCCEEDED,
                status_code=response.status_code,
            )

        retryable = response.status_code in {408, 425, 429} or response.status_code >= 500
        return JellyfinRefreshResult(
            state=JellyfinState.FAILED,
            status_code=response.status_code,
            error=f"Jellyfin library refresh failed (HTTP {response.status_code})",
            retryable=retryable,
        )


def refresh_jellyfin_library(
    settings: Settings, *, client: httpx.Client | None = None
) -> JellyfinRefreshResult:
    """Convenience wrapper for callers that do not need a persistent client."""

    jellyfin = JellyfinClient(settings, client=client)
    try:
        return jellyfin.refresh_library()
    finally:
        jellyfin.close()


def _refresh_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Jellyfin URL must be an absolute HTTP or HTTPS URL")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Jellyfin URL contains an invalid port") from exc
    if parsed.username or parsed.password:
        raise ValueError("Jellyfin URL must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Jellyfin URL must not contain a query string or fragment")
    path = f"{parsed.path.rstrip('/')}/Library/Refresh"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _authorization_header(api_key: str) -> str:
    if not api_key or any(character in api_key for character in ('"', "\r", "\n")):
        raise ValueError("Jellyfin API key is invalid")
    return f'MediaBrowser Token="{api_key}"'
