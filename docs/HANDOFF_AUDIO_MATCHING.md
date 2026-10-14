# Handoff: smarter preset ↔ sample audio matching

**Date:** 2026-10-14  
**Repo:** JedAI0609/nks-desktop-builder  
**Branch:** main  
**Reviewer target:** ChatGPT Desktop (independent check)

## Summary
NKS Desktop Builder no longer assigns a random first Samples WAV when a preset name does not match anything. Matching now scores filename similarity plus Samples subfolder/token relevance, and skips unsafe/non-audio files.

## Code changes
- `nks_desktop_builder.py`
  - `_preset_tokens`, `_samples_subfolder_labels`
  - `_folder_token_score`, `_filename_token_score`
  - `is_safe_audio_file` (exists, size, optional ffprobe)
  - `build_source_audio_index` filters junk/empty
  - `rank_matching_wavs` / `pick_matching_wav` — ranked relevance; may return `None`
- `tests/test_nks_desktop_builder.py`
  - `test_no_random_fallback_for_unrelated_name`
  - `test_subfolder_keyword_prefers_timing`
- `README.md` — matching + test docs
- `.gitignore` — local verify dirs / agent tooling

## Verification
```bash
cd ~/dev && source .venv/bin/activate
pytest tests/test_nks_desktop_builder.py -q
# expected: 17 passed
```

## Review checklist for ChatGPT
1. Confirm no silent wrong-audio assignment for unrelated preset names.
2. Check token stopwords / semantic hints for false positives (e.g. short tokens).
3. Confirm non-destructive preview path still writes only under `.previews/`.
4. Flag any missing tests (multi-ext, case, nested Samples).
5. Note residual gap: real AI artwork API still placeholder.

## Residual / future
- Wire generative artwork API (README already marks this).
- Rebuild `.app` with PyInstaller after matching lands if shipping desktop binary.

## Follow-up (2026-10-14 ChatGPT review)

Independent review of `5c3d65b` flagged P1 false positives. Addressed in a subsequent commit:

- Exact name lane always outranks folder affinity
- Short substring containment blocked (Air↛Chair)
- Library prefix peel requires digits (Soft_Piano keeps “soft”)
- Token evidence + true semantic folder hints (clocks→timing); literal folder-only (Loops/Alien) rejected
- Preview path must resolve under library root (symlink escape blocked)
- `ffprobe` results cached per file

Regression tests: exact-vs-folder, Air/Chair, Soft/Harsh Piano, Loops/Alien.
