"""Fill in title-card-less episodes using their position among
neighboring successfully-matched files.

Rip title numbering (Title_28, Title_29, ...) almost always tracks
episode order. If a run of un-matched files sits directly between two
confidently-matched files in the same season, and the numeric gap between
those two episodes exactly equals the number of un-matched files between
them, there is exactly one way to fill it in -- no guessing involved.
Anything less clean-cut (season boundary, uneven gap, no bordering match
on one side) is left alone.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .episodes import Episode
from .matcher import MatchResult


@dataclass
class FileOutcome:
    video: Path
    result: MatchResult
    inferred_episode: Optional[Episode] = None
    inferred_note: str = field(default="")


def infer_gaps(outcomes: List[FileOutcome], episodes: List[Episode]) -> None:
    """Mutates `outcomes` (in original file order) in place, setting
    `inferred_episode`/`inferred_note` on any run of un-matched entries
    whose position unambiguously pins down which episodes they must be."""
    episode_by_code = {(e.season, e.number): e for e in episodes}
    n = len(outcomes)

    i = 0
    while i < n:
        if outcomes[i].result.episode is not None:
            i += 1
            continue

        run_start = i
        j = i
        while j < n and outcomes[j].result.episode is None:
            j += 1
        run_end = j  # first index after the run (exclusive); may equal n

        prev_ep = outcomes[run_start - 1].result.episode if run_start > 0 else None
        next_ep = outcomes[run_end].result.episode if run_end < n else None
        run_len = run_end - run_start

        if prev_ep and next_ep and prev_ep.season == next_ep.season:
            gap = next_ep.number - prev_ep.number - 1
            if gap == run_len and gap > 0:
                for offset, idx in enumerate(range(run_start, run_end)):
                    guess_num = prev_ep.number + 1 + offset
                    guessed = episode_by_code.get((prev_ep.season, guess_num))
                    if guessed is not None:
                        outcomes[idx].inferred_episode = guessed
                        outcomes[idx].inferred_note = (
                            f"inferred from file order between {prev_ep.code} and {next_ep.code}"
                        )

        i = run_end
