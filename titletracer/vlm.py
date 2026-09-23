"""Optional local vision-LLM fallback, via Ollama, for frames plain OCR
can't read cleanly -- stylized fonts, title text over a busy background,
motion blur, etc. Only invoked when Tesseract's pass found no confident
match, since a local vision model is much slower per frame than OCR."""

import base64
import logging
from typing import List

import cv2
import numpy as np
import requests

logger = logging.getLogger(__name__)

_PROMPT = (
    "This image is a single frame from a TV episode. If it shows the "
    "episode's title card, transcribe the title text exactly as written, "
    "in its original script/language (do not translate it), and nothing "
    "else. If there is no legible episode title visible in this frame, "
    "respond with exactly: NONE"
)


def check_available(host: str, timeout: float = 3.0) -> bool:
    """Quick reachability check for the Ollama server -- callers should
    check this once per run rather than discovering it's down by burning
    through a full per-frame retry budget on every unmatched file."""
    try:
        resp = requests.get(f"{host.rstrip('/')}/api/tags", timeout=timeout)
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return False


def list_models(host: str, timeout: float = 3.0) -> List[str]:
    """Names of the models Ollama currently has pulled, or [] on any error."""
    try:
        resp = requests.get(f"{host.rstrip('/')}/api/tags", timeout=timeout)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except (requests.RequestException, ValueError):
        return []


def check_model_available(host: str, model: str, timeout: float = 3.0) -> bool:
    """Whether `model` is pulled on the Ollama server -- callers should check
    this once per run rather than discovering it's missing via a 404 on
    every single frame of every unmatched file. Matches ignoring an Ollama
    tag suffix (e.g. a configured "llava" matches a pulled "llava:latest")."""
    base = model.split(":")[0]
    names = list_models(host, timeout)
    return any(n == model or n.split(":")[0] == base for n in names)


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
