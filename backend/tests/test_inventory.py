from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from sqlalchemy import func, select

from foxden_music import inventory
from foxden_music.audio import AudioInspection, AudioInspectionError
from foxden_music.enums import LibraryScanState
from foxden_music.metadata import TagHints
from foxden_music.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryInventoryTrack,
    LibraryScanRun,
)


def _write(root: Path, relative_path: str, content: bytes) -> Path:
    destination = root / Path(*relative_path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return destination


def _install_fake_readers(
    monkeypatch,
    settings,
    specifications: dict[str, dict[str, object]],
    calls: list[str],
) -> None:
    def relative(path: Path) -> str:
        return path.relative_to(settings.music_dir).as_posix()

    def fake_inspect(path: Path, _settings) -> AudioInspection:
        name = relative(path)
        calls.append(name)
        specification = specifications[name]
        if specification.get("error"):
            raise AudioInspectionError("synthetic inspection failure")
        payload = path.read_bytes()
        codec = str(specification.get("codec", "flac"))
        container = {
            "flac": "flac",
            "mp3": "mp3",
            "aac": "mov,mp4,m4a,3gp,3g2,mj2",
            "alac": "mov,mp4,m4a,3gp,3g2,mj2",
            "opus": "ogg",
            "vorbis": "ogg",
        }[codec]
        return AudioInspection(
            container=container,
            codec=codec,
            duration_seconds=float(specification.get("duration", 180.0)),
            bitrate=specification.get("bitrate"),
            sample_rate=44_100,
            bit_depth=16 if codec == "flac" else None,
            channels=2,
            file_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def fake_tags(path: Path) -> TagHints:
        specification = specifications[relative(path)]
        tags = specification.get("tags")
        assert tags is None or isinstance(tags, TagHints)
        return tags or TagHints()

    monkeypatch.setattr(inventory, "inspect_audio", fake_inspect)
    monkeypatch.setattr(inventory, "read_tag_hints", fake_tags)


def test_scan_indexes_real_files_quality_health_duplicates_and_unicode(
    settings,
    database,
    monkeypatch,
) -> None:
    specifications: dict[str, dict[str, object]] = {
        "Path Artist/Path Album/01.flac": {
            "codec": "flac",
            "tags": TagHints(
                title="첫 번째 노래",
                artist="여우와 친구들",
                album_artist="여우와 친구들",
                album="밤의 신호",
                track_number=1,
                track_total=2,
                year=2026,
                musicbrainz_recording_id="11111111-1111-1111-1111-111111111111",
            ),
        },
        "Path Artist/Path Album/02.mp3": {
            "codec": "mp3",
            "bitrate": 320_000,
            "tags": TagHints(
                title="Deuxième chanson",
                artist="여우와 친구들",
                album_artist="여우와 친구들",
                album="밤의 신호",
                track_number=2,
                track_total=2,
                year=2026,
            ),
        },
        "Fallback Artist/Incomplete/01.mp3": {
            "codec": "mp3",
            "bitrate": 192_000,
            "tags": TagHints(),
        },
        "Copy Artist/Copy Album/01.flac": {
            "codec": "flac",
            "tags": TagHints(
                title="A legitimate second placement",
                artist="Copy Artist",
                album_artist="Copy Artist",
                album="Copy Album",
                track_number=1,
            ),
        },
        "Broken Artist/Broken Album/01.ogg": {
            "codec": "vorbis",
            "error": True,
            "tags": TagHints(
                title="Broken audio",
                artist="Broken Artist",
                album_artist="Broken Artist",
                album="Broken Album",
                track_number=1,
            ),
        },
    }
    duplicate_payload = b"same-audio-payload"
    _write(settings.music_dir, "Path Artist/Path Album/01.flac", duplicate_payload)
    _write(settings.music_dir, "Path Artist/Path Album/02.mp3", b"mp3-320")
    _write(settings.music_dir, "Fallback Artist/Incomplete/01.mp3", b"mp3-low")
    _write(settings.music_dir, "Copy Artist/Copy Album/01.flac", duplicate_payload)
    _write(settings.music_dir, "Broken Artist/Broken Album/01.ogg", b"bad-ogg")
    _write(settings.music_dir, "Path Artist/Path Album/cover.jpg", b"artwork")
    _write(settings.music_dir, ".imports/ignored/hidden.flac", b"must-not-be-read")
    calls: list[str] = []
    _install_fake_readers(monkeypatch, settings, specifications, calls)

    outside = _write(settings.config_dir, "outside.flac", b"outside")
    linked = settings.music_dir / "linked.flac"
    try:
        linked.symlink_to(outside)
    except OSError:
        pass

    scan = inventory.scan_library_now(database, settings=settings, reason="test")
    assert scan.state == LibraryScanState.COMPLETE.value
    assert scan.files_discovered == 5
    assert scan.files_inspected == 4
    assert scan.files_failed == 1
    assert sorted(calls) == sorted(specifications)

    with database.session() as session:
        statistics = inventory.inventory_statistics(session)
        assert statistics["library"] == {
            "artists": 4,
            "albums": 4,
            "tracks": 5,
            "bytes": sum(
                len(path.read_bytes())
                for path in settings.music_dir.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and ".imports" not in path.parts
                and path.suffix.lower() in {".flac", ".mp3", ".ogg"}
            ),
            "duration_seconds": 720.0,
        }
        assert statistics["quality"] == {
            "flac": 2,
            "mp3_320": 1,
            "mp3_below_320": 1,
            "aac_m4a": 0,
            "opus_ogg": 0,
            "unknown": 1,
            "lower_quality": 1,
        }
        assert statistics["health"]["missing_metadata"] == 1
        assert statistics["health"]["missing_title"] == 1
        assert statistics["health"]["missing_artist"] == 1
        assert statistics["health"]["missing_album"] == 1
        assert statistics["health"]["missing_track_number"] == 1
        assert statistics["health"]["failed_inspections"] == 1
        assert statistics["health"]["duplicate_hash_groups"] == 1
        assert statistics["health"]["duplicate_hash_tracks"] == 2
        assert statistics["health"]["possible_duplicates"] == 2
        assert statistics["health"]["low_quality_files"] == 1
        assert statistics["health"]["missing_artwork"] == 3
        assert statistics["storage"]["library_bytes"] is not None
        assert statistics["storage"]["music_filesystem_free_bytes"] is not None

        artists = inventory.list_artists(session)
        unicode_artist = next(artist for artist in artists if artist.canonical_name == "여우와 친구들")
        assert unicode_artist.album_count == 1
        assert unicode_artist.track_count == 2
        albums = inventory.list_albums(session, artist_id=unicode_artist.id)
        assert [(album.title, album.artwork_present) for album in albums] == [("밤의 신호", True)]
        tracks = inventory.album_tracks(session, albums[0].id)
        assert [track.title for track in tracks] == ["첫 번째 노래", "Deuxième chanson"]


def test_incremental_scan_reuses_unchanged_files_and_removes_only_after_success(
    settings,
    database,
    monkeypatch,
) -> None:
    specifications: dict[str, dict[str, object]] = {
        "Artist/Album/01.flac": {
            "codec": "flac",
            "tags": TagHints(
                title="One",
                artist="Artist",
                album_artist="Artist",
                album="Album",
                track_number=1,
            ),
        },
        "Artist/Album/02.flac": {
            "codec": "flac",
            "tags": TagHints(
                title="Two",
                artist="Artist",
                album_artist="Artist",
                album="Album",
                track_number=2,
            ),
        },
    }
    first_path = _write(settings.music_dir, "Artist/Album/01.flac", b"one")
    second_path = _write(settings.music_dir, "Artist/Album/02.flac", b"two")
    calls: list[str] = []
    _install_fake_readers(monkeypatch, settings, specifications, calls)

    first = inventory.scan_library_now(database, settings=settings, reason="first")
    assert (first.files_inspected, first.files_reused, first.files_failed) == (2, 0, 0)
    assert len(calls) == 2
    with database.session() as session:
        stable_id = session.scalar(
            select(LibraryInventoryTrack.id).where(
                LibraryInventoryTrack.relative_path == "Artist/Album/02.flac"
            )
        )

    second = inventory.scan_library_now(database, settings=settings, reason="second")
    assert (second.files_inspected, second.files_reused, second.files_failed) == (0, 2, 0)
    assert len(calls) == 2

    original_mtime = first_path.stat().st_mtime_ns
    first_path.write_bytes(b"one-has-changed")
    os.utime(first_path, ns=(original_mtime + 10_000_000, original_mtime + 10_000_000))
    third = inventory.scan_library_now(database, settings=settings, reason="third")
    assert (third.files_inspected, third.files_reused, third.files_failed) == (1, 1, 0)
    assert calls.count("Artist/Album/01.flac") == 2
    assert calls.count("Artist/Album/02.flac") == 1
    with database.session() as session:
        assert session.scalar(
            select(LibraryInventoryTrack.id).where(
                LibraryInventoryTrack.relative_path == "Artist/Album/02.flac"
            )
        ) == stable_id

    second_path.unlink()
    # Even after a completed upsert batch, a later scan failure must not run the
    # stale-row deletion that would remove a file the failed scan did not retain.
    original_persist_batch = inventory._persist_inspected_batch
    previous_mtime = first_path.stat().st_mtime_ns
    first_path.write_bytes(b"one-changed-again")
    os.utime(first_path, ns=(previous_mtime + 10_000_000, previous_mtime + 10_000_000))

    def fail_after_one_batch(*args, **kwargs) -> None:
        original_persist_batch(*args, **kwargs)
        raise inventory.InventoryScanError("synthetic batch failure")

    monkeypatch.setattr(inventory, "_persist_inspected_batch", fail_after_one_batch)
    failed_batch = inventory.scan_library_now(database, settings=settings, reason="failed-batch")
    assert failed_batch.state == LibraryScanState.FAILED.value
    with database.session() as session:
        assert inventory.library_counts(session)["tracks"] == 2

    monkeypatch.setattr(inventory, "_persist_inspected_batch", original_persist_batch)
    fourth = inventory.scan_library_now(database, settings=settings, reason="fourth")
    # The failed publication did not leak the changed file's size, mtime, or hash,
    # so the next successful scan must inspect it again.
    assert (fourth.files_discovered, fourth.files_inspected, fourth.files_reused) == (1, 1, 0)
    with database.session() as session:
        assert inventory.library_counts(session)["tracks"] == 1
        assert session.scalar(select(func.count(LibraryAlbum.id))) == 1
        assert session.scalar(select(func.count(LibraryArtist.id))) == 1

    # A fatal walk failure is recorded but leaves the last complete inventory intact.
    monkeypatch.setattr(
        inventory,
        "_discover_audio_files",
        lambda _root: (_ for _ in ()).throw(inventory.InventoryScanError("synthetic walk failure")),
    )
    failed = inventory.scan_library_now(database, settings=settings, reason="failed")
    assert failed.state == LibraryScanState.FAILED.value
    with database.session() as session:
        assert inventory.library_counts(session)["tracks"] == 1


def test_failed_batch_publication_rolls_back_tracks_metadata_and_aggregates(
    settings,
    database,
    monkeypatch,
) -> None:
    changed_relative = "Original Artist/Changed Album/01.flac"
    removed_relative = "Removed Artist/Removed Album/01.flac"
    specifications: dict[str, dict[str, object]] = {
        changed_relative: {
            "codec": "flac",
            "tags": TagHints(
                title="Original title",
                artist="Original Artist",
                album_artist="Original Artist",
                album="Changed Album",
                track_number=1,
            ),
        },
        removed_relative: {
            "codec": "flac",
            "tags": TagHints(
                title="Still in the completed snapshot",
                artist="Removed Artist",
                album_artist="Removed Artist",
                album="Removed Album",
                track_number=1,
            ),
        },
    }
    changed_path = _write(settings.music_dir, changed_relative, b"original bytes")
    removed_path = _write(settings.music_dir, removed_relative, b"removed album bytes")
    _install_fake_readers(monkeypatch, settings, specifications, [])

    first = inventory.scan_library_now(database, settings=settings, reason="atomic-first")
    assert first.state == LibraryScanState.COMPLETE.value

    def published_inventory() -> dict[str, object]:
        with database.session() as session:
            return {
                "tracks": [
                    tuple(row)
                    for row in session.execute(
                        select(
                            LibraryInventoryTrack.relative_path,
                            LibraryInventoryTrack.title,
                            LibraryInventoryTrack.artist,
                            LibraryInventoryTrack.album_title,
                            LibraryInventoryTrack.file_size,
                            LibraryInventoryTrack.sha256,
                            LibraryInventoryTrack.scan_generation,
                        ).order_by(LibraryInventoryTrack.relative_path)
                    ).all()
                ],
                "albums": [
                    tuple(row)
                    for row in session.execute(
                        select(
                            LibraryAlbum.relative_path,
                            LibraryAlbum.album_artist,
                            LibraryAlbum.title,
                            LibraryAlbum.track_count,
                            LibraryAlbum.total_bytes,
                            LibraryAlbum.codec_summary,
                            LibraryAlbum.scan_generation,
                        ).order_by(LibraryAlbum.relative_path)
                    ).all()
                ],
                "artists": [
                    tuple(row)
                    for row in session.execute(
                        select(
                            LibraryArtist.canonical_name,
                            LibraryArtist.album_count,
                            LibraryArtist.track_count,
                            LibraryArtist.total_bytes,
                            LibraryArtist.flac_track_count,
                        ).order_by(LibraryArtist.normalized_name)
                    ).all()
                ],
                "counts": inventory.library_counts(session),
                "quality": inventory.quality_counts(session),
                "health": inventory.health_counts(session),
                "storage": inventory.storage_values(session),
            }

    completed_snapshot = published_inventory()
    previous_mtime = changed_path.stat().st_mtime_ns
    changed_path.write_bytes(b"different and longer bytes after the completed scan")
    os.utime(changed_path, ns=(previous_mtime + 10_000_000, previous_mtime + 10_000_000))
    removed_path.unlink()
    specifications[changed_relative]["tags"] = TagHints(
        title="Unpublished replacement title",
        artist="Replacement Artist",
        album_artist="Replacement Artist",
        album="Unpublished replacement album",
        track_number=1,
    )
    monkeypatch.setattr(settings, "inventory_commit_batch_size", 1)

    original_persist_batch = inventory._persist_inspected_batch
    values_seen_before_rollback: list[str | None] = []

    def fail_after_would_be_batch(session, batch, *, generation) -> None:
        original_persist_batch(session, batch, generation=generation)
        values_seen_before_rollback.append(
            session.scalar(
                select(LibraryInventoryTrack.title).where(
                    LibraryInventoryTrack.relative_path == changed_relative
                )
            )
        )
        raise inventory.InventoryScanError("synthetic failure after flushed batch")

    monkeypatch.setattr(inventory, "_persist_inspected_batch", fail_after_would_be_batch)
    failed = inventory.scan_library_now(database, settings=settings, reason="atomic-failure")

    assert failed.state == LibraryScanState.FAILED.value
    assert failed.generation == first.generation + 1
    assert values_seen_before_rollback == ["Unpublished replacement title"]
    assert published_inventory() == completed_snapshot


def test_worker_scan_apis_schedule_claim_recover_and_refresh_storage(
    settings,
    database,
    monkeypatch,
) -> None:
    path = _write(settings.music_dir, "Artist/Album/01.flac", b"one")
    _write(settings.staging_dir, "pending/source.zip", b"staged")
    specifications = {
        "Artist/Album/01.flac": {
            "codec": "flac",
            "tags": TagHints(
                title="One",
                artist="Artist",
                album_artist="Artist",
                album="Album",
                track_number=1,
            ),
        }
    }
    _install_fake_readers(monkeypatch, settings, specifications, [])

    scan_id = inventory.ensure_initial_or_scheduled_scan(database, settings)
    assert scan_id is not None
    assert inventory.ensure_initial_or_scheduled_scan(database, settings) == scan_id
    with database.session() as session:
        assert inventory.claim_next_scan(session) == scan_id
    completed = inventory.run_library_scan(database, settings, scan_id)
    assert completed.state == LibraryScanState.COMPLETE.value
    assert inventory.ensure_initial_or_scheduled_scan(database, settings) is None

    with database.session() as session:
        interrupted = LibraryScanRun(
            state=LibraryScanState.SCANNING.value,
            reason="test-restart",
            generation=completed.generation + 1,
        )
        session.add(interrupted)
        session.flush()
        interrupted_id = interrupted.id
        assert inventory.recover_interrupted_scans(session) == 1
    with database.session() as session:
        recovered = session.get(LibraryScanRun, interrupted_id)
        assert recovered is not None
        assert recovered.state == LibraryScanState.FAILED.value
        assert "previous inventory was left intact" in (recovered.error_message or "")
        assert inventory.library_counts(session)["tracks"] == 1
    assert inventory.ensure_initial_or_scheduled_scan(database, settings) is None

    snapshot = inventory.refresh_storage_snapshot(database, settings)
    assert snapshot.library_bytes == path.stat().st_size
    assert snapshot.staging_bytes == len(b"staged")
    assert snapshot.music_filesystem_total_bytes is not None
    assert snapshot.music_filesystem_free_bytes is not None


def test_storage_snapshot_does_not_turn_failed_walk_into_zero(
    settings,
    database,
    monkeypatch,
) -> None:
    sizes = iter((None, 123))
    monkeypatch.setattr(
        inventory,
        "_safe_directory_size",
        lambda *_args, **_kwargs: next(sizes),
    )

    snapshot = inventory.refresh_storage_snapshot(database, settings)

    assert snapshot.library_bytes is None
    assert snapshot.staging_bytes == 123


def test_concurrent_manual_and_scheduled_scan_requests_coalesce(
    settings,
    database,
    monkeypatch,
) -> None:
    ready = Barrier(2)
    original_serialize = inventory._serialize_scan_enqueue

    def synchronize_then_serialize(session) -> None:
        ready.wait(timeout=5)
        original_serialize(session)

    monkeypatch.setattr(inventory, "_serialize_scan_enqueue", synchronize_then_serialize)

    def request_manual() -> str:
        with database.session() as session:
            return inventory.enqueue_library_scan(session, reason="manual").id

    def request_scheduled() -> str | None:
        return inventory.ensure_initial_or_scheduled_scan(database, settings)

    with ThreadPoolExecutor(max_workers=2) as executor:
        manual = executor.submit(request_manual)
        scheduled = executor.submit(request_scheduled)
        manual_id = manual.result(timeout=10)
        scheduled_id = scheduled.result(timeout=10)

    assert scheduled_id == manual_id
    with database.session() as session:
        active = session.scalars(
            select(LibraryScanRun).where(
                LibraryScanRun.state.in_(
                    (LibraryScanState.QUEUED.value, LibraryScanState.SCANNING.value)
                )
            )
        ).all()
        assert [scan.id for scan in active] == [manual_id]


def test_multidisc_directories_are_indexed_as_one_album(
    settings,
    database,
    monkeypatch,
) -> None:
    specifications: dict[str, dict[str, object]] = {}
    for relative_path, title, disc_number in (
        ("Artist/Album/CD1/01.flac", "Disc one", 1),
        ("Artist/Album/Disc 2/01.flac", "Disc two", 2),
    ):
        specifications[relative_path] = {
            "codec": "flac",
            "tags": TagHints(
                title=title,
                artist="Artist",
                album_artist="Artist",
                album="Album",
                track_number=1,
                disc_number=disc_number,
                disc_total=2,
            ),
        }
        _write(settings.music_dir, relative_path, title.encode("utf-8"))
    _install_fake_readers(monkeypatch, settings, specifications, [])

    scan = inventory.scan_library_now(database, settings=settings, reason="multidisc")
    assert scan.state == LibraryScanState.COMPLETE.value
    with database.session() as session:
        assert session.scalar(select(func.count(LibraryArtist.id))) == 1
        assert session.scalar(select(func.count(LibraryAlbum.id))) == 1
        album = session.scalar(select(LibraryAlbum))
        assert album is not None
        assert album.relative_path == "Artist/Album"
        assert album.track_count == 2
