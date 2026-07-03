"""PaddleOCR engine: khởi tạo 1 lần (singleton) + warm-up, cộng PP-Structure
cho layout analysis (tách figure/table) khi cần.

Tối ưu chi phí khởi tạo: model nặng nên chỉ load 1 lần lúc start. PP-Structure
là lazy — chỉ load khi job đầu tiên yêu cầu layout/extractImages.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np

from .config import config
from .schemas import OcrLine

logger = logging.getLogger("ocr.engine")

# PaddleOCR dùng mã 'vi' cho tiếng Việt; nếu bản cài không có thì fallback 'latin'.
_LANG_MAP = {
    "vi": "vi",
    "en": "en",
    "auto": "vi",
}


def _normalize_bbox(box) -> List[List[float]]:
    """Chuẩn hoá polygon PaddleOCR thành list [[x,y],...] số float."""
    return [[float(pt[0]), float(pt[1])] for pt in box]


class OcrEngine:
    _instance: Optional["OcrEngine"] = None

    def __init__(self) -> None:
        self._readers: Dict[str, object] = {}
        self._structure = None
        self._ready = False

    @classmethod
    def instance(cls) -> "OcrEngine":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Khởi tạo / warm-up ────────────────────────────────────────────────
    def warm_up(self) -> None:
        """Load model lang mặc định + chạy thử ảnh giả để nạp trọng số."""
        lang = _LANG_MAP.get(config.default_lang, "vi")
        reader = self._get_reader(lang)
        dummy = np.full((64, 256, 3), 255, dtype=np.uint8)
        try:
            reader.ocr(dummy, cls=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Warm-up OCR gặp lỗi (bỏ qua): %s", exc)
        self._ready = True
        logger.info("OCR engine sẵn sàng (lang=%s, gpu=%s)", lang, config.use_gpu)

    def is_ready(self) -> bool:
        return self._ready

    # ── Reader theo lang (cache) ──────────────────────────────────────────
    def _get_reader(self, lang: str):
        if lang in self._readers:
            return self._readers[lang]
        from paddleocr import PaddleOCR  # import trễ để warm-up kiểm soát thời điểm

        # ir_optim=False / enable_mkldnn=False: tránh SIGILL trên CPU thiếu AVX-512.
        ocr_kwargs = dict(
            use_angle_cls=True,
            lang=lang,
            use_gpu=config.use_gpu,
            show_log=False,
            ir_optim=False,
            enable_mkldnn=False,
        )
        try:
            reader = PaddleOCR(**ocr_kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Không tạo được reader lang=%s (%s), fallback 'latin'", lang, exc)
            reader = PaddleOCR(**{**ocr_kwargs, "lang": "latin"})
        self._readers[lang] = reader
        return reader

    def _resolve_lang(self, lang: Optional[str]) -> str:
        if not lang:
            return _LANG_MAP["auto"]
        return _LANG_MAP.get(lang.lower(), "vi")

    # ── OCR text ──────────────────────────────────────────────────────────
    def ocr_page(self, image: np.ndarray, lang: Optional[str]) -> List[OcrLine]:
        reader = self._get_reader(self._resolve_lang(lang))
        raw = reader.ocr(image, cls=True)
        lines: List[OcrLine] = []
        if not raw:
            return lines
        # PaddleOCR trả [[ [box, (text, conf)], ... ]] (1 phần tử/ảnh).
        page_result = raw[0] if isinstance(raw, list) and raw else raw
        if not page_result:
            return lines
        for item in page_result:
            try:
                box, (text, conf) = item[0], item[1]
            except (ValueError, IndexError, TypeError):
                continue
            if text is None:
                continue
            lines.append(
                OcrLine(
                    text=str(text),
                    confidence=float(conf),
                    bbox=_normalize_bbox(box),
                )
            )
        return lines

    # ── Layout analysis (PP-Structure) ────────────────────────────────────
    def get_structure(self):
        """Lazy-load PP-Structure engine (layout + table)."""
        if self._structure is not None:
            return self._structure
        from paddleocr import PPStructure

        self._structure = PPStructure(
            layout=True,
            table=config.enable_table,
            ocr=False,
            show_log=False,
            ir_optim=False,
            enable_mkldnn=False,
        )
        logger.info("PP-Structure sẵn sàng (table=%s)", config.enable_table)
        return self._structure

    def analyze_layout(self, image: np.ndarray) -> List[dict]:
        """Trả về danh sách vùng layout: {type, bbox:[x1,y1,x2,y2], res}."""
        engine = self.get_structure()
        try:
            regions = engine(image)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Layout analysis lỗi: %s", exc)
            return []
        return regions or []
