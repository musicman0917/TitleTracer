# TitleTracer

A local command-line tool that scans ripped TV episode video files for their
title card, OCRs the text, matches it against an official episode list, and
renames the file accordingly.

Pipeline: **sample frames -> preprocess -> OCR -> fuzzy match -> rename**.

## How it works

1. **Frame sampling** (`titletracer/video.py`) — seeks directly to timestamps
   every `--interval` seconds (default 5s), stopping at `--max-scan` seconds
   (default 300s / 5 minutes), since title cards live early in the episode.
   This avoids decoding the whole file.
2. **Preprocessing + OCR** (`titletracer/ocr.py`) — crops to the region where
   title cards usually sit (`--crop`, default `center`), upscales, denoises,
   boosts contrast, and tries a few binarization variants (both text
   polarities) before running Tesseract. It keeps whichever variant Tesseract
   itself scores most confidently.
3. **Episode list** (`titletracer/episodes.py`) — fetched from TVMaze (no key
   required) or TMDb (`--tmdb-api-key`), or loaded from a local JSON file.
   The online source falls back to `--episodes-json` automatically if the
   request fails. Show names aren't always unique on TVMaze (a reboot, a
   live-action adaptation, a movie can share a name) — if `--show` matches
   more than one TVMaze listing, the tool lists every candidate and asks
   you to pick (or auto-picks the top-ranked one with a warning if it isn't
   running in a terminal). Pass `--tvmaze-id` to skip the prompt entirely
   once you know the right id.
4. **Fuzzy matching** (`titletracer/matcher.py`) — normalizes and compares the
   OCR text against every candidate title using RapidFuzz's
   `token_sort_ratio`. A match is only accepted if it clears `--threshold`
   (default 80); otherwise the file is flagged for manual review instead of
   guessing.
5. **Renaming** (`titletracer/cli.py`) — builds the new filename from
   `--pattern`, checks for collisions, and either prints the plan
   (`--dry-run`) or renames the file.

### Optional: local vision-LLM fallback (`--vlm-verify`)

Tesseract's thresholding-based OCR struggles with stylized title cards —
text over a busy or animated background, unusual fonts, motion blur (common
in anime, for example). If a video finishes its OCR scan with no confident
match, `--vlm-verify` re-scans it and sends each sampled frame to a local
vision model through [Ollama](https://ollama.com) (`titletracer/vlm.py`),
asking it to transcribe the title text. That text goes through the same
fuzzy-matching step as OCR output, so it's held to the same `--threshold`.

This requires Ollama running locally with a vision-capable model pulled:

```bash
ollama pull llava
```

Then just add the flag:

```bash
python3 titletracer.py /path/to/episodes --show "Your Show" --vlm-verify --dry-run
```

It's opt-in and only runs on the subset of files OCR couldn't confidently
match, since a local model is much slower per frame than OCR. If Ollama
isn't reachable, requests fail gracefully (logged as warnings) and the file
falls through to `manual_review` same as if `--vlm-verify` weren't set.
`--vlm-max-frames` (default 15) bounds how many frames are tried per file
before giving up on it, so a long run of unmatched files can't stall for
minutes each on the local model.

## Installation

### System dependency: Tesseract OCR

```bash
# Debian/Ubuntu
sudo apt-get update && sudo apt-get install -y tesseract-ocr

# macOS (Homebrew)
brew install tesseract

# Windows
choco install tesseract
# or download the installer from https://github.com/UB-Mannheim/tesseract/wiki
```

If `tesseract` isn't on your `PATH`, point the tool at it with
`--tesseract-cmd /path/to/tesseract`.

### Python dependencies

```bash
python3 -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
```

## Usage

Always start with `--dry-run` to review the proposed matches before touching
any files.

### Using TVMaze (default, no API key needed)

```bash
python3 titletracer.py /path/to/episodes --show "Breaking Bad" --dry-run
```

### Using TMDb

```bash
python3 titletracer.py /path/to/episodes --show "Breaking Bad" \
  --source tmdb --tmdb-api-key YOUR_KEY --dry-run
# or export TMDB_API_KEY instead of passing --tmdb-api-key
```

### Using a local episode list (no network required)

```bash
python3 titletracer.py /path/to/episodes --show "My Show" \
  --source local --episodes-json sample_episodes.json --dry-run
```

`sample_episodes.json` in this repo shows the expected format:

```json
{
  "episodes": [
    { "season": 1, "episode": 1, "title": "Pilot" },
    { "season": 1, "episode": 2, "title": "The Second Episode" }
  ]
}
```

### Applying the renames

Once the dry run looks correct, re-run the exact same command without
`--dry-run`:

```bash
python3 titletracer.py /path/to/episodes --show "Breaking Bad"
```

### Naming for Jellyfin

`--jellyfin` switches the rename pattern to Jellyfin's own documented
episode naming scheme (`Show Name SxxExx - Episode Title.ext`), and
`--organize-seasons` additionally moves each renamed file into a
`Season NN/` subfolder under the input directory, matching Jellyfin's
recommended library layout:

```bash
python3 titletracer.py /path/to/episodes --show "Breaking Bad" --jellyfin --organize-seasons --dry-run
```

which produces, for example:

```
/path/to/episodes/Season 01/Breaking Bad S01E01 - Pilot.mkv
```

Both flags are independent -- use `--jellyfin` alone to fix filenames in
place without moving files, or combine with `--organize-seasons` for a full
per-season library layout. `--pattern`, if also given explicitly, takes
priority over `--jellyfin`.

### Useful flags

| Flag | Default | Description |
|---|---|---|
| `--tvmaze-id N` | (none) | Fetch episodes for this exact TVMaze show id, bypassing name search |
| `--season N` | (none) | Restrict matching to one season |
| `--interval` | `5` | Seconds between sampled frames |
| `--max-scan` | `300` | Only scan the first N seconds of each video |
| `--threshold` | `80` | Minimum fuzzy-match score (0-100) to accept |
| `--crop` | `center` | `full` \| `center` \| `lower-third` \| `upper-third` |
| `--extensions` | `mkv,mp4,m4v,avi` | Video extensions to process |
| `--pattern` | `{show} - S{season:02d}E{episode:02d} - {title}` | Rename template |
| `--jellyfin` | off | Use Jellyfin's documented naming scheme instead of the default pattern |
| `--organize-seasons` | off | Move renamed files into `Season NN/` subfolders (Jellyfin's recommended layout) |
| `--report path.json` | (none) | Write a JSON summary of every file's outcome |
| `--debug-dir path/` | (none) | Save every sampled frame (raw + cropped) and its OCR text per video |
| `--vlm-verify` | off | Fall back to a local Ollama vision model when OCR finds no confident match |
| `--vlm-model` | `llava` | Ollama vision model to use with `--vlm-verify` |
| `--vlm-host` | `http://localhost:11434` | Ollama API host |
| `--vlm-max-frames` | `15` | Max frames sent to the VLM per file before giving up on it |
| `-v` | off | Verbose/debug logging |

Files with no confident match, or whose target filename collides with
another file, are never renamed — they're logged as `manual_review` /
`collision` in the console output and in `--report`, so you can retitle them
by hand.

### Troubleshooting bad matches

If every file comes back as `manual_review` with garbled OCR text, the
title card is probably outside the sampled crop region or scan window
rather than a font/threshold problem. Run one file through with
`--debug-dir` to see exactly what's being captured:

```bash
python3 titletracer.py /path/to/episodes --show "Your Show" \
  --debug-dir debug_frames --dry-run
```

This writes `debug_frames/<video name>/<timestamp>s_raw.png` (the full
sampled frame) and `..._crop.png` (the exact region OCR ran on) for every
sample point, plus logs the OCR text read at each timestamp. Open a few of
the `_raw.png` files around where you expect the title card to appear —
if it isn't there, increase `--max-scan`; if it's there but outside the
white box in `_crop.png`, switch `--crop` to `full`, `lower-third`, or
`upper-third`.

If the debug log shows OCR reading real, legible title text at high
confidence but it still won't match anything, the problem usually isn't
OCR — it's the episode list. `--show "Name"` uses TVMaze's fuzzy search,
which returns exactly one best guess; for a show with multiple TVMaze
listings (a reboot, a live-action adaptation, a movie sharing the name)
that guess can silently be the wrong entry, quietly giving you the wrong
(often much shorter) episode list to match against. If `"Loaded N candidate
episode(s)"` looks far too small for the show, list every TVMaze entry for
the name and grab the right id:

```bash
curl "https://api.tvmaze.com/search/shows?q=Your+Show+Name"
```

then pin it explicitly:

```bash
python3 titletracer.py /path/to/episodes --show "Your Show" --tvmaze-id 1234 --dry-run
```

Since the tool now lists ambiguous TVMaze matches and prompts interactively
(or auto-picks the top match with a warning when run non-interactively),
this is mostly useful for scripting/automation where you want to pin the
id up front and skip the prompt.

## Project layout

```
titletracer/
  cli.py        # argument parsing + orchestration
  video.py       # frame sampling via OpenCV
  ocr.py         # preprocessing + pytesseract OCR
  matcher.py     # fuzzy matching + filename building
  episodes.py    # TVMaze / TMDb / local JSON episode fetchers
  vlm.py          # optional local Ollama vision-model fallback
  config.py      # RunConfig dataclass / defaults
titletracer.py    # `python titletracer.py ...` entry point
requirements.txt
sample_episodes.json
```
