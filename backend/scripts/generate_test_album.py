#!/usr/bin/env python3
"""Generate a tiny, copyright-free FLAC album ZIP for manual testing."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from foxden_music.metadata import NormalizedTrackMetadata, write_tags


TRACKS = (
    ("First Light", 440),
    ("여우별", 554),
    ("Café at Dawn", 659),
)


def generate(output: Path) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise SystemExit(f"Refusing to overwrite existing file: {output}")
    with tempfile.TemporaryDirectory(prefix="foxden-test-album-") as temporary:
        root = Path(temporary) / "Fox Den Test Artist" / "Signals from the Den"
        root.mkdir(parents=True)
        cover_path = root / "cover.jpg"
        image = Image.new("RGB", (800, 800), "#111116")
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 60, 740, 740), outline="#ef7a35", width=16)
        draw.text((110, 340), "FOX DEN TEST ALBUM", fill="#f5f3ef")
        image.save(cover_path, "JPEG", quality=92)
        artwork = cover_path.read_bytes()

        for index, (title, frequency) in enumerate(TRACKS, start=1):
            path = root / f"{index:02d} - {title}.flac"
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration=2",
                "-c:a",
                "flac",
                "-sample_fmt",
                "s16",
                str(path),
            ]
            result = subprocess.run(command, check=False, shell=False)
            if result.returncode != 0:
                raise SystemExit("ffmpeg could not generate the synthetic FLAC fixture")
            write_tags(
                path,
                NormalizedTrackMetadata(
                    title=title,
                    artist="Fox Den Test Artist",
                    album_artist="Fox Den Test Artist",
                    album="Signals from the Den",
                    track_number=index,
                    track_total=len(TRACKS),
                    release_date="2026-08-16",
                    year=2026,
                ),
                artwork,
            )

        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(Path(temporary)).as_posix())
    print(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new .zip path to create")
    args = parser.parse_args()
    if args.output.suffix.lower() != ".zip":
        parser.error("output must end in .zip")
    generate(args.output)


if __name__ == "__main__":
    main()

