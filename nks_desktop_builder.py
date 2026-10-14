#!/usr/bin/env python3
"""
NKS Desktop Builder
-------------------
Standalone macOS desktop app (tkinter + ttk) for building and registering
NKS-style user libraries from an unlicensed Quickload tree.

Requires a Tk-enabled Python (this project uses):
  /Users/fuckem/dev/venv-tk/bin/python /Users/fuckem/dev/nks_desktop_builder.py
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tkinter import (
    BOTH,
    BOTTOM,
    DISABLED,
    END,
    HORIZONTAL,
    LEFT,
    NORMAL,
    RIGHT,
    TOP,
    BooleanVar,
    Canvas,
    Frame,
    StringVar,
    Text,
    Tk,
    Toplevel,
    X,
    Y,
    filedialog,
    messagebox,
    scrolledtext,
)
from tkinter import ttk
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageTk

# ---------------------------------------------------------------------------
# Paths / constants (explicit project + NI roots)
# ---------------------------------------------------------------------------

APP_DIR = Path("/Users/fuckem/dev").resolve()
APP_TITLE = "NKS Desktop Builder"
APP_VERSION = "1.0.0"

DEFAULT_NI_ROOT = Path("/Users/Shared/NI Resources")
DEFAULT_IMAGE_ROOT = DEFAULT_NI_ROOT / "image"
DEFAULT_DIST_DB_ROOT = DEFAULT_NI_ROOT / "dist_database"
# Output is always the selected source library folder (in-place). No manual target.

PRESET_EXTENSIONS = {".nki", ".nksn", ".nksf", ".nkm", ".nrkt", ".nkt"}
SOURCE_AUDIO_EXTENSIONS = (".wav", ".wave", ".aif", ".aiff", ".flac", ".mp3", ".ogg")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
ARTWORK_KEYWORDS = ("wallpaper", "banner", "folder", "artwork", "icon")

NI_IMAGE_SPECS: dict[str, tuple[int, int]] = {
    "MST_artwork.png": (114, 46),
    "MST_logo.png": (114, 46),
    "BND_artwork.png": (272, 90),
}

PREVIEW_DURATION_SEC = 2.0
FFMPEG_CANDIDATES = (
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "ffmpeg",
)
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")

# Canvas display size for artwork preview
PREVIEW_CANVAS_W = 640
PREVIEW_CANVAS_H = 280


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resolve_ffmpeg() -> str | None:
    for candidate in FFMPEG_CANDIDATES:
        if os.path.sep in candidate:
            p = Path(candidate)
            if p.is_file() and os.access(p, os.X_OK):
                return str(p.resolve())
        else:
            found = shutil.which(candidate)
            if found:
                return str(Path(found).resolve())
    return shutil.which("ffmpeg")


def sanitize_segment(value: str, fallback: str = "Untitled") -> str:
    cleaned = SAFE_NAME_RE.sub("", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned or fallback


def slugify(value: str) -> str:
    return sanitize_segment(value, "untitled").replace(" ", "_").lower()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# Folder names that are storage/layout noise — never treat as vendor/library.
_GENERIC_PATH_SEGMENTS = frozenset(
    {
        "",
        "/",
        "volumes",
        "users",
        "shared",
        "applications",
        "system",
        "library",
        "libraries",
        "documents",
        "desktop",
        "downloads",
        "movies",
        "music",
        "pictures",
        "public",
        "home",
        "tmp",
        "temp",
        "private",
        "content",
        "contents",
        "instruments",
        "instrument",
        "samples",
        "sample",
        "presets",
        "preset",
        "quickload",
        "nks",
        "nki",
        "kontakt",
        "native instruments",
        "user content",
        "user library",
        "third-party",
        "third party",
        "3rd party",
        "installed",
        "products",
        "product",
        "banks",
        "bank",
        "packs",
        "pack",
    }
)

# Absolute path prefixes whose immediate child is not a vendor (e.g. /Users/alice).
_SKIP_VENDOR_PARENT_ROOTS = frozenset(
    {
        "/users",
        "/volumes",
        "/home",
        "/media",
        "/mnt",
        "/run/media",
    }
)


def _clean_folder_label(name: str) -> str:
    """Normalize a single path segment into a human library/vendor label."""
    label = (name or "").replace("\\", "/").strip()
    # Drop trailing separators / dots left by odd mounts
    label = label.strip("/ .")
    # Collapse internal whitespace
    label = re.sub(r"\s+", " ", label)
    return label


def _is_generic_segment(name: str) -> bool:
    cleaned = _clean_folder_label(name)
    if not cleaned:
        return True
    low = cleaned.lower()
    if low in _GENERIC_PATH_SEGMENTS:
        return True
    # Volume-style single-letter or bare drive labels
    if len(cleaned) <= 1:
        return True
    if re.fullmatch(r"[A-Za-z]:", cleaned):
        return True
    return False


def extract_library_name_from_path(path: Path | str) -> str:
    """
    Base folder name of the selected path.
    e.g. .../Silence & Other Sounds/Mystery Box 3  →  "Mystery Box 3"
    """
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = Path(path).expanduser()
    # If a file was passed, use its parent directory name
    if p.is_file():
        p = p.parent
    # Always prefer the selected folder's own name (even if it looks "generic",
    # e.g. a project folder literally named "dev").
    name = _clean_folder_label(p.name)
    if name:
        return name
    for parent in p.parents:
        cand = _clean_folder_label(parent.name)
        if cand and not _is_generic_segment(cand):
            return cand
    return ""


def _is_users_home_segment(parent: Path) -> bool:
    """True when parent is /Users or /home (so child is a username, not a vendor)."""
    try:
        low = str(parent.resolve()).lower().rstrip("/")
    except OSError:
        low = str(parent).lower().rstrip("/")
    return low in {"/users", "/home"} or low.endswith("/users") or low.endswith("/home")


def _vendor_candidate_ok(name: str, leaf: str) -> bool:
    if not name or _is_generic_segment(name):
        return False
    if name.lower() == leaf.lower():
        return False
    return True


def _path_norm(path: Path) -> str:
    try:
        return str(path.resolve()).lower().rstrip("/")
    except OSError:
        return str(path).lower().rstrip("/")


def _is_weak_container_path(path: Path) -> bool:
    """
    True for paths that should not supply a vendor name:
      /Users/<user>, /Volumes/<vol>, /home/<user>
    """
    low = _path_norm(path)
    return bool(
        re.fullmatch(r"/users/[^/]+", low)
        or re.fullmatch(r"/home/[^/]+", low)
        or re.fullmatch(r"/volumes/[^/]+", low)
    )


def guess_vendor_from_path(path: Path | str) -> str:
    """
    Smart vendor guess from parent / path conventions.

    Heuristics (first match wins):
      1. Immediate non-generic parent folder
         (.../Silence & Other Sounds/Mystery Box 3 → "Silence & Other Sounds")
      2. "Vendor - Library" split on the leaf name
      3. "Vendor_LibraryName" / "Vendor-LibraryName" when both sides look real
      4. Walk ancestors for first strong creator folder
      5. Weak volume-name fallback only if nothing else fits
    """
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = Path(path).expanduser()
    if p.is_file():
        p = p.parent

    leaf = _clean_folder_label(p.name)
    parent = p.parent if p.parent != p else None
    volume_guess = ""

    # 1) Immediate parent
    if parent is not None:
        parent_name = _clean_folder_label(parent.name)
        if _vendor_candidate_ok(parent_name, leaf):
            if _is_weak_container_path(parent):
                # /Volumes/Drive/Lib → remember volume label as last resort
                if re.fullmatch(r"/volumes/[^/]+", _path_norm(parent)):
                    volume_guess = parent_name
            else:
                return parent_name

    # 2) "Vendor - Library" embedded in leaf
    for sep in (" – ", " — ", " - "):
        if sep in leaf:
            left, right = (_clean_folder_label(x) for x in leaf.split(sep, 1))
            if left and right and _vendor_candidate_ok(left, right):
                return left

    # 3) Underscore / hyphen prefix: Vendor_Rest
    m = re.match(r"^([A-Za-z][A-Za-z0-9&'. ]{1,40})[_-](.+)$", leaf)
    if m:
        left = _clean_folder_label(m.group(1))
        right = _clean_folder_label(m.group(2))
        if left and right and _vendor_candidate_ok(left, right) and len(left.split()) <= 4:
            return left

    # 4) Walk ancestors (skip generic + weak containers)
    if parent is not None:
        for ancestor in parent.parents:
            if ancestor == parent:
                continue
            aname = _clean_folder_label(ancestor.name)
            if not _vendor_candidate_ok(aname, leaf):
                continue
            if aname.lower() in {"volumes", "users", "home"}:
                continue
            if _is_weak_container_path(ancestor):
                if re.fullmatch(r"/volumes/[^/]+", _path_norm(ancestor)) and not volume_guess:
                    volume_guess = aname
                continue
            # Parent of ancestor being /Users means ancestor is username — skip
            if _is_users_home_segment(ancestor.parent if ancestor.parent != ancestor else ancestor):
                continue
            return aname

    # 5) Volume fallback
    if volume_guess and _vendor_candidate_ok(volume_guess, leaf):
        return volume_guess

    return ""


def derive_metadata_from_source_path(path: Path | str) -> tuple[str, str]:
    """Return (library_name, vendor_guess) for a Quickload/source path."""
    library = extract_library_name_from_path(path)
    vendor = guess_vendor_from_path(path)
    return library, vendor


# ---------------------------------------------------------------------------
# Asset detection
# ---------------------------------------------------------------------------


@dataclass(order=True)
class DetectedAsset:
    score: int
    path: Path = field(compare=False)
    matched_keywords: tuple[str, ...] = field(default_factory=tuple, compare=False)

    @property
    def label(self) -> str:
        kws = ", ".join(self.matched_keywords) if self.matched_keywords else "image"
        return f"{self.path.name}  [{kws}]"


def score_artwork_candidate(path: Path) -> DetectedAsset | None:
    if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    name = path.name.lower()
    stem = path.stem.lower()
    hits = tuple(k for k in ARTWORK_KEYWORDS if k in name or k in stem)
    if not hits:
        return None
    priority = {"artwork": 100, "banner": 90, "wallpaper": 80, "folder": 70, "icon": 60}
    score = max(priority.get(k, 10) for k in hits)
    if path.suffix.lower() == ".png":
        score += 5
    return DetectedAsset(score=-score, path=path, matched_keywords=hits)


def scan_artwork(source_root: Path, limit: int = 48) -> list[DetectedAsset]:
    if not source_root.is_dir():
        return []
    found: list[DetectedAsset] = []
    for path in source_root.rglob("*"):
        asset = score_artwork_candidate(path)
        if asset is not None:
            found.append(asset)
    found.sort()
    seen: set[str] = set()
    unique: list[DetectedAsset] = []
    for a in found:
        key = str(a.path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)
        if len(unique) >= limit:
            break
    return unique


# ---------------------------------------------------------------------------
# Image formatting (Pillow)
# ---------------------------------------------------------------------------


def _load_font(size: int) -> ImageFont.ImageFont:
    for fp in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        if Path(fp).is_file():
            try:
                return ImageFont.truetype(fp, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def generate_placeholder_artwork(
    prompt: str,
    vendor: str,
    library: str,
    size: tuple[int, int] = (1024, 512),
) -> Image.Image:
    w, h = size
    base = Image.new("RGB", size, (12, 14, 22))
    draw = ImageDraw.Draw(base)
    for y in range(h):
        t = y / max(h - 1, 1)
        draw.line(
            [(0, y), (w, y)],
            fill=(int(18 + 40 * t), int(24 + 20 * (1 - t)), int(48 + 80 * t)),
        )
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse([-w * 0.2, -h * 0.4, w * 0.8, h * 0.9], fill=(80, 140, 255, 40))
    od.ellipse([w * 0.3, h * 0.1, w * 1.2, h * 1.3], fill=(255, 90, 160, 35))
    base = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    base = base.filter(ImageFilter.GaussianBlur(radius=0.6))
    draw = ImageDraw.Draw(base)

    title_font = _load_font(max(28, h // 10))
    sub_font = _load_font(max(16, h // 18))
    tiny_font = _load_font(max(14, h // 24))
    body = (prompt or "Custom NKS artwork").strip()
    if len(body) > 120:
        body = body[:117] + "…"

    def shadow(xy, text, font, fill):
        x, y = xy
        draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0))
        draw.text((x, y), text, font=font, fill=fill)

    shadow((36, h // 5), library or "NKS Library", title_font, (245, 248, 255))
    shadow((36, h // 5 + h // 8), vendor or "Vendor", sub_font, (180, 200, 255))
    shadow((36, h // 2 + 10), body, tiny_font, (210, 215, 230))
    shadow(
        (36, h - h // 6),
        f"{APP_TITLE} · {datetime.now(timezone.utc):%Y-%m-%d}",
        tiny_font,
        (140, 150, 170),
    )
    return base


def fit_cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(
        img.convert("RGBA"),
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def fit_logo(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    tw, th = size
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    src = ImageEnhance.Contrast(img.convert("RGBA")).enhance(1.08)
    src = ImageEnhance.Sharpness(src).enhance(1.15)
    src.thumbnail((max(1, tw - 4), max(1, th - 4)), Image.Resampling.LANCZOS)
    canvas.paste(src, ((tw - src.width) // 2, (th - src.height) // 2), src)
    return canvas


def format_ni_images(source_image: Image.Image, dest_dir: Path) -> dict[str, Path]:
    ensure_dir(dest_dir)
    written: dict[str, Path] = {}
    for filename, dims in NI_IMAGE_SPECS.items():
        out = fit_logo(source_image, dims) if filename == "MST_logo.png" else fit_cover(source_image, dims)
        target = dest_dir / filename
        out.save(target, format="PNG", optimize=True)
        written[filename] = target
    return written


# ---------------------------------------------------------------------------
# Database + audio pipeline
# ---------------------------------------------------------------------------


def write_dist_database(vendor: str, library: str, dest_root: Path = DEFAULT_DIST_DB_ROOT) -> Path:
    out_dir = ensure_dir(dest_root / vendor / library)
    short = slugify(library)
    payload = {
        "schema_version": 1,
        "generated_by": APP_TITLE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vendor": vendor,
        "vendor_id": slugify(vendor),
        "library": library,
        "library_id": slugify(library),
        "shortname": short,
        "name": library,
        "product": {
            "name": library,
            "shortname": short,
            "vendor": vendor,
            "type": "instrument",
            "nks": True,
        },
        "paths": {
            "image": f"image/{vendor}/{library}",
            "content": library,
        },
        "meta": {"app_dir": str(APP_DIR)},
    }
    primary = out_dir / "database.json"
    legacy = out_dir / f"{short}.json"
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    primary.write_text(text, encoding="utf-8")
    legacy.write_text(text, encoding="utf-8")
    return primary


def iter_preset_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in PRESET_EXTENSIONS:
            if any(part == ".previews" for part in path.parts):
                continue
            yield path


# Folder names that typically hold raw samples — preferred during match ranking.
_PREFERRED_AUDIO_DIR_NAMES = (
    "samples",
    "sample",
    "instruments data",
    "instrument data",
    "instruments",
    "instrument",
    "audio",
    "wavs",
    "waves",
    "multisamples",
    "resources",
    "data",
)


def _normalize_audio_key(name: str) -> str:
    """Normalize preset/audio basenames for fuzzy equality (case, separators)."""
    s = (name or "").strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[\s_\-./\\]+", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def _audio_dir_priority(path: Path, source_root: Path) -> int:
    """
    Lower is better. Prefer files nested under Samples / Instruments Data / etc.
    """
    try:
        rel_parts = [p.lower() for p in path.relative_to(source_root).parts[:-1]]
    except ValueError:
        rel_parts = [p.lower() for p in path.parts[:-1]]
    best = 50
    for i, preferred in enumerate(_PREFERRED_AUDIO_DIR_NAMES):
        if preferred in rel_parts:
            best = min(best, i)
    # Slight preference for deeper sample trees over root-level orphans
    depth_bonus = 0 if best < 50 else min(len(rel_parts), 9)
    return best * 10 + (0 if best < 50 else 10 - depth_bonus)


def build_source_audio_index(source_root: Path) -> dict[str, list[Path]]:
    """
    Deep recursive index of all audio under source_root (any nested depth),
    including Samples / Instruments Data / Instruments / …

    Keys are lowercased stems; also stores a parallel normalized map via
    the special key namespace used by pick_matching_wav.
    """
    index: dict[str, list[Path]] = {}
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SOURCE_AUDIO_EXTENSIONS:
            continue
        # Never treat generated previews as source audio
        if any(part == ".previews" for part in path.parts):
            continue
        # Read-only discovery — do not open/alter the file here
        stem_key = path.stem.lower()
        index.setdefault(stem_key, []).append(path)
        norm = _normalize_audio_key(path.stem)
        if norm and norm != stem_key:
            index.setdefault(f"norm:{norm}", []).append(path)
    return index


def pick_matching_wav(
    stem: str,
    index: dict[str, list[Path]],
    source_root: Path | None = None,
) -> Path | None:
    """
    Find best matching source audio for a preset stem.

    Match order:
      1. Exact stem (case-insensitive), e.g. MB03_Clocks_Loops.nki → MB03_Clocks_Loops.wav
      2. Normalized stem (ignore spaces/underscores/hyphens)
      3. Unique normalized containment (preset stem contained in audio name or vice versa)

    Among candidates, prefer files under Samples / Instruments Data / … then .wav.
    Original audio files are never modified — path is read-only for ffmpeg -i.
    """
    stem_l = (stem or "").lower()
    norm = _normalize_audio_key(stem)
    candidates: list[Path] = []

    # 1) Exact stem
    candidates.extend(index.get(stem_l, []))

    # 2) Normalized exact
    if norm:
        for p in index.get(f"norm:{norm}", []):
            if p not in candidates:
                candidates.append(p)

    # 3) Fuzzy containment across full recursive inventory (only if still empty)
    if not candidates and norm and len(norm) >= 4:
        fuzzy: list[Path] = []
        seen: set[str] = set()
        for key, paths in index.items():
            if key.startswith("norm:"):
                other = key[5:]
            else:
                other = _normalize_audio_key(key)
            if not other:
                continue
            if norm == other or norm in other or other in norm:
                for p in paths:
                    pk = str(p)
                    if pk not in seen:
                        seen.add(pk)
                        fuzzy.append(p)
        # Only accept fuzzy if unambiguous-ish (prefer unique; else still rank)
        candidates.extend(fuzzy)

    if not candidates:
        return None

    root = source_root or Path("/")
    ext_rank = {ext: i for i, ext in enumerate(SOURCE_AUDIO_EXTENSIONS)}

    def sort_key(p: Path) -> tuple[int, int, int, str]:
        return (
            _audio_dir_priority(p, root),
            ext_rank.get(p.suffix.lower(), 999),
            len(str(p)),  # shorter paths slightly preferred when tied
            str(p).lower(),
        )

    return sorted(candidates, key=sort_key)[0]


def ffmpeg_preview_cmd(ffmpeg: str, src: Path, dst: Path) -> list[str]:
    """
    Read-only ffmpeg invocation: -i opens source audio without writing it back.
    Output is exclusively the new .ogg under .previews/.
    """
    # Homebrew ffmpeg here ships native `vorbis` (not libvorbis); needs -strict -2
    # and stereo (-ac 2). Falls back cleanly for NKS-style .ogg previews.
    return [
        ffmpeg,
        "-y",  # overwrite only the destination .ogg if re-run
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),  # INPUT only — never the write target
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
        str(dst),  # OUTPUT only — .previews/*.ogg
    ]


@dataclass
class PipelineResult:
    images_written: dict[str, str] = field(default_factory=dict)
    database_path: str | None = None
    presets_mirrored: int = 0
    previews_created: int = 0
    previews_failed: int = 0
    previews_missing_audio: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def mirror_presets_and_previews(
    source_root: Path,
    target_root: Path,
    ffmpeg: str,
    log: Callable[[str], None] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> tuple[int, int, int, int, list[str]]:
    """
    NON-DESTRUCTIVE preview deploy.

    - Never deletes, moves, renames, or overwrites original .nki / .nkm / .nksn /
      .nksf / .wav (or other sample) files.
    - Never copies presets out of place when operating on the library root.
    - Only *creates/updates* files under hidden `.previews/` directories:
        [PresetDir]/.previews/[PresetName].[ext].ogg
    - Source audio is opened read-only by ffmpeg (`-i`); only the .ogg path is written.

    Audio discovery is deep-recursive across the whole tree, with preference for
    nested folders such as Samples and Instruments Data.
    """
    try:
        lib_root = source_root.expanduser().resolve()
    except OSError:
        lib_root = source_root.expanduser()

    # target_root is accepted for API compat; previews always sit beside each preset.
    _ = target_root

    if log:
        log("Non-destructive mode: originals (.nki/.nkm/.wav/…) are never modified or moved")
        log(f"Deep audio scan under {lib_root} (includes Samples / Instruments Data / …)")

    audio_index = build_source_audio_index(lib_root)
    audio_file_count = sum(len(v) for k, v in audio_index.items() if not k.startswith("norm:"))
    presets = list(iter_preset_files(lib_root))
    total = max(len(presets), 1)
    processed = ok = fail = missing = 0
    errors: list[str] = []

    if log:
        log(f"Indexed {audio_file_count} nested audio file(s); {len(presets)} preset(s)")

    for i, preset in enumerate(presets):
        # Preset path is reference-only — never written, moved, or replaced.
        dest_preset = preset
        processed += 1
        rel = preset.relative_to(lib_root)

        previews_dir = ensure_dir(dest_preset.parent / ".previews")
        out_ogg = previews_dir / f"{dest_preset.name}.ogg"

        wav = pick_matching_wav(preset.stem, audio_index, source_root=lib_root)
        if wav is None:
            missing += 1
            if log:
                log(f"MISS audio for {preset.name} (searched recursively under library)")
            if progress:
                progress((i + 1) / total, f"No audio: {preset.name}")
            continue

        # Safety: never allow ffmpeg output path to equal a source audio/preset path
        try:
            if out_ogg.resolve() == wav.resolve() or out_ogg.resolve() == preset.resolve():
                fail += 1
                errors.append(f"refusing to overwrite source path for {preset.name}")
                continue
        except OSError:
            pass

        if log:
            try:
                wav_rel = wav.relative_to(lib_root)
            except ValueError:
                wav_rel = wav
            log(f"MATCH {preset.name} ← {wav_rel}")

        try:
            # ffmpeg reads `wav` and writes only `out_ogg` under .previews/
            proc = subprocess.run(
                ffmpeg_preview_cmd(ffmpeg, wav, out_ogg),
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0 and out_ogg.is_file():
                ok += 1
                if log:
                    log(f"OK  {out_ogg}  (source untouched: {wav.name})")
            else:
                fail += 1
                err = (proc.stderr or "").strip()[:300]
                errors.append(f"ffmpeg failed {preset.name}: exit={proc.returncode} {err}")
        except OSError as exc:
            fail += 1
            errors.append(f"ffmpeg spawn failed {preset.name}: {exc}")

        if progress:
            progress((i + 1) / total, f"Preview {i + 1}/{len(presets)}: {rel}")

    return processed, ok, fail, missing, errors


def run_pipeline(
    *,
    vendor: str,
    library: str,
    source_path: Path,
    output_path: Path,
    use_found_artwork: bool,
    selected_asset: Path | None,
    ai_prompt: str,
    ffmpeg: str,
    log: Callable[[str], None] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> PipelineResult:
    result = PipelineResult()
    vendor_s = sanitize_segment(vendor, "Vendor")
    library_s = sanitize_segment(library, "Library")

    def _log(msg: str) -> None:
        if log:
            log(msg)

    def _prog(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    # Stage 1 — images
    _prog(0.05, "Stage 1/3 · Preparing artwork…")
    try:
        if use_found_artwork and selected_asset and selected_asset.is_file():
            master = Image.open(selected_asset)
            master.load()
            _log(f"Using found artwork: {selected_asset}")
        else:
            master = generate_placeholder_artwork(ai_prompt, vendor_s, library_s)
            _log("Generated placeholder / AI-description artwork")

        image_dest = DEFAULT_IMAGE_ROOT / vendor_s / library_s
        written = format_ni_images(master, image_dest)
        result.images_written = {k: str(v) for k, v in written.items()}
        _log(f"Wrote NI images → {image_dest}")
        for name, path in written.items():
            _log(f"  {name} ({NI_IMAGE_SPECS[name][0]}x{NI_IMAGE_SPECS[name][1]}) → {path}")
        _prog(0.30, "Stage 1/3 · Images complete")
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Image formatting failed: {exc}")
        _log(traceback.format_exc())
        return result

    # Stage 2 — database
    _prog(0.35, "Stage 2/3 · Writing dist_database JSON…")
    try:
        db_path = write_dist_database(vendor_s, library_s, DEFAULT_DIST_DB_ROOT)
        result.database_path = str(db_path)
        _log(f"Database → {db_path}")
        _prog(0.45, "Stage 2/3 · Database complete")
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Database injection failed: {exc}")
        _log(traceback.format_exc())
        return result

    # Stage 3 — presets + previews (always deploy into the original library folder)
    _prog(0.50, "Stage 3/3 · Building .previews in original library folder…")
    if not source_path.is_dir():
        result.errors.append(f"Source path is not a directory: {source_path}")
        return result

    try:
        library_out = source_path.expanduser().resolve()
    except OSError:
        library_out = source_path.expanduser()
    # output_path is accepted for API compatibility but ignored: conversion is in-place.
    _ = output_path
    ensure_dir(library_out)
    _log(f"In-place NKS target (original library) → {library_out}")
    _log(f"Global NI image root → {DEFAULT_IMAGE_ROOT}")
    _log(f"Global NI dist_database → {DEFAULT_DIST_DB_ROOT}")

    def stage3_progress(frac: float, msg: str) -> None:
        _prog(0.50 + 0.48 * frac, f"Stage 3/3 · {msg}")

    mirrored, ok, fail, missing, errs = mirror_presets_and_previews(
        source_root=source_path,
        target_root=library_out,
        ffmpeg=ffmpeg,
        log=_log,
        progress=stage3_progress,
    )
    result.presets_mirrored = mirrored
    result.previews_created = ok
    result.previews_failed = fail
    result.previews_missing_audio = missing
    result.errors.extend(errs[:50])
    if missing:
        result.warnings.append(f"{missing} preset(s) had no matching source audio.")
    if fail:
        result.warnings.append(f"{fail} ffmpeg preview job(s) failed.")

    _log(
        f"Done. processed={mirrored} previews_ok={ok} fail={fail} missing_audio={missing}"
    )
    _log(f"Library output (in-place) → {library_out}")
    _prog(1.0, "Pipeline complete.")
    return result


# ---------------------------------------------------------------------------
# Artwork choice dialog
# ---------------------------------------------------------------------------


class ArtworkChoiceDialog(Toplevel):
    """Modal dialog: use found artwork vs create custom by description."""

    def __init__(self, master: Tk, asset_count: int):
        super().__init__(master)
        self.title("Artwork source")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.result: str | None = None  # "found" | "custom"

        body = ttk.Frame(self, padding=16)
        body.pack(fill=BOTH, expand=True)

        ttk.Label(
            body,
            text=(
                f"Found {asset_count} library artwork candidate(s).\n"
                "How should NKS Desktop Builder proceed?"
            ),
            justify=LEFT,
        ).pack(anchor="w", pady=(0, 12))

        btn_row = ttk.Frame(body)
        btn_row.pack(fill=X)

        ttk.Button(
            btn_row,
            text="Use found library artwork",
            command=self._choose_found,
        ).pack(side=LEFT, padx=(0, 8))
        ttk.Button(
            btn_row,
            text="Create new custom artwork by description",
            command=self._choose_custom,
        ).pack(side=LEFT)

        self.protocol("WM_DELETE_WINDOW", self._choose_found)
        self.update_idletasks()
        self._center_on_parent(master)

    def _center_on_parent(self, master: Tk) -> None:
        self.update_idletasks()
        pw, ph = master.winfo_width(), master.winfo_height()
        px, py = master.winfo_rootx(), master.winfo_rooty()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + max(0, (pw - w) // 2)}+{py + max(0, (ph - h) // 2)}")

    def _choose_found(self) -> None:
        self.result = "found"
        self.destroy()

    def _choose_custom(self) -> None:
        self.result = "custom"
        self.destroy()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------


class NKSDesktopApp(Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.minsize(820, 720)
        self.geometry("920x780")

        # State
        self.vendor_var = StringVar()
        self.library_var = StringVar()
        self.source_var = StringVar()
        # Auto-target: always mirrors the selected source library absolute path.
        self.output_var = StringVar(value="(select a source library folder)")
        self.ai_prompt_var = StringVar()
        self.status_var = StringVar(value="Ready.")
        self.ffmpeg_path = resolve_ffmpeg()

        self.detected_assets: list[DetectedAsset] = []
        self.selected_asset: Path | None = None
        self.use_found_artwork = BooleanVar(value=False)
        self._photo_ref: ImageTk.PhotoImage | None = None  # prevent GC
        self._build_thread: threading.Thread | None = None
        self._ui_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self._set_ffmpeg_status()
        self._try_enable_drag_drop()
        self.after(120, self._drain_ui_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Default cwd / project root awareness
        try:
            os.chdir(APP_DIR)
        except OSError:
            pass

    # ----- UI construction -----

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill=X, pady=(0, 8))
        ttk.Label(header, text=APP_TITLE, font=("Helvetica", 18, "bold")).pack(side=LEFT)
        ttk.Label(header, text=f"  ·  project root {APP_DIR}").pack(side=LEFT)

        # --- Form ---
        form = ttk.LabelFrame(root, text="Library settings", padding=10)
        form.pack(fill=X, pady=(0, 10))

        self._add_text_row(form, 0, "Vendor Name", self.vendor_var)
        self._add_text_row(form, 1, "Library Name", self.library_var)
        self._add_path_row(
            form,
            2,
            "Source Unlicensed Quickload Path",
            self.source_var,
            self._browse_source,
        )
        # Output is fully automatic — no Browse. Read-only display of in-place target.
        ttk.Label(form, text="NKS Output Target (auto)").grid(
            row=3, column=0, sticky="w", pady=3, padx=(0, 8)
        )
        self.output_entry = ttk.Entry(form, textvariable=self.output_var, state="readonly")
        self.output_entry.grid(row=3, column=1, sticky="ew", pady=3, columnspan=2)
        ttk.Label(
            form,
            text=(
                "Presets & .previews write into the source folder above. "
                f"Artwork/DB still register under {DEFAULT_NI_ROOT}."
            ),
            foreground="#666",
            wraplength=640,
        ).grid(row=4, column=1, sticky="w", pady=(0, 4), columnspan=2)

        form.columnconfigure(1, weight=1)

        # Auto-extract Library/Vendor when the source path text changes (paste / type / drop).
        self.source_var.trace_add("write", self._on_source_path_var_write)
        self._source_meta_last_applied: str = ""
        self._suppress_source_trace = False

        # --- Artwork canvas ---
        art = ttk.LabelFrame(root, text="Detected library artwork", padding=8)
        art.pack(fill=BOTH, expand=False, pady=(0, 10))

        tools = ttk.Frame(art)
        tools.pack(fill=X, pady=(0, 6))
        ttk.Button(tools, text="Scan Quickload Path", command=self.scan_source).pack(side=LEFT)
        self.asset_combo = ttk.Combobox(tools, state="readonly", width=60)
        self.asset_combo.pack(side=LEFT, padx=8, fill=X, expand=True)
        self.asset_combo.bind("<<ComboboxSelected>>", self._on_asset_selected)

        self.canvas = Canvas(
            art,
            width=PREVIEW_CANVAS_W,
            height=PREVIEW_CANVAS_H,
            bg="#1a1d27",
            highlightthickness=1,
            highlightbackground="#3a3f55",
        )
        self.canvas.pack(fill=X, expand=False)
        self.canvas.create_text(
            PREVIEW_CANVAS_W // 2,
            PREVIEW_CANVAS_H // 2,
            text="No artwork loaded — choose a Quickload path and Scan",
            fill="#8b93a7",
            font=("Helvetica", 12),
            tags=("placeholder",),
        )

        # --- AI prompt (shown when needed) ---
        self.prompt_frame = ttk.LabelFrame(root, text="AI Artwork Prompt", padding=8)
        self.prompt_frame.pack(fill=X, pady=(0, 10))
        ttk.Entry(self.prompt_frame, textvariable=self.ai_prompt_var).pack(fill=X)
        ttk.Label(
            self.prompt_frame,
            text="Used when no artwork is found, or when you choose custom description artwork.",
            foreground="#666",
        ).pack(anchor="w", pady=(4, 0))
        # Hidden until scan decides — still pack but we'll toggle state
        self._set_prompt_visible(True)

        # --- Build ---
        actions = ttk.Frame(root)
        actions.pack(fill=X, pady=(0, 8))
        self.build_btn = ttk.Button(
            actions,
            text="Build & Register Library",
            command=self.start_build,
        )
        self.build_btn.pack(side=LEFT, ipadx=12, ipady=8)

        self.progress = ttk.Progressbar(actions, orient=HORIZONTAL, mode="determinate", maximum=100)
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=12)

        # --- Log ---
        log_frame = ttk.LabelFrame(root, text="Build log", padding=6)
        log_frame.pack(fill=BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=12,
            wrap="word",
            font=("Menlo", 11),
            state=DISABLED,
        )
        self.log_text.pack(fill=BOTH, expand=True)

        # --- Status bar ---
        status = ttk.Frame(root)
        status.pack(fill=X, side=BOTTOM, pady=(6, 0))
        ttk.Label(status, textvariable=self.status_var).pack(side=LEFT)

    def _add_text_row(self, parent: ttk.LabelFrame, row: int, label: str, var: StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", pady=3, columnspan=2)

    def _add_path_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        var: StringVar,
        browse_cmd: Callable[[], None],
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Button(parent, text="Browse…", command=browse_cmd).grid(row=row, column=2, padx=(6, 0), pady=3)

    def _set_prompt_visible(self, visible: bool) -> None:
        if visible:
            if not self.prompt_frame.winfo_manager():
                self.prompt_frame.pack(fill=X, pady=(0, 10), before=self.build_btn.master)
        else:
            self.prompt_frame.pack_forget()

    def _set_ffmpeg_status(self) -> None:
        if self.ffmpeg_path:
            self._log(f"ffmpeg resolved: {self.ffmpeg_path}")
            try:
                ver = subprocess.run(
                    [self.ffmpeg_path, "-version"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                first = (ver.stdout or "").splitlines()[:1]
                if first:
                    self._log(first[0])
            except OSError as exc:
                self._log(f"ffmpeg version check failed: {exc}")
            self.status_var.set(f"Ready · ffmpeg {self.ffmpeg_path}")
        else:
            self._log("ERROR: ffmpeg not found on this system.")
            self.status_var.set("ffmpeg missing")
            messagebox.showerror(
                APP_TITLE,
                "ffmpeg was not found.\nInstall via Homebrew: brew install ffmpeg",
                parent=self,
            )

    # ----- Logging / queue -----

    def _log(self, msg: str) -> None:
        """Thread-safe log append via UI queue if not on main thread."""
        if threading.current_thread() is threading.main_thread():
            self._append_log(msg)
        else:
            self._ui_queue.put(("log", msg))

    def _append_log(self, msg: str) -> None:
        self.log_text.configure(state=NORMAL)
        self.log_text.insert(END, msg.rstrip() + "\n")
        self.log_text.see(END)
        self.log_text.configure(state=DISABLED)

    def _set_progress(self, fraction: float, message: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self.progress["value"] = max(0.0, min(100.0, fraction * 100.0))
            self.status_var.set(message)
        else:
            self._ui_queue.put(("progress", fraction, message))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                item = self._ui_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "progress":
                    _, frac, message = item
                    self.progress["value"] = max(0.0, min(100.0, frac * 100.0))
                    self.status_var.set(message)
                elif kind == "done":
                    result: PipelineResult = item[1]
                    self._on_build_done(result)
                elif kind == "error":
                    self._on_build_error(item[1])
        except queue.Empty:
            pass
        self.after(120, self._drain_ui_queue)

    # ----- Browse / scan -----

    def _browse_source(self) -> None:
        initial = self.source_var.get().strip() or str(APP_DIR)
        path = filedialog.askdirectory(
            parent=self,
            title="Select Source Unlicensed Quickload Path",
            initialdir=initial if Path(initial).is_dir() else str(APP_DIR),
        )
        if path:
            self._apply_source_path(path, scan=True)

    def _set_auto_output_target(self, path: str | Path) -> None:
        """
        Lock NKS conversion output to the absolute source library path.
        No manual Browse — presets/.previews deploy in-place here.
        """
        p = Path(str(path).strip().strip('"').strip("'")).expanduser()
        try:
            if p.is_file():
                p = p.parent
            abs_path = str(p.resolve()) if p.exists() else str(p.absolute())
        except OSError:
            abs_path = str(p)
        # readonly Entry needs a brief state flip to update
        try:
            self.output_entry.configure(state=NORMAL)
            self.output_var.set(abs_path)
            self.output_entry.configure(state="readonly")
        except Exception:
            self.output_var.set(abs_path)

    def _on_source_path_var_write(self, *_args) -> None:
        """Trace callback: path typed/pasted/dropped into the source field."""
        if self._suppress_source_trace:
            return
        raw = self.source_var.get().strip().strip('"').strip("'")
        if not raw or raw == self._source_meta_last_applied:
            return
        # Debounce rapid keystrokes — apply metadata when path looks complete enough.
        p = Path(raw).expanduser()
        if p.is_dir() or p.is_file():
            self.after(10, lambda: self._apply_path_metadata(raw, force_library=True))

    def _apply_source_path(self, path: str | Path, *, scan: bool = False) -> None:
        """
        Set the source path, auto-fill Library/Vendor, lock in-place NKS output target,
        optionally kick off artwork scan. Used by Browse and drop handlers.
        """
        path_str = str(path).strip().strip('"').strip("'")
        if not path_str:
            return
        self._suppress_source_trace = True
        try:
            self.source_var.set(path_str)
        finally:
            self._suppress_source_trace = False
        self._apply_path_metadata(path_str, force_library=True)
        if scan:
            self.scan_source()

    def _apply_path_metadata(self, path: str | Path, *, force_library: bool = True) -> None:
        """
        Extract base folder → Library Name, parent/conventions → Vendor Name,
        and lock the NKS output target to this absolute path (in-place).
        """
        path_str = str(path).strip().strip('"').strip("'")
        if not path_str:
            return

        # Always sync auto output target to the absolute source path
        self._set_auto_output_target(path_str)

        if path_str == self._source_meta_last_applied and force_library:
            # Still allow re-apply if fields were cleared
            if self.library_var.get().strip() and self.vendor_var.get().strip():
                return

        library, vendor = derive_metadata_from_source_path(path_str)
        changed: list[str] = []

        if library and (force_library or not self.library_var.get().strip()):
            if self.library_var.get() != library:
                self.library_var.set(library)
                changed.append(f"Library Name ← {library!r}")

        if vendor:
            # Smart guess: populate when empty OR when previous value matched an older auto-guess
            current_vendor = self.vendor_var.get().strip()
            if (not current_vendor) or current_vendor == getattr(self, "_last_auto_vendor", ""):
                if current_vendor != vendor:
                    self.vendor_var.set(vendor)
                    changed.append(f"Vendor Name ← {vendor!r} (guess)")
                self._last_auto_vendor = vendor

        changed.append(f"NKS target (in-place) ← {self.output_var.get()!r}")

        self._source_meta_last_applied = path_str
        if changed:
            self._log("Path metadata: " + "; ".join(changed))
            self.status_var.set(" · ".join(changed[:3]))

    def _on_drop_path(self, event) -> str | None:
        """
        Optional drag-and-drop handler (TkDnD / macOS file drops).
        Expects event.data to contain one or more paths.
        """
        data = getattr(event, "data", None) or ""
        if not data:
            return None
        # TkDnD may brace-wrap paths with spaces: {/path/with spaces}
        tokens = re.findall(r"\{([^}]*)\}|(\S+)", str(data))
        paths = [a or b for a, b in tokens]
        if not paths:
            return None
        dropped = paths[0].strip().strip('"').strip("'")
        if dropped:
            self._apply_source_path(dropped, scan=True)
        return event.action if hasattr(event, "action") else None

    def _try_enable_drag_drop(self) -> None:
        """Enable path drops when tkinterdnd2 is available; no-op otherwise."""
        try:
            from tkinterdnd2 import DND_FILES  # type: ignore

            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop_path)
            # Also accept drops on the source entry area if present
            self._log("Drag-and-drop enabled (tkinterdnd2).")
        except Exception:
            # Native Tk without DnD extension — Browse + paste still auto-fill.
            pass

    def scan_source(self) -> None:
        raw = self.source_var.get().strip().strip('"').strip("'")
        if not raw:
            messagebox.showwarning(APP_TITLE, "Set a Source Unlicensed Quickload Path first.", parent=self)
            return
        source = Path(raw).expanduser()
        if not source.is_dir():
            messagebox.showerror(APP_TITLE, f"Not a directory:\n{source}", parent=self)
            return

        # Ensure metadata is current even if user scanned without re-browsing
        self._apply_path_metadata(source, force_library=True)

        self.status_var.set(f"Scanning {source}…")
        self.update_idletasks()
        assets = scan_artwork(source)
        self.detected_assets = assets
        self.selected_asset = None
        self.use_found_artwork.set(False)

        labels = [a.label for a in assets]
        self.asset_combo["values"] = labels
        if labels:
            self.asset_combo.current(0)
            self.selected_asset = assets[0].path
            self._show_image(self.selected_asset)
            self._log(f"Found {len(assets)} artwork candidate(s) under {source}")

            # True desktop dialog for artwork routing
            dlg = ArtworkChoiceDialog(self, len(assets))
            self.wait_window(dlg)
            choice = dlg.result or "found"
            if choice == "found":
                self.use_found_artwork.set(True)
                self._set_prompt_visible(False)
                self.status_var.set(f"Using found artwork: {self.selected_asset.name}")
                self._log("User chose: Use found library artwork")
            else:
                self.use_found_artwork.set(False)
                self._set_prompt_visible(True)
                self.status_var.set("Custom artwork by description — enter AI Artwork Prompt")
                self._log("User chose: Create new custom artwork by description")
        else:
            self.asset_combo.set("")
            self._clear_canvas("No matching wallpaper/banner/folder/artwork/icon images found")
            self.use_found_artwork.set(False)
            self._set_prompt_visible(True)
            self.status_var.set("No artwork found — enter an AI Artwork Prompt")
            self._log(f"No artwork keywords matched under {source}")
            messagebox.showinfo(
                APP_TITLE,
                "No library artwork images were found.\n"
                "Enter an AI Artwork Prompt, then Build & Register Library.",
                parent=self,
            )

    def _on_asset_selected(self, _event=None) -> None:
        idx = self.asset_combo.current()
        if idx < 0 or idx >= len(self.detected_assets):
            return
        self.selected_asset = self.detected_assets[idx].path
        self._show_image(self.selected_asset)
        if self.use_found_artwork.get():
            self.status_var.set(f"Selected artwork: {self.selected_asset.name}")

    def _clear_canvas(self, message: str) -> None:
        self.canvas.delete("all")
        self._photo_ref = None
        self.canvas.create_text(
            PREVIEW_CANVAS_W // 2,
            PREVIEW_CANVAS_H // 2,
            text=message,
            fill="#8b93a7",
            font=("Helvetica", 12),
        )

    def _show_image(self, path: Path) -> None:
        try:
            img = Image.open(path)
            img.load()
            preview = ImageOps.contain(
                img.convert("RGBA"),
                (PREVIEW_CANVAS_W - 16, PREVIEW_CANVAS_H - 16),
                method=Image.Resampling.LANCZOS,
            )
            photo = ImageTk.PhotoImage(preview)
            self._photo_ref = photo
            self.canvas.delete("all")
            self.canvas.create_image(
                PREVIEW_CANVAS_W // 2,
                PREVIEW_CANVAS_H // 2,
                image=photo,
                anchor="center",
            )
            self.canvas.create_text(
                8,
                PREVIEW_CANVAS_H - 8,
                text=str(path),
                fill="#c0c6d8",
                anchor="sw",
                font=("Menlo", 9),
            )
        except Exception as exc:  # noqa: BLE001
            self._clear_canvas(f"Could not load image:\n{exc}")

    # ----- Build -----

    def start_build(self) -> None:
        if self._build_thread and self._build_thread.is_alive():
            messagebox.showinfo(APP_TITLE, "A build is already running.", parent=self)
            return

        # Validate only at Build click-time using live field values (after any auto-fill).
        vendor = self.vendor_var.get().strip()
        library = self.library_var.get().strip()
        source_raw = self.source_var.get().strip().strip('"').strip("'")
        source = Path(source_raw).expanduser() if source_raw else None

        # Last-chance auto-fill + lock in-place output to absolute source path
        if source is not None and source.exists():
            self._apply_path_metadata(source, force_library=True)
            vendor = self.vendor_var.get().strip()
            library = self.library_var.get().strip()
            try:
                source = source.resolve()
            except OSError:
                pass

        # Output is always the source library folder (no manual selection).
        output = source

        errors: list[str] = []
        # Name alerts only if still completely empty at the exact moment Build is clicked.
        if not vendor:
            errors.append("Vendor Name is required.")
        if not library:
            errors.append("Library Name is required.")
        if not source or not source.is_dir():
            errors.append("Valid Source Unlicensed Quickload Path is required.")
        if not self.ffmpeg_path:
            errors.append("ffmpeg is not available.")
        if self.use_found_artwork.get() and (not self.selected_asset or not self.selected_asset.is_file()):
            errors.append("Select a valid found artwork image (or re-scan).")
        if (not self.use_found_artwork.get()) and not self.ai_prompt_var.get().strip() and not self.detected_assets:
            # Allow empty — placeholder still works — soft warning only
            pass

        if errors:
            messagebox.showerror(APP_TITLE, "\n".join(errors), parent=self)
            return

        assert source is not None and output is not None and self.ffmpeg_path is not None

        self.build_btn.configure(state=DISABLED)
        self.progress["value"] = 0
        self._log("=" * 60)
        self._log(f"Build started {datetime.now().isoformat(timespec='seconds')}")
        self._log(f"Vendor={vendor!r} Library={library!r}")
        self._log(f"Source library={source}")
        self._log(f"NKS output target (in-place)={output}")
        self._log(f"Global NI image → {DEFAULT_IMAGE_ROOT}")
        self._log(f"Global NI dist_database → {DEFAULT_DIST_DB_ROOT}")
        self._log(f"App dir={APP_DIR}")
        self.status_var.set("Building…")

        use_found = bool(self.use_found_artwork.get())
        selected = self.selected_asset
        prompt = self.ai_prompt_var.get().strip() or f"{library} by {vendor}"
        ffmpeg = self.ffmpeg_path

        def worker() -> None:
            try:
                result = run_pipeline(
                    vendor=vendor,
                    library=library,
                    source_path=source,
                    output_path=output,  # same as source; pipeline deploys in-place
                    use_found_artwork=use_found,
                    selected_asset=selected,
                    ai_prompt=prompt,
                    ffmpeg=ffmpeg,
                    log=self._log,
                    progress=self._set_progress,
                )
                self._ui_queue.put(("done", result))
            except Exception:  # noqa: BLE001
                self._ui_queue.put(("error", traceback.format_exc()))

        self._build_thread = threading.Thread(target=worker, name="nks-build", daemon=True)
        self._build_thread.start()

    def _on_build_done(self, result: PipelineResult) -> None:
        self.build_btn.configure(state=NORMAL)
        summary = (
            f"Presets mirrored: {result.presets_mirrored}\n"
            f"Previews created: {result.previews_created}\n"
            f"Missing audio: {result.previews_missing_audio}\n"
            f"Preview failures: {result.previews_failed}\n"
        )
        if result.database_path:
            summary += f"\nDatabase:\n{result.database_path}\n"
        if result.images_written:
            summary += "\nImages:\n" + "\n".join(f"  {k}: {v}" for k, v in result.images_written.items())

        if result.errors and not result.images_written:
            self.status_var.set("Build failed.")
            messagebox.showerror(APP_TITLE, "Build failed.\n\n" + "\n".join(result.errors[:8]), parent=self)
        elif result.errors or result.warnings:
            self.status_var.set("Build finished with warnings.")
            messagebox.showwarning(
                APP_TITLE,
                "Build finished with warnings.\n\n" + summary + "\n" + "\n".join(result.warnings + result.errors[:5]),
                parent=self,
            )
        else:
            self.status_var.set("Build complete.")
            messagebox.showinfo(APP_TITLE, "Library build & registration complete.\n\n" + summary, parent=self)

    def _on_build_error(self, tb: str) -> None:
        self.build_btn.configure(state=NORMAL)
        self._append_log(tb)
        self.status_var.set("Build crashed.")
        messagebox.showerror(APP_TITLE, "Unexpected error during build.\nSee build log.", parent=self)

    def _on_close(self) -> None:
        if self._build_thread and self._build_thread.is_alive():
            if not messagebox.askyesno(
                APP_TITLE,
                "A build is still running. Quit anyway?",
                parent=self,
            ):
                return
        self.destroy()


def main() -> int:
    # Ensure project-local resolution context
    os.environ.setdefault("NKS_DESKTOP_APP_DIR", str(APP_DIR))
    app = NKSDesktopApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
