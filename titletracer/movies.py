"""Movie identification: unlike TV mode (one show, many episodes to match
against), a movies directory is many independent files, each its own
lookup. The primary signal is the filename itself -- cleaned of rip/scene
tags and searched against TMDb -- with on-screen OCR text used only as an
optional cross-check, not as the thing that discovers candidates."""

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

TMDB_MOVIE_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"

# Common scene/rip tags to strip when guessing a title from a filename:
# resolution, source, codec, audio, rip group, container leftovers, etc.
_JUNK_TAGS = re.compile(
    r"\b("
    r"480p|576p|720p|1080p|2160p|4k|hdr10?|dv|"
    r"bluray|blu-ray|bdrip|brrip|dvdrip|webrip|web-?dl|hdtv|remux|"
    r"x264|x265|h264|h265|hevc|avc|"
    r"aac|ac3|dts(-hd)?|truehd|atmos|flac|"
    r"extended|unrated|directors?\.?cut|theatrical|remastered|proper|repack"
    r")\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_TRAILING_GROUP_RE = re.compile(r"-[A-Za-z0-9]+$")


def guess_title_and_year(filename_stem: str) -> "tuple[str, Optional[int]]":
    """Best-effort guess at (title, year) from a ripped movie's filename."""
    text = re.sub(r"[._]+", " ", filename_stem)

    year = None
    year_match = _YEAR_RE.search(text)
    if year_match:
        year = int(year_match.group(1))
        text = text[: year_match.start()]  # everything after the year is junk

    text = _JUNK_TAGS.sub(" ", text)
    text = _TRAILING_GROUP_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -.([{")
    return text, year


@dataclass(frozen=True)
class Movie:
    title: str
    year: Optional[int]
    tmdb_id: Optional[int] = None

    @property
    def display(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


class MovieLookupError(RuntimeError):
    """Raised when no usable movie match could be obtained for a file."""


def search_tmdb_movies(query: str, year: Optional[int], api_key: str, timeout: int = 10) -> List[Movie]:
    """Return every TMDb movie matching `query` (optionally narrowed by
    `year`), ranked by TMDb's own relevance/popularity ordering."""
    params = {"api_key": api_key, "query": query}
    if year:
        params["year"] = year

    resp = requests.get(TMDB_MOVIE_SEARCH_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    results = resp.json().get("results") or []

    movies = []
    for r in results:
        title = r.get("title")
        if not title:
            continue
        release_year = None
        release_date = r.get("release_date") or ""
        if len(release_date) >= 4 and release_date[:4].isdigit():
            release_year = int(release_date[:4])
        movies.append(Movie(title=title, year=release_year, tmdb_id=r.get("id")))
    return movies


def resolve_movie_match(
    filename_stem: str, api_key: Optional[str], interactive: bool = True, timeout: int = 10,
) -> Optional[Movie]:
    """Identify a single movie file: guess a title/year from its filename,
    search TMDb, and disambiguate. A unique match is used silently. An
    ambiguous one is shown to the user to pick from when `interactive`
    (a real terminal); otherwise the top-ranked (most popular) result is
    used automatically with a warning. Returns None if no API key is
    configured, the search errors, or nothing matched."""
    key = api_key or os.environ.get("TMDB_API_KEY")
    guess_title, guess_year = guess_title_and_year(filename_stem)

    if not key:
        logger.warning(
            "No TMDb API key configured; cannot look up %r automatically -- provide one via "
            "--tmdb-api-key/TMDB_API_KEY, or a filename override via --movies-json", filename_stem,
        )
        return None
    if not guess_title:
        logger.warning("Could not guess a title from filename %r", filename_stem)
        return None

    try:
        matches = search_tmdb_movies(guess_title, guess_year, key, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("TMDb search failed for %r (%s)", guess_title, exc)
        return None

    if not matches:
        logger.warning("TMDb found no movies matching %r (guessed from %r)", guess_title, filename_stem)
        return None
    if len(matches) == 1:
        m = matches[0]
        logger.info("TMDb matched %r -> %s", filename_stem, m.display)
        return m

    print(f"Multiple TMDb movies match {guess_title!r} (guessed from {filename_stem!r}):")
    for i, m in enumerate(matches[:10], 1):
        print(f"  {i}) {m.display}")

    if not interactive or not sys.stdin.isatty():
        top = matches[0]
        logger.warning(
            "Not running interactively -- auto-selecting the most popular match (%s). "
            "Use --movies-json to override this file specifically.", top.display,
        )
        return top

    while True:
        choice = input(f"Select a movie [1-{min(len(matches), 10)}] (or 's' to skip): ").strip().lower()
        if choice in ("s", "skip"):
            return None
        if choice.isdigit() and 1 <= int(choice) <= min(len(matches), 10):
            return matches[int(choice) - 1]
        print("Invalid selection, try again.")


def load_overrides(path: Path) -> dict:
    """Load a local JSON file of per-filename overrides:
    {"Title_01.mkv": {"title": "The Matrix", "year": 1999}, ...}
    Useful for movies where filename-guessing/TMDb search gets it wrong,
    or for fully offline use with no TMDb API key."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        overrides = {
            filename: Movie(title=str(entry["title"]), year=entry.get("year"))
            for filename, entry in data.items()
        }
    except OSError as exc:
        raise MovieLookupError(f"Could not read movie overrides file {path}: {exc}") from exc
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError) as exc:
        raise MovieLookupError(f"Movie overrides file {path} is malformed: {exc}") from exc
    return overrides
