from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.id3 import ID3

from foxden_music import metadata as metadata_module
from foxden_music.metadata import (
    MAX_TAG_BLOCK_BYTES,
    MAX_TEXT_TAG_CHARACTERS,
    MetadataError,
    TagHints,
    _clean_text,
    metadata_from_hints,
    parse_number_pair,
    read_tag_hints,
)
from foxden_music.pipeline import _release_track_rows


def test_track_and_disc_number_pairs() -> None:
    assert parse_number_pair("7/12") == (7, 12)
    assert parse_number_pair(" 02 / 09 ") == (2, 9)
    assert parse_number_pair("3") == (3, None)
    assert parse_number_pair("side A") == (None, None)


@pytest.mark.parametrize(
    ("track_total_key", "disc_total_key"),
    [("tracktotal", "disctotal"), ("totaltracks", "totaldiscs")],
)
def test_separate_track_and_disc_totals_are_read(
    monkeypatch: pytest.MonkeyPatch,
    track_total_key: str,
    disc_total_key: str,
) -> None:
    easy_audio = SimpleNamespace(
        tags={
            "tracknumber": ["2"],
            track_total_key: ["14"],
            "discnumber": ["1"],
            disc_total_key: ["2"],
        }
    )

    def fake_mutagen_file(_path: Path, *, easy: bool):
        return easy_audio if easy else object()

    monkeypatch.setattr(metadata_module.mutagen, "File", fake_mutagen_file)
    hints = read_tag_hints(Path("synthetic.flac"))

    assert (hints.track_number, hints.track_total) == (2, 14)
    assert (hints.disc_number, hints.disc_total) == (1, 2)


def test_oversized_id3_block_is_rejected_before_mutagen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "huge.mp3"
    declared_size = MAX_TAG_BLOCK_BYTES + 1
    value = declared_size - 10
    synchsafe = bytes(
        ((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F)
    )
    source.write_bytes(b"ID3\x04\x00\x00" + synchsafe)
    called = False

    def fake_mutagen_file(*args, **kwargs):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(metadata_module.mutagen, "File", fake_mutagen_file)
    with pytest.raises(MetadataError, match="safe metadata limit"):
        read_tag_hints(source)
    assert called is False


def test_oversized_text_tag_is_rejected() -> None:
    with pytest.raises(MetadataError, match="safe length"):
        _clean_text("x" * (MAX_TEXT_TAG_CHARACTERS + 1))


def test_incoming_metadata_normalization_preserves_unicode() -> None:
    hints = TagHints(
        title="神메뉴",
        artist="Stray Kids",
        album_artist="Stray Kids",
        album="GO生",
        track_number=2,
        track_total=14,
        disc_number=1,
        disc_total=2,
        release_date="2020-06-17",
        isrc="US-ABC-20-12345",
    )
    result = metadata_from_hints(hints, fallback_title="ignored", track_total=14)
    assert result.title == "神메뉴"
    assert result.album == "GO生"
    assert result.track_number == 2
    assert result.track_total == 14
    assert result.disc_total == 2
    assert result.release_date == "2020-06-17"
    assert result.year == 2020


def test_incoming_year_is_written_as_release_date_when_date_is_missing() -> None:
    result = metadata_from_hints(
        TagHints(
            title="Track",
            artist="Artist",
            album="Album",
            year=2024,
        ),
        fallback_title="ignored",
        track_total=1,
    )

    assert result.release_date == "2024"
    assert result.year == 2024


def test_all_supported_tag_writers_apply_release_date_and_album_numbers() -> None:
    metadata = metadata_module.NormalizedTrackMetadata(
        title="Track",
        artist="Artist",
        album_artist="Album Artist",
        album="Album",
        track_number=2,
        track_total=11,
        disc_number=1,
        disc_total=1,
        release_date="2024-07-19",
        year=2024,
    )

    class MappingAudio(dict):
        def save(self, **_kwargs) -> None:
            self.saved = True

    flac = MappingAudio()
    metadata_module._write_flac(flac, metadata, None)
    assert flac["date"] == "2024-07-19"
    assert flac["tracktotal"] == "11"

    ogg = MappingAudio()
    metadata_module._write_ogg(ogg, metadata, None)
    assert ogg["date"] == "2024-07-19"
    assert ogg["totaltracks"] == "11"

    class TaggedAudio:
        def __init__(self, tags) -> None:
            self.tags = tags

        def save(self, **_kwargs) -> None:
            self.saved = True

    mp4 = TaggedAudio({})
    metadata_module._write_mp4(mp4, metadata, None)
    assert mp4.tags["\xa9day"] == ["2024-07-19"]
    assert mp4.tags["trkn"] == [(2, 11)]

    mp3 = TaggedAudio(ID3())
    metadata_module._write_mp3(mp3, metadata, None)
    assert str(mp3.tags.getall("TDRC")[0].text[0]) == "2024-07-19"
    assert str(mp3.tags.getall("TRCK")[0].text[0]) == "2/11"


def test_musicbrainz_multi_disc_rows_keep_positions_and_recording_ids() -> None:
    payload = {
        "media": [
            {
                "position": 1,
                "track-count": 1,
                "tracks": [
                    {
                        "position": 1,
                        "title": "첫 번째",
                        "recording": {"id": "rec-1", "isrcs": ["KRA001"]},
                    }
                ],
            },
            {
                "position": 2,
                "track-count": 1,
                "tracks": [
                    {
                        "position": 1,
                        "title": "Deuxième",
                        "recording": {"id": "rec-2", "isrcs": ["FRB002"]},
                    }
                ],
            },
        ]
    }
    rows = _release_track_rows(payload)
    assert [(row["disc_number"], row["track_number"]) for row in rows] == [(1, 1), (2, 1)]
    assert [row["disc_total"] for row in rows] == [2, 2]
    assert rows[0]["recording_id"] == "rec-1"
    assert rows[1]["title"] == "Deuxième"
