"""Frame preprocessing and OCR extraction.

Title cards are usually short, high-contrast text over a plain or
softly-blurred background, so a small set of OpenCV preprocessing passes
(crop -> upscale -> denoise -> binarize) gets Tesseract most of the way
there. We try a couple of binarization polarities since title text can be
light-on-dark or dark-on-light, and keep whichever variant Tesseract itself
reports the highest confidence for.
"""

import logging
import re
from typing import List, Tuple

import cv2
import numpy as np
import pytesseract

logger = logging.getLogger(__name__)

# (y_start_frac, y_end_frac, x_start_frac, x_end_frac)
_CROP_BOUNDS = {
    "full": (0.0, 1.0, 0.0, 1.0),
    "center": (0.30, 0.70, 0.10, 0.90),
    "lower-third": (0.66, 1.0, 0.05, 0.95),
    "upper-third": (0.0, 0.34, 0.05, 0.95),
}


def crop_region(image: np.ndarray, mode: str) -> np.ndarray:
    h, w = image.shape[:2]
    y0f, y1f, x0f, x1f = _CROP_BOUNDS.get(mode, _CROP_BOUNDS["center"])
    y0, y1 = int(h * y0f), int(h * y1f)
    x0, x1 = int(w * x0f), int(w * x1f)
    return image[y0:y1, x0:x1]


def _binarize_variants(gray: np.ndarray) -> List[np.ndarray]:
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
    )
    return [otsu, cv2.bitwise_not(otsu), adaptive]


def preprocess(image: np.ndarray, crop_mode: str, upscale: float = 2.0) -> List[np.ndarray]:
    """Return candidate binarized images ready for OCR."""
    region = crop_region(image, crop_mode)
    if region.size == 0:
        return []

    if upscale and upscale != 1.0:
        region = cv2.resize(region, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)

    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    gray = cv2.fastNlMeansDenoising(gray, h=10)
    gray = cv2.convertScaleAbs(gray, alpha=1.3, beta=0)  # mild contrast boost

    return _binarize_variants(gray)


_ALLOWED_CHARS = re.compile(r"[^A-Za-z0-9 '\-:!?.,]")
_WHITESPACE = re.compile(r"\s+")


def clean_text(raw: str) -> str:
    """Strip characters that are almost never part of a real title and are
    usually OCR noise (box-drawing glyphs, stray punctuation, etc.)."""
    text = raw.replace("\n", " ")
    text = _ALLOWED_CHARS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def extract_text(
    image: np.ndarray, crop_mode: str = "center", tesseract_config: str = "--psm 6"
) -> Tuple[str, float]:
    """Run OCR on the best of several preprocessed variants of `image`.

    Returns (cleaned_text, mean_word_confidence) where confidence is
    Tesseract's own 0-100 per-word score averaged over the winning variant.
    Returns ("", 0.0) if no variant produced usable text.
    """
    best_text, best_conf = "", -1.0

    for candidate in preprocess(image, crop_mode):
        try:
            data = pytesseract.image_to_data(
                candidate, config=tesseract_config, output_type=pytesseract.Output.DICT
            )
        except pytesseract.TesseractError as exc:
            logger.debug("Tesseract failed on a candidate: %s", exc)
            continue

        words, confidences = [], []
        for word, conf in zip(data["text"], data["conf"]):
            word = word.strip()
            conf = float(conf)
            if word and conf >= 0:
                words.append(word)
                confidences.append(conf)

        if not words:
            continue

        mean_conf = sum(confidences) / len(confidences)
        text = clean_text(" ".join(words))
        if len(text) >= 3 and mean_conf > best_conf:
            best_text, best_conf = text, mean_conf

    return best_text, max(best_conf, 0.0)
