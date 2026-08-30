"""Mode-agnostic scanning/renaming engine shared by the CLI and the GUI.

Scanning (`scan_tv` / `scan_movie`) never touches the filesystem beyond
reading video files -- it only produces a `PlanItem` per file. Applying
(`apply_plan`) is the only place that creates directories or renames
files, and only for items whose plan was actually accepted. Splitting
these lets a caller preview a full plan, let a human review it, and only
then apply it -- without re-scanning (the expensive part) in between.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import cv2

from .config import RunConfig
from .episodes import Episode
from .gaps import FileOutcome, infer_gaps
from .matcher import MatchResult, build_filename, match_episode, sanitize_filename
from .movies import Movie, resolve_movie_match
from .ocr import clean_text, crop_region, extract_text
from .video import sample_frames
from .vlm import transcribe_title

logger = logging.getLogger("titletracer")

# Called with (current_index, total_count, video_path) after each file is
# scanned, so a UI can show progress. Optional everywhere; defaults to a
# no-op so CLI callers don't need to pass one.
ProgressCallback = Callable[[int, int, Path], None]


@dataclass
class PlanItem:
    video: Path
    status: str  # "matched" | "matched_inferred" | "manual_review" | "collision" | "error"
    target: Optional[Path] = None
    target_display: str = ""
    label: str = ""
    score: float = 0.0
    note: str = field(default="")


def find_video_files(directory: Path, extensions: List[str]) -> List[Path]:
    exts = {("." + e.lstrip(".")).lower() for e in extensions}
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise RuntimeError(f"Could not read directory {directory}: {exc}") from exc
    return sorted(p for p in entries if p.is_file() and p.suffix.lower() in exts)


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
        logger.info(
            "  No confident OCR match; asking local VLM (%s via Ollama), up to %d frame(s)",
            cfg.vlm_model, cfg.vlm_max_frames,
        )
        attempts = 0
        for frame in sample_frames(video_path, cfg.interval_sec, cfg.max_scan_sec):
            if attempts >= cfg.vlm_max_frames:
                logger.info("  VLM frame cap (%d) reached; giving up on this file", cfg.vlm_max_frames)
                break
            attempts += 1

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


def scan_tv(
    cfg: RunConfig, episodes: List[Episode], videos: List[Path], on_progress: Optional[ProgressCallback] = None,
) -> List[PlanItem]:
    """Scan every video against `episodes`, returning one PlanItem each in
    file order. Positional gap-inference (see gaps.py) runs automatically;
    it's applied to the plan only when cfg.fill_gaps is set, otherwise
    it's just attached as a hint on the manual_review item."""
    outcomes: List[FileOutcome] = []
    plan: List[PlanItem] = []

    for idx, video in enumerate(videos, 1):
        logger.info("Processing %s", video.name)
        try:
            result = process_video(video, episodes, cfg)
        except IOError as exc:
            logger.error("  Skipping (could not read video): %s", exc)
            plan.append(PlanItem(video=video, status="error", note=str(exc)))
        else:
            outcomes.append(FileOutcome(video=video, result=result))
        if on_progress:
            on_progress(idx, len(videos), video)

    infer_gaps(outcomes, episodes)

    used_targets = set()
    for fo in outcomes:
        video, result = fo.video, fo.result
        episode = result.episode
        applied_inference = False

        if episode is None and cfg.fill_gaps and fo.inferred_episode is not None:
            episode = fo.inferred_episode
            applied_inference = True

        if episode is None:
            note = f"ocr={result.ocr_text!r}"
            if fo.inferred_episode is not None:
                note += (
                    f"; possible: {fo.inferred_episode.code} {fo.inferred_episode.title!r} "
                    f"({fo.inferred_note})"
                )
            plan.append(PlanItem(
                video=video, status="manual_review", score=result.score, note=note,
                label=f"{fo.inferred_episode.code} {fo.inferred_episode.title}" if fo.inferred_episode else "",
            ))
            continue

        new_name = build_filename(cfg.show_name, episode, video.suffix, cfg.pattern)
        dest_dir = cfg.directory / f"Season {episode.season:02d}" if cfg.organize_seasons else cfg.directory
        target = dest_dir / new_name
        target_display = str(target.relative_to(cfg.directory)) if cfg.organize_seasons else new_name

        if str(target) in used_targets or (target.exists() and target != video):
            plan.append(PlanItem(
                video=video, status="collision", target_display=target_display,
                label=f"{episode.code} {episode.title}", note="target already exists/claimed",
            ))
            continue

        used_targets.add(str(target))
        plan.append(PlanItem(
            video=video,
            status="matched_inferred" if applied_inference else "matched",
            target=target, target_display=target_display,
            label=f"{episode.code} {episode.title}", score=result.score,
            note=fo.inferred_note if applied_inference else "",
        ))

    return plan


def scan_movie(
    cfg: RunConfig, videos: List[Path], movie_overrides: Optional[dict] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> List[PlanItem]:
    """Identify each video independently (filename -> TMDb, or a local
    override) and build a one-item-per-file rename plan. `--organize-seasons`
    is repurposed here as "one folder per movie", matching Jellyfin's
    per-movie library layout."""
    plan: List[PlanItem] = []
    used_targets = set()
    movie_overrides = movie_overrides or {}

    for idx, video in enumerate(videos, 1):
        logger.info("Processing %s", video.name)

        movie: Optional[Movie] = movie_overrides.get(video.name)
        if movie is None:
            movie = resolve_movie_match(
                video.stem, cfg.tmdb_api_key, interactive=cfg.interactive,
            )

        if on_progress:
            on_progress(idx, len(videos), video)

        if movie is None:
            plan.append(PlanItem(video=video, status="manual_review", note="no TMDb match found"))
            continue

        new_name = sanitize_filename(movie.display) + video.suffix
        dest_dir = cfg.directory / sanitize_filename(movie.display) if cfg.organize_seasons else cfg.directory
        target = dest_dir / new_name
        target_display = str(target.relative_to(cfg.directory)) if cfg.organize_seasons else new_name

        if str(target) in used_targets or (target.exists() and target != video):
            plan.append(PlanItem(
                video=video, status="collision", target_display=target_display,
                label=movie.display, note="target already exists/claimed",
            ))
            continue

        used_targets.add(str(target))
        plan.append(PlanItem(
            video=video, status="matched", target=target, target_display=target_display,
            label=movie.display, score=100.0,
        ))

    return plan


def apply_plan(plan: List[PlanItem], base_dir: Path, dry_run: bool = False) -> int:
    """Rename every accepted item in `plan`. Returns the count actually
    renamed (0 in dry-run mode, since nothing is touched)."""
    applied = 0
    for item in plan:
        if item.status not in ("matched", "matched_inferred") or item.target is None:
            continue
        if dry_run:
            logger.info("  Would rename %s -> %s", item.video.name, item.target_display)
            continue
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.video.rename(item.target)
        logger.info("  Renamed %s -> %s", item.video.name, item.target_display)
        applied += 1
    return applied
