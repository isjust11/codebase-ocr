"""Phase 6 — Tạo searchable PDF: ảnh gốc từng trang + lớp text ẩn (render_mode=3)
đặt đúng vị trí bbox để có thể tìm kiếm / copy text.

Dùng PyMuPDF: render trang ở cùng DPI với lúc OCR (config.render_dpi) nên toạ độ
pixel của bbox chia cho zoom = dpi/72 ra điểm PDF khớp ảnh.

Font: ưu tiên TTF Unicode (phủ tiếng Việt) theo `OCR_PDF_FONT`; thiếu thì fallback
'helv' (chỉ Latin) — lớp text vẫn ẩn, ảnh hiển thị vẫn đầy đủ.
"""

from __future__ import annotations

import io
import logging
import os
from typing import List, Optional

import fitz  # PyMuPDF
from PIL import Image

from .config import config
from .pdf_renderer import DocumentRenderer
from .schemas import OcrExportPage

logger = logging.getLogger("ocr.export")


def _encode_png(image) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(image).save(buf, format="PNG")
    return buf.getvalue()


def _poly_bounds(poly: List[List[float]]):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


class PdfExporter:
    def __init__(self) -> None:
        font_path = config.pdf_font_path
        self._font_file: Optional[str] = (
            font_path if font_path and os.path.exists(font_path) else None
        )
        self._font_name = "ocrfont" if self._font_file else "helv"
        if not self._font_file:
            logger.warning(
                "Không thấy font Unicode (%s); fallback 'helv' (lớp text có thể "
                "thiếu dấu tiếng Việt).",
                font_path,
            )

    def build(self, src_path: str, pages: List[OcrExportPage]) -> bytes:
        """Render lại từ file gốc + phủ lớp text ẩn theo `pages`. Trả về bytes PDF."""
        zoom = config.render_dpi / 72.0
        lines_by_page = {p.page: p.lines for p in pages}

        out = fitz.open()
        try:
            with DocumentRenderer(src_path, config.render_dpi) as renderer:
                # Nếu backend không gửi pages, export toàn bộ.
                page_numbers = (
                    [p.page for p in pages]
                    if pages
                    else renderer.select_pages(None)
                )
                for page_no in page_numbers:
                    rp = renderer.render_page(page_no)
                    w_pt = rp.width / zoom
                    h_pt = rp.height / zoom
                    new_page = out.new_page(width=w_pt, height=h_pt)
                    rect = fitz.Rect(0, 0, w_pt, h_pt)
                    new_page.insert_image(rect, stream=_encode_png(rp.image))
                    self._overlay_text(
                        new_page, lines_by_page.get(page_no, []), zoom
                    )
            return out.tobytes()
        finally:
            out.close()

    def _overlay_text(self, page, lines, zoom: float) -> None:
        for line in lines:
            text = (line.text or "").strip()
            if not text or not line.bbox:
                continue
            x1, y1, x2, y2 = _poly_bounds(line.bbox)
            # Baseline gần đáy bbox; toạ độ điểm = pixel / zoom.
            point = fitz.Point(x1 / zoom, y2 / zoom)
            fontsize = max(6.0, ((y2 - y1) / zoom) * 0.8)
            try:
                page.insert_text(
                    point,
                    text,
                    fontsize=fontsize,
                    fontname=self._font_name,
                    fontfile=self._font_file,
                    render_mode=3,  # invisible
                )
            except Exception as exc:  # noqa: BLE001
                # Ký tự ngoài font (fallback helv) → bỏ qua dòng này, ảnh vẫn còn.
                logger.debug("Bỏ qua dòng text trong PDF: %s", exc)
