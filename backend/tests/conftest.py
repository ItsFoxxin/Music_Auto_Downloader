from __future__ import annotations

from pathlib import Path

import pytest

from foxden_music.config import Settings
from foxden_music.database import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config = tmp_path / "config"
    staging = tmp_path / "staging"
    music = tmp_path / "music"
    return Settings(
        _env_file=None,
        app_environment="test",
        config_dir=config,
        staging_dir=staging,
        music_dir=music,
        database_url=f"sqlite:///{(config / 'test.db').as_posix()}",
        csrf_secret="test-secret-that-is-deliberately-longer-than-thirty-two-characters",
        max_upload_bytes=2 * 1024 * 1024,
        archive_max_total_bytes=4 * 1024 * 1024,
        archive_max_entry_bytes=2 * 1024 * 1024,
        worker_poll_seconds=0.01,
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    database = Database(settings)
    database.initialize()
    return database

