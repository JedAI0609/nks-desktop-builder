# NKS Desktop Builder

Standalone **macOS** desktop app that turns an instrument / Quickload folder into an NKS-friendly layout:

- Auto-fills **Vendor** and **Library** names from the selected path
- Formats NI artwork (`MST_artwork`, `MST_logo`, `BND_artwork`) with Pillow
- Registers metadata under `/Users/Shared/NI Resources/`
- Builds **2.0s** dual-extension `.ogg` previews next to presets (in-place) via **ffmpeg**

Primary UI: Tkinter (`nks_desktop_builder.py`). Optional CLI helper: `nks_preview_bot.py`.

---

## Requirements

| Dependency | Notes |
|------------|--------|
| **macOS** | Developed and packaged on macOS |
| **Python 3.13+ with Tk** | Homebrew `python@3.13` (system/Homebrew 3.14 may lack `_tkinter`) |
| **Pillow** | Image crop/scale for NI card sizes |
| **ffmpeg** | Preview encoding (`vorbis` + `-strict -2`, stereo) |
| **Write access** | `/Users/Shared/NI Resources/image` and `…/dist_database` |

Optional: [PyInstaller](https://pyinstaller.org/) to build `NKS Desktop Builder.app`.

---

## Quick setup

```bash
cd /Users/fuckem/dev   # or your clone of this repo

# Tk-enabled venv (recommended)
/usr/local/opt/python@3.13/bin/python3.13 -m venv venv-tk
source venv-tk/bin/activate
pip install --upgrade pip
pip install pillow requests pyinstaller

# ffmpeg (if needed)
brew install ffmpeg
```

On Apple Silicon, Python may live under `/opt/homebrew/opt/python@3.13/…`.

---

## Run (development)

```bash
./venv-tk/bin/python nks_desktop_builder.py
```

Or:

```bash
source venv-tk/bin/activate
python nks_desktop_builder.py
```

### CLI preview helper only

```bash
python nks_preview_bot.py \
  --source /path/to/wavs \
  --target /path/to/presets \
  --ffmpeg /usr/local/bin/ffmpeg
```

---

## Usage (desktop app)

1. **Browse** (or paste) **Source Unlicensed Quickload Path**  
   Example: `/Volumes/Instruments/Silence & Other Sounds/Mystery Box 3`
2. The app automatically:
   - Sets **Library Name** from the folder basename (`Mystery Box 3`)
   - Guesses **Vendor Name** from the parent path (`Silence & Other Sounds`)
   - Locks **NKS Output Target** to that same absolute path (**in-place** — no separate output browse)
3. **Scan** finds artwork whose names contain `wallpaper`, `banner`, `folder`, `artwork`, or `icon`
4. Choose found artwork or enter an **AI Artwork Prompt** (local placeholder generator until you wire a real API)
5. Click **Build & Register Library**

### What the build does

| Stage | Destination |
|-------|-------------|
| **Artwork** | Primary: `/Users/Shared/NI Resources/image/[Library]/` (factory-style) + vendor path copy. Full Kontakt/KK set: MST/BND/VB/OSO + `MST_plugin`, NKS2 tile **420×316** (PNG+WEBP), banners 1280×212 and 1280×152 (PNG+WEBP), plus `<Library>.meta` |
| **Database JSON** | `/Users/Shared/NI Resources/dist_database/[Vendor]/[Library]/` |
| **Presets + previews** | **Inside the selected source library**: hidden `.previews/` folders with `[PresetName].[ext].ogg` (hard trim **2.0s**, vorbis) |

Vendor/library path segments are sanitized for the filesystem (e.g. `&` removed) when writing under `NI Resources`.

---

## Package macOS `.app`

```bash
./venv-tk/bin/pyinstaller \
  --noconfirm \
  --windowed \
  --name "NKS Desktop Builder" \
  --distpath . \
  --workpath build \
  --specpath . \
  --collect-all PIL \
  --hidden-import PIL.ImageTk \
  nks_desktop_builder.py

open "./NKS Desktop Builder.app"
```

A checked-in `NKS Desktop Builder.spec` can be reused with:

```bash
./venv-tk/bin/pyinstaller --noconfirm "NKS Desktop Builder.spec"
```

---

## Project layout

```text
nks_desktop_builder.py   # Tk desktop app + full pipeline
nks_preview_bot.py       # Headless recursive .previews generator
NKS Desktop Builder.spec # PyInstaller spec
README.md
.gitignore
```

---

## Notes

- **In-place output**: conversion never asks for a separate “User Content” folder; `.previews` are written beside existing `.nki` / `.nksn` / `.nksf` (and related) presets.
- **ffmpeg**: this project’s Homebrew build often exposes native `vorbis` (not `libvorbis`). The app uses `-c:a vorbis -strict -2 -ac 2`.
- **Drag-and-drop**: enabled if `tkinterdnd2` is installed; otherwise use Browse or paste a path.
- **AI artwork**: placeholder is generated with Pillow; `requests` is available for a future generative API.
- **Preview audio matching**: presets are paired to source WAVs using filename similarity plus Samples-subfolder/token scoring (e.g. “loop/clock” → timing folders). Unrelated presets do **not** fall back to a random first sample.
- **Safe audio gate**: candidates must exist, be non-empty, and (when `ffprobe` is available) decode as audio before preview encode.

### Tests

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install pillow pytest
pytest tests/test_nks_desktop_builder.py -q
```

---

## License

Use and modify as needed for your own library tooling. Native Instruments, Kontakt, and NKS are trademarks of their respective owners; this project is an independent utility and is not affiliated with Native Instruments.
