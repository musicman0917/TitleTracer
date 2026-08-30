"""Command-line entry point: wires together episode/movie fetching and
the shared scan/apply engine."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

import pytesseract
import requests

from .config import DEFAULT_EXTENSIONS, DEFAULT_PATTERN, JELLYFIN_PATTERN, RunConfig
from .engine import apply_plan, find_video_files, scan_movie, scan_tv
from .episodes import EpisodeFetchError, get_episode_list, search_tvmaze_shows
from .movies import MovieLookupError, load_overrides

logger = logging.getLogger("titletracer")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="titletracer",
        description=(
            "Detect episode/movie title cards in ripped video files via OCR and "
            "rename them to match official metadata."
        ),
    )
    p.add_argument("directory", type=Path, help="Directory containing video files to process")
    p.add_argument(
        "--mode", choices=["tv", "movie"], default="tv",
        help="tv: match against an episode list for one show (default). "
             "movie: identify each file independently from its filename via TMDb",
    )
    p.add_argument(
        "--show", default=None,
        help="Show name (required for --mode tv; used for API lookup and in the filename)",
    )
    p.add_argument(
        "--source", choices=["tvmaze", "tmdb", "local"], default="tvmaze",
        help="Episode list source for --mode tv (default: tvmaze)",
    )
    p.add_argument(
        "--episodes-json", type=Path, default=None,
        help="Local JSON episode list (--mode tv); required with --source local, otherwise used as "
             "a fallback if the online source fails",
    )
    p.add_argument(
        "--movies-json", type=Path, default=None,
        help="Local JSON of per-filename overrides for --mode movie: "
             '{"File1.mkv": {"title": "...", "year": 1999}, ...}',
    )
    p.add_argument("--tmdb-api-key", default=None, help="TMDb API key (or set the TMDB_API_KEY env var)")
    p.add_argument(
        "--tvmaze-id", type=int, default=None,
        help="Fetch episodes for this exact TVMaze show id (--mode tv, --source tvmaze), bypassing "
             "name search. Use this when the show name is ambiguous (a reboot/live-action/movie "
             "shares the name) -- find the right id via https://api.tvmaze.com/search/shows?q=your+show",
    )
    p.add_argument("--season", type=int, default=None, help="Restrict matching to a single season number (--mode tv)")
    p.add_argument("--interval", type=float, default=5.0, help="Seconds between sampled frames (default: 5)")
    p.add_argument(
        "--max-scan", type=float, default=300.0,
        help="Only scan the first N seconds of each video (default: 300 = 5 minutes)",
    )
    p.add_argument(
        "--threshold", type=float, default=80.0,
        help="Minimum fuzzy-match confidence 0-100 to accept a match (default: 80, --mode tv)",
    )
    p.add_argument(
        "--crop", choices=["full", "center", "lower-third", "upper-third"], default="center",
        help="Region of the frame to run OCR on (default: center, --mode tv)",
    )
    p.add_argument(
        "--extensions", default=",".join(e.lstrip(".") for e in DEFAULT_EXTENSIONS),
        help="Comma-separated video extensions to process (default: mkv,mp4,m4v,avi)",
    )
    p.add_argument(
        "--pattern", default=DEFAULT_PATTERN,
        help="Rename pattern for --mode tv; available tokens: {show} {season} {episode} {title} "
             f"(default: '{DEFAULT_PATTERN}')",
    )
    p.add_argument(
        "--jellyfin", action="store_true",
        help=f"--mode tv: use Jellyfin's documented naming scheme ('{JELLYFIN_PATTERN}') instead of "
             "the default pattern -- has no effect if --pattern is also given explicitly. "
             "--mode movie always uses Jellyfin's 'Title (Year)' convention",
    )
    p.add_argument(
        "--organize-seasons", action="store_true",
        help="--mode tv: move renamed files into 'Season NN' subfolders. --mode movie: move each "
             "into its own 'Title (Year)/' subfolder. Either way, matches Jellyfin's recommended "
             "library layout instead of renaming files in place",
    )
    p.add_argument(
        "--fill-gaps", action="store_true",
        help="--mode tv: for a file with no confident title-card match, if its position in the "
             "(sorted) file list sits unambiguously between two confidently-matched episodes with a "
             "numeric gap matching the number of unmatched files between them, rename it too. "
             "Without this flag such files are only annotated with the same suggestion and left for "
             "manual review",
    )
    p.add_argument("--tesseract-cmd", default=None, help="Path to the tesseract executable, if not on PATH")
    p.add_argument("--report", type=Path, default=None, help="Write a JSON results report to this path")
    p.add_argument(
        "--debug-dir", type=Path, default=None,
        help="--mode tv: save every sampled frame (raw + the exact --crop region used) and its OCR "
             "text to this directory, one subfolder per video -- use this to see why a title card "
             "isn't matching",
    )
    p.add_argument(
        "--vlm-verify", action="store_true",
        help="--mode tv: if Tesseract finds no confident match, fall back to asking a local Ollama "
             "vision model to read the title card (requires Ollama running locally with a vision "
             "model pulled)",
    )
    p.add_argument("--vlm-model", default="llava", help="Ollama vision model for --vlm-verify (default: llava)")
    p.add_argument(
        "--vlm-host", default="http://localhost:11434",
        help="Ollama API host for --vlm-verify (default: http://localhost:11434)",
    )
    p.add_argument(
        "--vlm-max-frames", type=int, default=15,
        help="Max frames to send to the VLM per file before giving up (default: 15) -- each call is "
             "slow, so this bounds how long a single unmatched file can hang the run",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Only print planned renames; do not touch any files on disk",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose (debug) logging")
    return p.parse_args(argv)


def resolve_tvmaze_id(show_name: str, interactive: bool = True) -> Optional[int]:
    """Search TVMaze for `show_name`. A unique match is used silently; an
    ambiguous one (a reboot, a live-action adaptation, a movie sharing the
    name) is shown to the user to pick from when `interactive` (a real
    terminal) -- otherwise the top-ranked match is used with a loud
    warning so the run doesn't hang. Returns None on search failure or no
    matches, letting the caller fall back to TVMaze's own singlesearch."""
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

    if not interactive or not sys.stdin.isatty():
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


def build_plan_tv(cfg: RunConfig, on_progress=None):
    """Resolve the episode list and scan every video, returning a plan.
    Raises RuntimeError with a human-readable message on setup failure
    (no episode list, no video files) -- used by both the CLI and the GUI."""
    tvmaze_id = cfg.tvmaze_id
    if cfg.source == "tvmaze" and tvmaze_id is None:
        tvmaze_id = resolve_tvmaze_id(cfg.show_name, cfg.interactive)

    try:
        episodes = get_episode_list(cfg.show_name, cfg.source, cfg.local_json, cfg.tmdb_api_key, tvmaze_id)
    except EpisodeFetchError as exc:
        raise RuntimeError(f"Could not obtain an episode list: {exc}") from exc

    if cfg.season is not None:
        episodes = [e for e in episodes if e.season == cfg.season]
    if not episodes:
        raise RuntimeError("No episodes available to match against (check show name / season / episode source)")
    logger.info("Loaded %d candidate episode(s) for %r", len(episodes), cfg.show_name)

    videos = find_video_files(cfg.directory, cfg.extensions)
    if not videos:
        raise RuntimeError(f"No video files found in {cfg.directory}")

    return scan_tv(cfg, episodes, videos, on_progress=on_progress)


def build_plan_movie(cfg: RunConfig, on_progress=None):
    videos = find_video_files(cfg.directory, cfg.extensions)
    if not videos:
        raise RuntimeError(f"No video files found in {cfg.directory}")

    try:
        overrides = load_overrides(cfg.movies_json) if cfg.movies_json else {}
    except MovieLookupError as exc:
        raise RuntimeError(str(exc)) from exc
    return scan_movie(cfg, videos, overrides, on_progress=on_progress)


def run_tv(cfg: RunConfig) -> int:
    try:
        plan = build_plan_tv(cfg)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1
    return finish(cfg, plan)


def run_movie(cfg: RunConfig) -> int:
    try:
        plan = build_plan_movie(cfg)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1
    return finish(cfg, plan)


def finish(cfg: RunConfig, plan) -> int:
    """Shared reporting/apply tail for both modes."""
    inferred_count = 0
    for item in plan:
        if item.status == "manual_review":
            hint = f" -- {item.note}" if item.note else ""
            logger.warning("  MANUAL REVIEW: %s%s", item.video.name, hint)
        elif item.status == "collision":
            logger.warning("  MANUAL REVIEW: %s -> target %r already exists/claimed", item.video.name, item.target_display)
        elif item.status == "matched_inferred":
            inferred_count += 1
            logger.info(
                "  Inferred %s [POSITION-INFERRED, not confirmed by title card] -> %s",
                item.label, item.target_display,
            )
        elif item.status == "matched":
            logger.info("  Matched %s (score %.0f) -> %s", item.label, item.score, item.target_display)

    if not cfg.dry_run:
        apply_plan(plan, cfg.directory, dry_run=False)

    if cfg.report_path:
        report = [
            {
                "file": item.video.name, "status": item.status, "target": item.target_display,
                "label": item.label, "score": round(item.score, 1), "note": item.note,
            }
            for item in plan
        ]
        cfg.report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Wrote report to %s", cfg.report_path)

    manual = [item for item in plan if item.status not in ("matched", "matched_inferred")]
    if manual:
        logger.warning("%d file(s) need manual review; see the report above for details.", len(manual))
    if inferred_count:
        logger.warning(
            "%d file(s) were renamed by position inference only (no title card was read) -- "
            "double-check those names.", inferred_count,
        )

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
    if args.mode == "tv" and not args.show:
        logger.error("--show is required for --mode tv")
        sys.exit(1)

    if args.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = args.tesseract_cmd

    pattern = args.pattern
    if args.jellyfin and args.pattern == DEFAULT_PATTERN:
        pattern = JELLYFIN_PATTERN

    cfg = RunConfig(
        directory=args.directory,
        show_name=args.show,
        mode=args.mode,
        source=args.source,
        local_json=args.episodes_json,
        movies_json=args.movies_json,
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
        fill_gaps=args.fill_gaps,
        dry_run=args.dry_run,
        tesseract_cmd=args.tesseract_cmd,
        report_path=args.report,
        debug_dir=args.debug_dir,
        vlm_verify=args.vlm_verify,
        vlm_model=args.vlm_model,
        vlm_host=args.vlm_host,
        vlm_max_frames=args.vlm_max_frames,
    )

    run = run_tv if cfg.mode == "tv" else run_movie
    sys.exit(run(cfg))


if __name__ == "__main__":
    main()
