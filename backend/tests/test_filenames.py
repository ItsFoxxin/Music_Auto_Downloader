from __future__ import annotations

from foxden_music.filenames import (
    build_track_filename,
    normalize_identity,
    normalize_unicode,
    portable_collision_key,
    sanitize_component,
    sanitize_filename,
)


def test_nfc_normalization_preserves_non_latin_metadata() -> None:
    assert normalize_unicode("Cafe\u0301") == "Café"
    assert normalize_unicode("신메뉴") == "신메뉴"
    assert normalize_unicode("東京") == "東京"
    assert normalize_unicode("👨‍👩‍👧‍👦") == "👨‍👩‍👧‍👦"


def test_identity_is_unicode_aware_and_only_collapses_whitespace_and_case() -> None:
    first = normalize_identity("  STRAY   KIDS ", "ATE", "신메뉴")
    second = normalize_identity("stray kids", "ate", "신메뉴")

    assert first == second
    assert "신메뉴" in first
    assert normalize_identity("Beyoncé") != normalize_identity("Beyonce")


def test_portable_unsafe_characters_and_controls_are_sanitized() -> None:
    assert sanitize_component('A<>:"/\\|?*\nB') == "A_B"
    assert sanitize_component("Name. ") == "Name"
    assert sanitize_component("..") == "Unknown"
    assert sanitize_component("", fallback="bad/name") == "bad_name"
    assert sanitize_component("bad\ud800name") == "bad_name"


def test_windows_reserved_names_are_made_safe() -> None:
    assert sanitize_component("CON") == "_CON"
    assert sanitize_component("con.txt") == "_con.txt"
    assert sanitize_component("LPT9") == "_LPT9"
    assert sanitize_component("COM10") == "COM10"


def test_long_unicode_component_is_stably_truncated_by_utf8_bytes() -> None:
    original = "아주 긴 한국어 제목 🎵 " * 20
    first = sanitize_component(original, max_bytes=64)
    second = sanitize_component(original, max_bytes=64)
    different = sanitize_component(original + "다른", max_bytes=64)

    assert first == second
    assert first != different
    assert len(first.encode("utf-8")) <= 64
    assert "~" in first
    first.encode("utf-8", errors="strict")


def test_sanitized_filename_preserves_trusted_extension_and_limit() -> None:
    filename = sanitize_filename("노래 제목 " * 20, ".FLAC", max_bytes=72)

    assert filename.endswith(".flac")
    assert len(filename.encode("utf-8")) <= 72
    assert "~" in filename


def test_portable_collision_key_catches_normalization_case_and_invalid_chars() -> None:
    assert portable_collision_key("Café") == portable_collision_key("Cafe\u0301")
    assert portable_collision_key("SONG") == portable_collision_key("song")
    assert portable_collision_key("a:b") == portable_collision_key("a?b")
    assert portable_collision_key("A" * 300) == portable_collision_key("a" * 300)


def test_track_filename_supports_single_and_multi_disc_without_romanizing() -> None:
    assert build_track_filename("신메뉴", track_number=3, extension="flac") == "03 - 신메뉴.flac"
    assert (
        build_track_filename(
            "神メニュー",
            track_number=3,
            disc_number=2,
            multi_disc=True,
            extension="m4a",
        )
        == "02-03 - 神メニュー.m4a"
    )


def test_invalid_filename_arguments_fail_closed() -> None:
    import pytest

    with pytest.raises(ValueError):
        sanitize_component("name", max_bytes=7)
    with pytest.raises(ValueError):
        sanitize_component("name", replacement="/")
    with pytest.raises(ValueError):
        sanitize_filename("name", "../flac")
    with pytest.raises(ValueError):
        build_track_filename("name", track_number=0, extension="flac")
    with pytest.raises(ValueError):
        build_track_filename("name", track_number=1, multi_disc=True, extension="flac")
