"""Image-similarity matching against a user-supplied library of reference
title-card images, for shows whose on-screen title is too stylized
(calligraphy, heavy visual effects) for OCR or a vision-LLM to transcribe
reliably. Instead of reading any text, this compares a sampled frame
directly against known reference images using a difference hash (dHash) --
cheap enough to run against every frame with no new dependency, since it's
just resize + a pixel comparison.

Reference images are never fetched by this tool -- point
--reference-images-dir at a local folder you've populated yourself (a
screenshot per episode, sourced however you like), with filenames that
identify the episode the same way --filename-hint parses video filenames
(S01E05, 1x05, Ep5, 5Ep, etc.).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from .episodes import Episode
from .filename_hint import guess_episode_from_filename
from .matcher import MatchResult
from .ocr import crop_region

logger = logging.getLogger(__name__)

_HASH_SIZE = 16  # 16x16 -> 256-bit difference hash
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

# A dHash of a near-uniform frame (a black transition, a flat-colored scene)
# ends up mostly-False just like a mostly-blank reference image's background
# does -- the two can spuriously look "similar" even though neither has any
# real title-card content, since the hash has nothing but sameness to
# compare. Requiring a minimum fraction of "different" bits before even
# querying the library filters those out up front, rather than trusting a
# high score computed from a signal that was never really there.
_MIN_ACTIVE_FRACTION = 0.03

# Tag stashed in MatchResult.ocr_text so callers/reports can tell an
# image-hash match apart from an OCR/VLM one without a wider API change.
SOURCE_TAG = "image-hash"


def _difference_hash(image: np.ndarray, hash_size: int = _HASH_SIZE) -> np.ndarray:
    """A dHash is robust to minor scale/compression differences between the
    reference image and an actual video frame, since it only compares each
    pixel's brightness to its neighbor rather than to an absolute value."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    resized = cv2.resize(gray, (hash_size + 1, hash_size), interpolation=cv2.INTER_AREA)
    return resized[:, 1:] > resized[:, :-1]


def _similarity(hash_a: np.ndarray, hash_b: np.ndarray) -> float:
    """0-100, where 100 is identical -- matches the same scale as the
    OCR/VLM fuzzy-match score, so a single --threshold applies to both."""
    hamming = int(np.count_nonzero(hash_a != hash_b))
    return 100.0 * (1.0 - hamming / hash_a.size)


@dataclass
class ReferenceImage:
    episode: Episode
    image_hash: np.ndarray
    path: Path


def load_reference_library(directory: Path, episodes: List[Episode]) -> List[ReferenceImage]:
    """Load every image in `directory` whose filename identifies exactly
    one episode in `episodes` (via the same parsing --filename-hint uses
    on video filenames). A file that doesn't parse, or whose number doesn't
    match any loaded episode, is skipped with a warning rather than
    aborting the whole run -- a partially-organized folder still works for
    whatever episodes it does cover."""
    if not directory.is_dir():
        raise RuntimeError(f"Reference images directory not found: {directory}")

    paths = sorted(
        p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in _IMAGE_EXTENSIONS
    )

    library: List[ReferenceImage] = []
    for path in paths:
        episode = guess_episode_from_filename(path.stem, episodes)
        if episode is None:
            logger.warning(
                "Reference image %s doesn't unambiguously identify an episode by filename "
                "(same rules as --filename-hint) -- skipping it.", path.name,
            )
            continue

        image = cv2.imread(str(path))
        if image is None:
            logger.warning("Could not read reference image %s -- skipping it.", path.name)
            continue

        library.append(ReferenceImage(episode=episode, image_hash=_difference_hash(image), path=path))

    if not library:
        raise RuntimeError(
            f"No usable reference images found in {directory} -- filenames must identify an "
            "episode the same way --filename-hint does (S01E05, 1x05, Ep5, 5Ep, etc.)"
        )
    logger.info("Loaded %d reference title-card image(s) from %s", len(library), directory)
    return library


def match_frame(
    frame_image: np.ndarray, crop_mode: str, library: List[ReferenceImage], threshold: float,
) -> Optional[MatchResult]:
    """Compare one video frame's crop region against every reference image,
    returning the best match if it clears `threshold`. Returns None (not a
    zero-score MatchResult) when the crop region is empty, so callers can
    tell "nothing to compare" apart from "compared and scored low"."""
    region = crop_region(frame_image, crop_mode)
    if region.size == 0:
        return None

    frame_hash = _difference_hash(region)
    active_fraction = float(np.count_nonzero(frame_hash)) / frame_hash.size
    if min(active_fraction, 1.0 - active_fraction) < _MIN_ACTIVE_FRACTION:
        # Too close to uniform (all-True or all-False) to plausibly be a
        # real title card -- don't let a low-detail frame borrow apparent
        # similarity from a reference image's own blank background.
        return MatchResult(None, 0.0, SOURCE_TAG)

    best_episode, best_score = None, 0.0
    for ref in library:
        score = _similarity(frame_hash, ref.image_hash)
        if score > best_score:
            best_episode, best_score = ref.episode, score

    if best_score >= threshold:
        return MatchResult(best_episode, best_score, SOURCE_TAG)
    return MatchResult(None, best_score, SOURCE_TAG)
