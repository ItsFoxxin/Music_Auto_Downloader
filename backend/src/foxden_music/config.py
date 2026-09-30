from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Fox Den Music"
    app_environment: str = "production"
    config_dir: Path = Path("/config")
    staging_dir: Path = Path("/staging")
    music_dir: Path = Path("/music")
    download_inbox_dir: Path | None = None
    download_inbox_settle_seconds: int = 30
    download_inbox_min_bytes: int = 1024 * 1024
    download_inbox_recursive: bool = True
    download_inbox_max_files: int = 200
    download_inbox_move_files: bool = True
    download_inbox_auto_import: bool = True
    remote_browser_url: str | None = None
    database_url: str | None = None

    web_host: str = "0.0.0.0"
    web_port: int = 8000
    web_workers: int = 1
    log_level: str = "INFO"
    csrf_secret: str | None = None

    max_upload_bytes: int = 4 * 1024**3
    upload_chunk_bytes: int = 1024**2
    archive_max_files: int = 2000
    archive_max_total_bytes: int = 8 * 1024**3
    archive_max_entry_bytes: int = 2 * 1024**3
    archive_max_compression_ratio: float = 250.0

    worker_poll_seconds: float = 2.0
    library_scan_interval_seconds: int = 6 * 60 * 60
    storage_snapshot_interval_seconds: int = 15 * 60
    inventory_commit_batch_size: int = 50
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_seconds: float = 90.0
    ffmpeg_path: str = "ffmpeg"
    ffmpeg_timeout_seconds: float = 180.0

    musicbrainz_contact: str | None = None
    musicbrainz_base_url: str = "https://musicbrainz.org/ws/2"
    cover_art_base_url: str = "https://coverartarchive.org"
    metadata_http_timeout_seconds: float = 20.0
    metadata_cache_days: int = 30
    automatic_match_threshold: float = 94.0
    automatic_match_margin: float = 8.0
    max_release_candidates: int = 5
    max_artwork_bytes: int = 25 * 1024**2
    max_artwork_pixels: int = 40_000_000

    jellyfin_url: str | None = None
    jellyfin_api_key: str | None = None
    jellyfin_timeout_seconds: float = 15.0

    spotidownloader_url: str = "https://spotidownloader.com/"
    spotify_batch_max_urls: int = 100
    spotify_batch_max_characters: int = 20_000

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator(
        "musicbrainz_contact",
        "jellyfin_url",
        "download_inbox_dir",
        "remote_browser_url",
        mode="before",
    )
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("spotidownloader_url")
    @classmethod
    def validate_provider_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("SPOTIDOWNLOADER_URL must be a configured public HTTPS URL")
        try:
            if parsed.port not in (None, 443):
                raise ValueError("SPOTIDOWNLOADER_URL must use the default HTTPS port")
        except ValueError as exc:
            raise ValueError("SPOTIDOWNLOADER_URL contains an invalid port") from exc
        return value

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.config_dir / 'foxden-music.db').as_posix()}"

    @property
    def jobs_dir(self) -> Path:
        return self.staging_dir / "jobs"

    @property
    def imports_dir(self) -> Path:
        return self.music_dir / ".imports"

    @property
    def acquisitions_dir(self) -> Path:
        return self.staging_dir / "acquisitions"

    @property
    def musicbrainz_user_agent(self) -> str | None:
        if not self.musicbrainz_contact:
            return None
        return f"FoxDenMusic/2.3.4 ({self.musicbrainz_contact})"

    @property
    def jellyfin_configured(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key)

    def ensure_directories(self) -> None:
        for path in (
            self.config_dir,
            self.staging_dir,
            self.jobs_dir,
            self.acquisitions_dir,
            self.music_dir,
            self.imports_dir,
        ):
            path.mkdir(mode=0o750, parents=True, exist_ok=True)
        ignore_file = self.imports_dir / ".ignore"
        ignore_file.touch(mode=0o640, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
