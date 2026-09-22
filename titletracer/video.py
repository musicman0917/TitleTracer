"""Cheap frame sampling: seek directly to timestamps instead of decoding
every frame, and stop scanning well before the video ends."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class Frame:
    timestamp_sec: float
    image: np.ndarray


def sample_frames(video_path: Path, interval_sec: float, max_scan_sec: float) -> Iterator[Frame]:
    """Yield frames sampled every `interval_sec` from the start of the
    video, stopping at `max_scan_sec` (or the video's own duration, if
    shorter) since title cards usually live in the first few minutes.
    `max_scan_sec <= 0` means no cap at all -- scan the entire video, for
    the episodes where that isn't true."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise IOError(f"Could not open video file: {video_path}")

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        duration = (frame_count / fps) if fps else 0

        if max_scan_sec <= 0:
            # Duration is usually known; fall back to a generous finite cap
            # (rather than no cap at all) for the rare file whose metadata
            # doesn't report one, so a corrupt file can't hang forever.
            scan_limit = duration if duration > 0 else 24 * 3600.0
        else:
            scan_limit = min(max_scan_sec, duration) if duration > 0 else max_scan_sec

        t = 0.0
        while t <= scan_limit:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if ok and frame is not None:
                yield Frame(timestamp_sec=t, image=frame)
            else:
                logger.debug("Could not decode frame at %.1fs in %s", t, video_path.name)
            t += interval_sec
    finally:
        cap.release()
