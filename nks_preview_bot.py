#!/usr/bin/env python3
"""Generate Native Instruments-style .previews OGG files from raw WAVs."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen
from typing import Iterable

PRESET_EXTENSIONS = {".nki", ".nksn", ".nksf"}
# Prefer lossless / uncompressed first when multiple matches exist.
SOURCE_AUDIO_EXTENSIONS = (".wav", ".wave", ".aif", ".aiff", ".flac", ".mp3", ".ogg")
FFMPEG_BIN = "ffmpeg"
PREVIEW_DURATION_SEC = 2.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan a preset tree for .nki/.nksn/.nksf files and write "
            "2.0s OGG previews into a sibling .previews folder."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Directory of raw audio samples (searched recursively by stem name).",
    )
    parser.add_argument(
        "--target",
        required=True,
        type=Path,
        help="Root directory of preset files (.nki, .nksn, .nksf).",
    )
    parser.add_argument(
        "--ffmpeg",
        default=FFMPEG_BIN,
        help=f"Path to ffmpeg binary (default: {FFMPEG_BIN}).",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=4,
        help="Maximum concurrent background ffmpeg processes (default: 4).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-encode even if the dual-extension .ogg already exists.",
    )
    return parser.parse_args(argv)


def iter_preset_files(target_root: Path) -> Iterable[Path]:
    for path in target_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in PRESET_EXTENSIONS:
            yield path


def build_source_index(source_root: Path) -> dict[str, list[Path]]:
    """Map lowercased stem -> candidate audio paths under source_root."""
    index: dict[str, list[Path]] = {}
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SOURCE_AUDIO_EXTENSIONS:
            continue
        key = path.stem.lower()
        index.setdefault(key, []).append(path)
    return index


def pick_source_audio(candidates: list[Path]) -> Path | None:
    if not candidates:
        return None
    by_ext_rank = {
        ext: i for i, ext in enumerate(SOURCE_AUDIO_EXTENSIONS)
    }

    def sort_key(p: Path) -> tuple[int, str]:
        return (by_ext_rank.get(p.suffix.lower(), 999), str(p).lower())

    return sorted(candidates, key=sort_key)[0]


def preview_output_path(preset: Path) -> Path:
    """
    Dual-extension NI preview name:
      OriginalPresetName.nki  ->  .previews/OriginalPresetName.nki.ogg
    """
    previews_dir = preset.parent / ".previews"
    return previews_dir / f"{preset.name}.ogg"


def build_ffmpeg_cmd(ffmpeg: str, src: Path, dst: Path) -> list[str]:
    # Hard length cut-off of exactly 2.0s (-t after -i).
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-t",
        f"{PREVIEW_DURATION_SEC:.1f}",
        "-ac",
        "2",
        "-c:a",
        "vorbis",
        "-strict",
        "-2",
        "-q:a",
        "5",
        "-vn",
        str(dst),
    ]


def drain_finished(
    procs: list[tuple[Popen, Path, Path]],
) -> list[tuple[Popen, Path, Path]]:
    still_running: list[tuple[Popen, Path, Path]] = []
    for proc, src, dst in procs:
        code = proc.poll()
        if code is None:
            still_running.append((proc, src, dst))
            continue
        if code == 0:
            print(f"OK  {dst}")
        else:
            err = (proc.stderr.read() if proc.stderr else b"").decode(
                "utf-8", errors="replace"
            ).strip()
            print(
                f"FAIL {dst} (ffmpeg exit {code})"
                + (f": {err}" if err else ""),
                file=sys.stderr,
            )
    return still_running


def wait_for_slot(
    procs: list[tuple[Popen, Path, Path]],
    max_parallel: int,
) -> list[tuple[Popen, Path, Path]]:
    while True:
        procs = drain_finished(procs)
        if len(procs) < max_parallel:
            return procs
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_root: Path = args.source.expanduser().resolve()
    target_root: Path = args.target.expanduser().resolve()
    ffmpeg: str = args.ffmpeg
    max_parallel: int = max(1, args.max_parallel)

    if not source_root.is_dir():
        print(f"error: --source is not a directory: {source_root}", file=sys.stderr)
        return 2
    if not target_root.is_dir():
        print(f"error: --target is not a directory: {target_root}", file=sys.stderr)
        return 2

    print(f"Indexing source audio under {source_root} ...")
    source_index = build_source_index(source_root)
    print(f"Found {sum(len(v) for v in source_index.values())} source file(s).")

    presets = list(iter_preset_files(target_root))
    print(f"Found {len(presets)} preset file(s) under {target_root}.")

    procs: list[tuple[Popen, Path, Path]] = []
    scheduled = 0
    skipped_exists = 0
    missing_source = 0

    try:
        for preset in presets:
            out_path = preview_output_path(preset)
            if out_path.exists() and not args.overwrite:
                skipped_exists += 1
                continue

            candidates = source_index.get(preset.stem.lower(), [])
            src_audio = pick_source_audio(candidates)
            if src_audio is None:
                missing_source += 1
                print(
                    f"MISS no source match for stem '{preset.stem}' "
                    f"(preset: {preset})",
                    file=sys.stderr,
                )
                continue

            out_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = build_ffmpeg_cmd(ffmpeg, src_audio, out_path)
            procs = wait_for_slot(procs, max_parallel)
            proc = Popen(
                cmd,
                stdin=DEVNULL,
                stdout=DEVNULL,
                stderr=PIPE,
            )
            procs.append((proc, src_audio, out_path))
            scheduled += 1
            print(f"START {src_audio.name} -> {out_path}")

        while procs:
            procs = drain_finished(procs)
            if procs:
                time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nInterrupted; terminating background ffmpeg jobs...", file=sys.stderr)
        for proc, _, _ in procs:
            proc.terminate()
        for proc, _, _ in procs:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        return 130

    print(
        f"Done. scheduled={scheduled} skipped_existing={skipped_exists} "
        f"missing_source={missing_source}"
    )
    return 0 if missing_source == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
