"""Fetch an official episode list (season, number, title) from an online
API, with a local JSON file as source or fallback."""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

TVMAZE_SEARCH_URL = "https://api.tvmaze.com/singlesearch/shows"
TVMAZE_EPISODES_URL = "https://api.tvmaze.com/shows/{id}/episodes"

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/tv"
TMDB_SHOW_URL = "https://api.themoviedb.org/3/tv/{id}"
TMDB_SEASON_URL = "https://api.themoviedb.org/3/tv/{id}/season/{season}"


@dataclass(frozen=True)
class Episode:
    season: int
    number: int
    title: str

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.number:02d}"


class EpisodeFetchError(RuntimeError):
    """Raised when no usable episode list could be obtained."""


def fetch_tvmaze_episodes_by_id(show_id: int, timeout: int = 10) -> List[Episode]:
    """Fetch episodes for a known TVMaze show id, bypassing search entirely.
    Use this once you've confirmed the correct id via TVMAZE_SEARCH_URL --
    `singlesearch` below picks TVMaze's single best guess for a name, which
    can silently resolve to the wrong entry (a reboot, a live-action
    adaptation, a movie) when a show has multiple listings."""
    resp = requests.get(TVMAZE_EPISODES_URL.format(id=show_id), timeout=timeout)
    resp.raise_for_status()

    episodes = [
        Episode(season=ep["season"], number=ep["number"], title=ep["name"])
        for ep in resp.json()
        if ep.get("name") and ep.get("season") is not None and ep.get("number") is not None
    ]
    if not episodes:
        raise EpisodeFetchError(f"TVMaze show id {show_id} has no usable episodes")
    return episodes


def fetch_from_tvmaze(show_name: str, timeout: int = 10) -> List[Episode]:
    resp = requests.get(TVMAZE_SEARCH_URL, params={"q": show_name}, timeout=timeout)
    resp.raise_for_status()
    show = resp.json()
    show_id = show["id"]
    logger.info(
        "TVMaze matched %r -> id=%s (%s, premiered %s) -- if this is the wrong "
        "entry (a reboot/live-action/movie sharing the name), use --tvmaze-id "
        "with the correct id from https://api.tvmaze.com/search/shows?q=...",
        show_name, show_id, show.get("name"), show.get("premiered"),
    )
    return fetch_tvmaze_episodes_by_id(show_id, timeout=timeout)


def fetch_from_tmdb(show_name: str, api_key: str, timeout: int = 10) -> List[Episode]:
    resp = requests.get(
        TMDB_SEARCH_URL, params={"api_key": api_key, "query": show_name}, timeout=timeout
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        raise EpisodeFetchError(f"TMDb found no shows matching {show_name!r}")
    show_id = results[0]["id"]
    logger.info("TMDb matched %r -> id=%s (%s)", show_name, show_id, results[0].get("name"))

    resp = requests.get(TMDB_SHOW_URL.format(id=show_id), params={"api_key": api_key}, timeout=timeout)
    resp.raise_for_status()
    season_count = resp.json().get("number_of_seasons", 0)

    episodes: List[Episode] = []
    for season in range(1, season_count + 1):
        resp = requests.get(
            TMDB_SEASON_URL.format(id=show_id, season=season),
            params={"api_key": api_key},
            timeout=timeout,
        )
        if resp.status_code != 200:
            continue
        for ep in resp.json().get("episodes", []):
            if ep.get("name"):
                episodes.append(Episode(season=season, number=ep["episode_number"], title=ep["name"]))

    if not episodes:
        raise EpisodeFetchError(f"TMDb returned no usable episodes for {show_name!r}")
    return episodes


def load_from_json(path: Path) -> List[Episode]:
    """Load episodes from a local JSON file, either a bare list of
    {"season", "episode", "title"} objects or {"episodes": [...]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    records = data["episodes"] if isinstance(data, dict) and "episodes" in data else data
    episodes = [
        Episode(season=int(r["season"]), number=int(r["episode"]), title=str(r["title"]))
        for r in records
    ]
    if not episodes:
        raise EpisodeFetchError(f"Local episode file {path} contained no episodes")
    return episodes


def get_episode_list(
    show_name: str,
    source: str = "tvmaze",
    local_json: Optional[Path] = None,
    tmdb_api_key: Optional[str] = None,
    tvmaze_id: Optional[int] = None,
) -> List[Episode]:
    """Fetch the episode list from the requested source, falling back to a
    local JSON file (if provided) when the online lookup fails."""
    try:
        if source == "tvmaze":
            if tvmaze_id is not None:
                return fetch_tvmaze_episodes_by_id(tvmaze_id)
            return fetch_from_tvmaze(show_name)
        if source == "tmdb":
            api_key = tmdb_api_key or os.environ.get("TMDB_API_KEY")
            if not api_key:
                raise EpisodeFetchError(
                    "TMDb source selected but no API key provided (--tmdb-api-key or TMDB_API_KEY)"
                )
            return fetch_from_tmdb(show_name, api_key)
        if source == "local":
            if not local_json:
                raise EpisodeFetchError("Local source selected but --episodes-json was not provided")
            return load_from_json(local_json)
        raise ValueError(f"Unknown episode source: {source!r}")
    except (requests.RequestException, EpisodeFetchError, KeyError) as exc:
        logger.warning("Episode source %r failed (%s)", source, exc)
        if local_json and source != "local":
            logger.info("Falling back to local episode list: %s", local_json)
            return load_from_json(local_json)
        raise EpisodeFetchError(str(exc)) from exc
