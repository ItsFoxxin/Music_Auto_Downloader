from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable

from .audio import sha256_file


class LibraryImportError(RuntimeError):
    pass


class LibraryConflictError(LibraryImportError):
    pass


_RESERVED_LIBRARY_TOP_LEVEL = frozenset({".imports", ".foxden-import.lock"})
_LOCK_MARKER = b"0FOX-DEN-MUSIC-IMPORT-LOCK-v1\n"
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    value
    for value in {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    }
    if value is not None
)
_UNSUPPORTED_FILE_CHMOD_ERRNOS = frozenset(
    value
    for value in {
        errno.EPERM,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    }
    if value is not None
)
_UNSUPPORTED_WINDOWS_DIRECTORY_FLUSH_ERRORS = frozenset(
    {
        1,  # ERROR_INVALID_FUNCTION: filesystem does not implement the flush.
        50,  # ERROR_NOT_SUPPORTED.
    }
)


@dataclass(frozen=True, slots=True)
class PreparedTrack:
    source_path: Path
    relative_path: str
    source_sha256: str
    final_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedAlbum:
    job_id: str
    build_root: Path
    album_root: Path
    destination_relative_path: str
    tracks: tuple[PreparedTrack, ...]
    manifest_digest: str
    cover_relative_path: str | None = None
    cover_sha256: str | None = None


def _safe_relative(value: str) -> Path:
    posix = PurePosixPath(value.replace("\\", "/"))
    if posix.is_absolute() or not posix.parts or any(part in {"", ".", ".."} for part in posix.parts):
        raise LibraryImportError("Generated library path was not a safe relative path")
    return Path(*posix.parts)


def _safe_library_destination(value: str) -> Path:
    relative = _safe_relative(value)
    # Windows aliases names with trailing spaces/dots, so compare the portable
    # form even when the worker currently runs on Linux.
    top_level = relative.parts[0].rstrip(" .").casefold()
    if top_level in _RESERVED_LIBRARY_TOP_LEVEL:
        raise LibraryImportError("Library destination uses a reserved internal path")
    return relative


def _is_within(root: Path, target: Path) -> bool:
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (ValueError, OSError):
        return False


def _reject_symlink_ancestors(root: Path, target_parent: Path) -> None:
    root = root.resolve(strict=True)
    try:
        relative = target_parent.relative_to(root)
    except ValueError as exc:
        raise LibraryImportError("Library destination escaped the configured music root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LibraryImportError(f"Library destination contains a symlinked directory: {part}")


def _fsync_file(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _chmod_regular_file_if_supported(path: Path, mode: int) -> None:
    """Apply a private mode unless the mounted filesystem lacks chmod.

    Removable NTFS volumes mounted on Linux can allow file creation and fsync
    while rejecting POSIX mode changes with EPERM.  Their effective access mode
    is controlled by mount options, so that specific limitation must not turn a
    completed album assembly into a failed import.  Ordinary ACL denial and I/O
    failures still fail closed.
    """

    try:
        os.chmod(path, mode)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_FILE_CHMOD_ERRNOS:
            raise
        try:
            details = path.lstat()
        except OSError:
            raise exc
        if path.is_symlink() or not stat.S_ISREG(details.st_mode):
            raise exc


def _fsync_directory_posix(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise
    finally:
        os.close(descriptor)


def _fsync_directory_windows(path: Path) -> None:
    """Flush directory metadata using a real Windows directory handle.

    MSVCRT ``open`` rejects directories, so silently returning on Windows would
    hide both durability failures and ordinary access errors.  CreateFileW with
    ``FILE_FLAG_BACKUP_SEMANTICS`` is the documented way to obtain a directory
    handle.  Only filesystem-level "not supported" responses are nonfatal.
    """

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [wintypes.HANDLE]
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0x40000000,  # GENERIC_WRITE is required by FlushFileBuffers.
        0x00000001 | 0x00000002 | 0x00000004,  # Share read/write/delete.
        None,
        3,  # OPEN_EXISTING
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not flush_file_buffers(handle):
            error_number = ctypes.get_last_error()
            if error_number not in _UNSUPPORTED_WINDOWS_DIRECTORY_FLUSH_ERRORS:
                raise ctypes.WinError(error_number)
    finally:
        close_handle(handle)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        _fsync_directory_windows(path)
    else:
        _fsync_directory_posix(path)


def _manifest_payload(
    job_id: str,
    destination: str,
    tracks: Iterable[PreparedTrack],
    *,
    cover_relative_path: str | None,
    cover_sha256: str | None,
) -> dict[str, object]:
    if (cover_relative_path is None) != (cover_sha256 is None):
        raise LibraryImportError("Cover path and digest must either both be set or both be absent")
    if cover_relative_path not in {None, "cover.jpg"}:
        raise LibraryImportError("The atomic album manifest only permits cover.jpg as cover artwork")
    return {
        "schema": 2,
        "job_id": job_id,
        "destination_relative_path": destination,
        "tracks": [
            {
                "relative_path": item.relative_path,
                "source_sha256": item.source_sha256,
                "final_sha256": item.final_sha256,
            }
            for item in tracks
        ],
        "cover": (
            {
                "relative_path": cover_relative_path,
                "sha256": cover_sha256,
            }
            if cover_relative_path is not None
            else None
        ),
    }


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def prepare_album(
    *,
    music_root: Path,
    job_id: str,
    destination_relative_path: str,
    tracks: Iterable[PreparedTrack],
    cover_jpeg: bytes | None,
) -> PreparedAlbum:
    music_root = music_root.resolve(strict=True)
    _safe_library_destination(destination_relative_path)
    imports_root = music_root / ".imports"
    if imports_root.is_symlink():
        raise LibraryImportError("The .imports directory must not be a symlink")
    imports_root.mkdir(mode=0o750, exist_ok=True)
    (imports_root / ".ignore").touch(mode=0o640, exist_ok=True)
    build_root = imports_root / job_id
    if build_root.exists() or build_root.is_symlink():
        if build_root.is_symlink() or not _is_within(imports_root, build_root):
            raise LibraryImportError("Unsafe prior import workspace")
        shutil.rmtree(build_root)
    album_root = build_root / "album"
    album_root.mkdir(mode=0o750, parents=True)

    prepared_tracks = tuple(tracks)
    if not prepared_tracks:
        raise LibraryImportError("An album cannot be imported without audio tracks")
    used_paths: set[str] = set()
    for item in prepared_tracks:
        relative = _safe_relative(item.relative_path)
        collision_key = relative.as_posix().casefold()
        if collision_key in used_paths:
            raise LibraryImportError("Two tracks resolved to the same final filename")
        used_paths.add(collision_key)
        if not item.source_path.is_file() or item.source_path.is_symlink():
            raise LibraryImportError("A prepared audio source was missing or unsafe")
        destination = album_root / relative
        if not _is_within(album_root, destination):
            raise LibraryImportError("Generated track destination escaped the album build directory")
        destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        with item.source_path.open("rb") as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            _fsync_file(output)
        _chmod_regular_file_if_supported(destination, 0o640)
        if sha256_file(destination) != item.final_sha256:
            raise LibraryImportError("Prepared track changed while the album was assembled")

    cover_relative_path: str | None = None
    cover_sha256: str | None = None
    if cover_jpeg is not None:
        cover_relative_path = "cover.jpg"
        cover_sha256 = hashlib.sha256(cover_jpeg).hexdigest()
        cover_path = album_root / cover_relative_path
        with cover_path.open("xb") as output:
            output.write(cover_jpeg)
            _fsync_file(output)
        _chmod_regular_file_if_supported(cover_path, 0o640)
        if sha256_file(cover_path) != cover_sha256:
            raise LibraryImportError("Prepared cover changed while the album was assembled")

    payload = _manifest_payload(
        job_id,
        destination_relative_path,
        prepared_tracks,
        cover_relative_path=cover_relative_path,
        cover_sha256=cover_sha256,
    )
    encoded = _canonical_json(payload)
    manifest_digest = hashlib.sha256(encoded).hexdigest()
    with (album_root / ".foxden-import.json").open("xb") as output:
        output.write(encoded)
        _fsync_file(output)
    _chmod_regular_file_if_supported(album_root / ".foxden-import.json", 0o640)
    _fsync_directory(album_root)
    _fsync_directory(build_root)
    return PreparedAlbum(
        job_id=job_id,
        build_root=build_root,
        album_root=album_root,
        destination_relative_path=destination_relative_path,
        tracks=prepared_tracks,
        manifest_digest=manifest_digest,
        cover_relative_path=cover_relative_path,
        cover_sha256=cover_sha256,
    )


def _expected_manifest_payload(album: PreparedAlbum) -> dict[str, object]:
    return _manifest_payload(
        album.job_id,
        album.destination_relative_path,
        album.tracks,
        cover_relative_path=album.cover_relative_path,
        cover_sha256=album.cover_sha256,
    )


def _cover_matches(root: Path, album: PreparedAlbum) -> bool:
    if album.cover_relative_path is None:
        cover_path = root / "cover.jpg"
        return not cover_path.exists() and not cover_path.is_symlink()
    try:
        relative = _safe_relative(album.cover_relative_path)
    except LibraryImportError:
        return False
    path = root / relative
    return (
        album.cover_sha256 is not None
        and path.is_file()
        and not path.is_symlink()
        and sha256_file(path) == album.cover_sha256
    )


def verify_prepared_album(album: PreparedAlbum) -> None:
    if not album.album_root.is_dir() or album.album_root.is_symlink():
        raise LibraryImportError("Prepared album directory is missing or unsafe")
    manifest_path = album.album_root / ".foxden-import.json"
    try:
        encoded = manifest_path.read_bytes()
        payload = json.loads(encoded)
    except (OSError, json.JSONDecodeError) as exc:
        raise LibraryImportError("Prepared album manifest is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise LibraryImportError("Prepared album manifest is missing or invalid")
    if hashlib.sha256(encoded).hexdigest() != album.manifest_digest:
        raise LibraryImportError("Prepared album manifest digest changed")
    if payload != _expected_manifest_payload(album):
        raise LibraryImportError("Prepared album manifest does not describe this prepared album")
    for item in album.tracks:
        path = album.album_root / _safe_relative(item.relative_path)
        if not path.is_file() or path.is_symlink() or sha256_file(path) != item.final_sha256:
            raise LibraryImportError(f"Prepared track failed verification: {item.relative_path}")
    if not _cover_matches(album.album_root, album):
        raise LibraryImportError("Prepared cover failed verification")


def _validate_lock_descriptor(path: Path, descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(path)
    except OSError as exc:
        raise LibraryImportError("Could not verify the library import lock") from exc
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(named, "st_file_attributes", 0) & reparse_attribute:
        raise LibraryImportError("The library import lock must not be a reparse point")
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode):
        raise LibraryImportError("The library import lock is not a regular file")
    if opened.st_nlink != 1 or named.st_nlink != 1:
        raise LibraryImportError("The library import lock must not be hard-linked")
    if not os.path.samestat(opened, named):
        raise LibraryImportError("The library import lock changed while it was opened")


def _open_windows_lock_descriptor(path: Path) -> int:
    """Open-or-create a Windows lock without following a reparse point."""

    import msvcrt
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0x00000001 | 0x00000002,  # Share read/write, but never delete/rename.
        None,
        4,  # OPEN_ALWAYS, without truncating an existing lock.
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = FileAttributeTagInfo()
        if not get_information(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if information.file_attributes & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise LibraryImportError("The library import lock must not be a reparse point")
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(handle)
        raise
    # open_osfhandle transferred ownership of the Windows handle to the CRT.
    return descriptor


def _open_lock_descriptor(path: Path) -> int:
    if os.name == "nt":
        descriptor = _open_windows_lock_descriptor(path)
    else:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
    try:
        _validate_lock_descriptor(path, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class ImportLock(AbstractContextManager["ImportLock"]):
    def __init__(self, path: Path):
        self.path = path
        self.handle: BinaryIO | None = None
        self._locked = False

    def __enter__(self) -> "ImportLock":
        try:
            descriptor = _open_lock_descriptor(self.path)
        except LibraryImportError:
            raise
        except OSError as exc:
            raise LibraryImportError("Could not safely open the library import lock") from exc
        try:
            self.handle = os.fdopen(descriptor, "r+b")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            self._locked = True
            _validate_lock_descriptor(self.path, self.handle.fileno())
            if os.name == "nt":
                self.handle.seek(0)
                marker = self.handle.read(len(_LOCK_MARKER) + 1)
                if marker == b"":
                    self.handle.seek(0)
                    self.handle.write(_LOCK_MARKER)
                    _fsync_file(self.handle)
                elif marker not in {_LOCK_MARKER, b"0"}:
                    raise LibraryImportError("The library import lock has an invalid marker")
            return self
        except BaseException as exc:
            try:
                self._release()
            except OSError:
                pass
            if isinstance(exc, LibraryImportError):
                raise
            raise LibraryImportError("Could not acquire the library import lock") from exc

    def _release(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            if self._locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False
            handle.close()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._release()


def _linux_renameat2_noreplace(source: Path, destination: Path) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return errno.ENOSYS
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    return 0 if result == 0 else ctypes.get_errno()


def _guarded_rename_noreplace(source: Path, destination: Path) -> None:
    """Best-effort no-replace rename for filesystems lacking renameat2.

    The caller holds Fox Den's cross-process import lock.  An existence check
    therefore prevents every Fox Den writer from replacing a destination.  On
    Linux, a concurrent external writer can only be replaced if it races an
    *empty directory* into this exact path; files and non-empty directories are
    rejected by rename(2).  This narrow fallback keeps atomic directory
    publication working on NTFS/FUSE mounts that return EINVAL for renameat2.
    """

    try:
        os.lstat(destination)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LibraryImportError("Could not verify the final album destination") from exc
    else:
        raise LibraryConflictError("The final album directory already exists")
    try:
        os.rename(source, destination)
    except OSError as exc:
        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise LibraryConflictError("The final album directory already exists") from exc
        if exc.errno == errno.EXDEV:
            raise LibraryImportError("Atomic import requires .imports and the library destination on one filesystem") from exc
        raise LibraryImportError("Atomic album rename failed") from exc


def _rename_noreplace(source: Path, destination: Path) -> None:
    if sys.platform.startswith("linux"):
        error_number = _linux_renameat2_noreplace(source, destination)
        if error_number == 0:
            return
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise LibraryConflictError("The final album directory already exists")
        if error_number == errno.EXDEV:
            raise LibraryImportError("Atomic import requires .imports and the library destination on one filesystem")
        if error_number in {
            errno.EINVAL,
            errno.ENOSYS,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }:
            _guarded_rename_noreplace(source, destination)
            return
        raise LibraryImportError(f"Atomic no-replace rename failed with errno {error_number}")
    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError as exc:
            raise LibraryConflictError("The final album directory already exists") from exc
        except OSError as exc:
            if getattr(exc, "winerror", None) in {17, 183}:
                raise LibraryConflictError("The final album directory already exists") from exc
            raise LibraryImportError("Atomic album rename failed") from exc
        return
    raise LibraryImportError("Atomic no-replace directory rename is unsupported on this platform")


def _existing_manifest_matches(destination: Path, album: PreparedAlbum) -> bool:
    try:
        encoded = (destination / ".foxden-import.json").read_bytes()
        payload = json.loads(encoded)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if hashlib.sha256(encoded).hexdigest() != album.manifest_digest:
        return False
    if payload != _expected_manifest_payload(album):
        return False
    for item in album.tracks:
        try:
            path = destination / _safe_relative(item.relative_path)
        except LibraryImportError:
            return False
        if not path.is_file() or path.is_symlink() or sha256_file(path) != item.final_sha256:
            return False
    return _cover_matches(destination, album)


def commit_album(*, music_root: Path, album: PreparedAlbum) -> tuple[Path, bool]:
    """Atomically publish a complete album; return (destination, reconciled)."""

    music_root = music_root.resolve(strict=True)
    destination = music_root / _safe_library_destination(album.destination_relative_path)
    _reject_symlink_ancestors(music_root, destination.parent)
    destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    _reject_symlink_ancestors(music_root, destination.parent)
    with ImportLock(music_root / ".foxden-import.lock"):
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink() and _existing_manifest_matches(destination, album):
                shutil.rmtree(album.build_root, ignore_errors=True)
                return destination, True
            raise LibraryConflictError("The final album directory already exists and was not modified")
        if os.stat(album.album_root).st_dev != os.stat(destination.parent).st_dev:
            raise LibraryImportError("Atomic import requires the build and destination on the same filesystem")
        verify_prepared_album(album)
        _rename_noreplace(album.album_root, destination)
        _fsync_directory(destination.parent)
    try:
        album.build_root.rmdir()
    except OSError:
        pass
    return destination, False
