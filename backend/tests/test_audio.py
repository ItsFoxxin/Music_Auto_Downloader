from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from foxden_music import audio as audio_module
from foxden_music.audio import AudioInspectionError, _validate_audio_format, inspect_audio


@pytest.mark.parametrize(
    ("filename", "container", "codec"),
    [
        ("track.flac", "flac", "flac"),
        ("track.mp3", "mp3", "mp3"),
        ("track.m4a", "mov,mp4,m4a,3gp,3g2,mj2", "aac"),
        ("track.m4a", "mov,mp4,m4a,3gp,3g2,mj2", "alac"),
        ("track.aac", "aac", "aac"),
        ("track.opus", "ogg", "opus"),
        ("track.ogg", "ogg", "vorbis"),
        ("track.oga", "ogg", "opus"),
    ],
)
def test_supported_extensions_match_ffprobe_container_and_codec(
    filename: str,
    container: str,
    codec: str,
) -> None:
    _validate_audio_format(Path(filename), container, codec)


def test_inspection_rejects_audio_renamed_to_misleading_extension(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "renamed.flac"
    source.write_bytes(b"synthetic-mp3-payload")
    payload = {
        "streams": [
            {
                "codec_name": "mp3",
                "codec_type": "audio",
                "duration": "1.0",
                "sample_rate": "44100",
                "channels": 2,
            }
        ],
        "format": {"format_name": "mp3", "duration": "1.0", "bit_rate": "128000"},
    }
    monkeypatch.setattr(
        audio_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    with pytest.raises(AudioInspectionError, match=r"does not match the \.flac"):
        inspect_audio(source, settings)


@pytest.mark.parametrize(
    "extra_streams",
    [
        [{"codec_name": "h264", "codec_type": "video", "disposition": {"attached_pic": 0}}],
        [{"codec_name": "aac", "codec_type": "audio"}],
        [{"codec_name": "bin_data", "codec_type": "data"}],
    ],
)
def test_inspection_rejects_extra_active_streams(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_streams: list[dict[str, object]],
) -> None:
    source = tmp_path / "track.m4a"
    source.write_bytes(b"synthetic-m4a")
    payload = {
        "streams": [
            {
                "codec_name": "aac",
                "codec_type": "audio",
                "duration": "1.0",
                "sample_rate": "44100",
                "channels": 2,
            },
            *extra_streams,
        ],
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "1.0",
            "bit_rate": "128000",
        },
    }
    monkeypatch.setattr(
        audio_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    with pytest.raises(AudioInspectionError, match="stream"):
        inspect_audio(source, settings)


def test_inspection_allows_embedded_cover_stream(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "track.mp3"
    source.write_bytes(b"synthetic-mp3")
    payload = {
        "streams": [
            {
                "codec_name": "mp3",
                "codec_type": "audio",
                "duration": "1.0",
                "sample_rate": "44100",
                "channels": 2,
            },
            {
                "codec_name": "mjpeg",
                "codec_type": "video",
                "disposition": {"attached_pic": 1},
            },
        ],
        "format": {"format_name": "mp3", "duration": "1.0", "bit_rate": "128000"},
    }
    monkeypatch.setattr(
        audio_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    assert inspect_audio(source, settings).codec == "mp3"


def test_ffprobe_failure_does_not_expose_the_input_path(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "private-location.flac"
    source.write_bytes(b"not audio")
    monkeypatch.setattr(
        audio_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"{source}: Invalid data found when processing input",
        ),
    )

    with pytest.raises(AudioInspectionError) as caught:
        inspect_audio(source, settings)
    assert str(source) not in str(caught.value)
    assert str(caught.value) == "ffprobe rejected the audio content"
