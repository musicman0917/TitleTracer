"""Run configuration shared across the pipeline stages."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

DEFAULT_EXTENSIONS = [".mkv", ".mp4", ".m4v", ".avi"]

# Tokens available: {show} {season} {episode} {title}
DEFAULT_PATTERN = "{show} - S{season:02d}E{episode:02d} - {title}"

# https://jellyfin.org/docs/general/server/media/shows -- Jellyfin's own
# documented example naming scheme for episode files.
JELLYFIN_PATTERN = "{show} S{season:02d}E{episode:02d} - {title}"


@dataclass
class RunConfig:
    directory: Path
    show_name: str

    # Episode list source
    source: str = "tvmaze"  # "tvmaze" | "tmdb" | "local"
    local_json: Optional[Path] = None
    tmdb_api_key: Optional[str] = None
    tvmaze_id: Optional[int] = None
    season: Optional[int] = None

    # Frame sampling
    interval_sec: float = 5.0
    max_scan_sec: float = 300.0

    # OCR / matching
    crop_mode: str = "center"  # "full" | "center" | "lower-third" | "upper-third"
    threshold: float = 80.0
    tesseract_cmd: Optional[str] = None

    # Output
    extensions: List[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    pattern: str = DEFAULT_PATTERN
    organize_seasons: bool = False
    dry_run: bool = False
    report_path: Optional[Path] = None

    # Diagnostics
    debug_dir: Optional[Path] = None

    # Local vision-LLM fallback (via Ollama) for frames OCR can't read
    vlm_verify: bool = False
    vlm_model: str = "llava"
    vlm_host: str = "http://localhost:11434"
    vlm_max_frames: int = 15
