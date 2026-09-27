from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from foxden_music.archive import (
    ArchiveContentError,
    ArchiveExtractionError,
    ArchiveLimitError,
    ArchiveLimits,
    ArchiveSecurityError,
    ArchiveValidationError,
    secure_extract_zip,
)


FLAC_BYTES = b"fLaC\x00\x00\x00\x22synthetic-test-audio"
MP3_BYTES = b"ID3\x04\x00\x00synthetic-test-audio"
JPEG_BYTES = b"\xff\xd8\xff\xe0synthetic-test-image\xff\xd9"


def _write_zip(
    path: Path,
    members: list[tuple[str | zipfile.ZipInfo, bytes]],
    *,
    compression: int = zipfile.ZIP_STORED,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return path


def _set_flag(path: Path, flag: int) -> None:
    payload = bytearray(path.read_bytes())
    local = payload.index(b"PK\x03\x04")
    central = payload.index(b"PK\x01\x02")
    local_flags = int.from_bytes(payload[local + 6 : local + 8], "little") | flag
    central_flags = int.from_bytes(payload[central + 8 : central + 10], "little") | flag
    payload[local + 6 : local + 8] = local_flags.to_bytes(2, "little")
    payload[central + 8 : central + 10] = central_flags.to_bytes(2, "little")
    path.write_bytes(payload)


def test_valid_zip_extracts_to_generated_flat_names_and_unicode_manifest(tmp_path: Path) -> None:
    original_nfd = "앨범/Cafe\u0301.flac"
    note_bytes = "정상적인 메모".encode()
    archive = _write_zip(
        tmp_path / "앨범.zip",
        [
            ("앨범/", b""),
            (original_nfd, FLAC_BYTES),
            ("앨범/02 - 신메뉴.MP3", MP3_BYTES),
            ("앨범/cover.JPG", JPEG_BYTES),
            ("앨범/notes.txt", note_bytes),
        ],
    )
    destination = tmp_path / "job" / "extracted"

    manifest = secure_extract_zip(archive, destination)

    assert destination.is_dir()
    assert manifest.file_count == 4
    assert manifest.directory_count == 1
    assert manifest.total_bytes == sum((len(FLAC_BYTES), len(MP3_BYTES), len(JPEG_BYTES), len(note_bytes)))
    assert manifest.path_mapping[original_nfd] == "000001.flac"
    assert manifest.files[0].normalized_path == "앨범/Café.flac"
    assert (destination / "000001.flac").read_bytes() == FLAC_BYTES
    assert (destination / "000002.mp3").read_bytes() == MP3_BYTES
    assert (destination / "000003.jpg").read_bytes() == JPEG_BYTES
    assert not (destination / "앨범").exists()
    assert not list(destination.parent.glob("extracted.part-*"))

    manifest_text = (destination / "manifest.json").read_text(encoding="utf-8")
    assert original_nfd in manifest_text
    manifest_payload = json.loads(manifest_text)
    assert manifest_payload["files"][0]["original_path"] == original_nfd
    assert manifest_payload["files"][0]["sha256"] == hashlib.sha256(FLAC_BYTES).hexdigest()


@pytest.mark.parametrize(
    "member_name",
    [
        "../escape.flac",
        "album/../../escape.flac",
        "/absolute/song.flac",
        r"C:\absolute\song.flac",
        "C:relative.flac",
        r"\\server\share\song.flac",
        "album/./song.flac",
        "album//song.flac",
        r"album\..\escape.flac",
    ],
)
def test_traversal_and_absolute_paths_are_rejected(tmp_path: Path, member_name: str) -> None:
    archive = _write_zip(tmp_path / "unsafe.zip", [(member_name, FLAC_BYTES)])
    destination = tmp_path / "extracted"
    canary = tmp_path / "canary"
    canary.write_text("unchanged", encoding="utf-8")

    with pytest.raises(ArchiveSecurityError):
        secure_extract_zip(archive, destination)

    assert canary.read_text(encoding="utf-8") == "unchanged"
    assert not destination.exists()
    assert not list(tmp_path.glob("extracted.part-*"))


def test_nul_in_original_zipinfo_name_is_rejected() -> None:
    from foxden_music.archive import _validate_member_path

    info = zipfile.ZipInfo("safe.flac")
    info.orig_filename = "safe.flac\x00../../escape.flac"

    with pytest.raises(ArchiveSecurityError):
        _validate_member_path(info, ArchiveLimits())


def test_unix_symlink_is_rejected_without_reading_target(tmp_path: Path) -> None:
    link = zipfile.ZipInfo("album/link.flac")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = _write_zip(tmp_path / "link.zip", [(link, b"../../outside")])

    with pytest.raises(ArchiveSecurityError):
        secure_extract_zip(archive, tmp_path / "extracted")

    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("special_type", [stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK])
def test_unix_special_files_are_rejected(tmp_path: Path, special_type: int) -> None:
    member = zipfile.ZipInfo("album/special.flac")
    member.create_system = 3
    member.external_attr = (special_type | 0o600) << 16
    archive = _write_zip(tmp_path / f"special-{special_type}.zip", [(member, FLAC_BYTES)])

    with pytest.raises(ArchiveSecurityError):
        secure_extract_zip(archive, tmp_path / "extracted")


def test_encrypted_flag_is_rejected_during_preflight(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "encrypted.zip", [("song.flac", FLAC_BYTES)])
    _set_flag(archive, 0x0001)

    with pytest.raises(ArchiveSecurityError, match="encrypted"):
        secure_extract_zip(archive, tmp_path / "extracted")


@pytest.mark.parametrize("name", ["payload.zip", "payload.tar.gz", "payload.RAR"])
def test_nested_archive_extensions_are_rejected(tmp_path: Path, name: str) -> None:
    archive = _write_zip(tmp_path / "outer.zip", [(name, b"not-even-opened")])

    with pytest.raises(ArchiveContentError, match="nested archive"):
        secure_extract_zip(archive, tmp_path / "extracted")


def test_nested_archive_renamed_as_audio_is_rejected_by_magic(tmp_path: Path) -> None:
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as inner:
        inner.writestr("song.flac", FLAC_BYTES)
    archive = _write_zip(tmp_path / "outer.zip", [("innocent.flac", nested.getvalue())])

    with pytest.raises(ArchiveContentError, match="nested ZIP"):
        secure_extract_zip(archive, tmp_path / "extracted")


def test_executable_renamed_as_audio_is_rejected_by_magic(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "outer.zip", [("innocent.flac", b"MZ" + b"\x00" * 50)])

    with pytest.raises(ArchiveContentError, match="Windows executable"):
        secure_extract_zip(archive, tmp_path / "extracted")


@pytest.mark.parametrize("name", ["program.exe", "script.sh", "document.pdf", "no-extension"])
def test_unsupported_file_types_are_rejected(tmp_path: Path, name: str) -> None:
    archive = _write_zip(tmp_path / "unsupported.zip", [(name, b"content")])

    with pytest.raises(ArchiveContentError, match="unsupported archive member"):
        secure_extract_zip(archive, tmp_path / "extracted")


@pytest.mark.parametrize(
    "first,second",
    [
        ("Album/Song.flac", "album/song.FLAC"),
        ("Café.flac", "Cafe\u0301.flac"),
        ("aa:b.flac", "aa?b.flac"),
        (r"album\Song.flac", "ALBUM/song.flac"),
    ],
)
def test_ambiguous_portable_path_collisions_are_rejected(
    tmp_path: Path, first: str, second: str
) -> None:
    archive = _write_zip(tmp_path / "collision.zip", [(first, FLAC_BYTES), (second, FLAC_BYTES)])

    with pytest.raises(ArchiveSecurityError, match="colliding archive paths"):
        secure_extract_zip(archive, tmp_path / "extracted")


def test_file_directory_prefix_conflict_is_rejected(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "prefix.zip",
        [("album.flac", FLAC_BYTES), ("album.flac/song.mp3", MP3_BYTES)],
    )

    with pytest.raises(ArchiveSecurityError, match="file/directory path conflict"):
        secure_extract_zip(archive, tmp_path / "extracted")


def test_duplicate_archive_member_is_rejected(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        archive = _write_zip(
            tmp_path / "duplicate.zip",
            [("song.flac", FLAC_BYTES), ("song.flac", FLAC_BYTES)],
        )

    with pytest.raises(ArchiveSecurityError, match="colliding archive paths"):
        secure_extract_zip(archive, tmp_path / "extracted")


def test_file_count_limit_is_enforced_before_extraction(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "many.zip", [("a.flac", FLAC_BYTES), ("b.mp3", MP3_BYTES)])

    with pytest.raises(ArchiveLimitError, match="more than 1 files"):
        secure_extract_zip(archive, tmp_path / "extracted", limits=ArchiveLimits(max_files=1))


def test_per_entry_and_total_size_limits_are_enforced(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "large.zip", [("a.flac", b"fLaC" + b"a" * 20)])

    with pytest.raises(ArchiveLimitError, match="member exceeds size limit"):
        secure_extract_zip(archive, tmp_path / "entry", limits=ArchiveLimits(max_entry_bytes=10))

    two_files = _write_zip(
        tmp_path / "total.zip",
        [("a.flac", b"fLaC123456"), ("b.flac", b"fLaC123456")],
    )
    with pytest.raises(ArchiveLimitError, match="expanded size"):
        secure_extract_zip(two_files, tmp_path / "total", limits=ArchiveLimits(max_total_bytes=15))


def test_archive_file_size_depth_and_name_limits_are_enforced(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "limits.zip", [("one/two/song.flac", FLAC_BYTES)])

    with pytest.raises(ArchiveLimitError, match="upload size"):
        secure_extract_zip(
            archive,
            tmp_path / "archive-size",
            limits=ArchiveLimits(max_archive_bytes=archive.stat().st_size - 1),
        )
    with pytest.raises(ArchiveLimitError, match="depth"):
        secure_extract_zip(archive, tmp_path / "depth", limits=ArchiveLimits(max_depth=2))
    with pytest.raises(ArchiveLimitError, match="name exceeds"):
        secure_extract_zip(archive, tmp_path / "name", limits=ArchiveLimits(max_name_bytes=8))


def test_compression_ratio_limit_is_enforced(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "bomb.zip",
        [("song.flac", b"A" * 20_000)],
        compression=zipfile.ZIP_DEFLATED,
    )
    limits = ArchiveLimits(max_compression_ratio=2, compression_ratio_min_bytes=1)

    with pytest.raises(ArchiveLimitError, match="compression ratio"):
        secure_extract_zip(archive, tmp_path / "extracted", limits=limits)


def test_bad_crc_fails_and_cleans_atomic_partial_directory(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "corrupt.zip", [("song.flac", FLAC_BYTES)])
    raw = bytearray(archive.read_bytes())
    payload_offset = raw.index(FLAC_BYTES)
    raw[payload_offset + 5] ^= 0xFF
    archive.write_bytes(raw)
    destination = tmp_path / "extracted"

    with pytest.raises(ArchiveValidationError, match="invalid or corrupt ZIP"):
        secure_extract_zip(archive, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob("extracted.part-*"))


def test_empty_media_and_empty_archive_are_rejected(tmp_path: Path) -> None:
    empty_media = _write_zip(tmp_path / "empty-media.zip", [("song.flac", b"")])
    with pytest.raises(ArchiveContentError, match="empty media"):
        secure_extract_zip(empty_media, tmp_path / "media")

    empty_archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_archive, "w"):
        pass
    with pytest.raises(ArchiveValidationError, match="empty ZIP"):
        secure_extract_zip(empty_archive, tmp_path / "archive")


def test_existing_destination_is_never_replaced(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "album.zip", [("song.flac", FLAC_BYTES)])
    destination = tmp_path / "extracted"
    destination.mkdir()
    canary = destination / "keep.txt"
    canary.write_text("keep", encoding="utf-8")

    with pytest.raises(ArchiveExtractionError, match="already exists"):
        secure_extract_zip(archive, destination)

    assert canary.read_text(encoding="utf-8") == "keep"


def test_archive_io_error_does_not_expose_absolute_source_path(tmp_path: Path) -> None:
    missing = tmp_path / "private" / "missing.zip"

    with pytest.raises(ArchiveExtractionError) as captured:
        secure_extract_zip(missing, tmp_path / "extracted")

    assert str(missing) not in str(captured.value)
    assert str(captured.value) == "cannot access ZIP archive"


def test_zip_source_symlink_is_rejected(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "album.zip", [("song.flac", FLAC_BYTES)])
    link = tmp_path / "link.zip"
    try:
        link.symlink_to(archive)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")

    with pytest.raises(ArchiveSecurityError, match="regular file"):
        secure_extract_zip(link, tmp_path / "extracted")
