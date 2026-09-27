from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .filenames import normalize_unicode, portable_collision_key


__all__ = [
    "ArchiveContentError",
    "ArchiveError",
    "ArchiveExtractionError",
    "ArchiveLimitError",
    "ArchiveLimits",
    "ArchiveSecurityError",
    "ArchiveValidationError",
    "ExtractedFile",
    "ExtractionManifest",
    "extract_zip_safely",
    "secure_extract_zip",
]


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SUPPORTED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_AUDIO_SUFFIXES = frozenset({".flac", ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".oga"})
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_SIDECAR_SUFFIXES = frozenset({".cue", ".log", ".txt", ".m3u", ".m3u8", ".nfo"})
_NESTED_ARCHIVE_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".tbz2",
    ".tgz",
    ".tbz",
    ".txz",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".zst",
    ".cab",
    ".iso",
)
_ENCRYPTION_FLAGS = 0x0001 | 0x0040
_PREFIX_BYTES = 512
_MANIFEST_NAME = "manifest.json"


class ArchiveError(Exception):
    """Base class for safe, user-displayable archive failures."""

    code = "archive_error"


class ArchiveValidationError(ArchiveError):
    code = "archive_invalid"


class ArchiveSecurityError(ArchiveValidationError):
    code = "archive_unsafe"


class ArchiveLimitError(ArchiveValidationError):
    code = "archive_limit_exceeded"


class ArchiveContentError(ArchiveValidationError):
    code = "archive_content_unsupported"


class ArchiveExtractionError(ArchiveError):
    code = "archive_extraction_failed"


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_archive_bytes: int = 4 * 1024**3
    max_entries: int = 2_000
    max_files: int = 2_000
    max_entry_bytes: int = 2 * 1024**3
    max_total_bytes: int = 8 * 1024**3
    max_compression_ratio: float = 250.0
    compression_ratio_min_bytes: int = 1024**2
    max_depth: int = 12
    max_name_bytes: int = 1_024
    max_seconds: float = 15 * 60
    chunk_bytes: int = 1024**2

    def __post_init__(self) -> None:
        integer_fields = (
            "max_archive_bytes",
            "max_entries",
            "max_files",
            "max_entry_bytes",
            "max_total_bytes",
            "compression_ratio_min_bytes",
            "max_depth",
            "max_name_bytes",
            "chunk_bytes",
        )
        for field_name in integer_fields:
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.max_compression_ratio <= 0:
            raise ValueError("max_compression_ratio must be positive")
        if self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ExtractedFile:
    original_path: str
    normalized_path: str
    stored_name: str
    media_kind: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "original_path": self.original_path,
            "normalized_path": self.normalized_path,
            "stored_name": self.stored_name,
            "media_kind": self.media_kind,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ExtractionManifest:
    archive_name: str
    files: tuple[ExtractedFile, ...]
    total_bytes: int
    directory_count: int
    version: int = 1

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def path_mapping(self) -> dict[str, str]:
        return {entry.original_path: entry.stored_name for entry in self.files}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "archive_name": self.archive_name,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "total_bytes": self.total_bytes,
            "files": [entry.to_dict() for entry in self.files],
        }


@dataclass(frozen=True, slots=True)
class _PlannedMember:
    info: zipfile.ZipInfo
    original_path: str
    normalized_path: str
    collision_key: str
    is_directory: bool
    media_kind: str | None
    suffix: str | None
    stored_name: str | None


def _member_name(info: zipfile.ZipInfo) -> str:
    # ZipInfo preserves the pre-NUL name in orig_filename. filename alone is
    # insufficient because the standard library truncates it at the first NUL.
    value = getattr(info, "orig_filename", info.filename)
    if not isinstance(value, str):
        raise ArchiveSecurityError("archive member name is not text")
    return value


def _validate_member_path(
    info: zipfile.ZipInfo, limits: ArchiveLimits
) -> tuple[str, str, tuple[str, ...], bool]:
    original = _member_name(info)
    if "\x00" in original:
        raise ArchiveSecurityError("archive member name contains NUL")
    try:
        encoded_length = len(original.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ArchiveSecurityError("archive member name is not valid Unicode") from exc
    if encoded_length > limits.max_name_bytes:
        raise ArchiveLimitError(f"archive member name exceeds {limits.max_name_bytes} bytes")

    slash_name = original.replace("\\", "/")
    if slash_name.startswith("/") or _WINDOWS_DRIVE_RE.match(slash_name):
        raise ArchiveSecurityError(f"absolute archive path rejected: {original!r}")

    is_directory = slash_name.endswith("/")
    body = slash_name[:-1] if is_directory else slash_name
    if not body:
        raise ArchiveSecurityError("empty archive member path rejected")
    raw_parts = body.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ArchiveSecurityError(f"ambiguous or traversing archive path rejected: {original!r}")
    if len(raw_parts) > limits.max_depth:
        raise ArchiveLimitError(f"archive path exceeds depth {limits.max_depth}: {original!r}")

    parts = tuple(normalize_unicode(part) for part in raw_parts)
    normalized_path = "/".join(parts) + ("/" if is_directory else "")
    collision_key = "/".join(portable_collision_key(part) for part in parts)
    return original, normalized_path, parts, is_directory


def _validate_member_type(info: zipfile.ZipInfo, *, is_directory: bool, original: str) -> None:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ArchiveSecurityError(f"symlink or special archive member rejected: {original!r}")
    if file_type == 0:
        return
    if is_directory:
        if file_type != stat.S_IFDIR:
            raise ArchiveSecurityError(f"inconsistent directory type rejected: {original!r}")
        return
    if file_type != stat.S_IFREG:
        raise ArchiveSecurityError(f"symlink or special archive member rejected: {original!r}")


def _member_suffix(parts: tuple[str, ...], original: str) -> tuple[str, str]:
    basename = parts[-1]
    lowered = basename.casefold()
    if any(lowered.endswith(suffix) for suffix in _NESTED_ARCHIVE_SUFFIXES):
        raise ArchiveContentError(f"nested archive rejected: {original!r}")
    dot_index = lowered.rfind(".")
    suffix = lowered[dot_index:] if dot_index > 0 else ""
    if suffix in _AUDIO_SUFFIXES:
        return suffix, "audio"
    if suffix in _IMAGE_SUFFIXES:
        return suffix, "image"
    if suffix in _SIDECAR_SUFFIXES:
        return suffix, "sidecar"
    raise ArchiveContentError(f"unsupported archive member rejected: {original!r}")


def _preflight(zf: zipfile.ZipFile, limits: ArchiveLimits) -> list[_PlannedMember]:
    infos = zf.infolist()
    if not infos:
        raise ArchiveValidationError("empty ZIP archive rejected")
    if len(infos) > limits.max_entries:
        raise ArchiveLimitError(f"archive contains more than {limits.max_entries} entries")

    plans: list[_PlannedMember] = []
    total_declared = 0
    file_number = 0
    keys: dict[str, _PlannedMember] = {}

    for info in infos:
        original, normalized_path, parts, is_directory = _validate_member_path(info, limits)
        _validate_member_type(info, is_directory=is_directory, original=original)

        if info.flag_bits & _ENCRYPTION_FLAGS:
            raise ArchiveSecurityError(f"encrypted archive member rejected: {original!r}")
        if info.compress_type not in _SUPPORTED_COMPRESSION:
            raise ArchiveContentError(f"unsupported ZIP compression rejected: {original!r}")
        if info.file_size < 0 or info.compress_size < 0:
            raise ArchiveValidationError(f"negative member size rejected: {original!r}")

        collision_key = "/".join(portable_collision_key(part) for part in parts)
        media_kind: str | None = None
        suffix: str | None = None
        stored_name: str | None = None
        if is_directory:
            if info.file_size != 0:
                raise ArchiveValidationError(f"directory has non-zero content: {original!r}")
        else:
            file_number += 1
            if file_number > limits.max_files:
                raise ArchiveLimitError(f"archive contains more than {limits.max_files} files")
            if info.file_size > limits.max_entry_bytes:
                raise ArchiveLimitError(f"archive member exceeds size limit: {original!r}")
            total_declared += info.file_size
            if total_declared > limits.max_total_bytes:
                raise ArchiveLimitError("archive declared expanded size exceeds limit")
            if info.file_size > 0 and info.compress_size == 0:
                raise ArchiveLimitError(f"impossible compression ratio rejected: {original!r}")
            if info.file_size >= limits.compression_ratio_min_bytes:
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > limits.max_compression_ratio:
                    raise ArchiveLimitError(f"archive member compression ratio exceeds limit: {original!r}")
            suffix, media_kind = _member_suffix(parts, original)
            stored_name = f"{file_number:06d}{suffix}"

        plan = _PlannedMember(
            info=info,
            original_path=original,
            normalized_path=normalized_path,
            collision_key=collision_key,
            is_directory=is_directory,
            media_kind=media_kind,
            suffix=suffix,
            stored_name=stored_name,
        )
        if collision_key in keys:
            other = keys[collision_key]
            raise ArchiveSecurityError(
                f"colliding archive paths rejected: {other.original_path!r} and {original!r}"
            )
        keys[collision_key] = plan
        plans.append(plan)

    if file_number == 0:
        raise ArchiveValidationError("archive contains no supported files")

    all_keys = tuple(keys)
    for plan in plans:
        if plan.is_directory:
            continue
        prefix = f"{plan.collision_key}/"
        if any(other_key.startswith(prefix) for other_key in all_keys):
            raise ArchiveSecurityError(f"file/directory path conflict rejected: {plan.original_path!r}")
    return plans


def _nested_archive_kind(prefix: bytes, path: Path) -> str | None:
    signatures = (
        (b"PK\x03\x04", "ZIP"),
        (b"PK\x05\x06", "ZIP"),
        (b"PK\x07\x08", "ZIP"),
        (b"Rar!\x1a\x07", "RAR"),
        (b"7z\xbc\xaf'\x1c", "7z"),
        (b"\x1f\x8b", "gzip"),
        (b"BZh", "bzip2"),
        (b"\xfd7zXZ\x00", "xz"),
        (b"\x28\xb5\x2f\xfd", "zstd"),
        (b"\x04\x22\x4d\x18", "LZ4"),
        (b"MSCF", "CAB"),
        (b"MZ", "Windows executable"),
        (b"\x7fELF", "ELF executable"),
        (b"#!", "script"),
        (b"%PDF-", "PDF"),
    )
    for signature, label in signatures:
        if prefix.startswith(signature):
            return label
    if len(prefix) >= 262 and prefix[257:262] == b"ustar":
        return "tar"
    # This also catches ZIP polyglots with an innocuous prefix and an end-of-
    # central-directory record appended later in the file.
    if zipfile.is_zipfile(path):
        return "ZIP"
    return None


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # Directory fsync is not available on every supported platform.
            pass
    finally:
        os.close(descriptor)


def _write_manifest(path: Path, manifest: ExtractionManifest) -> None:
    temporary = path.with_name(f"{path.name}.part")
    payload = (
        json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.replace(temporary, path)


def _extract_plans(
    zf: zipfile.ZipFile,
    plans: list[_PlannedMember],
    temporary_dir: Path,
    archive_name: str,
    limits: ArchiveLimits,
) -> ExtractionManifest:
    started = time.monotonic()
    total_actual = 0
    extracted: list[ExtractedFile] = []
    directory_count = sum(plan.is_directory for plan in plans)

    for plan in plans:
        if plan.is_directory:
            continue
        if time.monotonic() - started > limits.max_seconds:
            raise ArchiveLimitError("archive extraction exceeded time limit")
        assert plan.stored_name is not None
        assert plan.media_kind is not None
        output_path = temporary_dir / plan.stored_name
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(output_path, flags, 0o600)
        actual = 0
        digest = hashlib.sha256()
        prefix = bytearray()
        try:
            with zf.open(plan.info, "r") as source, os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                while True:
                    chunk = source.read(limits.chunk_bytes)
                    if not chunk:
                        break
                    actual += len(chunk)
                    total_actual += len(chunk)
                    if actual > limits.max_entry_bytes:
                        raise ArchiveLimitError(
                            f"archive member exceeded streamed size limit: {plan.original_path!r}"
                        )
                    if total_actual > limits.max_total_bytes:
                        raise ArchiveLimitError("archive exceeded streamed total size limit")
                    if len(prefix) < _PREFIX_BYTES:
                        prefix.extend(chunk[: _PREFIX_BYTES - len(prefix)])
                    digest.update(chunk)
                    output.write(chunk)
                    if time.monotonic() - started > limits.max_seconds:
                        raise ArchiveLimitError("archive extraction exceeded time limit")
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if actual != plan.info.file_size:
            raise ArchiveValidationError(
                f"archive member size did not match central directory: {plan.original_path!r}"
            )
        if actual == 0 and plan.media_kind in {"audio", "image"}:
            raise ArchiveContentError(f"empty media file rejected: {plan.original_path!r}")
        nested_kind = _nested_archive_kind(bytes(prefix), output_path)
        if nested_kind is not None:
            raise ArchiveContentError(
                f"nested {nested_kind} content rejected in member: {plan.original_path!r}"
            )
        extracted.append(
            ExtractedFile(
                original_path=plan.original_path,
                normalized_path=plan.normalized_path,
                stored_name=plan.stored_name,
                media_kind=plan.media_kind,
                size=actual,
                sha256=digest.hexdigest(),
            )
        )

    manifest = ExtractionManifest(
        archive_name=archive_name,
        files=tuple(extracted),
        total_bytes=total_actual,
        directory_count=directory_count,
    )
    _write_manifest(temporary_dir / _MANIFEST_NAME, manifest)
    _fsync_directory(temporary_dir)
    return manifest


def secure_extract_zip(
    archive_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    limits: ArchiveLimits | None = None,
    metadata_encoding: str | None = None,
) -> ExtractionManifest:
    """Validate and atomically extract an untrusted ZIP to flat generated names.

    ``destination`` is made visible only after preflight, complete streamed
    extraction, CRC verification, content checks, and manifest persistence all
    succeed. The caller must provide a private job-owned destination parent.
    ``metadata_encoding`` is an explicit legacy-ZIP override; the ZIP UTF-8 flag
    still takes precedence in the standard library.
    """

    active_limits = limits or ArchiveLimits()
    source_path = Path(archive_path)
    destination_path = Path(destination)

    try:
        source_stat = source_path.lstat()
    except OSError as exc:
        raise ArchiveExtractionError("cannot access ZIP archive") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise ArchiveSecurityError("ZIP source must be a regular file, not a symlink or special file")
    if source_stat.st_size > active_limits.max_archive_bytes:
        raise ArchiveLimitError("ZIP archive exceeds upload size limit")
    if destination_path.exists() or destination_path.is_symlink():
        raise ArchiveExtractionError("extraction destination already exists")

    try:
        destination_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_dir = destination_path.with_name(
            f"{destination_path.name}.part-{uuid.uuid4().hex}"
        )
        temporary_dir.mkdir(mode=0o700)
    except OSError as exc:
        raise ArchiveExtractionError("cannot create extraction directory") from exc

    try:
        try:
            with zipfile.ZipFile(
                source_path,
                mode="r",
                metadata_encoding=metadata_encoding,
            ) as zf:
                plans = _preflight(zf, active_limits)
                manifest = _extract_plans(
                    zf,
                    plans,
                    temporary_dir,
                    source_path.name,
                    active_limits,
                )
        except ArchiveError:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, RuntimeError) as exc:
            raise ArchiveValidationError(f"invalid or corrupt ZIP archive: {exc}") from exc
        except NotImplementedError as exc:
            raise ArchiveContentError(f"unsupported ZIP feature: {exc}") from exc
        except LookupError as exc:
            raise ArchiveValidationError(f"invalid ZIP metadata encoding: {exc}") from exc
        except OSError as exc:
            raise ArchiveExtractionError("archive extraction I/O failure") from exc

        if destination_path.exists() or destination_path.is_symlink():
            raise ArchiveExtractionError("extraction destination appeared during processing")
        try:
            os.rename(temporary_dir, destination_path)
            _fsync_directory(destination_path.parent)
        except OSError as exc:
            raise ArchiveExtractionError("could not atomically promote extraction") from exc
        return manifest
    except BaseException:
        # temporary_dir is server-generated beneath the trusted destination
        # parent. Never derive cleanup targets from archive member names.
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


# A descriptive alias for callers that read naturally as "extract safely".
extract_zip_safely = secure_extract_zip
