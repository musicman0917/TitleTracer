"""Command-line entry point: wires together episode fetching, frame
sampling, OCR, fuzzy matching, and (optionally) the actual renaming."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

import cv2
import pytesseract
import requests

from .config import DEFAULT_EXTENSIONS, DEFAULT_PATTERN, JELLYFIN_PATTERN, RunConfig
from .episodes import Episode, EpisodeFetchError, get_episode_list, search_tvmaze_shows
from .matcher import MatchResult, build_filename, match_episode
from .ocr import clean_text, crop_region, extract_text
from .video import sample_frames
from .vlm import transcribe_title

logger = logging.getLogger("titletracer")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="titletracer",
        description=(
            "Detect episode title cards in ripped video files via OCR and "
            "rename them to match an official episode list."
        ),
    )
    p.add_argument("directory", type=Path, help="Directory containing video files to process")
    p.add_argument("--show", required=True, help="Show name (used for API lookup and in the filename)")
    p.add_argument(
        "--source", choices=["tvmaze", "tmdb", "local"], default="tvmaze",
        help="Episode list source (default: tvmaze)",
    )
    p.add_argument(
        "--episodes-json", type=Path, default=None,
        help="Local JSON episode list; required with --source local, otherwise used as a fallback "
             "if the online source fails",
    )
    p.add_argument("--tmdb-api-key", default=None, help="TMDb API key (or set the TMDB_API_KEY env var)")
    p.add_argument(
        "--tvmaze-id", type=int, default=None,
        help="Fetch episodes for this exact TVMaze show id (--source tvmaze), bypassing name search. "
             "Use this when the show name is ambiguous (a reboot/live-action/movie shares the name) -- "
             "find the right id via https://api.tvmaze.com/search/shows?q=your+show",
    )
    p.add_argument("--season", type=int, default=None, help="Restrict matching to a single season number")
    p.add_argument("--interval", type=float, default=5.0, help="Seconds between sampled frames (default: 5)")
    p.add_argument(
        "--max-scan", type=float, default=300.0,
        help="Only scan the first N seconds of each video (default: 300 = 5 minutes)",
    )
    p.add_argument(
        "--threshold", type=float, default=80.0,
        help="Minimum fuzzy-match confidence 0-100 to accept a match (default: 80)",
    )
    p.add_argument(
        "--crop", choices=["full", "center", "lower-third", "upper-third"], default="center",
        help="Region of the frame to run OCR on (default: center)",
    )
    p.add_argument(
        "--extensions", default=",".join(e.lstrip(".") for e in DEFAULT_EXTENSIONS),
        help="Comma-separated video extensions to process (default: mkv,mp4,m4v,avi)",
    )
    p.add_argument(
        "--pattern", default=DEFAULT_PATTERN,
        help="Rename pattern; available tokens: {show} {season} {episode} {title} "
             f"(default: '{DEFAULT_PATTERN}')",
    )
    p.add_argument(
        "--jellyfin", action="store_true",
        help=f"Use Jellyfin's documented naming scheme ('{JELLYFIN_PATTERN}') instead of the default "
             "pattern -- has no effect if --pattern is also given explicitly",
    )
    p.add_argument(
        "--organize-seasons", action="store_true",
        help="Move renamed files into 'Season NN' subfolders under the input directory, as Jellyfin's "
             "library layout recommends, instead of renaming them in place",
    )
    p.add_argument("--tesseract-cmd", default=None, help="Path to the tesseract executable, if not on PATH")
    p.add_argument("--report", type=Path, default=None, help="Write a JSON results report to this path")
    p.add_argument(
        "--debug-dir", type=Path, default=None,
        help="Save every sampled frame (raw + the exact --crop region used) and its OCR text to this "
             "directory, one subfolder per video -- use this to see why a title card isn't matching",
    )
    p.add_argument(
        "--vlm-verify", action="store_true",
        help="If Tesseract finds no confident match, fall back to asking a local Ollama vision model "
             "to read the title card (requires Ollama running locally with a vision model pulled)",
    )
    p.add_argument("--vlm-model", default="llava", help="Ollama vision model for --vlm-verify (default: llava)")
    p.add_argument(
        "--vlm-host", default="http://localhost:11434",
        help="Ollama API host for --vlm-verify (default: http://localhost:11434)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Only print planned renames; do not touch any files on disk",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose (debug) logging")
    return p.parse_args(argv)


def find_video_files(directory: Path, extensions: List[str]) -> List[Path]:
    exts = {("." + e.lstrip(".")).lower() for e in extensions}
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in exts)


def process_video(video_path: Path, episodes: List[Episode], cfg: RunConfig) -> MatchResult:
    """Scan a single video for its title card, stopping as soon as a
    confident match is found (or the scan window is exhausted)."""
    best = MatchResult(None, 0.0, "")

    debug_video_dir = None
    if cfg.debug_dir:
        debug_video_dir = cfg.debug_dir / video_path.stem
        debug_video_dir.mkdir(parents=True, exist_ok=True)

    for frame in sample_frames(video_path, cfg.interval_sec, cfg.max_scan_sec):
        text, ocr_conf = extract_text(frame.image, cfg.crop_mode)

        if debug_video_dir is not None:
            stamp = f"{frame.timestamp_sec:06.1f}s"
            cv2.imwrite(str(debug_video_dir / f"{stamp}_raw.png"), frame.image)
            cv2.imwrite(str(debug_video_dir / f"{stamp}_crop.png"), crop_region(frame.image, cfg.crop_mode))
            logger.info("  @ %s  ocr=%r  ocr_conf=%.0f", stamp, text, ocr_conf)

        if not text:
            continue

        result = match_episode(text, episodes, cfg.threshold)
        logger.debug(
            "%s @ %.0fs: ocr=%r ocr_conf=%.0f match=%s score=%.1f",
            video_path.name, frame.timestamp_sec, text, ocr_conf,
            result.episode.code if result.episode else None, result.score,
        )
        if result.score > best.score:
            best = result
        if best.episode is not None:
            break

    if best.episode is None and cfg.vlm_verify:
        logger.info("  No confident OCR match; asking local VLM (%s via Ollama)", cfg.vlm_model)
        for frame in sample_frames(video_path, cfg.interval_sec, cfg.max_scan_sec):
            raw_text = transcribe_title(frame.image, cfg.vlm_model, cfg.vlm_host)
            if not raw_text:
                continue

            text = clean_text(raw_text)
            result = match_episode(text, episodes, cfg.threshold)
            logger.debug(
                "%s @ %.0fs [vlm]: text=%r match=%s score=%.1f",
                video_path.name, frame.timestamp_sec, text,
                result.episode.code if result.episode else None, result.score,
            )
            if result.score > best.score:
                best = result
            if best.episode is not None:
                break

    return best


def resolve_tvmaze_id(show_name: str) -> Optional[int]:
    """Search TVMaze for `show_name`. A unique match is used silently; an
    ambiguous one (a reboot, a live-action adaptation, a movie sharing the
    name) is shown to the user to pick from -- or, outside a terminal, the
    top-ranked match is used with a loud warning so the run doesn't hang.
    Returns None on search failure or no matches, letting the caller fall
    back to TVMaze's own singlesearch."""
    try:
        matches = search_tvmaze_shows(show_name)
    except (requests.RequestException, EpisodeFetchError) as exc:
        logger.warning("TVMaze show search failed (%s); falling back to singlesearch", exc)
        return None

    if not matches:
        return None
    if len(matches) == 1:
        m = matches[0]
        logger.info("TVMaze matched %r -> id=%s (%s, premiered %s)", show_name, m.id, m.name, m.premiered)
        return m.id

    print(f"Multiple TVMaze shows match {show_name!r}:")
    for i, m in enumerate(matches, 1):
        print(f"  {i}) id={m.id:<7} {m.name:<35} {m.show_type or '?':<12} "
              f"premiered={m.premiered or '?':<12} network={m.network or '?'}")

    if not sys.stdin.isatty():
        top = matches[0]
        logger.warning(
            "Not running interactively -- auto-selecting the top match (id=%s). "
            "Re-run with --tvmaze-id to pin a different one.", top.id,
        )
        return top.id

    while True:
        choice = input(f"Select a show [1-{len(matches)}] (or 'q' to quit): ").strip().lower()
        if choice in ("q", "quit"):
            print("Cancelled.")
            sys.exit(1)
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            return matches[int(choice) - 1].id
        print("Invalid selection, try again.")


def run(cfg: RunConfig) -> int:
    if cfg.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = cfg.tesseract_cmd

    tvmaze_id = cfg.tvmaze_id
    if cfg.source == "tvmaze" and tvmaze_id is None:
        tvmaze_id = resolve_tvmaze_id(cfg.show_name)

    try:
        episodes = get_episode_list(cfg.show_name, cfg.source, cfg.local_json, cfg.tmdb_api_key, tvmaze_id)
    except EpisodeFetchError as exc:
        logger.error("Could not obtain an episode list: %s", exc)
        return 1

    if cfg.season is not None:
        episodes = [e for e in episodes if e.season == cfg.season]
    if not episodes:
        logger.error("No episodes available to match against (check --show / --season / episode source)")
        return 1
    logger.info("Loaded %d candidate episode(s) for %r", len(episodes), cfg.show_name)

    videos = find_video_files(cfg.directory, cfg.extensions)
    if not videos:
        logger.error("No video files found in %s", cfg.directory)
        return 1

    used_targets = set()
    report = []

    for video in videos:
        logger.info("Processing %s", video.name)
        try:
            result = process_video(video, episodes, cfg)
        except IOError as exc:
            logger.error("  Skipping (could not read video): %s", exc)
            report.append({"file": video.name, "status": "error", "reason": str(exc)})
            continue

        if result.episode is None:
            logger.warning(
                "  MANUAL REVIEW: no confident match (best score %.0f, ocr=%r)",
                result.score, result.ocr_text,
            )
            report.append({
                "file": video.name, "status": "manual_review",
                "best_score": round(result.score, 1), "ocr_text": result.ocr_text,
            })
            continue

        new_name = build_filename(cfg.show_name, result.episode, video.suffix, cfg.pattern)
        dest_dir = cfg.directory / f"Season {result.episode.season:02d}" if cfg.organize_seasons else cfg.directory
        target = dest_dir / new_name
        target_display = str(target.relative_to(cfg.directory)) if cfg.organize_seasons else new_name

        if str(target) in used_targets or (target.exists() and target != video):
            logger.warning("  MANUAL REVIEW: target %r already exists/claimed", target_display)
            report.append({
                "file": video.name, "status": "collision",
                "target": target_display, "matched": result.episode.code,
            })
            continue

        used_targets.add(str(target))
        logger.info(
            "  Matched %s %r (score %.0f) -> %s",
            result.episode.code, result.episode.title, result.score, target_display,
        )
        report.append({
            "file": video.name, "status": "matched", "target": target_display,
            "episode": result.episode.code, "title": result.episode.title,
            "score": round(result.score, 1),
        })

        if not cfg.dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            video.rename(target)
            logger.info("  Renamed.")

    if cfg.report_path:
        cfg.report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Wrote report to %s", cfg.report_path)

    manual = [r for r in report if r["status"] != "matched"]
    if manual:
        logger.warning("%d file(s) need manual review; see the report above for details.", len(manual))

    if cfg.dry_run:
        logger.info("Dry run complete -- no files were renamed. Re-run without --dry-run to apply.")

    return 0


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not args.directory.is_dir():
        logger.error("Not a directory: %s", args.directory)
        sys.exit(1)

    pattern = args.pattern
    if args.jellyfin and args.pattern == DEFAULT_PATTERN:
        pattern = JELLYFIN_PATTERN

    cfg = RunConfig(
        directory=args.directory,
        show_name=args.show,
        source=args.source,
        local_json=args.episodes_json,
        tmdb_api_key=args.tmdb_api_key,
        tvmaze_id=args.tvmaze_id,
        season=args.season,
        interval_sec=args.interval,
        max_scan_sec=args.max_scan,
        threshold=args.threshold,
        crop_mode=args.crop,
        extensions=[e.strip() for e in args.extensions.split(",") if e.strip()],
        pattern=pattern,
        organize_seasons=args.organize_seasons,
        dry_run=args.dry_run,
        tesseract_cmd=args.tesseract_cmd,
        report_path=args.report,
        debug_dir=args.debug_dir,
        vlm_verify=args.vlm_verify,
        vlm_model=args.vlm_model,
        vlm_host=args.vlm_host,
    )

    sys.exit(run(cfg))


if __name__ == "__main__":
    main()
