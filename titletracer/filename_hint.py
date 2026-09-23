"""Fast-path episode identification straight from the filename, for rips
that already embed a reliable episode number (e.g. "1000Ep.mp4"). When a
filename unambiguously identifies exactly one episode in the loaded list,
there's no reason to pay for a full OCR/VLM scan just to confirm what the
filename already says -- so this runs before process_video(), not instead
of it: anything it can't confidently resolve still falls through to the
normal OCR -> VLM -> manual_review pipeline unchanged.
"""

import re
from typing import List, Optional

from .episodes import Episode

# Tried in order, most specific/reliable first. All search (not match) so
# they work regardless of what else is in the filename (release group tags,
# resolution, etc.).
_SEASON_EPISODE_RES = [
    re.compile(r"[Ss](\d{1,3})[._ -]?[Ee](\d{1,4})"),           # S01E05, S1E5
    re.compile(r"(?<!\d)(\d{1,3})[xX](\d{1,4})(?!\d)"),          # 1x05
]

# Absolute/episode-only number, anchored to an explicit "Ep"/"Episode"
# marker so we don't misread a resolution (1080p) or release year as an
# episode number. Matches either side: "Ep1000" or "1000Ep".
_EPISODE_ONLY_RES = [
    re.compile(r"[Ee]p(?:isode)?[\s._-]*(\d{1,5})"),
    re.compile(r"(\d{1,5})[\s._-]*[Ee]p(?:isode)?\b"),
]


def guess_episode_from_filename(stem: str, episodes: List[Episode]) -> Optional[Episode]:
    """Return the one Episode this filename unambiguously identifies, or
    None if no pattern matched or the match is ambiguous (e.g. the number
    collides across un-collapsed seasons) -- callers should treat None as
    "fall through to OCR/VLM", not as an error."""
    for pattern in _SEASON_EPISODE_RES:
        m = pattern.search(stem)
        if not m:
            continue
        season, number = int(m.group(1)), int(m.group(2))
        matches = [e for e in episodes if e.season == season and e.number == number]
        if len(matches) == 1:
            return matches[0]

    for pattern in _EPISODE_ONLY_RES:
        m = pattern.search(stem)
        if not m:
            continue
        number = int(m.group(1))
        matches = [e for e in episodes if e.number == number]
        if len(matches) == 1:
            return matches[0]

    return None
