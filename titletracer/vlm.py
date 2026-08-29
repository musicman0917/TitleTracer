"""Optional local vision-LLM fallback, via Ollama, for frames plain OCR
can't read cleanly -- stylized fonts, title text over a busy background,
motion blur, etc. Only invoked when Tesseract's pass found no confident
match, since a local vision model is much slower per frame than OCR."""

import base64
import logging

import cv2
import numpy as np
import requests

logger = logging.getLogger(__name__)

_PROMPT = (
    "This image is a single frame from a TV episode. If it shows the "
    "episode's title card, transcribe the title text exactly as written "
    "and nothing else. If there is no legible episode title visible in "
    "this frame, respond with exactly: NONE"
)


def transcribe_title(image: np.ndarray, model: str, host: str, timeout: float = 60.0) -> str:
    """Ask a local Ollama vision model to read any title text in `image`.

    Returns the raw model response text (the caller should clean/fuzzy-match
    it same as OCR output), or "" if the model saw no title text or the
    request failed for any reason -- callers should treat "" as "try the
    next frame" rather than an error.
    """
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        return ""
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")

    try:
        resp = requests.post(
            f"{host.rstrip('/')}/api/generate",
            json={"model": model, "prompt": _PROMPT, "images": [b64], "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Ollama request failed (%s); skipping VLM check for this frame", exc)
        return ""

    if not text or text.strip().upper().startswith("NONE"):
        return ""
    return text
