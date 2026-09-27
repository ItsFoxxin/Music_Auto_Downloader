from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path, PurePath

from fastapi import UploadFile

from .audio import SUPPORTED_AUDIO_EXTENSIONS
from .config import Settings


class IntakeError(ValueError):
    pass


ALLOWED_UPLOAD_EXTENSIONS = SUPPORTED_AUDIO_EXTENSIONS | frozenset({".zip"})


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
