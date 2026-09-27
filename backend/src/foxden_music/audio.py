from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import Settings


SUPPORTED_AUDIO_EXTENSIONS = frozenset({".flac", ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".oga"})

# ffprobe reports a comma-separated set of demuxer aliases for ISO BMFF. Keep
# these policies conservative so a renamed file cannot retain a misleading
# library extension. ALAC is valid in an M4A container and does not require a
# lossy transcode, so it is accepted alongside AAC.
_AUDIO_FORMAT_POLICIES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    ".flac": (frozenset({"flac"}), frozenset({"flac"})),
    ".mp3": (frozenset({"mp3"}), frozenset({"mp3"})),
    ".m4a": (
        frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}),
        frozenset({"aac", "alac"}),
    ),
    ".aac": (frozenset({"aac"}), frozenset({"aac"})),
    ".opus": (frozenset({"ogg"}), frozenset({"opus"})),
    ".ogg": (frozenset({"ogg"}), frozenset({"vorbis", "opus"})),
    ".oga": (frozenset({"ogg"}), frozenset({"vorbis", "opus"})),
}


class AudioInspectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AudioInspection:
    container: str
    codec: str
    duration_seconds: float
    bitrate: int | None
    sample_rate: int | None
    bit_depth: int | None
    channels: int | None
    file_size: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_int(value: object) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _duration(format_data: dict[str, Any], stream: dict[str, Any]) -> float:
    for value in (format_data.get("duration"), stream.get("duration")):
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration
    raise AudioInspectionError("ffprobe did not report a positive audio duration")


def _validate_audio_format(path: Path, container: str, codec: str) -> None:
    suffix = path.suffix.lower()
    policy = _AUDIO_FORMAT_POLICIES.get(suffix)
    if policy is None:
        raise AudioInspectionError("Audio filename extension is unsupported")
    allowed_containers, allowed_codecs = policy
    reported_containers = {
        value.strip().casefold() for value in container.split(",") if value.strip()
    }
    if not reported_containers.intersection(allowed_containers) or codec.casefold() not in allowed_codecs:
        raise AudioInspectionError(
            f"Audio content does not match the {suffix} filename extension"
        )


def inspect_audio(path: Path, settings: Settings) -> AudioInspection:
    """Inspect an audio file with ffprobe without invoking a shell."""

    if not path.is_file():
        raise AudioInspectionError("Audio input is not a regular file")
    command = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        (
            "format=format_name,duration,bit_rate,size:"
            "stream=codec_name,duration,bit_rate,sample_rate,bits_per_sample,"
            "bits_per_raw_sample,channels,codec_type:stream_disposition=attached_pic"
        ),
        "-of",
        "json",
        "-i",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.ffprobe_timeout_seconds,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise AudioInspectionError("ffprobe is not installed or FFPROBE_PATH is incorrect") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioInspectionError("ffprobe timed out while inspecting audio") from exc
    if result.returncode != 0:
        # ffprobe commonly includes the absolute input filename in stderr. That
        # path must not enter persisted job errors or the read-only JSON API.
        raise AudioInspectionError("ffprobe rejected the audio content")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AudioInspectionError("ffprobe returned invalid JSON") from exc
    streams = payload.get("streams") or []
    if not isinstance(streams, list) or any(not isinstance(item, dict) for item in streams):
        raise AudioInspectionError("ffprobe returned invalid stream information")
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    if len(audio_streams) != 1 or not audio_streams[0].get("codec_name"):
        raise AudioInspectionError("Exactly one supported audio stream is required")
    unexpected_streams = [
        item
        for item in streams
        if item.get("codec_type") != "audio"
        and not (
            item.get("codec_type") == "video"
            and isinstance(item.get("disposition"), dict)
            and item["disposition"].get("attached_pic") == 1
        )
    ]
    if unexpected_streams:
        raise AudioInspectionError("Audio files must not contain video, data, or other active streams")
    stream = audio_streams[0]
    if not stream.get("codec_name"):
        raise AudioInspectionError("No supported audio stream was found")
    format_data = payload.get("format") or {}
    container = str(format_data.get("format_name") or "unknown")
    codec = str(stream["codec_name"])
    _validate_audio_format(path, container, codec)
    bit_depth = _optional_int(stream.get("bits_per_raw_sample")) or _optional_int(
        stream.get("bits_per_sample")
    )
    return AudioInspection(
        container=container[:100],
        codec=codec[:100],
        duration_seconds=_duration(format_data, stream),
        bitrate=_optional_int(stream.get("bit_rate")) or _optional_int(format_data.get("bit_rate")),
        sample_rate=_optional_int(stream.get("sample_rate")),
        bit_depth=bit_depth,
        channels=_optional_int(stream.get("channels")),
        file_size=path.stat().st_size,
        sha256=sha256_file(path),
    )


def is_supported_audio_path(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
