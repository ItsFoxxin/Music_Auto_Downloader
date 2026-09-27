from __future__ import annotations

import hashlib
import re
import unicodedata


__all__ = [
    "DEFAULT_COMPONENT_BYTES",
    "build_track_filename",
    "normalize_identity",
    "normalize_unicode",
    "portable_collision_key",
    "sanitize_component",
    "sanitize_filename",
]


DEFAULT_COMPONENT_BYTES = 240
_MIN_COMPONENT_BYTES = 8
_PORTABLE_UNSAFE = frozenset('<>:"/\\|?*\x00')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_WHITESPACE_RE = re.compile(r"\s+")
_EXTENSION_RE = re.compile(r"[A-Za-z0-9]{1,16}")


def normalize_unicode(value: str) -> str:
    """Return canonical NFC without transliterating or discarding Unicode."""

    if not isinstance(value, str):
        raise TypeError("filename and metadata values must be strings")
    return unicodedata.normalize("NFC", value)


def _is_portable_unsafe(character: str) -> bool:
    # Surrogate code points are not valid standalone Unicode scalar values and
    # cannot be encoded as ordinary UTF-8 filenames.
    return character in _PORTABLE_UNSAFE or unicodedata.category(character) in {"Cc", "Cs"}


def _replace_unsafe(value: str, replacement: str) -> str:
    output: list[str] = []
    replacing = False
    for character in value:
        if _is_portable_unsafe(character):
            if not replacing:
                output.append(replacement)
            replacing = True
        else:
            output.append(character)
            replacing = False
    return "".join(output).strip().rstrip(" .")


def normalize_identity(*values: str | None) -> str:
    """Build a conservative, Unicode-aware comparison key.

    This deliberately does not remove accents, punctuation, or non-Latin text.
    It is suitable as a duplicate *signal*, not as proof that two recordings are
    identical.
    """

    normalized: list[str] = []
    for value in values:
        if value is None:
            normalized.append("")
            continue
        collapsed = _WHITESPACE_RE.sub(" ", normalize_unicode(value)).strip()
        normalized.append(collapsed.casefold())
    return "\x1f".join(normalized)


def _utf8_prefix(value: str, byte_budget: int) -> str:
    """Take a prefix that is valid UTF-8 and no larger than byte_budget."""

    if byte_budget <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_budget:
        return value
    return encoded[:byte_budget].decode("utf-8", errors="ignore")


def _stable_truncate(value: str, *, source: str, max_bytes: int) -> str:
    if len(value.encode("utf-8")) <= max_bytes:
        return value

    digest = hashlib.sha256(source.encode("utf-8", errors="surrogatepass")).hexdigest()
    digest_length = min(10, max(4, max_bytes - 2))
    suffix = f"~{digest[:digest_length]}"
    prefix = _utf8_prefix(value, max_bytes - len(suffix)).rstrip(" .")
    if not prefix:
        # max_bytes is validated by the public function, so this remains a
        # meaningful stable value even for an all-multibyte input.
        return digest[:max_bytes]
    return f"{prefix}{suffix}"


def sanitize_component(
    value: str,
    *,
    max_bytes: int = DEFAULT_COMPONENT_BYTES,
    replacement: str = "_",
    fallback: str = "Unknown",
) -> str:
    """Create one portable path component while preserving legitimate Unicode.

    Metadata should be stored separately and must not be replaced with this
    derived filename. Characters invalid on common Windows/SMB filesystems and
    C0/C1 controls are replaced. Long names get a deterministic hash suffix.
    """

    if max_bytes < _MIN_COMPONENT_BYTES:
        raise ValueError(f"max_bytes must be at least {_MIN_COMPONENT_BYTES}")
    if not replacement or len(replacement) != 1:
        raise ValueError("replacement must be exactly one character")
    if _is_portable_unsafe(replacement):
        raise ValueError("replacement must itself be portable")

    original = normalize_unicode(value)
    candidate = _replace_unsafe(original, replacement)
    if not candidate or candidate in {".", ".."}:
        normalized_fallback = normalize_unicode(fallback)
        candidate = _replace_unsafe(normalized_fallback, replacement)
        if not candidate or candidate in {".", ".."}:
            candidate = "Unknown"

    # Windows reserves these basenames even when an extension is present.
    basename = candidate.split(".", 1)[0]
    if basename.upper() in _WINDOWS_RESERVED:
        candidate = f"_{candidate}"

    hash_source = original if original else normalize_unicode(fallback)
    return _stable_truncate(candidate, source=hash_source, max_bytes=max_bytes)


def sanitize_filename(
    stem: str,
    extension: str,
    *,
    max_bytes: int = DEFAULT_COMPONENT_BYTES,
    fallback: str = "Unknown",
) -> str:
    """Sanitize a filename while reserving space for a trusted extension."""

    clean_extension = extension.removeprefix(".").lower()
    if not _EXTENSION_RE.fullmatch(clean_extension):
        raise ValueError("extension must contain 1-16 ASCII letters or digits")
    suffix = f".{clean_extension}"
    stem_budget = max_bytes - len(suffix.encode("ascii"))
    if stem_budget < _MIN_COMPONENT_BYTES:
        raise ValueError("max_bytes leaves insufficient room for the filename stem")
    return f"{sanitize_component(stem, max_bytes=stem_budget, fallback=fallback)}{suffix}"


def portable_collision_key(value: str) -> str:
    """Return the case-insensitive portable form used to detect path collisions."""

    return sanitize_component(normalize_unicode(value).casefold()).casefold()


def build_track_filename(
    title: str,
    *,
    track_number: int,
    extension: str,
    disc_number: int | None = None,
    multi_disc: bool = False,
    max_bytes: int = DEFAULT_COMPONENT_BYTES,
) -> str:
    """Build the current deterministic track filename convention."""

    if track_number < 1:
        raise ValueError("track_number must be positive")
    if multi_disc:
        if disc_number is None or disc_number < 1:
            raise ValueError("multi-disc filenames require a positive disc_number")
        position = f"{disc_number:02d}-{track_number:02d}"
    else:
        position = f"{track_number:02d}"
    return sanitize_filename(f"{position} - {title}", extension, max_bytes=max_bytes)
