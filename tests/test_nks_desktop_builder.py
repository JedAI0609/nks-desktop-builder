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
def _sandbox_ni_roots(monkeypatch, tmp_path):
    """Keep every test away from live NI resources and registration."""
    import nks_register

    monkeypatch.setattr(nks_register, "install_library_for_kontakt_kk", lambda **kwargs: None)
    monkeypatch.setattr(m, "ensure_quickload_aliases", lambda *args: None)
    monkeypatch.setattr(m, "DEFAULT_IMAGE_ROOT", tmp_path / "ni" / "image")
    monkeypatch.setattr(m, "DEFAULT_DIST_DB_ROOT", tmp_path / "ni" / "db")


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

    def test_subfolder_keyword_alone_does_not_assign_timing(self, tmp_path: Path, ffmpeg: str):
        root = tmp_path / "Lib"
        timing = root / "Samples" / "01_2nd Timing"
        oneshots = root / "Samples" / "05_OneShots"
        timing.mkdir(parents=True)
        oneshots.mkdir(parents=True)
        _make_wav(ffmpeg, timing / "GrooveA.wav", 440)
        _make_wav(ffmpeg, oneshots / "HitA.wav", 880)
        idx = m.build_source_audio_index(root)
        hit = m.pick_matching_wav("MB03_Clocks_Loops", idx, root)
        assert hit is None

    def test_exact_name_beats_folder_affinity(self, tmp_path: Path, ffmpeg: str):
        """Exact Deep_Pad.wav must beat Samples/Pad/Other.wav folder affinity."""
        root = tmp_path / "Lib"
        samples = root / "Samples"
        pad = samples / "Pad"
        pad.mkdir(parents=True)
        _make_wav(ffmpeg, samples / "Deep_Pad.wav", 440)
        _make_wav(ffmpeg, pad / "Other.wav", 880)
        idx = m.build_source_audio_index(root)
        hit = m.pick_matching_wav("Deep_Pad", idx, root)
        assert hit is not None
        assert hit.name == "Deep_Pad.wav"

    def test_rejects_short_substring_false_positive(self, tmp_path: Path, ffmpeg: str):
        root = tmp_path / "Lib"
        samples = root / "Samples"
        samples.mkdir(parents=True)
        _make_wav(ffmpeg, samples / "Chair.wav", 440)
        idx = m.build_source_audio_index(root)
        assert m.pick_matching_wav("Air", idx, root) is None

    def test_rejects_shared_token_only_piano(self, tmp_path: Path, ffmpeg: str):
        root = tmp_path / "Lib"
        samples = root / "Samples"
        samples.mkdir(parents=True)
        _make_wav(ffmpeg, samples / "Harsh_Piano.wav", 440)
        idx = m.build_source_audio_index(root)
        assert m.pick_matching_wav("Soft_Piano", idx, root) is None

    def test_rejects_folder_only_loops_alien(self, tmp_path: Path, ffmpeg: str):
        root = tmp_path / "Lib"
        loops = root / "Samples" / "Loops"
        loops.mkdir(parents=True)
        _make_wav(ffmpeg, loops / "Alien.wav", 440)
        idx = m.build_source_audio_index(root)
        assert m.pick_matching_wav("MB03_Clocks_Loops", idx, root) is None

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
        # PNG specs + WEBP aliases for NKS2 assets
        assert set(m.NI_IMAGE_SPECS).issubset(set(written))
        for webp in m.NI_WEBP_ALIASES.values():
            assert webp in written
        for name, dims in m.NI_IMAGE_SPECS.items():
            assert Image.open(written[name]).size == dims
        assert Image.open(written["NKS2_software_tile.webp"]).size == (420, 316)
        assert Image.open(written["NKS2_hardware_banner.webp"]).size == (1280, 212)
        assert Image.open(written["NKS2_hardware_banner_1280x152.webp"]).size == (1280, 152)

    def test_dist_database_payload(self, tmp_path: Path):
        path = m.write_dist_database("Acme", "Neon Pads", dest_root=tmp_path)
        data = json.loads(path.read_text())
        assert data["vendor"] == "Acme"
        assert data["library"] == "Neon Pads"
        assert data["shortname"] == "neon_pads"
        assert data["product"]["nks"] is True
        assert data["paths"]["image"] == "image/Neon Pads"
        assert (tmp_path / "Neon Pads" / "database.json").is_file()
        assert (tmp_path / "Acme" / "Neon Pads" / "Neon Pads.meta").is_file()


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


class TestDragDropParsing:
    def test_brace_wrapped_path_with_spaces(self):
        paths = m.parse_drop_paths("{/Volumes/Instruments/Silence & Other Sounds/Mystery Box 3}")
        assert paths == ["/Volumes/Instruments/Silence & Other Sounds/Mystery Box 3"]

    def test_multiple_plain_paths(self):
        paths = m.parse_drop_paths("/tmp/a /tmp/b")
        assert paths == ["/tmp/a", "/tmp/b"]

    def test_file_url_decoding(self):
        paths = m.parse_drop_paths("file:///Users/me/My%20Lib")
        assert paths == ["/Users/me/My Lib"]

    def test_empty_payload(self):
        assert m.parse_drop_paths("") == []
        assert m.parse_drop_paths(None) == []


class TestAiArtworkHelpers:
    def test_compose_prompt_includes_library_context(self):
        text = m.compose_ai_image_prompt("neon fog pads", "Acme", "Neon Pads")
        assert "neon fog pads" in text
        assert "Neon Pads" in text
        assert "Acme" in text

    def test_resolve_key_prefers_explicit(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        monkeypatch.setattr(m, "APP_CONFIG_PATH", tmp_path / "cfg.json")
        (tmp_path / "cfg.json").write_text('{"openai_api_key": "cfg-key"}\n')
        assert m.resolve_openai_api_key("explicit-key") == "explicit-key"
        assert m.resolve_openai_api_key(None) == "env-key"

    def test_resolve_key_from_config_when_no_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = tmp_path / "cfg.json"
        monkeypatch.setattr(m, "APP_CONFIG_PATH", cfg)
        m.save_app_config({"openai_api_key": "from-config"}, path=cfg)
        assert m.resolve_openai_api_key() == "from-config"

    def test_placeholder_fallback_without_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(m, "load_app_config", lambda path=None: {})
        img, source = m.generate_artwork_from_prompt(
            "cinematic brass", "Vendor", "Lib", prefer_api=True
        )
        assert source == "placeholder"
        assert img.size[0] > 0 and img.size[1] > 0

    def test_decode_b64_payload(self):
        import base64
        import io

        buf = io.BytesIO()
        Image.new("RGB", (32, 16), (10, 20, 30)).save(buf, format="PNG")
        payload = {"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode("ascii")}]}
        img = m._decode_openai_image_payload(payload)
        assert img.size == (32, 16)

    def test_pipeline_uses_ai_helper_when_no_found_art(self, tmp_path, ffmpeg, monkeypatch):
        lib = tmp_path / "Lib"
        presets = lib / "P"
        presets.mkdir(parents=True)
        (presets / "Ghost.nki").write_bytes(b"x")

        calls = {}

        def fake_gen(prompt, vendor, library, **kwargs):
            calls["prompt"] = prompt
            img = Image.new("RGB", (64, 32), (1, 2, 3))
            return img, "placeholder"

        monkeypatch.setattr(m, "generate_artwork_from_prompt", fake_gen)
        m.DEFAULT_IMAGE_ROOT = tmp_path / "ni" / "image"
        m.DEFAULT_DIST_DB_ROOT = tmp_path / "ni" / "db"

        result = m.run_pipeline(
            vendor="V",
            library="L",
            source_path=lib,
            output_path=lib,
            use_found_artwork=False,
            selected_asset=None,
            ai_prompt="deep ocean pads",
            ffmpeg=ffmpeg,
            prefer_ai_api=True,
        )
        assert result.errors == []
        assert calls["prompt"] == "deep ocean pads"
        assert (m.DEFAULT_IMAGE_ROOT / "L" / "NKS2_software_tile.webp").is_file()
        assert (m.DEFAULT_IMAGE_ROOT / "L" / "NKS2_hardware_banner.webp").is_file()

    def test_save_master_cache(self, tmp_path):
        img = Image.new("RGBA", (40, 20), (9, 8, 7, 255))
        path = m.save_master_artwork_cache(img, "Acme", "Pads", dest_dir=tmp_path)
        assert path.is_file()
        assert Image.open(path).size == (40, 20)


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


def _stub_audio(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not really audio" * 20)
    return path


class TestMatchingRegressions:
    @pytest.mark.parametrize("exact", ["MB03_Deep_Pad.WaV", "mb03 deep-pad.AiFf", "mb03deeppad.WAVE"])
    def test_full_name_beats_stripped_and_folder_matches(self, tmp_path, monkeypatch, exact):
        monkeypatch.setattr(m, "is_safe_audio_file", lambda path, **kwargs: True)
        expected = _stub_audio(tmp_path / exact)
        _stub_audio(tmp_path / "Content" / "sAmPlEs" / "Pad" / "Deep_Pad.fLaC")
        _stub_audio(tmp_path / "Content" / "sAmPlEs" / "Pad" / "Other.ogg")
        index = m.build_source_audio_index(tmp_path)
        assert m.pick_matching_wav("MB03_Deep_Pad", index, tmp_path) == expected

    @pytest.mark.parametrize(
        "preset,sample",
        [
            ("MB03_Soft_Piano", "Harsh_Piano.wav"),
            ("MB03_Deep_Pad", "Pad.wav"),
            ("Phase2_Soft_Piano", "Harsh_Piano.wav"),
            ("Soft_Piano", "Piano.wav"),
            ("Soft_Warm_Piano", "Harsh_Warm_Piano.wav"),
            ("Air", "Chair.wav"),
        ],
    )
    def test_descriptive_tokens_not_discarded(self, tmp_path, monkeypatch, preset, sample):
        monkeypatch.setattr(m, "is_safe_audio_file", lambda path, **kwargs: True)
        _stub_audio(tmp_path / "Samples" / sample)
        assert m.pick_matching_wav(preset, m.build_source_audio_index(tmp_path), tmp_path) is None

    @pytest.mark.parametrize("folder", ["Loops", "01_2nd Timing", "Clocks_Loops"])
    def test_no_folder_only_evidence(self, tmp_path, monkeypatch, folder):
        monkeypatch.setattr(m, "is_safe_audio_file", lambda path, **kwargs: True)
        _stub_audio(tmp_path / "Nested" / "sAmPlEs" / folder / "Alien.WAVE")
        assert m.pick_matching_wav(
            "MB03_Clocks_Loops", m.build_source_audio_index(tmp_path), tmp_path
        ) is None

    def test_filename_evidence_with_note_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "is_safe_audio_file", lambda path, **kwargs: True)
        expected = _stub_audio(tmp_path / "Nested" / "sAmPlEs" / "Pads" / "Soft_Piano_C4.FLAC")
        assert m.pick_matching_wav(
            "MB03_Soft_Piano", m.build_source_audio_index(tmp_path), tmp_path
        ) == expected

    @pytest.mark.parametrize("suffix,codec", [(".AiFf", "pcm_s16be"), (".fLaC", "flac")])
    def test_real_mixed_case_nested_audio(self, tmp_path, ffmpeg, suffix, codec):
        source = tmp_path / "source.wav"
        _make_wav(ffmpeg, source, dur=0.1)
        expected = tmp_path / "Content" / "sAmPlEs" / "Nested" / f"dEeP pAd{suffix}"
        expected.parent.mkdir(parents=True)
        subprocess.check_call([
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-c:a", codec, str(expected),
        ])
        assert m.pick_matching_wav(
            "Deep_Pad", m.build_source_audio_index(tmp_path), tmp_path
        ) == expected

    @pytest.mark.parametrize("prefix", ["Phase2", "Soft2", "Dark", "Bright", "Deep"])
    def test_numbered_descriptions_are_not_library_codes(self, prefix):
        assert m._strip_library_prefixes(f"{prefix}_Warm_Piano") == [f"{prefix}_Warm_Piano"]
        assert m._preset_tokens(f"{prefix}_Warm_Piano") == [prefix.lower(), "warm", "piano"]

    @pytest.mark.parametrize("prefix", ["MB03", "mb03", "Mb03", "LIB1", "lib1", "LiB1", "A1", "001"])
    def test_code_stripping_preserves_entire_description(self, prefix):
        assert m._strip_library_prefixes(f"{prefix}_Soft_Piano") == [
            f"{prefix}_Soft_Piano", "Soft_Piano"
        ]
        assert m._preset_tokens(f"{prefix}_Soft_Piano") == ["soft", "piano"]


class TestPreviewSafety:
    @pytest.mark.parametrize("kind", ["outside_dir", "inside_dir", "dangling_dir", "file", "dangling_file"])
    def test_rejects_symlink_destinations(self, tmp_path, monkeypatch, kind):
        root = tmp_path / "Lib"
        root.mkdir()
        (root / "Tone.nki").write_bytes(b"preset")
        _stub_audio(root / "Tone.wav")
        outside = tmp_path / "Outside"
        outside.mkdir()
        sentinel = outside / "sentinel.ogg"
        sentinel.write_bytes(b"unchanged")
        previews = root / ".previews"
        if kind.endswith("dir"):
            target = root / "Inside" if kind == "inside_dir" else outside
            if kind == "inside_dir":
                target.mkdir()
            if kind == "dangling_dir":
                target = outside / "must-not-create"
            previews.symlink_to(target, target_is_directory=True)
        else:
            previews.mkdir()
            target = sentinel if kind == "file" else outside / "must-not-create.ogg"
            (previews / "Tone.nki.ogg").symlink_to(target)
        monkeypatch.setattr(m, "is_safe_audio_file", lambda path, **kwargs: True)
        calls = []
        monkeypatch.setattr(m.subprocess, "run", lambda *args, **kwargs: calls.append(args))
        result = m.mirror_presets_and_previews(root, root, "fake-ffmpeg")
        assert result[:4] == (1, 0, 1, 0)
        assert result[4]
        assert calls == []
        assert sentinel.read_bytes() == b"unchanged"
        assert not (outside / "must-not-create").exists()
        assert not (outside / "must-not-create.ogg").exists()

    @pytest.mark.parametrize("failure", ["exit", "exception", "empty"])
    def test_failed_conversion_preserves_preview(self, tmp_path, monkeypatch, failure):
        (tmp_path / "Tone.nki").write_bytes(b"preset")
        audio = _stub_audio(tmp_path / "Tone.wav")
        previews = tmp_path / ".previews"
        previews.mkdir()
        output = previews / "Tone.nki.ogg"
        output.write_bytes(b"previous valid preview")
        monkeypatch.setattr(m, "is_safe_audio_file", lambda path, **kwargs: True)

        def convert(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"" if failure == "empty" else b"partial")
            if failure == "exception":
                raise OSError("encoder unavailable")
            return subprocess.CompletedProcess(cmd, 0 if failure == "empty" else 1, "", "failure")

        monkeypatch.setattr(m.subprocess, "run", convert)
        result = m.mirror_presets_and_previews(tmp_path, tmp_path, "fake-ffmpeg")
        assert result[:4] == (1, 0, 1, 0)
        assert output.read_bytes() == b"previous valid preview"
        assert audio.read_bytes() == b"not really audio" * 20
        assert list(previews.iterdir()) == [output]

    def test_retry_then_atomic_success(self, tmp_path, monkeypatch):
        (tmp_path / "Tone.nki").write_bytes(b"preset")
        _stub_audio(tmp_path / "Tone.wav")
        _stub_audio(tmp_path / "Samples" / "Tone.flac")
        previews = tmp_path / ".previews"
        previews.mkdir()
        output = previews / "Tone.nki.ogg"
        output.write_bytes(b"old")
        monkeypatch.setattr(m, "is_safe_audio_file", lambda path, **kwargs: True)
        calls = []

        def convert(cmd, **kwargs):
            assert Path(cmd[-1]) != output
            assert output.read_bytes() == b"old"
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"partial" if len(calls) == 1 else b"new preview")
            return subprocess.CompletedProcess(cmd, 1 if len(calls) == 1 else 0, "", "")

        monkeypatch.setattr(m.subprocess, "run", convert)
        assert m.mirror_presets_and_previews(tmp_path, tmp_path, "fake")[1:4] == (1, 0, 0)
        assert len(calls) == 2
        assert output.read_bytes() == b"new preview"
        assert list(previews.iterdir()) == [output]

    @pytest.mark.parametrize("kind", ["directory_symlink", "directory_replaced", "file_symlink", "staging_symlink"])
    def test_destination_swap_during_conversion_is_rejected(self, tmp_path, monkeypatch, kind):
        root = tmp_path / "Lib"
        root.mkdir()
        (root / "Tone.nki").write_bytes(b"preset")
        _stub_audio(root / "Tone.wav")
        previews = root / ".previews"
        previews.mkdir()
        output = previews / "Tone.nki.ogg"
        output.write_bytes(b"old preview")
        outside = tmp_path / "Outside"
        outside.mkdir()
        sentinel = outside / "sentinel.ogg"
        sentinel.write_bytes(b"do not touch")
        monkeypatch.setattr(m, "is_safe_audio_file", lambda path, **kwargs: True)

        def convert(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"new preview")
            if kind.startswith("directory"):
                previews.rename(root / "Held")
                if kind == "directory_symlink":
                    previews.symlink_to(outside, target_is_directory=True)
                else:
                    previews.mkdir()
            elif kind == "file_symlink":
                output.rename(previews / "old.ogg")
                output.symlink_to(sentinel)
            else:
                stage = next(previews.glob(".preview-*.ogg"))
                stage.unlink()
                stage.symlink_to(sentinel)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(m.subprocess, "run", convert)
        assert m.mirror_presets_and_previews(root, root, "fake")[:4] == (1, 0, 1, 0)
        assert sentinel.read_bytes() == b"do not touch"
        preserved = root / "Held" / output.name if kind.startswith("directory") else (
            previews / "old.ogg" if kind == "file_symlink" else output
        )
        assert preserved.read_bytes() == b"old preview"
        assert list(root.rglob(".preview-*.ogg")) == []


class TestAudioValidation:
    @pytest.mark.parametrize("mode", ["invalid", "no_stream", "empty", "timeout", "oserror"])
    def test_probe_failures_rejected(self, tmp_path, monkeypatch, mode):
        audio = _stub_audio(tmp_path / "Tone.wav")
        monkeypatch.setattr(m.shutil, "which", lambda name: "/fake/ffprobe")

        def probe(cmd, **kwargs):
            assert kwargs["timeout"] == 8
            if mode == "timeout":
                raise subprocess.TimeoutExpired(cmd, 8)
            if mode == "oserror":
                raise OSError("probe unavailable")
            return subprocess.CompletedProcess(
                cmd, 1 if mode == "invalid" else 0, "wav\n" if mode == "no_stream" else "", ""
            )

        monkeypatch.setattr(m.subprocess, "run", probe)
        assert not m.is_safe_audio_file(audio)

    def test_absent_probe_defers_to_encoder(self, tmp_path, monkeypatch):
        audio = _stub_audio(tmp_path / "Tone.wav")
        monkeypatch.setattr(m.shutil, "which", lambda name: None)
        monkeypatch.setattr(m.subprocess, "run", lambda *a, **kw: pytest.fail("no probe should run"))
        assert m.is_safe_audio_file(audio)

    def test_absent_probe_invalid_audio_cannot_replace_preview(self, tmp_path, monkeypatch, ffmpeg):
        _stub_audio(tmp_path / "Tone.wav")
        (tmp_path / "Tone.nki").write_bytes(b"preset")
        previews = tmp_path / ".previews"
        previews.mkdir()
        output = previews / "Tone.nki.ogg"
        output.write_bytes(b"old preview")
        monkeypatch.setattr(m.shutil, "which", lambda name: None)
        assert m.mirror_presets_and_previews(tmp_path, tmp_path, ffmpeg)[:4] == (1, 0, 1, 0)
        assert output.read_bytes() == b"old preview"
        assert list(previews.iterdir()) == [output]

    def test_standalone_validation_has_no_global_cache(self, tmp_path, monkeypatch):
        audio = _stub_audio(tmp_path / "Tone.wav")
        monkeypatch.setattr(m.shutil, "which", lambda name: "/fake/ffprobe")
        responses = iter([0, 1])
        monkeypatch.setattr(m.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, next(responses), "audio\nwav\n", ""
        ))
        assert m.is_safe_audio_file(audio)
        assert not m.is_safe_audio_file(audio)

    def test_changed_file_revalidated_within_cache(self, tmp_path, monkeypatch):
        audio = _stub_audio(tmp_path / "Tone.wav")
        monkeypatch.setattr(m.shutil, "which", lambda name: "/fake/ffprobe")
        responses = iter([0, 1])
        monkeypatch.setattr(m.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, next(responses), "audio\nwav\n", ""
        ))
        cache = {}
        assert m.is_safe_audio_file(audio, cache=cache)
        assert m.is_safe_audio_file(audio, cache=cache)
        audio.write_bytes(b"changed invalid audio" * 50)
        assert not m.is_safe_audio_file(audio, cache=cache)

    def test_probe_cache_is_shared_within_run_only(self, tmp_path, monkeypatch):
        _stub_audio(tmp_path / "Tone.wav")
        for ext in ["nki", "nksn", "nkm"]:
            (tmp_path / f"Tone.{ext}").write_bytes(b"preset")
        monkeypatch.setattr(m.shutil, "which", lambda name: "/fake/ffprobe")
        probes = []

        def run(cmd, **kwargs):
            if cmd[0] == "/fake/ffprobe":
                probes.append(cmd)
                return subprocess.CompletedProcess(cmd, 0 if len(probes) == 1 else 1, "audio\nwav\n", "")
            Path(cmd[-1]).write_bytes(b"preview")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(m.subprocess, "run", run)
        first = m.mirror_presets_and_previews(tmp_path, tmp_path, "fake-ffmpeg")
        assert first[:4] == (3, 3, 0, 0)
        assert len(probes) == 1
        second = m.mirror_presets_and_previews(tmp_path, tmp_path, "fake-ffmpeg")
        assert second[:4] == (3, 0, 0, 3)
        assert len(probes) == 2

    def test_actual_invalid_audio_rejected(self, tmp_path):
        if not m.shutil.which("ffprobe"):
            pytest.skip("ffprobe not available")
        assert not m.is_safe_audio_file(_stub_audio(tmp_path / "Invalid.wav"))
