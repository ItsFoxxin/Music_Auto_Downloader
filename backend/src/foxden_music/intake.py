from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePath

from fastapi import UploadFile

from .audio import SUPPORTED_AUDIO_EXTENSIONS
from .config import Settings


class IntakeError(ValueError):
    pass


ALLOWED_UPLOAD_EXTENSIONS = SUPPORTED_AUDIO_EXTENSIONS | frozenset({".zip"})
IGNORED_INBOX_SUFFIXES = frozenset(
    {
        ".aria2",
        ".crdownload",
        ".download",
        ".part",
        ".partial",
        ".tmp",
    }
)


@dataclass(frozen=True)
class InboxFile:
    relative_path: str
    display_name: str
    byte_size: int
    age_seconds: float
    modified_at_epoch: float
    ready: bool
    reason: str | None = None


def safe_display_name(client_filename: str | None) -> str:
    name = PurePath((client_filename or "album-upload").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return (name or "album-upload")[:500]


def cleanup_staged_job(settings: Settings, job_id: str) -> None:
    """Remove one server-generated staging tree without accepting arbitrary paths."""

    try:
        canonical_job_id = str(uuid.UUID(job_id))
    except ValueError as exc:
        raise ValueError("Staged job cleanup requires a UUID job ID") from exc
    if canonical_job_id != job_id:
        raise ValueError("Staged job cleanup requires a canonical UUID job ID")

    job_root = settings.jobs_dir / canonical_job_id
    try:
        if job_root.is_symlink():
            job_root.unlink()
        else:
            shutil.rmtree(job_root)
    except FileNotFoundError:
        pass


def _resolved_inbox_root(settings: Settings) -> Path:
    if settings.download_inbox_dir is None:
        raise IntakeError("Download inbox is not configured")
    try:
        root = settings.download_inbox_dir.resolve(strict=True)
    except FileNotFoundError as exc:
        raise IntakeError("Download inbox folder does not exist") from exc
    if not root.is_dir():
        raise IntakeError("Download inbox path is not a folder")
    return root


def _safe_inbox_path(settings: Settings, relative_path: str) -> Path:
    if not relative_path or "\x00" in relative_path:
        raise IntakeError("Invalid inbox file")
    pure = PurePath(relative_path.replace("\\", "/"))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise IntakeError("Invalid inbox file")
    root = _resolved_inbox_root(settings)
    candidate = (root / Path(*pure.parts)).resolve(strict=True)
    if candidate == root or root not in candidate.parents:
        raise IntakeError("Inbox file is outside the configured download inbox")
    if not candidate.is_file() or candidate.is_symlink():
        raise IntakeError("Inbox item is not a regular file")
    return candidate


def _inbox_file_state(
    path: Path,
    settings: Settings,
    *,
    root: Path | None = None,
    now: float | None = None,
) -> InboxFile | None:
    suffix = path.suffix.lower()
    if suffix in IGNORED_INBOX_SUFFIXES or path.name.startswith("."):
        return None
    display_name = safe_display_name(path.name)
    extension = Path(display_name).suffix.lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        return None
    stat = path.stat()
    current = time.time() if now is None else now
    age = max(0.0, current - stat.st_mtime)
    ready = (
        stat.st_size >= settings.download_inbox_min_bytes
        and age >= settings.download_inbox_settle_seconds
    )
    reason = None
    if stat.st_size == 0:
        reason = "empty"
    elif stat.st_size < settings.download_inbox_min_bytes:
        reason = f"too small; expected at least {settings.download_inbox_min_bytes} bytes"
    elif not ready:
        reason = "still downloading"
    return InboxFile(
        relative_path=path.relative_to(root or _resolved_inbox_root(settings)).as_posix(),
        display_name=display_name,
        byte_size=stat.st_size,
        age_seconds=age,
        modified_at_epoch=stat.st_mtime,
        ready=ready,
        reason=reason,
    )


def list_download_inbox(settings: Settings) -> list[InboxFile]:
    root = _resolved_inbox_root(settings)
    paths = root.rglob("*") if settings.download_inbox_recursive else root.glob("*")
    files: list[InboxFile] = []
    for path in paths:
        if len(files) >= settings.download_inbox_max_files:
            break
        try:
            if not path.is_file() or path.is_symlink():
                continue
            item = _inbox_file_state(path, settings, root=root)
        except (OSError, ValueError):
            continue
        if item is not None:
            files.append(item)
    files.sort(key=lambda item: (not item.ready, item.display_name.lower()))
    return files


def stage_inbox_file(relative_path: str, settings: Settings) -> tuple[str, Path, str]:
    """Move or copy one trusted server-side inbox file into immutable job staging."""

    source = _safe_inbox_path(settings, relative_path)
    item = _inbox_file_state(source, settings, root=_resolved_inbox_root(settings))
    if item is None:
        raise IntakeError("Inbox file type is not supported")
    if not item.ready:
        raise IntakeError("Inbox file is not ready yet")
    if item.byte_size > settings.max_upload_bytes:
        raise IntakeError(f"File exceeds the configured {settings.max_upload_bytes} byte limit")

    job_id: str | None = None
    try:
        display_name = item.display_name
        extension = Path(display_name).suffix.lower()
        job_id = str(uuid.uuid4())
        incoming_dir = settings.jobs_dir / job_id / "incoming"
        incoming_dir.mkdir(mode=0o750, parents=True, exist_ok=False)
        final_path = incoming_dir / f"source{extension}"
        if settings.download_inbox_move_files:
            shutil.move(str(source), str(final_path))
        else:
            shutil.copy2(source, final_path)
        os.chmod(final_path, 0o640)
        return job_id, final_path, display_name
    except BaseException:
        if job_id is not None:
            cleanup_staged_job(settings, job_id)
        raise


async def stage_upload(upload: UploadFile, settings: Settings) -> tuple[str, Path, str]:
    """Stream one upload into an immutable, server-named job input."""

    job_id: str | None = None
    try:
        display_name = safe_display_name(upload.filename)
        extension = Path(display_name).suffix.lower()
        if extension not in ALLOWED_UPLOAD_EXTENSIONS:
            supported = ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))
            raise IntakeError(f"Unsupported upload type. Accepted extensions: {supported}")

        job_id = str(uuid.uuid4())
        incoming_dir = settings.jobs_dir / job_id / "incoming"
        incoming_dir.mkdir(mode=0o750, parents=True, exist_ok=False)
        final_path = incoming_dir / f"source{extension}"
        partial_path = incoming_dir / f".{uuid.uuid4().hex}.part"
        total = 0
        with partial_path.open("xb") as output:
            os.chmod(partial_path, 0o600)
            while chunk := await upload.read(settings.upload_chunk_bytes):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise IntakeError(
                        f"Upload exceeds the configured {settings.max_upload_bytes} byte limit"
                    )
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total == 0:
            raise IntakeError("The uploaded file was empty")
        os.rename(partial_path, final_path)
        os.chmod(final_path, 0o640)
        return job_id, final_path, display_name
    except BaseException:
        if job_id is not None:
            cleanup_staged_job(settings, job_id)
        raise
    finally:
        await upload.close()
