"""Tiền xử lý ảnh trang trước OCR.

Ảnh chụp độ phân giải thấp / mờ / nhiễu sáng cần upscale nhẹ + tăng tương phản
trước khi đưa vào PaddleOCR. Ảnh đã xử lý cũng là ảnh upload S3 để bbox khớp
preview trên client.
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from .config import config
from .document_rectifier import rectify_document

logger = logging.getLogger("ocr.preprocess")


def preprocess_for_ocr(
    image: np.ndarray,
    *,
    allow_upscale: bool = True,
    allow_rectify: bool = True,
) -> tuple[np.ndarray, float]:
    """Trả về (ảnh RGB uint8, coord_scale).

    ``coord_scale`` > 1 khi upscale — nhân bbox ảnh embedded PDF (fitz) cho khớp
    kích thước pixel sau xử lý.
    """
    if not config.preprocess_enabled or image is None or image.size == 0:
        return image, 1.0

    rgb = _as_rgb_uint8(image)

    if allow_rectify and config.rectify_enabled:
        rgb, _ = rectify_document(rgb)

    h, w = rgb.shape[:2]
    short = min(h, w)
    blur_score = _laplacian_variance(rgb)

    needs_upscale = (
        allow_upscale and short < config.preprocess_min_short_edge
    )
    needs_enhance = needs_upscale or blur_score < config.preprocess_blur_threshold

    if not needs_enhance:
        return rgb, 1.0

    scale = 1.0
    if needs_upscale:
        scale = min(
            config.preprocess_min_short_edge / short,
            config.preprocess_max_scale,
        )
        if scale > 1.05:
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            rgb = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

    rgb = _enhance(rgb)

    if scale > 1.05 or blur_score < config.preprocess_blur_threshold:
        logger.info(
            "Preprocess: %dx%d -> %dx%d scale=%.2f blur=%.1f upscale=%s",
            w,
            h,
            rgb.shape[1],
            rgb.shape[0],
            scale,
            blur_score,
            needs_upscale,
        )

    return rgb, scale


def _as_rgb_uint8(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        rgb = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    else:
        rgb = image.copy()
    if rgb.dtype != np.uint8:
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    return rgb


def _laplacian_variance(rgb: np.ndarray) -> float:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _enhance(rgb: np.ndarray) -> np.ndarray:
    """CLAHE + khử nhiễu nhẹ + unsharp mask."""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(
        clipLimit=config.preprocess_clahe_clip,
        tileGridSize=(8, 8),
    )
    l_channel = clahe.apply(l_channel)
    rgb = cv2.cvtColor(
        cv2.merge([l_channel, a_channel, b_channel]),
        cv2.COLOR_LAB2RGB,
    )

    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    bgr = cv2.fastNlMeansDenoisingColored(
        bgr,
        None,
        h=config.preprocess_denoise_strength,
        hColor=config.preprocess_denoise_strength,
        templateWindowSize=7,
        searchWindowSize=21,
    )
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    blurred = cv2.GaussianBlur(rgb, (0, 0), sigmaX=1.0)
    rgb = cv2.addWeighted(rgb, 1.45, blurred, -0.45, 0)
    return np.clip(rgb, 0, 255).astype(np.uint8)
