from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

import foxden_music.library as library
from foxden_music.audio import sha256_file
from foxden_music.library import (
    ImportLock,
    LibraryConflictError,
    LibraryImportError,
    PreparedAlbum,
    PreparedTrack,
    commit_album,
    prepare_album,
)


def _prepared_track(tmp_path: Path, content: bytes = b"synthetic-audio") -> PreparedTrack:
    source = tmp_path / "working.flac"
    source.write_bytes(content)
    digest = sha256_file(source)
    return PreparedTrack(
        source_path=source,
        relative_path="01 - 신메뉴.flac",
        source_sha256=digest,
        final_sha256=digest,
    )


def test_atomic_album_import_publishes_complete_directory(settings, tmp_path: Path) -> None:
    settings.ensure_directories()
    prepared = prepare_album(
        music_root=settings.music_dir,
        job_id="00000000-0000-0000-0000-000000000001",
        destination_relative_path="Stray Kids/GO生 (2020)",
        tracks=[_prepared_track(tmp_path)],
        cover_jpeg=None,
    )
    destination, reconciled = commit_album(music_root=settings.music_dir, album=prepared)
    assert reconciled is False
    assert destination == settings.music_dir / "Stray Kids" / "GO生 (2020)"
    assert (destination / "01 - 신메뉴.flac").read_bytes() == b"synthetic-audio"
    assert (destination / ".foxden-import.json").is_file()
    manifest = json.loads((destination / ".foxden-import.json").read_bytes())
    assert manifest["schema"] == 2
    assert manifest["cover"] is None
    assert not prepared.album_root.exists()


def test_existing_destination_is_never_overwritten(settings, tmp_path: Path) -> None:
    settings.ensure_directories()
    existing = settings.music_dir / "Artist" / "Album"
    existing.mkdir(parents=True)
    canary = existing / "keep.txt"
    canary.write_text("do not replace", encoding="utf-8")
    prepared = prepare_album(
        music_root=settings.music_dir,
        job_id="00000000-0000-0000-0000-000000000002",
        destination_relative_path="Artist/Album",
        tracks=[_prepared_track(tmp_path)],
        cover_jpeg=None,
    )
    with pytest.raises(LibraryConflictError):
        commit_album(music_root=settings.music_dir, album=prepared)
    assert canary.read_text(encoding="utf-8") == "do not replace"


def test_linux_unsupported_renameat2_uses_guarded_atomic_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "published"
    source.mkdir()
    (source / "track.flac").write_bytes(b"audio")
    monkeypatch.setattr(library.sys, "platform", "linux")
    monkeypatch.setattr(library, "_linux_renameat2_noreplace", lambda *_args: errno.EINVAL)

    library._rename_noreplace(source, destination)

    assert not source.exists()
    assert (destination / "track.flac").read_bytes() == b"audio"


def test_linux_guarded_rename_refuses_an_existing_empty_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "published"
    source.mkdir()
    destination.mkdir()
    monkeypatch.setattr(library.sys, "platform", "linux")
    monkeypatch.setattr(library, "_linux_renameat2_noreplace", lambda *_args: errno.EOPNOTSUPP)

    with pytest.raises(LibraryConflictError, match="already exists"):
        library._rename_noreplace(source, destination)

    assert source.is_dir()
    assert destination.is_dir()


def test_linux_renameat2_real_failure_does_not_use_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "published"
    source.mkdir()
    monkeypatch.setattr(library.sys, "platform", "linux")
    monkeypatch.setattr(library, "_linux_renameat2_noreplace", lambda *_args: errno.EIO)

    with pytest.raises(LibraryImportError, match="errno"):
        library._rename_noreplace(source, destination)

    assert source.is_dir()
    assert not destination.exists()


def test_failed_preparation_does_not_modify_live_library(settings, tmp_path: Path) -> None:
    settings.ensure_directories()
    source = tmp_path / "bad.flac"
    source.write_bytes(b"synthetic")
    track = PreparedTrack(
        source_path=source,
        relative_path="01 - Track.flac",
        source_sha256="0" * 64,
        final_sha256="0" * 64,
    )
    with pytest.raises(LibraryImportError):
        prepare_album(
            music_root=settings.music_dir,
            job_id="00000000-0000-0000-0000-000000000003",
            destination_relative_path="Artist/Album",
            tracks=[track],
            cover_jpeg=None,
        )
    assert not (settings.music_dir / "Artist" / "Album").exists()


def test_preparation_tolerates_unsupported_chmod_on_regular_files(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.ensure_directories()

    def chmod_unsupported(_path: Path, _mode: int) -> None:
        raise PermissionError(errno.EPERM, "NTFS does not implement POSIX modes")

    monkeypatch.setattr(library.os, "chmod", chmod_unsupported)
    prepared = prepare_album(
        music_root=settings.music_dir,
        job_id="00000000-0000-0000-0000-000000000008",
        destination_relative_path="Artist/NTFS Album",
        tracks=[_prepared_track(tmp_path)],
        cover_jpeg=b"synthetic-cover",
    )
    library.verify_prepared_album(prepared)
    assert (prepared.album_root / "01 - 신메뉴.flac").is_file()
    assert (prepared.album_root / "cover.jpg").is_file()
    assert (prepared.album_root / ".foxden-import.json").is_file()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_preparation_propagates_real_chmod_failures(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    settings.ensure_directories()

    def chmod_failed(_path: Path, _mode: int) -> None:
        raise OSError(error_number, "real filesystem failure")

    monkeypatch.setattr(library.os, "chmod", chmod_failed)
    with pytest.raises(OSError) as caught:
        prepare_album(
            music_root=settings.music_dir,
            job_id=f"00000000-0000-0000-0000-0000000000{error_number:02d}",
            destination_relative_path="Artist/Denied Album",
            tracks=[_prepared_track(tmp_path)],
            cover_jpeg=None,
        )
    assert caught.value.errno == error_number


@pytest.mark.parametrize(
    "destination_relative_path",
    [".imports/Hidden Album", ".IMPORTS./Hidden Album", ".foxden-import.lock/Hidden Album"],
)
def test_reserved_internal_destinations_are_rejected_during_preparation(
    settings,
    tmp_path: Path,
    destination_relative_path: str,
) -> None:
    settings.ensure_directories()
    with pytest.raises(LibraryImportError, match="reserved internal path"):
        prepare_album(
            music_root=settings.music_dir,
            job_id="00000000-0000-0000-0000-000000000005",
            destination_relative_path=destination_relative_path,
            tracks=[_prepared_track(tmp_path)],
            cover_jpeg=None,
        )


def test_commit_independently_rejects_reserved_internal_destination(settings, tmp_path: Path) -> None:
    settings.ensure_directories()
    build_root = settings.imports_dir / "forged"
    album_root = build_root / "album"
    album_root.mkdir(parents=True)
    forged = PreparedAlbum(
        job_id="forged",
        build_root=build_root,
        album_root=album_root,
        destination_relative_path=".imports/Hidden Album",
        tracks=(),
        manifest_digest="0" * 64,
    )

    with pytest.raises(LibraryImportError, match="reserved internal path"):
        commit_album(music_root=settings.music_dir, album=forged)


def test_reconciliation_verifies_tracks_and_cleans_rebuilt_workspace(settings, tmp_path: Path) -> None:
    settings.ensure_directories()
    job_id = "00000000-0000-0000-0000-000000000004"
    first = prepare_album(
        music_root=settings.music_dir,
        job_id=job_id,
        destination_relative_path="Artist/Album",
        tracks=[_prepared_track(tmp_path)],
        cover_jpeg=None,
    )
    destination, _ = commit_album(music_root=settings.music_dir, album=first)

    rebuilt = prepare_album(
        music_root=settings.music_dir,
        job_id=job_id,
        destination_relative_path="Artist/Album",
        tracks=[_prepared_track(tmp_path)],
        cover_jpeg=None,
    )
    reconciled_destination, reconciled = commit_album(music_root=settings.music_dir, album=rebuilt)
    assert reconciled is True
    assert reconciled_destination == destination
    assert not rebuilt.build_root.exists()

    (destination / "01 - 신메뉴.flac").write_bytes(b"tampered")
    third = prepare_album(
        music_root=settings.music_dir,
        job_id=job_id,
        destination_relative_path="Artist/Album",
        tracks=[_prepared_track(tmp_path)],
        cover_jpeg=None,
    )
    with pytest.raises(LibraryConflictError):
        commit_album(music_root=settings.music_dir, album=third)


def test_cover_is_manifested_and_verified_before_publication(settings, tmp_path: Path) -> None:
    settings.ensure_directories()
    cover = b"\xff\xd8\xff\xe0synthetic-jpeg\xff\xd9"
    prepared = prepare_album(
        music_root=settings.music_dir,
        job_id="00000000-0000-0000-0000-000000000006",
        destination_relative_path="Artist/Covered Album",
        tracks=[_prepared_track(tmp_path)],
        cover_jpeg=cover,
    )
    manifest = json.loads((prepared.album_root / ".foxden-import.json").read_bytes())
    assert manifest["cover"] == {
        "relative_path": "cover.jpg",
        "sha256": sha256_file(prepared.album_root / "cover.jpg"),
    }

    (prepared.album_root / "cover.jpg").write_bytes(b"tampered")
    with pytest.raises(LibraryImportError, match="cover failed verification"):
        commit_album(music_root=settings.music_dir, album=prepared)
    assert not (settings.music_dir / "Artist" / "Covered Album").exists()


def test_reconciliation_rejects_a_tampered_published_cover(settings, tmp_path: Path) -> None:
    settings.ensure_directories()
    job_id = "00000000-0000-0000-0000-000000000007"
    cover = b"\xff\xd8\xff\xe0synthetic-jpeg\xff\xd9"
    first = prepare_album(
        music_root=settings.music_dir,
        job_id=job_id,
        destination_relative_path="Artist/Covered Album",
        tracks=[_prepared_track(tmp_path)],
        cover_jpeg=cover,
    )
    destination, _ = commit_album(music_root=settings.music_dir, album=first)
    (destination / "cover.jpg").write_bytes(b"tampered")

    rebuilt = prepare_album(
        music_root=settings.music_dir,
        job_id=job_id,
        destination_relative_path="Artist/Covered Album",
        tracks=[_prepared_track(tmp_path)],
        cover_jpeg=cover,
    )
    with pytest.raises(LibraryConflictError):
        commit_album(music_root=settings.music_dir, album=rebuilt)


def test_import_lock_rejects_a_hard_link_without_modifying_its_target(tmp_path: Path) -> None:
    target = tmp_path / "unrelated.txt"
    target.write_bytes(b"must-not-change")
    lock_path = tmp_path / ".foxden-import.lock"
    os.link(target, lock_path)

    with pytest.raises(LibraryImportError, match="hard-linked"):
        with ImportLock(lock_path):
            pass
    assert target.read_bytes() == b"must-not-change"


def test_import_lock_rejects_a_reparse_or_symlink_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "unrelated.txt"
    target.write_bytes(b"must-not-change")
    lock_path = tmp_path / ".foxden-import.lock"
    try:
        lock_path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"This test environment cannot create symlinks: {exc}")

    with pytest.raises(LibraryImportError):
        with ImportLock(lock_path):
            pass
    assert target.read_bytes() == b"must-not-change"


@pytest.mark.skipif(os.name != "nt", reason="Windows marker initialization is platform-specific")
def test_windows_import_lock_initializes_once_under_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / ".foxden-import.lock"
    with ImportLock(lock_path):
        pass
    assert lock_path.read_bytes() == library._LOCK_MARKER
    marker = lock_path.read_bytes()
    with ImportLock(lock_path):
        pass
    assert lock_path.read_bytes() == marker


@pytest.mark.skipif(os.name != "nt", reason="Windows marker validation is platform-specific")
def test_windows_import_lock_fails_closed_without_rewriting_an_unknown_file(tmp_path: Path) -> None:
    lock_path = tmp_path / ".foxden-import.lock"
    lock_path.write_bytes(b"do-not-rewrite")

    with pytest.raises(LibraryImportError, match="invalid marker"):
        with ImportLock(lock_path):
            pass
    assert lock_path.read_bytes() == b"do-not-rewrite"


def test_directory_fsync_ignores_documented_unsupported_error(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    monkeypatch.setattr(library.os, "open", lambda *_args, **_kwargs: 73)
    monkeypatch.setattr(library.os, "close", closed.append)

    def unsupported(_descriptor: int) -> None:
        raise OSError(errno.EINVAL, "directory fsync unsupported")

    monkeypatch.setattr(library.os, "fsync", unsupported)
    library._fsync_directory_posix(Path("ignored"))
    assert closed == [73]


def test_directory_fsync_propagates_real_io_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    monkeypatch.setattr(library.os, "open", lambda *_args, **_kwargs: 74)
    monkeypatch.setattr(library.os, "close", closed.append)

    def failed(_descriptor: int) -> None:
        raise OSError(errno.EIO, "durability failure")

    monkeypatch.setattr(library.os, "fsync", failed)
    with pytest.raises(OSError) as caught:
        library._fsync_directory_posix(Path("ignored"))
    assert caught.value.errno == errno.EIO
    assert closed == [74]


def test_directory_fsync_propagates_open_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args, **_kwargs) -> int:
        raise PermissionError(errno.EACCES, "access denied")

    monkeypatch.setattr(library.os, "open", denied)
    with pytest.raises(PermissionError):
        library._fsync_directory_posix(Path("ignored"))
