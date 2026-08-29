"""Fuzzy-match OCR'd text against the official episode list and build the
resulting filename."""

import re
from dataclasses import dataclass
from typing import List, Optional

from rapidfuzz import fuzz

from .episodes import Episode

_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]")


def normalize(text: str) -> str:
    text = text.lower()
    text = _NORMALIZE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class MatchResult:
    episode: Optional[Episode]
    score: float
    ocr_text: str


def match_episode(ocr_text: str, episodes: List[Episode], threshold: float) -> MatchResult:
    """Return the best-scoring episode, or episode=None if nothing cleared
    `threshold` (0-100) -- callers should treat that as "flag for manual
    review" rather than falling back to a low-confidence guess."""
    query = normalize(ocr_text)
    if not query:
        return MatchResult(None, 0.0, ocr_text)

    best_episode, best_score = None, 0.0
    for ep in episodes:
        target = normalize(ep.title)
        if not target:
            continue
        score = fuzz.token_sort_ratio(query, target)
        if score > best_score:
            best_episode, best_score = ep, score

    if best_score >= threshold:
        return MatchResult(best_episode, best_score, ocr_text)
    return MatchResult(None, best_score, ocr_text)


_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*]')


def sanitize_filename(name: str) -> str:
    name = _INVALID_FS_CHARS.sub("", name)
    return name.strip().rstrip(".")


def build_filename(show_name: str, episode: Episode, ext: str, pattern: str) -> str:
    stem = pattern.format(
        show=show_name, season=episode.season, episode=episode.number, title=episode.title
    )
    return sanitize_filename(stem) + ext
