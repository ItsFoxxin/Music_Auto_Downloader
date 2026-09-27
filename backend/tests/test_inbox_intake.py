from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from foxden_music.config import Settings
from foxden_music.intake import IntakeError, list_download_inbox, stage_inbox_file


def inbox_settings(settings: Settings, inbox: Path, **overrides: object) -> Settings:
    values = settings.model_dump()
    values.update(
        {
            "download_inbox_dir": inbox,
            "download_inbox_settle_seconds": 30,
            "download_inbox_min_bytes": 1,
            "download_inbox_recursive": True,
            "download_inbox_move_files": True,
        }
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_download_inbox_lists_supported_ready_files(settings: Settings, tmp_path: Path) -> None:
    inbox = tmp_path / "downloads"
    nested = inbox / "artist"
    nested.mkdir(parents=True)
    ready = nested / "album.zip"
    ready.write_bytes(b"PK\x03\x04example")
    os.utime(ready, (time.time() - 120, time.time() - 120))
    (inbox / "song.mp3.crdownload").write_bytes(b"partial")
    (inbox / "notes.txt").write_text("ignored", encoding="utf-8")

    configured = inbox_settings(settings, inbox)

    files = list_download_inbox(configured)

    assert [item.relative_path for item in files] == ["artist/album.zip"]
    assert files[0].ready is True
    assert files[0].byte_size == ready.stat().st_size


def test_download_inbox_waits_for_fresh_files(settings: Settings, tmp_path: Path) -> None:
    inbox = tmp_path / "downloads"
    inbox.mkdir()
    fresh = inbox / "album.zip"
    fresh.write_bytes(b"PK\x03\x04example")

    configured = inbox_settings(settings, inbox, download_inbox_settle_seconds=600)

    files = list_download_inbox(configured)

    assert len(files) == 1
    assert files[0].ready is False
    assert files[0].reason == "still downloading"


def test_download_inbox_holds_tiny_files_for_redownload(
    settings: Settings, tmp_path: Path
) -> None:
    inbox = tmp_path / "downloads"
    inbox.mkdir()
    tiny = inbox / "album.zip"
    tiny.write_bytes(b"no")
    os.utime(tiny, (time.time() - 120, time.time() - 120))

    configured = inbox_settings(settings, inbox, download_inbox_min_bytes=1024)

    files = list_download_inbox(configured)

    assert len(files) == 1
    assert files[0].ready is False
    assert "too small" in (files[0].reason or "")


def test_stage_inbox_file_moves_ready_file_into_job_staging(
    settings: Settings, tmp_path: Path
) -> None:
    settings.ensure_directories()
    inbox = tmp_path / "downloads"
    inbox.mkdir()
    source = inbox / "album.zip"
    source.write_bytes(b"PK\x03\x04example")
    os.utime(source, (time.time() - 120, time.time() - 120))
    configured = inbox_settings(settings, inbox)

    job_id, staged_path, display_name = stage_inbox_file("album.zip", configured)

    assert display_name == "album.zip"
    assert not source.exists()
    assert staged_path == configured.jobs_dir / job_id / "incoming" / "source.zip"
    assert staged_path.read_bytes() == b"PK\x03\x04example"


def test_stage_inbox_rejects_path_traversal(settings: Settings, tmp_path: Path) -> None:
    inbox = tmp_path / "downloads"
    inbox.mkdir()
    configured = inbox_settings(settings, inbox)

    with pytest.raises(IntakeError):
        stage_inbox_file("../album.zip", configured)
