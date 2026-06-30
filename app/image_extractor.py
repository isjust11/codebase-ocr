"""Phase 5b — Tách ảnh / figure / table trong trang.

Hai cơ chế bổ trợ:
1. Ảnh nhúng (PDF digital) qua PyMuPDF: lấy ảnh gốc chất lượng cao + bbox.
2. Layout analysis (PP-Structure) qua PaddleOCR: bắt figure/table trên ảnh render
   (hữu ích cho PDF scan hoặc khi ảnh không nhúng dạng xref).

Khử trùng giữa hai cơ chế bằng IoU bbox. Bỏ ảnh quá nhỏ theo ngưỡng diện tích.
Mỗi ảnh tách được upload S3, trả về OcrAssetMessage (bbox theo pixel của trang render).
"""

from __future__ import annotations

import io
import logging
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

from .config import config
from .ocr_engine import OcrEngine
from .pdf_renderer import RenderedPage
from .s3_client import S3Client
from .schemas import OcrAssetMessage

logger = logging.getLogger("ocr.extract")

Rect = Tuple[float, float, float, float]  # x1, y1, x2, y2


def _rect_to_poly(r: Rect) -> List[List[float]]:
    x1, y1, x2, y2 = r
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def _iou(a: Rect, b: Rect) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _encode_png(image: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(image).save(buf, format="PNG")
    return buf.getvalue()


def _clamp_rect(r: Rect, w: int, h: int) -> Rect:
    x1, y1, x2, y2 = r
    x1 = max(0.0, min(x1, w))
    x2 = max(0.0, min(x2, w))
    y1 = max(0.0, min(y1, h))
    y2 = max(0.0, min(y2, h))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return (x1, y1, x2, y2)


class ImageExtractor:
    def __init__(self, engine: OcrEngine, s3: S3Client) -> None:
        self._engine = engine
        self._s3 = s3

    def extract(
        self,
        page: RenderedPage,
        doc=None,
        mode: str = "layout",
    ) -> Tuple[List[OcrAssetMessage], List[OcrAssetMessage]]:
        """Trả về (images, tables) cho 1 trang."""
        page_area = float(page.width * page.height) or 1.0
        min_area = config.min_asset_area_ratio * page_area

        embedded = self._extract_embedded(page, doc, min_area)

        images: List[OcrAssetMessage] = list(embedded)
        tables: List[OcrAssetMessage] = []

        # Chạy layout khi mode=layout (bắt figure/table cho cả scan lẫn fallback).
        if mode == "layout":
            embedded_rects = [self._poly_to_rect(a.bbox) for a in embedded]
            figures, tbls = self._extract_layout(
                page, min_area, embedded_rects
            )
            images.extend(figures)
            tables.extend(tbls)

        return images, tables

    # ── Cơ chế 1: ảnh nhúng (PDF digital) ─────────────────────────────────
    def _extract_embedded(
        self, page: RenderedPage, doc, min_area: float
    ) -> List[OcrAssetMessage]:
        out: List[OcrAssetMessage] = []
        if doc is None or page.fitz_page is None:
            return out
        zoom = config.render_dpi / 72.0
        try:
            xrefs = page.fitz_page.get_images(full=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_images lỗi trang %s: %s", page.page_number, exc)
            return out

        for img_info in xrefs:
            xref = img_info[0]
            try:
                rects = page.fitz_page.get_image_rects(xref)
            except Exception:  # noqa: BLE001
                rects = []
            if not rects:
                continue
            try:
                extracted = doc.extract_image(xref)
            except Exception:  # noqa: BLE001
                continue
            img_bytes = extracted.get("image")
            ext = extracted.get("ext", "png")
            if not img_bytes:
                continue

            for rect in rects:
                px_rect = _clamp_rect(
                    (rect.x0 * zoom, rect.y0 * zoom, rect.x1 * zoom, rect.y1 * zoom),
                    page.width,
                    page.height,
                )
                area = (px_rect[2] - px_rect[0]) * (px_rect[3] - px_rect[1])
                if area < min_area:
                    continue
                try:
                    url, key = self._s3.upload_bytes(img_bytes, ext=ext)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Upload ảnh embedded lỗi: %s", exc)
                    continue
                out.append(
                    OcrAssetMessage(
                        type="image",
                        bbox=_rect_to_poly(px_rect),
                        imageUrl=url,
                        imageKey=key,
                        source="embedded",
                    )
                )
        return out

    # ── Cơ chế 2: layout PP-Structure ─────────────────────────────────────
    def _extract_layout(
        self,
        page: RenderedPage,
        min_area: float,
        embedded_rects: List[Rect],
    ) -> Tuple[List[OcrAssetMessage], List[OcrAssetMessage]]:
        figures: List[OcrAssetMessage] = []
        tables: List[OcrAssetMessage] = []
        regions = self._engine.analyze_layout(page.image)

        for region in regions:
            rtype = str(region.get("type", "")).lower()
            if rtype not in {"figure", "table"}:
                continue
            bbox = region.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            rect = _clamp_rect(
                (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                page.width,
                page.height,
            )
            area = (rect[2] - rect[0]) * (rect[3] - rect[1])
            if area < min_area:
                continue

            # Khử trùng với ảnh embedded.
            if any(_iou(rect, er) >= config.dedup_iou for er in embedded_rects):
                continue

            crop = page.image[
                int(rect[1]) : int(rect[3]), int(rect[0]) : int(rect[2])
            ]
            if crop.size == 0:
                continue
            try:
                png = _encode_png(crop)
                url, key = self._s3.upload_bytes(png, ext="png")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Upload ảnh layout lỗi: %s", exc)
                continue

            if rtype == "figure":
                figures.append(
                    OcrAssetMessage(
                        type="figure",
                        bbox=_rect_to_poly(rect),
                        imageUrl=url,
                        imageKey=key,
                        source="layout",
                    )
                )
            else:  # table
                table_html = self._table_html(region)
                tables.append(
                    OcrAssetMessage(
                        type="table",
                        bbox=_rect_to_poly(rect),
                        imageUrl=url,
                        imageKey=key,
                        tableHtml=table_html,
                        source="layout",
                    )
                )
        return figures, tables

    @staticmethod
    def _table_html(region: dict) -> Optional[str]:
        res = region.get("res")
        if isinstance(res, dict):
            return res.get("html")
        return None

    @staticmethod
    def _poly_to_rect(poly: List[List[float]]) -> Rect:
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        return (min(xs), min(ys), max(xs), max(ys))
