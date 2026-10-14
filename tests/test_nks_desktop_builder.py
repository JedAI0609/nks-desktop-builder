"""Regression suite for nks_desktop_builder (and preview naming parity)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import nks_desktop_builder as m  # noqa: E402


@pytest.fixture(scope="module")
def ffmpeg() -> str:
    path = m.resolve_ffmpeg()
    if not path:
        pytest.skip("ffmpeg not available")
    return path


@pytest.fixture(autouse=True)
def _restore_ni_roots():
    """Prevent DEFAULT_* path mutations from leaking across tests."""
    img = m.DEFAULT_IMAGE_ROOT
    db = m.DEFAULT_DIST_DB_ROOT
    yield
    m.DEFAULT_IMAGE_ROOT = img
    m.DEFAULT_DIST_DB_ROOT = db


def _make_wav(ffmpeg: str, path: Path, freq: float = 440.0, dur: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={dur}",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )


# ---------------------------------------------------------------------------
# Path metadata / auto-fill
# ---------------------------------------------------------------------------


class TestPathMetadata:
    def test_mystery_box_vendor_library(self):
        lib, vend = m.derive_metadata_from_source_path(
            "/Volumes/Instruments/Silence & Other Sounds/Mystery Box 3"
        )
        assert lib == "Mystery Box 3"
        assert vend == "Silence & Other Sounds"

    def test_nested_creator_library(self):
        lib, vend = m.derive_metadata_from_source_path(
            "/Users/someone/Libraries/Acme Soundworks/Neon Pads XL"
        )
        assert lib == "Neon Pads XL"
        assert vend == "Acme Soundworks"

    def test_users_home_not_vendor(self):
        lib, vend = m.derive_metadata_from_source_path("/Users/fuckem/dev")
        assert lib == "dev"
        assert vend == ""

    def test_sanitize_strips_ampersand_for_fs(self):
        assert m.sanitize_segment("Silence & Other Sounds") == "Silence Other Sounds"
        assert m.slugify("Mystery Box 3") == "mystery_box_3"


# ---------------------------------------------------------------------------
# Audio discovery
# ---------------------------------------------------------------------------


class TestAudioDiscovery:
    def test_recursive_index_and_samples_preference(self, tmp_path: Path, ffmpeg: str):
        root = tmp_path / "Lib"
        samples = root / "Samples" / "Loops"
        samples.mkdir(parents=True)
        _make_wav(ffmpeg, samples / "MB03_Clocks_Loops.wav", 440)
        _make_wav(ffmpeg, root / "MB03_Clocks_Loops.wav", 100)  # decoy

        idx = m.build_source_audio_index(root)
        assert "mb03_clocks_loops" in idx
        assert len(idx["mb03_clocks_loops"]) == 2

        hit = m.pick_matching_wav("MB03_Clocks_Loops", idx, root)
        assert hit is not None
        assert "Samples" in hit.parts

    def test_instruments_data_fuzzy_stem(self, tmp_path: Path, ffmpeg: str):
        root = tmp_path / "Lib"
        nested = root / "Instruments Data" / "Wavs"
        nested.mkdir(parents=True)
        _make_wav(ffmpeg, nested / "Deep Pad.wav", 220)

        idx = m.build_source_audio_index(root)
        hit = m.pick_matching_wav("Deep_Pad", idx, root)
        assert hit is not None
        assert hit.name == "Deep Pad.wav"
        assert "Instruments Data" in hit.parts

    def test_strip_mb03_prefix_matches_samples(self, tmp_path: Path, ffmpeg: str):
        root = tmp_path / "Lib"
        samples = root / "Samples" / "Loops"
        samples.mkdir(parents=True)
        # wav without library prefix
        _make_wav(ffmpeg, samples / "Clocks_Loops.wav", 440)
        idx = m.build_source_audio_index(root)
        hit = m.pick_matching_wav("MB03_Clocks_Loops", idx, root)
        assert hit is not None
        assert hit.name == "Clocks_Loops.wav"
        assert "Samples" in hit.parts

    def test_no_random_fallback_for_unrelated_name(self, tmp_path: Path, ffmpeg: str):
        """Unrelated presets must NOT grab a random first Samples wav."""
        root = tmp_path / "Lib"
        samples = root / "Samples" / "05_OneShots"
        samples.mkdir(parents=True)
        _make_wav(ffmpeg, samples / "OnlyOne.wav", 330)
        idx = m.build_source_audio_index(root)
        hit = m.pick_matching_wav("TotallyUnrelatedPresetName", idx, root)
        assert hit is None

    def test_subfolder_keyword_prefers_timing(self, tmp_path: Path, ffmpeg: str):
        root = tmp_path / "Lib"
        timing = root / "Samples" / "01_2nd Timing"
        oneshots = root / "Samples" / "05_OneShots"
        timing.mkdir(parents=True)
        oneshots.mkdir(parents=True)
        _make_wav(ffmpeg, timing / "GrooveA.wav", 440)
        _make_wav(ffmpeg, oneshots / "HitA.wav", 880)
        idx = m.build_source_audio_index(root)
        hit = m.pick_matching_wav("MB03_Clocks_Loops", idx, root)
        assert hit is not None
        assert "01_2nd Timing" in hit.parts or "timing" in str(hit).lower()

    def test_previews_folder_excluded_from_index(self, tmp_path: Path, ffmpeg: str):
        root = tmp_path / "Lib"
        (root / ".previews").mkdir(parents=True)
        _make_wav(ffmpeg, root / "Real.wav")
        # Pretend preview artifact (extension in SOURCE_AUDIO list) under .previews
        (root / ".previews" / "Real.nki.ogg").write_bytes(b"OggS-fake-preview")

        idx = m.build_source_audio_index(root)
        # ogg under .previews must not be indexed as source stem "Real.nki"
        assert all(".previews" not in p.parts for paths in idx.values() for p in paths)


# ---------------------------------------------------------------------------
# NI images + database
# ---------------------------------------------------------------------------


class TestNiAssets:
    def test_format_ni_image_dimensions(self, tmp_path: Path):
        img = m.generate_placeholder_artwork("test", "Vendor", "Library")
        written = m.format_ni_images(img, tmp_path / "out")
        assert set(written) == set(m.NI_IMAGE_SPECS)
        assert len(written) == 6
        expected = {
            "MST_artwork.png": (134, 66),
            "MST_logo.png": (134, 66),
            "BND_artwork.png": (272, 90),
            "BND_logo.png": (272, 90),
            "NKS2_software_tile.png": (420, 136),
            "NKS2_hardware_banner.png": (1280, 212),
        }
        assert m.NI_IMAGE_SPECS == expected
        for name, dims in expected.items():
            assert Image.open(written[name]).size == dims

    def test_dist_database_payload(self, tmp_path: Path):
        path = m.write_dist_database("Acme", "Neon Pads", dest_root=tmp_path)
        data = json.loads(path.read_text())
        assert data["vendor"] == "Acme"
        assert data["library"] == "Neon Pads"
        assert data["shortname"] == "neon_pads"
        assert data["product"]["nks"] is True


# ---------------------------------------------------------------------------
# Pipeline: non-destructive in-place previews
# ---------------------------------------------------------------------------


class TestPipeline:
    def test_in_place_previews_non_destructive(self, tmp_path: Path, ffmpeg: str):
        lib = tmp_path / "Vendor" / "Library"
        presets = lib / "Presets"
        samples = lib / "Samples" / "A"
        presets.mkdir(parents=True)
        samples.mkdir(parents=True)

        _make_wav(ffmpeg, samples / "Tone.wav", 523.25)
        nki = presets / "Tone.nki"
        nki.write_bytes(b"NKI_BYTES_UNCHANGED")
        art = lib / "folder_artwork.png"
        Image.new("RGB", (200, 100), (10, 20, 30)).save(art)

        before_nki = nki.read_bytes()
        before_wav = (samples / "Tone.wav").read_bytes()

        # sandbox NI roots
        m.DEFAULT_IMAGE_ROOT = tmp_path / "ni" / "image"
        m.DEFAULT_DIST_DB_ROOT = tmp_path / "ni" / "db"

        result = m.run_pipeline(
            vendor="Vendor",
            library="Library",
            source_path=lib,
            output_path=lib,
            use_found_artwork=True,
            selected_asset=art,
            ai_prompt="",
            ffmpeg=ffmpeg,
        )
        assert result.errors == []
        assert result.previews_created == 1
        assert result.previews_missing_audio == 0

        ogg = presets / ".previews" / "Tone.nki.ogg"
        assert ogg.is_file()
        probe = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_name",
                "-of",
                "default=noprint_wrappers=1",
                str(ogg),
            ],
            text=True,
        )
        assert "vorbis" in probe
        assert "duration=2.0" in probe

        # originals untouched
        assert nki.read_bytes() == before_nki
        assert (samples / "Tone.wav").read_bytes() == before_wav

        # global-style registry written to sandbox
        assert (m.DEFAULT_IMAGE_ROOT / "Vendor" / "Library" / "MST_artwork.png").is_file()
        assert Path(result.database_path).is_file()

    def test_missing_audio_counted(self, tmp_path: Path, ffmpeg: str):
        lib = tmp_path / "L"
        presets = lib / "P"
        presets.mkdir(parents=True)
        (presets / "Ghost.nki").write_bytes(b"x")
        Image.new("RGB", (20, 20)).save(lib / "icon.png")

        m.DEFAULT_IMAGE_ROOT = tmp_path / "ni" / "image"
        m.DEFAULT_DIST_DB_ROOT = tmp_path / "ni" / "db"

        result = m.run_pipeline(
            vendor="V",
            library="L",
            source_path=lib,
            output_path=lib,
            use_found_artwork=True,
            selected_asset=lib / "icon.png",
            ai_prompt="",
            ffmpeg=ffmpeg,
        )
        assert result.previews_created == 0
        assert result.previews_missing_audio == 1
        assert not (presets / ".previews" / "Ghost.nki.ogg").exists()


# ---------------------------------------------------------------------------
# Artwork scan
# ---------------------------------------------------------------------------


class TestArtworkScan:
    def test_keyword_detection(self, tmp_path: Path):
        (tmp_path / "sub").mkdir()
        Image.new("RGB", (8, 8)).save(tmp_path / "sub" / "my_artwork.png")
        Image.new("RGB", (8, 8)).save(tmp_path / "ignore_me.png")
        found = m.scan_artwork(tmp_path)
        assert len(found) == 1
        assert "artwork" in found[0].matched_keywords


# ---------------------------------------------------------------------------
# ffmpeg command safety
# ---------------------------------------------------------------------------


class TestFfmpegCmd:
    def test_output_is_only_dst_ogg(self, tmp_path: Path):
        src = tmp_path / "in.wav"
        dst = tmp_path / ".previews" / "X.nki.ogg"
        cmd = m.ffmpeg_preview_cmd("ffmpeg", src, dst)
        assert cmd[cmd.index("-i") + 1] == str(src)
        assert cmd[-1] == str(dst)
        assert "-t" in cmd
        assert cmd[cmd.index("-t") + 1] == "2.0"


# ---------------------------------------------------------------------------
# CLI helper still importable / naming parity
# ---------------------------------------------------------------------------


class TestPreviewBotParity:
    def test_preview_bot_dual_extension_name(self):
        import nks_preview_bot as bot

        preset = Path("/lib/Bank/Foo.nki")
        out = bot.preview_output_path(preset)
        assert out == Path("/lib/Bank/.previews/Foo.nki.ogg")
