from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis


class MetadataError(RuntimeError):
    pass


class UnsupportedTaggingError(MetadataError):
    pass


MAX_TAG_BLOCK_BYTES = 32 * 1024**2
MAX_TEXT_TAG_CHARACTERS = 16_384
MAX_EMBEDDED_ARTWORK_BYTES = 25 * 1024**2


def _reject_large_tag_blocks(path: Path) -> None:
    """Stream container headers and reject metadata Mutagen should not materialize."""

    try:
        file_size = path.stat().st_size
    except FileNotFoundError:
        # Keeps isolated/mock callers useful; normal pipeline sources always exist.
        return
    suffix = path.suffix.casefold()
    with path.open("rb") as source:
        if suffix == ".mp3":
            header = source.read(10)
            if len(header) == 10 and header[:3] == b"ID3":
                size_bytes = header[6:10]
                if any(byte & 0x80 for byte in size_bytes):
                    raise MetadataError("The ID3 header is invalid")
                tag_size = 0
                for byte in size_bytes:
                    tag_size = (tag_size << 7) | byte
                tag_size += 10
                if tag_size > MAX_TAG_BLOCK_BYTES:
                    raise MetadataError("The ID3 tag block exceeds the safe metadata limit")
            return

        if suffix == ".flac":
            if source.read(4) != b"fLaC":
                return
            total = 4
            while True:
                header = source.read(4)
                if len(header) != 4:
                    raise MetadataError("The FLAC metadata headers are truncated")
                block_size = int.from_bytes(header[1:4], "big")
                total += 4 + block_size
                if total > MAX_TAG_BLOCK_BYTES:
                    raise MetadataError("The FLAC metadata blocks exceed the safe limit")
                source.seek(block_size, 1)
                if header[0] & 0x80:
                    return

        if suffix in {".m4a", ".mp4"}:
            offset = 0
            while offset + 8 <= file_size:
                source.seek(offset)
                header = source.read(8)
                atom_size = int.from_bytes(header[:4], "big")
                atom_type = header[4:8]
                header_size = 8
                if atom_size == 1:
                    extended = source.read(8)
                    if len(extended) != 8:
                        raise MetadataError("The MP4 atom header is truncated")
                    atom_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif atom_size == 0:
                    atom_size = file_size - offset
                if atom_size < header_size or offset + atom_size > file_size:
                    raise MetadataError("The MP4 atom sizes are invalid")
                if atom_type == b"moov" and atom_size > MAX_TAG_BLOCK_BYTES:
                    raise MetadataError("The MP4 metadata/index atom exceeds the safe limit")
                offset += atom_size
            return

        if suffix in {".ogg", ".oga", ".opus"}:
            packet_count = 0
            header_packets_needed: int | None = None
            packet_prefix = bytearray()
            header_bytes = 0
            while header_packets_needed is None or packet_count < header_packets_needed:
                fixed = source.read(27)
                if len(fixed) != 27 or fixed[:4] != b"OggS":
                    raise MetadataError("The Ogg headers are invalid or truncated")
                segment_table = source.read(fixed[26])
                if len(segment_table) != fixed[26]:
                    raise MetadataError("The Ogg lacing table is truncated")
                for segment_size in segment_table:
                    data = source.read(segment_size)
                    if len(data) != segment_size:
                        raise MetadataError("The Ogg header packet is truncated")
                    header_bytes += segment_size
                    if header_bytes > MAX_TAG_BLOCK_BYTES:
                        raise MetadataError("The Ogg header/tag packets exceed the safe limit")
                    if len(packet_prefix) < 8:
                        packet_prefix.extend(data[: 8 - len(packet_prefix)])
                    if segment_size < 255:
                        packet_count += 1
                        if packet_count == 1:
                            if packet_prefix.startswith(b"\x01vorbis"):
                                header_packets_needed = 3
                            elif packet_prefix.startswith(b"OpusHead"):
                                header_packets_needed = 2
                            else:
                                return
                        packet_prefix.clear()
                        if header_packets_needed is not None and packet_count >= header_packets_needed:
                            return
            return


@dataclass(slots=True)
class TagHints:
    title: str | None = None
    artist: str | None = None
    album_artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    release_date: str | None = None
    year: int | None = None
    isrc: str | None = None
    musicbrainz_recording_id: str | None = None
    musicbrainz_release_id: str | None = None
    embedded_artwork: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class NormalizedTrackMetadata:
    title: str
    artist: str
    album_artist: str
    album: str
    track_number: int
    track_total: int
    disc_number: int = 1
    disc_total: int = 1
    release_date: str | None = None
    year: int | None = None
    isrc: str | None = None
    musicbrainz_recording_id: str | None = None
    musicbrainz_release_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = text.replace("\x00", "").strip()
    if len(text) > MAX_TEXT_TAG_CHARACTERS:
        raise MetadataError("An embedded text tag exceeds the safe length limit")
    return text or None


def _first(tags: Mapping[str, Any], *keys: str) -> str | None:
    lowered = {str(key).casefold(): value for key, value in tags.items()}
    for key in keys:
        value = lowered.get(key.casefold())
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            value = value[0] if value else None
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return None


NUMBER_PAIR = re.compile(r"^\s*(\d+)(?:\s*/\s*(\d+))?")


def parse_number_pair(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    match = NUMBER_PAIR.match(value)
    if not match:
        return None, None
    first = int(match.group(1))
    total = int(match.group(2)) if match.group(2) else None
    return first or None, total or None


def _separate_total(tags: Mapping[str, Any], *keys: str) -> int | None:
    """Read the first positive value from a standalone total-count tag."""

    total, _ = parse_number_pair(_first(tags, *keys))
    return total


def _has_embedded_artwork(raw_audio: Any) -> bool:
    if isinstance(raw_audio, FLAC):
        return bool(raw_audio.pictures)
    if isinstance(raw_audio, MP3):
        return bool(raw_audio.tags and raw_audio.tags.getall("APIC"))
    if isinstance(raw_audio, MP4):
        return bool(raw_audio.tags and raw_audio.tags.get("covr"))
    if isinstance(raw_audio, (OggVorbis, OggOpus)):
        return bool(raw_audio.tags and raw_audio.tags.get("metadata_block_picture"))
    return False


def read_tag_hints(path: Path) -> TagHints:
    _reject_large_tag_blocks(path)
    try:
        easy_audio = mutagen.File(path, easy=True)
        raw_audio = mutagen.File(path, easy=False)
    except mutagen.MutagenError as exc:
        raise MetadataError("Mutagen could not read this audio file") from exc
    tags: Mapping[str, Any] = easy_audio.tags if easy_audio is not None and easy_audio.tags else {}
    track_number, paired_track_total = parse_number_pair(_first(tags, "tracknumber", "track"))
    disc_number, paired_disc_total = parse_number_pair(_first(tags, "discnumber", "disc"))
    track_total = paired_track_total or _separate_total(tags, "tracktotal", "totaltracks")
    disc_total = paired_disc_total or _separate_total(tags, "disctotal", "totaldiscs")
    release_date = _first(tags, "date", "originaldate", "year")
    year_match = re.match(r"^(\d{4})", release_date or "")
    return TagHints(
        title=_first(tags, "title"),
        artist=_first(tags, "artist"),
        album_artist=_first(tags, "albumartist", "album artist"),
        album=_first(tags, "album"),
        track_number=track_number,
        track_total=track_total,
        disc_number=disc_number,
        disc_total=disc_total,
        release_date=release_date,
        year=int(year_match.group(1)) if year_match else None,
        isrc=_first(tags, "isrc"),
        musicbrainz_recording_id=_first(
            tags, "musicbrainz_trackid", "musicbrainz recording id", "musicbrainz_recordingid"
        ),
        musicbrainz_release_id=_first(
            tags, "musicbrainz_albumid", "musicbrainz release id", "musicbrainz_releaseid"
        ),
        embedded_artwork=_has_embedded_artwork(raw_audio),
    )


def serialize_hints(hints: TagHints) -> str:
    return json.dumps(hints.as_dict(), ensure_ascii=False, sort_keys=True)


def deserialize_hints(payload: str) -> TagHints:
    return TagHints(**json.loads(payload))


def extract_embedded_artwork(path: Path) -> bytes | None:
    _reject_large_tag_blocks(path)
    try:
        audio = mutagen.File(path, easy=False)
    except mutagen.MutagenError:
        return None
    if isinstance(audio, FLAC) and audio.pictures:
        front = next((picture for picture in audio.pictures if picture.type == 3), audio.pictures[0])
        return _bounded_artwork(front.data)
    if isinstance(audio, MP3) and audio.tags:
        pictures = audio.tags.getall("APIC")
        if pictures:
            front = next((picture for picture in pictures if picture.type == 3), pictures[0])
            return _bounded_artwork(front.data)
    if isinstance(audio, MP4) and audio.tags and audio.tags.get("covr"):
        return _bounded_artwork(audio.tags["covr"][0])
    if isinstance(audio, (OggVorbis, OggOpus)) and audio.tags:
        values = audio.tags.get("metadata_block_picture") or []
        if values:
            try:
                encoded = values[0]
                if len(encoded) > (MAX_EMBEDDED_ARTWORK_BYTES * 4 // 3) + 16:
                    return None
                return _bounded_artwork(Picture(base64.b64decode(encoded, validate=True)).data)
            except (ValueError, TypeError):
                return None
    return None


def _bounded_artwork(value: object) -> bytes | None:
    data = bytes(value)
    return data if len(data) <= MAX_EMBEDDED_ARTWORK_BYTES else None


def _number_pair(number: int, total: int) -> str:
    return f"{number}/{total}"


def _picture(artwork: bytes) -> Picture:
    picture = Picture()
    picture.type = 3
    picture.mime = "image/jpeg"
    picture.desc = "Front cover"
    picture.data = artwork
    return picture


def _write_flac(audio: FLAC, metadata: NormalizedTrackMetadata, artwork: bytes | None) -> None:
    values: dict[str, str | None] = {
        "title": metadata.title,
        "artist": metadata.artist,
        "albumartist": metadata.album_artist,
        "album": metadata.album,
        "tracknumber": str(metadata.track_number),
        "tracktotal": str(metadata.track_total),
        "discnumber": str(metadata.disc_number),
        "disctotal": str(metadata.disc_total),
        "date": metadata.release_date,
        "isrc": metadata.isrc,
        "musicbrainz_trackid": metadata.musicbrainz_recording_id,
        "musicbrainz_albumid": metadata.musicbrainz_release_id,
    }
    for key, value in values.items():
        if value:
            audio[key] = value
    if artwork:
        audio.clear_pictures()
        audio.add_picture(_picture(artwork))
    audio.save()


def _replace_id3(audio: MP3, frame_id: str, frame: Any) -> None:
    assert audio.tags is not None
    audio.tags.delall(frame_id)
    audio.tags.add(frame)


def _write_mp3(audio: MP3, metadata: NormalizedTrackMetadata, artwork: bytes | None) -> None:
    if audio.tags is None:
        audio.add_tags()
    _replace_id3(audio, "TIT2", TIT2(encoding=3, text=[metadata.title]))
    _replace_id3(audio, "TPE1", TPE1(encoding=3, text=[metadata.artist]))
    _replace_id3(audio, "TPE2", TPE2(encoding=3, text=[metadata.album_artist]))
    _replace_id3(audio, "TALB", TALB(encoding=3, text=[metadata.album]))
    _replace_id3(audio, "TRCK", TRCK(encoding=3, text=[_number_pair(metadata.track_number, metadata.track_total)]))
    _replace_id3(audio, "TPOS", TPOS(encoding=3, text=[_number_pair(metadata.disc_number, metadata.disc_total)]))
    if metadata.release_date:
        _replace_id3(audio, "TDRC", TDRC(encoding=3, text=[metadata.release_date]))
    if metadata.isrc:
        _replace_id3(audio, "TSRC", TSRC(encoding=3, text=[metadata.isrc]))
    assert audio.tags is not None
    for description, value in (
        ("MusicBrainz Recording Id", metadata.musicbrainz_recording_id),
        ("MusicBrainz Album Id", metadata.musicbrainz_release_id),
    ):
        existing = [frame for frame in audio.tags.getall("TXXX") if frame.desc == description]
        for frame in existing:
            audio.tags.delall(frame.HashKey)
        if value:
            audio.tags.add(TXXX(encoding=3, desc=description, text=[value]))
    if artwork:
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Front cover", data=artwork))
    audio.save(v2_version=4)


def _write_mp4(audio: MP4, metadata: NormalizedTrackMetadata, artwork: bytes | None) -> None:
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags["\xa9nam"] = [metadata.title]
    audio.tags["\xa9ART"] = [metadata.artist]
    audio.tags["aART"] = [metadata.album_artist]
    audio.tags["\xa9alb"] = [metadata.album]
    audio.tags["trkn"] = [(metadata.track_number, metadata.track_total)]
    audio.tags["disk"] = [(metadata.disc_number, metadata.disc_total)]
    if metadata.release_date:
        audio.tags["\xa9day"] = [metadata.release_date]
    freeform = (
        ("ISRC", metadata.isrc),
        ("MusicBrainz Track Id", metadata.musicbrainz_recording_id),
        ("MusicBrainz Album Id", metadata.musicbrainz_release_id),
    )
    for name, value in freeform:
        key = f"----:com.apple.iTunes:{name}"
        if value:
            audio.tags[key] = [value.encode("utf-8")]
    if artwork:
        audio.tags["covr"] = [MP4Cover(artwork, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def _write_ogg(
    audio: OggVorbis | OggOpus,
    metadata: NormalizedTrackMetadata,
    artwork: bytes | None,
) -> None:
    values: dict[str, str | None] = {
        "title": metadata.title,
        "artist": metadata.artist,
        "albumartist": metadata.album_artist,
        "album": metadata.album,
        "tracknumber": str(metadata.track_number),
        "totaltracks": str(metadata.track_total),
        "discnumber": str(metadata.disc_number),
        "totaldiscs": str(metadata.disc_total),
        "date": metadata.release_date,
        "isrc": metadata.isrc,
        "musicbrainz_trackid": metadata.musicbrainz_recording_id,
        "musicbrainz_albumid": metadata.musicbrainz_release_id,
    }
    for key, value in values.items():
        if value:
            audio[key] = value
    if artwork:
        audio["metadata_block_picture"] = [base64.b64encode(_picture(artwork).write()).decode("ascii")]
    audio.save()


def write_tags(path: Path, metadata: NormalizedTrackMetadata, artwork: bytes | None = None) -> None:
    """Write known fields while retaining unrelated valuable source tags."""

    try:
        audio = mutagen.File(path, easy=False)
    except mutagen.MutagenError as exc:
        raise MetadataError("Mutagen could not open the working audio file") from exc
    if isinstance(audio, FLAC):
        _write_flac(audio, metadata, artwork)
    elif isinstance(audio, MP3):
        _write_mp3(audio, metadata, artwork)
    elif isinstance(audio, MP4):
        _write_mp4(audio, metadata, artwork)
    elif isinstance(audio, (OggVorbis, OggOpus)):
        _write_ogg(audio, metadata, artwork)
    else:
        raise UnsupportedTaggingError(
            f"Tag writing is not supported for {path.suffix.lower() or 'this container'}; the source was preserved"
        )


def metadata_from_hints(
    hints: TagHints,
    *,
    fallback_title: str,
    track_total: int,
    disc_total: int = 1,
) -> NormalizedTrackMetadata:
    missing = [
        field
        for field, value in (
            ("title", hints.title or fallback_title),
            ("artist", hints.artist),
            ("album", hints.album),
        )
        if not value
    ]
    if missing:
        raise MetadataError(f"Incoming metadata is missing required fields: {', '.join(missing)}")
    title = hints.title or fallback_title
    artist = hints.artist or ""
    release_date = hints.release_date or (str(hints.year) if hints.year else None)
    year_match = re.match(r"^(\d{4})", release_date or "")
    return NormalizedTrackMetadata(
        title=title,
        artist=artist,
        album_artist=hints.album_artist or artist,
        album=hints.album or "",
        track_number=hints.track_number or 1,
        track_total=hints.track_total or track_total,
        disc_number=hints.disc_number or 1,
        disc_total=hints.disc_total or disc_total,
        release_date=release_date,
        year=int(year_match.group(1)) if year_match else hints.year,
        isrc=hints.isrc,
        musicbrainz_recording_id=hints.musicbrainz_recording_id,
        musicbrainz_release_id=hints.musicbrainz_release_id,
    )
